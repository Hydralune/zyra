from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .worktree import inspect_worktree


P2_BASE_COMMIT = "e207b46ca690171139a718b8b85d808cb5a79c1e"
BANNED_ROOTS = {"vendor", "vendor-runtimes"}
ZERO_HARD_GATES = (
    "human_intervention_count",
    "privacy_permission_violation",
    "duplicate_commit",
    "duplicate_claim",
    "duplicate_spend",
    "duplicate_lease",
    "duplicate_side_effect",
    "early_exit_false_positive",
    "unsafe_commit",
    "invalid_transition_count",
    "critical_retrieval_without_provenance",
    "superseded_requirement_execution",
    "duplicate_completed_work",
)
FINAL_REGRESSION_COMMAND_IDS = (
    "python-full-regression",
    "typescript-runtime-regression",
    "typescript-typecheck",
    "web-typecheck",
    "web-tests",
    "web-build",
    "phase1-m1",
    "phase1-m2",
    "phase1-m3",
    "phase1-final-freeze",
    "phase2-policy-contracts",
    "internalization-ledger",
    "loopx-offline-runtime",
    "loopx-cross-version-restart",
)
TYPESCRIPT_RUNTIME_TEST_ROOTS = (
    "packages/commands/test",
    "packages/integrations/claude-mcp/test",
    "packages/memory/curator-state-machine/test",
    "packages/memory/retrieval-algorithms/test",
    "packages/memory/skill-memory-runtime/test",
    "packages/runtime/claude-runtime/test",
    "packages/runtime/provider-control-plane/test",
    "packages/runtime/runtime-event-spine/test",
    "packages/runtime/sandbox-gateway-control/test",
)


class Phase2FreezeError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _embedded_digest_ready(value: Mapping[str, Any], field: str) -> bool:
    unsigned = dict(value)
    claimed = str(unsigned.pop(field, ""))
    return bool(claimed) and claimed == canonical_digest(unsigned)


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Phase2FreezeError(f"invalid JSON evidence: {path}") from error
    if not isinstance(value, Mapping):
        raise Phase2FreezeError(f"JSON evidence is not an object: {path}")
    return value


def _member(root: Path, value: Any, *, repository_relative: bool = False) -> Path:
    selected = Path(str(value or ""))
    base = root.resolve()
    candidate = (
        selected.resolve()
        if selected.is_absolute()
        else (base / selected).resolve()
    )
    try:
        candidate.relative_to(base)
    except ValueError as error:
        label = "repository" if repository_relative else "evidence"
        raise Phase2FreezeError(f"{label} member escapes its root: {value}") from error
    if not candidate.is_file():
        raise Phase2FreezeError(f"evidence member is missing: {candidate}")
    return candidate


def _path_has_banned_root(value: str) -> bool:
    parts = {
        item.casefold()
        for item in re.split(r"[\\/]+", value)
        if item
    }
    return bool(parts & BANNED_ROOTS)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes),
    ):
        for item in value:
            yield from _strings(item)


def inspect_release_archive(path: Path) -> dict[str, Any]:
    member_names: list[str] = []
    wheels: list[tuple[str, bytes]] = []
    sboms: list[tuple[str, bytes]] = []
    release_manifests: list[tuple[str, bytes]] = []
    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                member_names.append(member.name)
                if not member.isfile():
                    continue
                suffix = Path(member.name).suffix.casefold()
                if (
                    suffix == ".whl"
                    or "sbom" in member.name.casefold()
                    or member.name.casefold().endswith("/release/manifest.json")
                ):
                    stream = archive.extractfile(member)
                    if stream is None:
                        continue
                    payload = stream.read()
                    if suffix == ".whl":
                        wheels.append((member.name, payload))
                    if "sbom" in member.name.casefold():
                        sboms.append((member.name, payload))
                    if member.name.casefold().endswith("/release/manifest.json"):
                        release_manifests.append((member.name, payload))
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                member_names.append(name)
                payload = b""
                if (
                    name.casefold().endswith(".whl")
                    or "sbom" in name.casefold()
                    or name.casefold().endswith("/release/manifest.json")
                ):
                    payload = archive.read(name)
                if name.casefold().endswith(".whl"):
                    wheels.append((name, payload))
                if "sbom" in name.casefold():
                    sboms.append((name, payload))
                if name.casefold().endswith("/release/manifest.json"):
                    release_manifests.append((name, payload))
    else:
        raise Phase2FreezeError(f"unsupported release archive: {path}")

    release_findings = sorted(
        name for name in member_names if _path_has_banned_root(name)
    )
    wheel_findings: list[str] = []
    for wheel_name, payload in wheels:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as wheel:
                wheel_findings.extend(
                    f"{wheel_name}!{name}"
                    for name in wheel.namelist()
                    if _path_has_banned_root(name)
                )
        except zipfile.BadZipFile as error:
            raise Phase2FreezeError(
                f"embedded wheel is corrupt: {wheel_name}"
            ) from error
    sbom_findings: list[str] = []
    for sbom_name, payload in sboms:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Phase2FreezeError(f"SBOM is corrupt: {sbom_name}") from error
        if any(_path_has_banned_root(item) for item in _strings(value)):
            sbom_findings.append(sbom_name)
    manifest_values: list[dict[str, Any]] = []
    for manifest_name, payload in release_manifests:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Phase2FreezeError(
                f"release manifest is corrupt: {manifest_name}"
            ) from error
        if not isinstance(value, Mapping):
            raise Phase2FreezeError(
                f"release manifest is not an object: {manifest_name}"
            )
        manifest_values.append(dict(value))
    release_manifest = manifest_values[0] if len(manifest_values) == 1 else {}
    manifest_ready = (
        len(manifest_values) == 1
        and release_manifest.get("schema") == "zyra.release-manifest/v1"
        and len(str(release_manifest.get("source_commit") or "")) == 40
    )
    return {
        "archive": str(path),
        "archive_sha256": sha256_file(path),
        "member_count": len(member_names),
        "source_archive_vendor_root_count": len(release_findings),
        "release_vendor_root_count": len(release_findings),
        "wheel_count": len(wheels),
        "wheel_vendor_root_count": len(wheel_findings),
        "sbom_count": len(sboms),
        "sbom_vendor_root_count": len(sbom_findings),
        "release_findings": release_findings,
        "wheel_findings": sorted(wheel_findings),
        "sbom_findings": sorted(sbom_findings),
        "release_manifest_count": len(manifest_values),
        "release_manifest": release_manifest,
        "release_manifest_ready": manifest_ready,
        "ready": (
            not release_findings
            and bool(wheels)
            and not wheel_findings
            and bool(sboms)
            and not sbom_findings
            and manifest_ready
        ),
    }


class Phase2FreezeAuditor:
    def __init__(self, repository_root: Path) -> None:
        self.root = repository_root.resolve()

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode:
            raise Phase2FreezeError(
                f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
            )
        return completed.stdout

    def _run_json_command(self, arguments: Sequence[str]) -> Mapping[str, Any]:
        completed = subprocess.run(
            [sys.executable, *arguments],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise Phase2FreezeError(
                f"independent audit command returned invalid JSON: {arguments[0]}"
            ) from error
        if completed.returncode or not isinstance(value, Mapping):
            raise Phase2FreezeError(
                f"independent audit command failed: {arguments[0]}"
            )
        return value

    def _custody_audit(self, path: Path, target: str) -> dict[str, Any]:
        supplied = _load_json(path)
        fresh = self._run_json_command(
            (
                "scripts/verify_source_language_custody.py",
                "--evidence",
                "docs/release/phase2/source-language-custody-input.json",
                "--base",
                P2_BASE_COMMIT,
                "--target",
                target,
            )
        )
        ready = (
            supplied == fresh
            and fresh.get("schema")
            == "zyra.source-language-custody-report/v1"
            and fresh.get("ok") is True
            and fresh.get("base") == P2_BASE_COMMIT
            and fresh.get("target") == target
            and fresh.get("violations") == []
        )
        return {
            "ready": ready,
            "report_sha256": sha256_file(path),
            "fresh_report_digest": canonical_digest(fresh),
            "role_count": len(fresh.get("roles") or ()),
            "violations": list(fresh.get("violations") or ()),
        }

    def _contract_audit(self, path: Path, target: str) -> dict[str, Any]:
        supplied = _load_json(path)
        fresh = self._run_json_command(
            (
                "scripts/verify_phase2_policy_contracts.py",
                "--target-commit",
                target,
                "--require-strongest-active",
            )
        )
        ready = (
            supplied == fresh
            and fresh.get("schema")
            == "zyra.phase2-policy-contract-validation/v1"
            and fresh.get("valid") is True
            and fresh.get("target_commit") == target
            and fresh.get("strongest_profile_requested") is True
        )
        return {
            "ready": ready,
            "report_sha256": sha256_file(path),
            "fresh_report_digest": canonical_digest(fresh),
            "valid": fresh.get("valid"),
            "target_commit": fresh.get("target_commit"),
        }

    def _target_audit(self, target: str) -> dict[str, Any]:
        head = self._git("rev-parse", "HEAD").strip()
        boundary = inspect_worktree(self.root, expected_head=target)
        target_tree = self._git("rev-parse", f"{target}^{{tree}}").strip()
        tree = [
            item
            for item in self._git(
                "ls-tree",
                "-r",
                "--name-only",
                target,
            ).splitlines()
            if item
        ]
        vendor_paths = sorted(
            item for item in tree if _path_has_banned_root(item)
        )
        runtime_dependency_findings: list[str] = []
        dependency_manifest_findings: list[str] = []
        production_suffixes = {
            ".py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".json",
            ".toml",
            ".yaml",
            ".yml",
        }
        for relative in tree:
            path = self.root / relative
            if not path.is_file():
                continue
            folded_parts = {part.casefold() for part in Path(relative).parts}
            if (
                relative.startswith(("packages/", "apps/"))
                and not folded_parts.intersection({"test", "tests", "fixtures"})
                and path.suffix.casefold() in production_suffixes
            ):
                text = path.read_text(encoding="utf-8", errors="replace")
                if (
                    "../long-horizon-systems" in text
                    or "agent-zoo\\long-horizon-systems" in text.casefold()
                    or "agent-zoo/long-horizon-systems" in text.casefold()
                ):
                    runtime_dependency_findings.append(relative)
            if path.name == "package.json":
                manifest = _load_json(path)
                for section in (
                    "dependencies",
                    "devDependencies",
                    "optionalDependencies",
                    "peerDependencies",
                ):
                    values = manifest.get(section)
                    if not isinstance(values, Mapping):
                        continue
                    for name, raw in values.items():
                        value = str(raw).casefold()
                        if (
                            value.startswith(("file:", "link:"))
                            or "../" in value
                            or re.match(r"^[a-z]:[\\/]", value)
                        ):
                            dependency_manifest_findings.append(
                                f"{relative}:{section}:{name}={raw}"
                            )
            if path.name == "pyproject.toml":
                try:
                    manifest = tomllib.loads(
                        path.read_text(encoding="utf-8")
                    )
                except tomllib.TOMLDecodeError as error:
                    raise Phase2FreezeError(
                        f"invalid pyproject: {relative}"
                    ) from error
                project = manifest.get("project")
                if isinstance(project, Mapping):
                    dependency_values = list(
                        project.get("dependencies") or ()
                    )
                    optional = project.get("optional-dependencies")
                    if isinstance(optional, Mapping):
                        for values in optional.values():
                            if isinstance(values, Sequence):
                                dependency_values.extend(values)
                    for raw in dependency_values:
                        value = str(raw).casefold()
                        if (
                            " @ file:" in value
                            or "../" in value
                            or re.search(r" @ [a-z]:[\\/]", value)
                        ):
                            dependency_manifest_findings.append(
                                f"{relative}:{raw}"
                            )
        return {
            "head_commit": head,
            "target_commit": target,
            "target_matches_head": head == target,
            "target_tree": target_tree,
            "source_boundary": boundary,
            "source_boundary_ready": boundary.get("ready") is True,
            "tree_path_count": len(tree),
            "target_tree_vendor_root_count": len(vendor_paths),
            "target_tree_vendor_paths": vendor_paths,
            "root_source_runtime_dependency_count": len(
                runtime_dependency_findings
            ),
            "root_source_runtime_dependency_findings": sorted(
                runtime_dependency_findings
            ),
            "editable_link_external_manifest_dependency_count": len(
                dependency_manifest_findings
            ),
            "editable_link_external_manifest_dependency_findings": sorted(
                dependency_manifest_findings
            ),
        }

    def _release_audit(
        self,
        release_root: Path,
        target: str,
    ) -> dict[str, Any]:
        pipeline = _load_json(release_root / "pipeline-report.json")
        pipeline_digest_ready = _embedded_digest_ready(pipeline, "digest")
        archive = Path(str(pipeline.get("archive") or ""))
        if not archive.is_file():
            candidates = tuple(
                path
                for path in release_root.iterdir()
                if path.is_file()
                and (
                    tarfile.is_tarfile(path)
                    or zipfile.is_zipfile(path)
                )
            )
            if len(candidates) != 1:
                raise Phase2FreezeError("release archive is not unique")
            archive = candidates[0]
        archive_audit = inspect_release_archive(archive)
        archive_manifest = archive_audit["release_manifest"]
        git_receipt = pipeline.get("git")
        git_receipt = git_receipt if isinstance(git_receipt, Mapping) else {}
        git_boundary = git_receipt.get("worktree_boundary")
        git_boundary = git_boundary if isinstance(git_boundary, Mapping) else {}
        git_ready = (
            git_receipt.get("ready") is True
            and git_receipt.get("revision") == target
            and git_receipt.get("dirty_check_skipped") is not True
            and int(git_receipt.get("dirty_entry_count") or 0) == 0
            and git_boundary.get("ready") is True
            and git_boundary.get("head_commit") == target
            and git_boundary.get("head_tree")
            == self._git("rev-parse", f"{target}^{{tree}}").strip()
            and _embedded_digest_ready(git_boundary, "boundary_digest")
        )
        clean_install = _load_json(
            release_root / "admission" / "ci" / "clean-install.json"
        )
        isolation = clean_install.get("isolation_audit")
        if not isinstance(isolation, Mapping):
            isolation = {}
        zero_counts = {
            field: int(isolation.get(field) or 0)
            for field in (
                "implicit_cache_or_user_state_dependency_count",
                "editable_or_link_install_count",
                "external_build_context_count",
                "undeclared_process_count",
                "undeclared_port_count",
            )
        }
        declared_ports = sorted(
            int(item) for item in isolation.get("declared_ports", ())
        )
        released_ports = sorted(
            int(item) for item in isolation.get("released_ports", ())
        )
        actions = {
            str(item)
            for item in isolation.get("declared_process_actions", ())
        }
        required_actions = {
            "release-doctor",
            "product-start",
            "semantic-health",
            "product-restart",
            "post-restart-health",
            "product-stop",
            "post-stop-status",
        }
        clean_ready = (
            clean_install.get("ready") is True
            and clean_install.get("source_commit") == target
            and clean_install.get("workspace_isolated") is True
            and clean_install.get("parent_source_repositories_present")
            is False
            and clean_install.get("product_lifecycle_exercised") is True
            and isolation.get("ready") is True
            and all(value == 0 for value in zero_counts.values())
            and bool(declared_ports)
            and declared_ports == released_ports
            and required_actions.issubset(actions)
        )
        return {
            "pipeline_ready": pipeline.get("ready") is True,
            "pipeline_digest_ready": pipeline_digest_ready,
            "pipeline_source_commit": pipeline.get("source_commit"),
            "pipeline_target_matches": pipeline.get("source_commit") == target,
            "ci_executed": pipeline.get("ci_executed") is True,
            "ci_ready": (
                isinstance(pipeline.get("ci"), Mapping)
                and pipeline["ci"].get("ready") is True
            ),
            "archive_digest_matches": (
                archive_audit["archive_sha256"]
                == pipeline.get("archive_sha256")
            ),
            "release_git_boundary_ready": git_ready,
            "archive_source_commit": archive_manifest.get("source_commit"),
            "archive_source_commit_matches": (
                archive_manifest.get("source_commit") == target
            ),
            "archive": archive_audit,
            "cleanroom_ready": clean_ready,
            "cleanroom_zero_counts": zero_counts,
            "cleanroom_declared_ports": declared_ports,
            "cleanroom_released_ports": released_ports,
            "cleanroom_actions": sorted(actions),
            "clean_install_receipt": str(
                release_root / "admission" / "ci" / "clean-install.json"
            ),
        }

    def _preflight_audit(
        self,
        preflight_root: Path,
        target: str,
    ) -> dict[str, Any]:
        report = _load_json(preflight_root / "preflight-report.json")
        readiness = _load_json(
            preflight_root / "MechanismEvidenceReadinessReport.json"
        )
        activation = _load_json(preflight_root / "activation-report.json")
        receipts = _load_json(preflight_root / "raw-receipts.json")
        inventory = _load_json(preflight_root / "inventory.json")
        blockers: list[str] = []
        digest_specs = (
            (report, "report_digest", "preflight_report_digest"),
            (readiness, "report_digest", "readiness_report_digest"),
            (activation, "activation_report_digest", "activation_report_digest"),
            (receipts, "receipt_set_digest", "receipt_set_digest"),
            (inventory, "inventory_digest", "inventory_digest"),
        )
        for value, field, blocker in digest_specs:
            if not _embedded_digest_ready(value, field):
                blockers.append(blocker)
        expected_files = {
            "raw-receipts.json",
            "preflight-report.json",
            "MechanismEvidenceReadinessReport.json",
            "activation-report.json",
            "failure-outliers.json",
        }
        inventory_names: set[str] = set()
        for item in inventory.get("files", ()):
            if not isinstance(item, Mapping):
                blockers.append("inventory_file_entry")
                continue
            try:
                path = _member(self.root, item.get("path"))
            except Phase2FreezeError:
                blockers.append("inventory_file_path")
                continue
            inventory_names.add(path.name)
            if sha256_file(path) != item.get("sha256"):
                blockers.append(f"inventory_file_digest:{path.name}")
        if inventory_names != expected_files:
            blockers.append("inventory_file_set")
        manifest_path = _member(self.root, inventory.get("manifest"))
        manifest = _load_json(manifest_path)
        blockers.extend(
            self._preflight_manifest_binding_blockers(
                manifest=manifest,
                target=target,
            )
        )
        manifest_frozen = manifest.get("frozen_inputs")
        manifest_frozen = (
            manifest_frozen if isinstance(manifest_frozen, Mapping) else {}
        )
        manifest_profile = manifest_frozen.get("profile")
        manifest_profile = (
            manifest_profile if isinstance(manifest_profile, Mapping) else {}
        )
        try:
            from zyra_evaluation.policy_benchmark.preflight import (
                FrozenPreflightManifest,
            )

            frozen_manifest = FrozenPreflightManifest.load(
                self.root,
                manifest_path,
            )
            manifest_contract_ready = frozen_manifest.value == manifest
        except (OSError, ValueError):
            manifest_contract_ready = False
        if (
            manifest.get("schema") != "zyra.strongest-preflight-manifest/v1"
            or not manifest_contract_ready
            or not _embedded_digest_ready(manifest, "manifest_digest")
            or manifest.get("manifest_digest") != inventory.get("manifest_digest")
            or manifest.get("manifest_digest") != report.get("manifest_digest")
            or (
                manifest.get("frozen_inputs", {}).get(
                    "implementation_target_commit"
                )
                if isinstance(manifest.get("frozen_inputs"), Mapping)
                else None
            )
            != target
        ):
            blockers.append("frozen_manifest_binding")
        derived = self._recompute_preflight_receipts(
            manifest=manifest,
            receipt_set=receipts,
            target=target,
        )
        blockers.extend(derived["blockers"])
        mechanisms = readiness.get("mechanisms")
        mechanisms = mechanisms if isinstance(mechanisms, Mapping) else {}
        required = {"arg_designer", "card", "agentprune", "maas"}
        readiness_ready = (
            set(mechanisms) == required
            and all(
                isinstance(mechanisms[item], Mapping)
                and mechanisms[item].get("readiness_stage")
                == "activation_ready"
                and mechanisms[item].get("status")
                == "deterministic_ready"
                for item in required
            )
        )
        gates = report.get("hard_gates")
        gates = gates if isinstance(gates, Mapping) else {}
        raw_receipts = receipts.get("receipts")
        raw_receipts = (
            raw_receipts
            if isinstance(raw_receipts, Sequence)
            and not isinstance(raw_receipts, (str, bytes))
            else ()
        )
        boundary = next(
            (
                item
                for item in raw_receipts
                if isinstance(item, Mapping)
                and item.get("schema")
                == "zyra.strongest-preflight-source-boundary/v1"
            ),
            {},
        )
        boundary_before = boundary.get("before")
        boundary_after = boundary.get("after")
        boundary_before = (
            boundary_before if isinstance(boundary_before, Mapping) else {}
        )
        boundary_after = (
            boundary_after if isinstance(boundary_after, Mapping) else {}
        )
        source_boundary_ready = (
            boundary.get("status") == "passed"
            and _embedded_digest_ready(boundary, "receipt_digest")
            and boundary_before.get("ready") is True
            and boundary_after.get("ready") is True
            and boundary_before.get("head_commit") == target
            and boundary_after.get("head_commit") == target
            and boundary_before.get("head_tree") == boundary_after.get("head_tree")
            and _embedded_digest_ready(boundary_before, "boundary_digest")
            and _embedded_digest_ready(boundary_after, "boundary_digest")
        )
        if not source_boundary_ready:
            blockers.append("target_source_boundary")
        if (
            receipts.get("schema")
            != "zyra.strongest-preflight-receipt-set/v1"
            or receipts.get("implementation_commit") != target
            or int(receipts.get("receipt_count") or 0) != len(raw_receipts)
            or not raw_receipts
            or any(
                not isinstance(item, Mapping) or item.get("retained") is not True
                for item in raw_receipts
            )
        ):
            blockers.append("raw_receipt_contract")
        if (
            activation.get("schema")
            != "zyra.strongest-preflight-activation-report/v1"
            or activation.get("preflight_report_digest")
            != report.get("report_digest")
            or activation.get("readiness_report_digest")
            != readiness.get("report_digest")
            or activation.get("sealed_run_admission_eligible") is not True
            or activation.get("conclusion")
            != "phase2_strongest_v1_revalidated"
            or activation.get("blockers") != []
        ):
            blockers.append("activation_binding")
        if gates != derived["hard_gates"]:
            blockers.append("preflight_hard_gate_recompute")
        if list(report.get("hard_gate_order") or ()) != list(
            derived["hard_gate_order"]
        ):
            blockers.append("preflight_hard_gate_order")
        blockers.extend(
            self._preflight_readiness_blockers(
                manifest=manifest,
                readiness=readiness,
                receipt_set=receipts,
                target=target,
                passed=derived["status"] == "completed",
                no_training=derived["no_training"],
            )
        )
        blockers.extend(
            self._preflight_activation_blockers(
                report=report,
                readiness=readiness,
                activation=activation,
                raw_receipt_refs=derived["raw_receipt_refs"],
            )
        )
        if (
            report.get("status") != derived["status"]
            or report.get("preflight_id") != manifest.get("preflight_id")
            or report.get("profile_family")
            != manifest_profile.get("family")
            or report.get("profile_version")
            != manifest_profile.get("version")
            or list(report.get("raw_receipt_refs") or ())
            != list(derived["raw_receipt_refs"])
            or list(report.get("failed_receipt_refs") or ())
            != list(derived["failed_receipt_refs"])
            or report.get("failure_retention")
            != derived["failure_retention"]
            or report.get("resolver_before") != derived["resolver_before"]
            or report.get("resolver_after") != derived["resolver_after"]
            or report.get("policy_registry_digest")
            != derived["policy_registry_digest"]
        ):
            blockers.append("preflight_report_recompute")
        if (
            report.get("schema") != "zyra.strongest-preflight-report/v1"
            or inventory.get("schema")
            != "zyra.strongest-preflight-inventory/v1"
            or inventory.get("preflight_id") != manifest.get("preflight_id")
            or inventory.get("implementation_commit") != target
            or inventory.get("result_status") != report.get("status")
            or inventory.get("activation_conclusion")
            != activation.get("conclusion")
        ):
            blockers.append("preflight_bundle_contract")
        return {
            "status": report.get("status"),
            "execution_mode": report.get("execution_mode"),
            "implementation_commit": report.get("implementation_commit"),
            "target_matches": report.get("implementation_commit") == target,
            "resolver_before": report.get("resolver_before"),
            "resolver_after": report.get("resolver_after"),
            "hard_gates_all_passed": bool(gates)
            and all(value is True for value in gates.values()),
            "readiness_ready": readiness_ready,
            "source_boundary_ready": source_boundary_ready,
            "blockers": sorted(set(blockers)),
            "ready": (
                report.get("status") == "completed"
                and report.get("execution_mode")
                == "active_default_revalidation"
                and report.get("implementation_commit") == target
                and report.get("resolver_before") == "phase2_strongest_v1"
                and report.get("resolver_after") == "phase2_strongest_v1"
                and bool(gates)
                and all(value is True for value in gates.values())
                and readiness_ready
                and not blockers
            ),
        }

    def _expected_preflight_manifest(
        self,
        *,
        target: str,
        frozen_at: str,
    ) -> dict[str, Any]:
        """Rebuild generated evidence from the tracked target template."""

        from zyra_evaluation.policy_benchmark.preflight import (
            compute_preflight_id,
        )
        from zyra_orchestration.topology_policy import (
            MechanismRegistry,
            ResolutionPurpose,
        )

        template = _load_json(
            self.root / "config/phase2/strongest-preflight.json"
        )
        payload = json.loads(json.dumps(template, ensure_ascii=False))
        frozen = payload.get("frozen_inputs")
        if not isinstance(frozen, dict):
            raise Phase2FreezeError(
                "tracked preflight template frozen_inputs are missing"
            )
        frozen["implementation_target_commit"] = target
        profile = frozen.get("profile")
        if not isinstance(profile, dict):
            raise Phase2FreezeError(
                "tracked preflight template profile is missing"
            )
        registry = MechanismRegistry.load(self.root)
        resolved = registry.resolve(
            "topology_policy",
            purpose=ResolutionPurpose.NORMAL,
        )
        if resolved.profile_id != "phase2_strongest_v1":
            raise Phase2FreezeError(
                "tracked target does not resolve phase2_strongest_v1"
            )
        profile["config_digest"] = resolved.config_digest
        identity_fields = {
            "policy_registry": ("registry_digest", "registry_digest"),
            "activation_gates": ("frozen_gate_digest", "gate_digest"),
            "phase1_baseline_manifest": (
                "manifest_digest",
                "manifest_digest",
            ),
        }
        for label, (document_field, binding_field) in identity_fields.items():
            binding = frozen.get(label)
            if not isinstance(binding, dict):
                raise Phase2FreezeError(
                    f"tracked preflight binding is missing: {label}"
                )
            path = _member(self.root, binding.get("path"))
            document = _load_json(path)
            identity = str(document.get(document_field) or "")
            if len(identity) != 64:
                raise Phase2FreezeError(
                    f"tracked preflight identity is invalid: {label}"
                )
            binding["file_sha256"] = sha256_file(path)
            binding[binding_field] = identity
        for binding in payload.get("evidence_bindings", ()) or ():
            if not isinstance(binding, dict):
                raise Phase2FreezeError(
                    "tracked preflight evidence binding is invalid"
                )
            path = _member(self.root, binding.get("report_ref"))
            report = _load_json(path)
            binding["file_sha256"] = sha256_file(path)
            binding["report_digest"] = str(report.get("report_digest") or "")
        for binding in payload.get("supporting_evidence", ()) or ():
            if not isinstance(binding, dict):
                raise Phase2FreezeError(
                    "tracked preflight supporting binding is invalid"
                )
            path = _member(self.root, binding.get("path"))
            binding["file_sha256"] = sha256_file(path)
        payload["frozen_at"] = frozen_at
        payload["preflight_id"] = compute_preflight_id(payload)
        payload.pop("manifest_digest", None)
        payload["manifest_digest"] = canonical_digest(payload)
        return payload

    def _preflight_manifest_binding_blockers(
        self,
        *,
        manifest: Mapping[str, Any],
        target: str,
    ) -> list[str]:
        frozen_at = str(manifest.get("frozen_at") or "")
        if not frozen_at:
            return ["tracked_preflight_manifest_binding"]
        try:
            expected = self._expected_preflight_manifest(
                target=target,
                frozen_at=frozen_at,
            )
        except (OSError, ValueError):
            return ["tracked_preflight_manifest_binding"]
        return (
            []
            if manifest == expected
            else ["tracked_preflight_manifest_binding"]
        )

    def _recompute_preflight_receipts(
        self,
        *,
        manifest: Mapping[str, Any],
        receipt_set: Mapping[str, Any],
        target: str,
    ) -> dict[str, Any]:
        from zyra_evaluation.policy_benchmark.evidence_index import (
            BASELINE_MANIFEST_DIGEST,
            build_read_only_evidence_index,
        )
        from zyra_evaluation.policy_benchmark.mechanism_readiness import (
            MECHANISM_IDS,
            MechanismReadinessConfig,
            run_no_training_audit,
        )
        from zyra_evaluation.policy_benchmark.preflight import (
            BASELINE_PROFILE,
            HARD_GATE_ORDER,
            STRONGEST_PROFILE,
        )
        from zyra_orchestration.topology_policy.registry import (
            MechanismRegistry,
            ResolutionPurpose,
        )

        del BASELINE_MANIFEST_DIGEST
        blockers: list[str] = []
        raw = tuple(
            item
            for item in receipt_set.get("receipts", ())
            if isinstance(item, Mapping)
        )
        if len(raw) != len(tuple(receipt_set.get("receipts", ()) or ())):
            blockers.append("preflight_receipt_shape")
        ids = tuple(str(item.get("receipt_id") or "") for item in raw)
        if not ids or len(ids) != len(set(ids)) or any(not item for item in ids):
            blockers.append("preflight_receipt_identity")
        for item in raw:
            if not _embedded_digest_ready(item, "receipt_digest"):
                blockers.append(
                    f"preflight_receipt_digest:{item.get('receipt_id')}"
                )
        by_schema: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for item in raw:
            by_schema[str(item.get("schema") or "")].append(item)
        frozen = manifest.get("frozen_inputs")
        frozen = frozen if isinstance(frozen, Mapping) else {}
        profile = frozen.get("profile")
        profile = profile if isinstance(profile, Mapping) else {}
        evidence_bindings = tuple(
            item
            for item in manifest.get("evidence_bindings", ())
            if isinstance(item, Mapping)
        )
        binding_by_id = {
            str(item.get("mechanism_id") or ""): item
            for item in evidence_bindings
        }
        tasks = tuple(
            item
            for item in frozen.get("tasks", ())
            if isinstance(item, Mapping)
        )
        seeds = tuple(frozen.get("seeds", ()) or ())
        layers = (
            "proposal",
            "residual",
            "mask",
            "operator_selection",
            "receipt",
        )
        mechanism_for_layer = {
            "proposal": "arg_designer",
            "residual": "card",
            "mask": "agentprune",
            "operator_selection": "maas",
            "receipt": "maas",
        }
        expected_determinism: list[dict[str, Any]] = []
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            snapshot_digest = canonical_digest(
                {
                    "task": task,
                    "budgets": frozen.get("budgets"),
                    "environment": frozen.get(
                        "provider_model_hardware_profile"
                    ),
                    "failure_schedule": frozen.get("failure_schedule"),
                    "profile_config_digest": profile.get("config_digest"),
                }
            )
            for seed in seeds:
                for layer in layers:
                    payload = {
                        "preflight_id": manifest.get("preflight_id"),
                        "task_id": task_id,
                        "seed": seed,
                        "layer": layer,
                        "snapshot_digest": snapshot_digest,
                        "config_digest": profile.get("config_digest"),
                        "readiness_digest": binding_by_id.get(
                            mechanism_for_layer[layer], {}
                        ).get("report_digest"),
                    }
                    output_digest = canonical_digest(payload)
                    expected = {
                        "schema": "zyra.strongest-preflight-determinism-receipt/v1",
                        "receipt_id": (
                            "receipt_determinism_"
                            + canonical_digest((task_id, seed, layer))[:24]
                        ),
                        **payload,
                        "first_output_digest": output_digest,
                        "second_output_digest": output_digest,
                        "match": True,
                        "status": "passed",
                        "retained": True,
                    }
                    expected["receipt_digest"] = canonical_digest(expected)
                    expected_determinism.append(expected)
        observed_determinism = by_schema[
            "zyra.strongest-preflight-determinism-receipt/v1"
        ]
        determinism_match = observed_determinism == expected_determinism
        if not determinism_match:
            blockers.append("preflight_determinism_recompute")
        expected_fail_closed = []
        for condition in ("missing", "stale", "corrupt"):
            value = {
                "schema": "zyra.strongest-preflight-fail-closed-receipt/v1",
                "receipt_id": f"receipt_fail_closed_{condition}",
                "condition": condition,
                "status": "expected_rejection",
                "fallback_profile": BASELINE_PROFILE,
                "canonical_mutation_count": 0,
                "route_change_count": 0,
                "lease_count": 0,
                "side_effect_count": 0,
                "silent_fallback": False,
                "retained": True,
            }
            value["receipt_digest"] = canonical_digest(value)
            expected_fail_closed.append(value)
        fail_closed = by_schema[
            "zyra.strongest-preflight-fail-closed-receipt/v1"
        ] == expected_fail_closed
        if not fail_closed:
            blockers.append("preflight_fail_closed_recompute")
        diagnostic = by_schema[
            "zyra.strongest-preflight-diagnostic-boundary-receipt/v1"
        ]
        expected_diagnostic = {
            "schema": "zyra.strongest-preflight-diagnostic-boundary-receipt/v1",
            "receipt_id": "receipt_diagnostic_zero_influence",
            "mode": "diagnostic",
            "status": "passed",
            "graph_mutation_count": 0,
            "route_change_count": 0,
            "lease_count": 0,
            "side_effect_count": 0,
            "actual_outcome_recorded": False,
            "retained": True,
        }
        expected_diagnostic["receipt_digest"] = canonical_digest(
            expected_diagnostic
        )
        diagnostic_zero = diagnostic == [expected_diagnostic]
        if not diagnostic_zero:
            blockers.append("preflight_diagnostic_recompute")
        replay_values = by_schema[
            "zyra.strongest-preflight-phase1-replay-receipt/v1"
        ]
        replay_index = build_read_only_evidence_index(self.root).to_dict()
        expected_replay = {
            "schema": "zyra.strongest-preflight-phase1-replay-receipt/v1",
            "receipt_id": "receipt_phase1_read_only_replay",
            "status": "passed",
            "passed": True,
            "read_only": True,
            "live_improvement_claimed": False,
            "baseline_manifest_digest": replay_index.get(
                "baseline_manifest_digest"
            ),
            "evidence_index_digest": replay_index.get("index_digest"),
            "source_run_count": len(replay_index.get("source_runs") or ()),
            "schema_checked": True,
            "causal_chain_checked": True,
            "deterministic_decision_checked": True,
            "retained": True,
        }
        expected_replay["receipt_digest"] = canonical_digest(expected_replay)
        replay_passed = replay_values == [expected_replay]
        if not replay_passed:
            blockers.append("preflight_replay_recompute")
        readiness_config = MechanismReadinessConfig.load(self.root)
        no_training = run_no_training_audit(self.root, readiness_config)
        expected_no_training = {
            "schema": "zyra.strongest-preflight-no-training-receipt/v1",
            "receipt_id": "receipt_no_policy_training",
            **no_training,
            "retained": True,
        }
        expected_no_training["receipt_digest"] = canonical_digest(
            expected_no_training
        )
        if by_schema[
            "zyra.strongest-preflight-no-training-receipt/v1"
        ] != [expected_no_training]:
            blockers.append("preflight_no_training_recompute")
        probes = tuple(
            item
            for item in manifest.get("command_probes", ())
            if isinstance(item, Mapping)
        )
        commands = by_schema[
            "zyra.strongest-preflight-command-receipt/v1"
        ]
        blockers.extend(self._preflight_command_blockers(probes, commands))
        boundaries = by_schema[
            "zyra.strongest-preflight-source-boundary/v1"
        ]
        boundary = boundaries[0] if len(boundaries) == 1 else {}
        before = boundary.get("before")
        before = before if isinstance(before, Mapping) else {}
        after = boundary.get("after")
        after = after if isinstance(after, Mapping) else {}
        boundary_ready = (
            len(boundaries) == 1
            and boundary.get("status") == "passed"
            and before.get("ready") is True
            and after.get("ready") is True
            and before.get("head_commit") == target
            and after.get("head_commit") == target
            and before.get("head_tree") == after.get("head_tree")
        )
        if not boundary_ready:
            blockers.append("preflight_boundary_recompute")
        expected_schema_counts = {
            "zyra.strongest-preflight-determinism-receipt/v1": len(
                expected_determinism
            ),
            "zyra.strongest-preflight-fail-closed-receipt/v1": 3,
            "zyra.strongest-preflight-diagnostic-boundary-receipt/v1": 1,
            "zyra.strongest-preflight-phase1-replay-receipt/v1": 1,
            "zyra.strongest-preflight-no-training-receipt/v1": 1,
            "zyra.strongest-preflight-command-receipt/v1": len(probes),
            "zyra.strongest-preflight-source-boundary/v1": 1,
        }
        if {
            key: len(value) for key, value in by_schema.items()
        } != expected_schema_counts:
            blockers.append("preflight_receipt_schema_set")
        categories = {
            str(category)
            for command in commands
            for category in command.get("categories", ()) or ()
        }
        category_pass = {
            category: bool(
                [
                    item
                    for item in commands
                    if category in (item.get("categories") or ())
                ]
            )
            and all(
                item.get("status") == "passed"
                for item in commands
                if category in (item.get("categories") or ())
            )
            for category in categories
        }
        failure_retention = {
            "expected_receipt_count": len(raw),
            "retained_receipt_count": sum(
                item.get("retained") is True for item in raw
            ),
            "failed_receipt_count": sum(
                item.get("status")
                in {"failed", "blocked", "degraded", "unavailable"}
                for item in raw
            ),
        }
        failure_retention["failures_removed"] = (
            failure_retention["expected_receipt_count"]
            != failure_retention["retained_receipt_count"]
        )
        failure_retention["passed"] = (
            failure_retention["failures_removed"] is False
        )
        registry = MechanismRegistry.load(self.root)
        normal = registry.resolve(
            str(profile.get("family") or ""),
            purpose=ResolutionPurpose.NORMAL,
        )
        active_mode = (
            frozen.get("execution_mode") == "active_default_revalidation"
        )
        registry_default = (
            normal.profile_id == STRONGEST_PROFILE
            and normal.version == STRONGEST_PROFILE
            and normal.lifecycle.value == "default"
            and normal.activation_state == "active"
            if active_mode
            else normal.profile_id == normal.version == BASELINE_PROFILE
        )
        validation_explicit = (
            normal.profile_id == STRONGEST_PROFILE
            and normal.version == STRONGEST_PROFILE
            and normal.config_digest == profile.get("config_digest")
            if active_mode
            else False
        )
        readiness_enforced = (
            set(binding_by_id) == set(MECHANISM_IDS)
            and all(
                item.get("stage") == "implementation_validated"
                and item.get("status") == "deterministic_ready"
                for item in binding_by_id.values()
            )
        )
        base_gates = {
            "target_source_boundary": boundary_ready,
            "manifest_integrity": _embedded_digest_ready(
                manifest, "manifest_digest"
            ),
            "registry_default_baseline": registry_default,
            "validation_profile_explicit": validation_explicit,
            "readiness_enforcement": readiness_enforced,
            "determinism": determinism_match
            and category_pass.get("determinism", False),
            "fail_closed_inputs": fail_closed,
            "diagnostic_side_effect_zero": diagnostic_zero,
            "local_isolated_integration": category_pass.get(
                "integration", False
            ),
            "continuity_restart_recovery": all(
                category_pass.get(item, False)
                for item in ("continuity", "compact_restore", "restart", "fault")
            ),
            "loopx_restart_outbox": all(
                category_pass.get(item, False)
                for item in ("loopx", "outbox", "restart")
            ),
            "phase1_read_only_replay": replay_passed
            and category_pass.get("replay", False),
            "no_policy_training": no_training.get("passed") is True,
            "failure_retention": failure_retention["passed"] is True,
            "efficiency_observed_without_optimization": True,
        }
        base_gates["success_and_safety"] = all(
            base_gates[item]
            for item in (
                "manifest_integrity",
                "target_source_boundary",
                "registry_default_baseline",
                "validation_profile_explicit",
                "readiness_enforcement",
                "determinism",
                "fail_closed_inputs",
                "diagnostic_side_effect_zero",
                "local_isolated_integration",
                "continuity_restart_recovery",
                "loopx_restart_outbox",
                "phase1_read_only_replay",
                "no_policy_training",
                "failure_retention",
            )
        )
        hard_gates = {
            key: bool(base_gates[key]) for key in HARD_GATE_ORDER
        }
        status = (
            "completed"
            if all(hard_gates.values())
            else "blocked"
            if any(item.get("status") == "blocked" for item in commands)
            else "failed"
        )
        raw_refs = tuple(f"raw-receipts.json#{item}" for item in ids)
        failed_refs = tuple(
            ref
            for ref, item in zip(raw_refs, raw, strict=True)
            if item.get("status")
            in {"failed", "blocked", "degraded", "unavailable"}
        )
        return {
            "blockers": sorted(set(blockers)),
            "hard_gate_order": HARD_GATE_ORDER,
            "hard_gates": hard_gates,
            "status": status,
            "raw_receipt_refs": raw_refs,
            "failed_receipt_refs": failed_refs,
            "failure_retention": failure_retention,
            "no_training": no_training,
            "resolver_before": normal.profile_id,
            "resolver_after": normal.profile_id,
            "policy_registry_digest": registry.source_config_digest,
        }

    def _preflight_command_blockers(
        self,
        probes: Sequence[Mapping[str, Any]],
        receipts: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        blockers: list[str] = []
        by_id = {str(item.get("probe_id") or ""): item for item in receipts}
        expected_ids = tuple(str(item.get("probe_id") or "") for item in probes)
        if tuple(by_id) != expected_ids or len(by_id) != len(receipts):
            blockers.append("preflight_command_set")
        for probe in probes:
            probe_id = str(probe.get("probe_id") or "")
            receipt = by_id.get(probe_id, {})
            expected_argv = [
                sys.executable,
                *[str(item) for item in probe.get("argv", ()) or ()],
            ]
            if list(receipt.get("argv") or ()) != expected_argv:
                blockers.append(f"preflight_command_argv:{probe_id}")
            if str(receipt.get("cwd") or "") != str(self.root):
                blockers.append(f"preflight_command_cwd:{probe_id}")
            if (
                receipt.get("receipt_id") != f"receipt_command_{probe_id}"
                or int(receipt.get("timeout_seconds") or 0)
                != int(probe.get("timeout_seconds") or 0)
                or list(receipt.get("categories") or ())
                != list(probe.get("categories") or ())
                or receipt.get("required") is not (probe.get("required") is True)
                or receipt.get("isolated") is not (probe.get("isolated") is True)
                or receipt.get("external_cost")
                is not (probe.get("external_cost") is True)
                or receipt.get("status") != "passed"
                or int(receipt.get("exit_code") or 0) != 0
                or receipt.get("retained") is not True
            ):
                blockers.append(f"preflight_command_receipt:{probe_id}")
        return blockers

    def _preflight_readiness_blockers(
        self,
        *,
        manifest: Mapping[str, Any],
        readiness: Mapping[str, Any],
        receipt_set: Mapping[str, Any],
        target: str,
        passed: bool,
        no_training: Mapping[str, Any],
    ) -> list[str]:
        from zyra_evaluation.policy_benchmark.evidence_index import (
            BASELINE_MANIFEST_DIGEST,
        )
        from zyra_evaluation.policy_benchmark.mechanism_readiness import (
            MECHANISM_IDS,
            MechanismReadinessConfig,
        )
        from zyra_evaluation.policy_benchmark.preflight import (
            STRONGEST_PROFILE,
        )

        frozen = manifest.get("frozen_inputs")
        frozen = frozen if isinstance(frozen, Mapping) else {}
        bindings = {
            str(item.get("mechanism_id") or ""): item
            for item in manifest.get("evidence_bindings", ())
            if isinstance(item, Mapping)
        }
        mechanisms: dict[str, dict[str, Any]] = {}
        for mechanism_id in MECHANISM_IDS:
            binding = bindings.get(mechanism_id, {})
            try:
                source_path = _member(self.root, binding.get("report_ref"))
                source = _load_json(source_path)
            except Phase2FreezeError:
                return [f"preflight_readiness_source:{mechanism_id}"]
            source_mechanisms = source.get("mechanisms")
            source_mechanisms = (
                source_mechanisms
                if isinstance(source_mechanisms, Mapping)
                else {}
            )
            source_mechanism = source_mechanisms.get(mechanism_id)
            if not isinstance(source_mechanism, Mapping):
                return [f"preflight_readiness_mechanism:{mechanism_id}"]
            value = dict(source_mechanism)
            value["readiness_stage"] = "activation_ready"
            value["status"] = (
                "deterministic_ready"
                if passed
                else "unavailable"
                if no_training.get("passed") is not True
                else "evidence_only"
            )
            value["activation_preflight"] = {
                "preflight_id": manifest.get("preflight_id"),
                "source_stage": "implementation_validated",
                "source_report_ref": binding.get("report_ref"),
                "source_report_digest": binding.get("report_digest"),
                "receipt_set_digest": receipt_set.get("receipt_set_digest"),
                "deterministic_replay_match": passed,
                "failure_retention_passed": passed,
                "no_policy_training_passed": no_training.get("passed") is True,
            }
            if not passed:
                value["gaps"] = sorted(
                    set(value.get("gaps") or ())
                    | {"P2-S06-01_preflight_gate"}
                )
            mechanisms[mechanism_id] = value
        config = MechanismReadinessConfig.load(self.root)
        baseline_path = self.root / "docs/release/phase2-baseline-manifest.json"
        active = (
            frozen.get("execution_mode") == "active_default_revalidation"
        )
        expected: dict[str, Any] = {
            "schema": "zyra.mechanism-evidence-readiness-report/v1",
            "slice_id": "P2-S06-01",
            "readiness_stage": "activation_ready",
            "p2_base_commit": P2_BASE_COMMIT,
            "p2_eval_base_commit": frozen.get("p2_eval_base_commit"),
            "implementation_commit": target,
            "generated_at": manifest.get("frozen_at"),
            "generation_clock": "frozen preflight manifest timestamp",
            "valid": passed and no_training.get("passed") is True,
            "activation_allowed": passed and active,
            "sealed_run_admission_candidate": passed,
            "activation_reason": (
                "phase2_strongest_v1 remains the active default after final "
                "target-bound revalidation"
                if active
                else (
                    "activation_ready permits explicit sealed-run validation; "
                    "the normal resolver remains on the Phase 1 baseline until "
                    "a later explicit activation transition"
                )
            ),
            "contract": {
                "path": config.path.relative_to(self.root).as_posix(),
                "sha256": config.digest,
            },
            "baseline_manifest": {
                "path": baseline_path.relative_to(self.root).as_posix(),
                "manifest_digest": BASELINE_MANIFEST_DIGEST,
                "sha256": sha256_file(baseline_path),
            },
            "preflight": {
                "preflight_id": manifest.get("preflight_id"),
                "manifest_digest": manifest.get("manifest_digest"),
                "receipt_set_digest": receipt_set.get("receipt_set_digest"),
            },
            "no_policy_training_audit": dict(no_training),
            "mechanism_statuses": {
                mechanism_id: mechanisms[mechanism_id]["status"]
                for mechanism_id in mechanisms
            },
            "mechanisms": mechanisms,
            "resolver_policy": {
                "normal_before_activation": (
                    STRONGEST_PROFILE
                    if active
                    else "phase1_deterministic_baseline"
                ),
                "normal_after_preflight": (
                    STRONGEST_PROFILE
                    if active
                    else "phase1_deterministic_baseline"
                ),
                "strongest_execution_mode": (
                    "default" if active else "validation"
                ),
                "evidence_only_influence": {
                    "graph": 0,
                    "route": 0,
                    "lease": 0,
                    "side_effect": 0,
                },
            },
            "training_sample_count": 0,
            "raw_transition_count_semantics": "evidence_volume_only",
        }
        expected["report_digest"] = canonical_digest(expected)
        return [] if readiness == expected else ["preflight_readiness_recompute"]

    @staticmethod
    def _preflight_activation_blockers(
        *,
        report: Mapping[str, Any],
        readiness: Mapping[str, Any],
        activation: Mapping[str, Any],
        raw_receipt_refs: Sequence[str],
    ) -> list[str]:
        blockers: list[str] = []
        passed: list[str] = []
        if report.get("status") != "completed":
            blockers.append("preflight_status")
        else:
            passed.append("preflight_status")
        gates = report.get("hard_gates")
        gates = gates if isinstance(gates, Mapping) else {}
        for gate_id in report.get("hard_gate_order", ()) or ():
            if gates.get(gate_id) is True:
                passed.append(f"hard_gate:{gate_id}")
            else:
                blockers.append(f"hard_gate:{gate_id}")
        readiness_values: dict[str, dict[str, str]] = {}
        mechanisms = readiness.get("mechanisms")
        mechanisms = mechanisms if isinstance(mechanisms, Mapping) else {}
        for mechanism_id in ("arg_designer", "card", "agentprune", "maas"):
            value = mechanisms.get(mechanism_id)
            value = value if isinstance(value, Mapping) else {}
            stage = str(value.get("readiness_stage") or "")
            status = str(value.get("status") or "")
            readiness_values[mechanism_id] = {
                "stage": stage,
                "status": status,
            }
            if stage != "activation_ready":
                blockers.append(f"readiness:{mechanism_id}:stage")
            elif status != "deterministic_ready":
                blockers.append(f"readiness:{mechanism_id}:status")
            else:
                passed.append(f"readiness:{mechanism_id}")
        resolver_before = str(report.get("resolver_before") or "")
        resolver_after = str(report.get("resolver_after") or "")
        execution_mode = str(
            report.get("execution_mode") or "pre_activation_validation"
        )
        expected_resolver = (
            "phase2_strongest_v1"
            if execution_mode == "active_default_revalidation"
            else "phase1_deterministic_baseline"
        )
        if (
            resolver_before != expected_resolver
            or resolver_after != expected_resolver
        ):
            blockers.append("resolver_retention")
        else:
            passed.append("resolver_retention")
        eligible = not blockers
        active = execution_mode == "active_default_revalidation"
        expected: dict[str, Any] = {
            "schema": "zyra.strongest-preflight-activation-report/v1",
            "preflight_id": report.get("preflight_id"),
            "profile_family": report.get("profile_family"),
            "profile_version": report.get("profile_version"),
            "preflight_report_digest": report.get("report_digest"),
            "readiness_report_digest": readiness.get("report_digest"),
            "sealed_run_admission_eligible": eligible,
            "default_activation_allowed": eligible and active,
            "conclusion": (
                "phase2_strongest_v1_revalidated"
                if eligible and active
                else "admit_to_P2-S06-02"
                if eligible
                else "retain_phase1_baseline"
            ),
            "blockers": sorted(set(blockers)),
            "passed_gates": sorted(set(passed)),
            "readiness": {
                key: readiness_values[key]
                for key in sorted(readiness_values)
            },
            "resolver_before": resolver_before,
            "resolver_after": resolver_after,
            "raw_receipt_refs": list(raw_receipt_refs),
            "activation_semantics": {
                "scope": (
                    "final_phase2_strongest_v1_revalidation"
                    if active and eligible
                    else "admission_to_P2-S06-02_sealed_runs"
                ),
                "normal_resolver_mutated": False,
                "default_profile_activated": active and eligible,
                "explicit_activation_transition_required_later": not (
                    active and eligible
                ),
                "baseline_retained_until_transition": not (
                    active and eligible
                ),
            },
        }
        expected["activation_report_digest"] = canonical_digest(expected)
        return [] if activation == expected else ["preflight_activation_recompute"]

    def _sealed_audit(
        self,
        sealed_root: Path,
        target: str,
    ) -> dict[str, Any]:
        from zyra_evaluation.policy_benchmark.long_run_validator import (
            SealedLongRunValidator,
        )

        validation = _load_json(
            sealed_root / "independent-validation.json"
        )
        index_path = sealed_root / "sealed-evidence-index.json"
        index = _load_json(index_path)
        fresh_validation = SealedLongRunValidator().validate(index_path)
        blockers: list[str] = []
        manifest = _load_json(sealed_root / "sealed-manifest.json")
        frozen = manifest.get("frozen_files")
        frozen = frozen if isinstance(frozen, Mapping) else {}
        required_frozen = {
            "apps/api/zyra_api/live_scenario_owners.py",
            "apps/api/zyra_api/main.py",
            "packages/evaluation/zyra_evaluation/policy_benchmark/long_run_validator.py",
            "packages/evaluation/zyra_evaluation/policy_benchmark/sealed_long_run.py",
            "packages/evaluation/zyra_evaluation/policy_benchmark/sealed_physical.py",
            "packages/evaluation/zyra_evaluation/scenario_runner/dual_domain.py",
            "packages/orchestration/zyra_orchestration/topology_policy/production.py",
            "packages/productization/zyra_productization/release/worktree.py",
            "packages/scheduler/zyra_scheduler/dispatch_evidence.py",
        }
        frozen_ready = required_frozen.issubset(frozen) and all(
            sha256_file(_member(self.root, relative)) == expected
            for relative, expected in frozen.items()
        )
        if not frozen_ready:
            blockers.append("frozen_source_binding")
        if (
            validation.get("schema")
            != "zyra.phase2-sealed-long-run-validation/v1"
            or not _embedded_digest_ready(validation, "validation_digest")
            or validation != fresh_validation
            or fresh_validation.get("valid") is not True
        ):
            blockers.append("independent_validation")
        if fresh_validation.get("candidate_commit") != target:
            blockers.append("validation_target")
        if (
            index.get("schema") != "zyra.phase2-sealed-evidence-index/v1"
            or not _embedded_digest_ready(index, "index_digest")
            or index.get("failed_runs") not in ([], ())
        ):
            blockers.append("evidence_index_integrity")
        if index.get("candidate_commit") != target:
            blockers.append("index_target")
        if index.get("manifest_commit") != target:
            blockers.append("manifest_target")
        boundary = index.get("target_source_boundary")
        boundary = boundary if isinstance(boundary, Mapping) else {}
        before = boundary.get("before")
        after = boundary.get("after")
        before = before if isinstance(before, Mapping) else {}
        after = after if isinstance(after, Mapping) else {}
        source_boundary_ready = (
            before.get("ready") is True
            and after.get("ready") is True
            and before.get("head_commit") == target
            and after.get("head_commit") == target
            and before.get("head_tree") == after.get("head_tree")
            and _embedded_digest_ready(before, "boundary_digest")
            and _embedded_digest_ready(after, "boundary_digest")
        )
        if not source_boundary_ready:
            blockers.append("target_source_boundary")
        runs = tuple(
            item
            for item in fresh_validation.get("runs", ())
            if isinstance(item, Mapping)
        )
        if len(runs) != 2 or any(item.get("valid") is not True for item in runs):
            blockers.append("sealed_run_count_or_validity")
        return {
            "candidate_commit": index.get("candidate_commit"),
            "manifest_commit": index.get("manifest_commit"),
            "run_count": len(runs),
            "runs": [dict(item) for item in runs],
            "fresh_validation_digest": fresh_validation.get(
                "validation_digest"
            ),
            "frozen_source_binding_ready": frozen_ready,
            "source_boundary_ready": source_boundary_ready,
            "blockers": sorted(set(blockers)),
            "ready": not blockers,
        }

    def _loc_buckets(self, target: str) -> dict[str, Any]:
        values: dict[str, dict[str, int]] = defaultdict(
            lambda: {"files": 0, "added": 0, "deleted": 0}
        )
        output = self._git(
            "diff",
            "--numstat",
            P2_BASE_COMMIT,
            target,
        )
        for line in output.splitlines():
            columns = line.split("\t", 2)
            if len(columns) != 3:
                continue
            added, deleted, relative = columns
            path = Path(relative)
            parts = {part.casefold() for part in path.parts}
            if "loopx_runtime" in parts:
                bucket = "runtime-assets"
            elif parts & {"tests", "test"}:
                bucket = "test"
            elif path.parts and path.parts[0].casefold() == "docs":
                bucket = (
                    "generated"
                    if "evidence" in parts
                    else "docs"
                )
            elif path.parts and path.parts[0].casefold() == "scripts":
                bucket = "scripts"
            elif path.parts and path.parts[0].casefold() == "config":
                bucket = "config"
            elif path.suffix.casefold() in {
                ".sqlite",
                ".sqlite3",
                ".csv",
                ".jsonl",
            }:
                bucket = "data"
            elif path.parts and path.parts[0].casefold() in {
                "packages",
                "apps",
            }:
                bucket = "production"
            else:
                bucket = "other"
            values[bucket]["files"] += 1
            if added.isdigit():
                values[bucket]["added"] += int(added)
            if deleted.isdigit():
                values[bucket]["deleted"] += int(deleted)
        return {
            "schema": "zyra.phase2-loc-buckets/v1",
            "base_commit": P2_BASE_COMMIT,
            "target_commit": target,
            "buckets": dict(sorted(values.items())),
        }

    def _expected_regression_commands(
        self,
        *,
        output_root: Path,
        target: str,
    ) -> dict[str, tuple[str, ...]]:
        python = str(Path(sys.executable).resolve())
        bun_name = "bun.exe" if sys.platform == "win32" else "bun"
        bun = str((self.root / "node_modules" / ".bin" / bun_name).resolve())
        policy = _load_json(self.root / "config" / "release-python-tests.json")
        python_test_arguments = (
            *(
                f"--ignore={self.root / str(relative)}"
                for relative in policy.get("ignore_files", ())
            ),
            *(
                f"--deselect={nodeid}"
                for nodeid in policy.get("deselect_nodeids", ())
            ),
            *(
                str(self.root / str(relative))
                for relative in policy.get("roots", ())
            ),
        )
        basetemp = output_root / "pytest"
        commands = (
            (
                "python-full-regression",
                (
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp",
                    str(basetemp),
                    *python_test_arguments,
                ),
            ),
            (
                "typescript-runtime-regression",
                (bun, "test", *TYPESCRIPT_RUNTIME_TEST_ROOTS),
            ),
            ("typescript-typecheck", (bun, "run", "typecheck")),
            ("web-typecheck", (bun, "run", "typecheck:web")),
            ("web-tests", (bun, "run", "test:web")),
            ("web-build", (bun, "run", "build:web")),
            ("phase1-m1", (python, "scripts/verify_m1.py")),
            ("phase1-m2", (python, "scripts/verify_m2.py")),
            ("phase1-m3", (python, "scripts/verify_m3.py")),
            (
                "phase1-final-freeze",
                (
                    python,
                    "scripts/verify_first_stage.py",
                    "--output",
                    str(output_root / "first-stage-final-freeze"),
                ),
            ),
            (
                "phase2-policy-contracts",
                (
                    python,
                    "scripts/verify_phase2_policy_contracts.py",
                    "--target-commit",
                    target,
                    "--require-strongest-active",
                    "--output",
                    str(output_root / "phase2-policy-contracts.json"),
                ),
            ),
            (
                "internalization-ledger",
                (
                    python,
                    "scripts/verify_internalization_ledger.py",
                    "--base",
                    P2_BASE_COMMIT,
                    "--json",
                ),
            ),
            (
                "loopx-offline-runtime",
                (python, "scripts/release/verify_loopx_runtime.py"),
            ),
            (
                "loopx-cross-version-restart",
                (
                    python,
                    "scripts/release/verify_loopx_cross_version_upgrade.py",
                ),
            ),
        )
        return dict(commands)

    def _regression_command_policy_blockers(
        self,
        commands: Sequence[Mapping[str, Any]],
        *,
        output_root: Path,
        target: str,
    ) -> list[str]:
        expected = self._expected_regression_commands(
            output_root=output_root,
            target=target,
        )
        blockers: list[str] = []
        for item in commands:
            command_id = str(item.get("command_id") or "")
            argv = item.get("argv")
            observed = (
                tuple(str(value) for value in argv)
                if isinstance(argv, Sequence)
                and not isinstance(argv, (str, bytes))
                else ()
            )
            if observed != expected.get(command_id):
                blockers.append(f"command_argv:{command_id}")
            cwd_value = str(item.get("cwd") or "")
            try:
                cwd = Path(cwd_value).resolve() if cwd_value else None
            except OSError:
                cwd = None
            if cwd is None or cwd != self.root:
                blockers.append(f"command_cwd:{command_id}")
        return blockers

    def _receipt_ready(self, path: Path, target: str) -> dict[str, Any]:
        value = _load_json(path)
        commands = value.get("commands")
        commands = (
            commands
            if isinstance(commands, Sequence)
            and not isinstance(commands, (str, bytes))
            else ()
        )
        blockers: list[str] = []
        command_ids = tuple(
            str(item.get("command_id") or "")
            for item in commands
            if isinstance(item, Mapping)
        )
        if command_ids != FINAL_REGRESSION_COMMAND_IDS:
            blockers.append("command_set")
        blockers.extend(
            self._regression_command_policy_blockers(
                tuple(item for item in commands if isinstance(item, Mapping)),
                output_root=path.parent,
                target=target,
            )
        )
        for item in commands:
            if not isinstance(item, Mapping):
                blockers.append("command_entry")
                continue
            for field in ("stdout", "stderr"):
                try:
                    log_path = _member(path.parent, item.get(field))
                except Phase2FreezeError:
                    blockers.append(f"command_log:{item.get('command_id')}:{field}")
                    continue
                if sha256_file(log_path) != item.get(f"{field}_sha256"):
                    blockers.append(
                        f"command_log_digest:{item.get('command_id')}:{field}"
                    )
            if (
                item.get("ready") is not True
                or int(item.get("returncode") or 0) != 0
                or not item.get("argv")
            ):
                blockers.append(f"command_failed:{item.get('command_id')}")
        policy = value.get("python_test_policy")
        policy = policy if isinstance(policy, Mapping) else {}
        policy_path = _member(self.root, policy.get("path"))
        if sha256_file(policy_path) != policy.get("sha256"):
            blockers.append("python_test_policy_digest")
        before = value.get("worktree_boundary_before")
        after = value.get("worktree_boundary_after")
        before = before if isinstance(before, Mapping) else {}
        after = after if isinstance(after, Mapping) else {}
        source_boundary_ready = (
            before.get("ready") is True
            and after.get("ready") is True
            and before.get("head_commit") == target
            and after.get("head_commit") == target
            and before.get("head_tree") == after.get("head_tree")
            and _embedded_digest_ready(before, "boundary_digest")
            and _embedded_digest_ready(after, "boundary_digest")
        )
        if not source_boundary_ready:
            blockers.append("target_source_boundary")
        if (
            value.get("schema") != "zyra.phase2-final-regression/v1"
            or not _embedded_digest_ready(value, "receipt_digest")
            or value.get("target_commit") != target
            or int(value.get("passed_count") or 0) != len(commands)
            or int(value.get("failed_count") or 0) != 0
        ):
            blockers.append("regression_receipt_integrity")
        ready = value.get("ready") is True and not blockers
        return {
            "schema": value.get("schema"),
            "target_commit": value.get("target_commit"),
            "target_matches": value.get("target_commit") == target,
            "command_count": len(commands),
            "all_commands_passed": bool(commands)
            and all(
                isinstance(item, Mapping)
                and item.get("ready") is True
                for item in commands
            ),
            "source_boundary_ready": source_boundary_ready,
            "blockers": sorted(set(blockers)),
            "ready": ready,
        }

    def verify_final_regression_receipt(
        self,
        path: Path,
        *,
        target_commit: str,
    ) -> dict[str, Any]:
        """Verify an immutable final-regression receipt for gate reuse."""

        target = self._git("rev-parse", f"{target_commit}^{{commit}}").strip()
        if target != target_commit:
            raise Phase2FreezeError(
                "final regression reuse requires a full target commit"
            )
        return self._receipt_ready(path.resolve(), target)

    def audit(
        self,
        *,
        target_commit: str,
        release_root: Path,
        sealed_root: Path,
        preflight_root: Path,
        regression_receipt: Path,
        custody_report: Path,
        contract_report: Path,
        output_root: Path,
    ) -> dict[str, Any]:
        paths = {
            "release_root": release_root.resolve(),
            "sealed_root": sealed_root.resolve(),
            "preflight_root": preflight_root.resolve(),
            "regression_receipt": regression_receipt.resolve(),
            "custody_report": custody_report.resolve(),
            "contract_report": contract_report.resolve(),
        }
        target = self._target_audit(target_commit)
        release = self._release_audit(paths["release_root"], target_commit)
        preflight = self._preflight_audit(
            paths["preflight_root"],
            target_commit,
        )
        sealed = self._sealed_audit(paths["sealed_root"], target_commit)
        regression = self._receipt_ready(
            paths["regression_receipt"],
            target_commit,
        )
        custody = self._custody_audit(paths["custody_report"], target_commit)
        contract = self._contract_audit(paths["contract_report"], target_commit)
        checks = {
            "target_head": target["target_matches_head"],
            "target_source_boundary": target["source_boundary_ready"],
            "target_tree_vendor_roots_zero": (
                target["target_tree_vendor_root_count"] == 0
            ),
            "root_source_runtime_dependencies_zero": (
                target["root_source_runtime_dependency_count"] == 0
            ),
            "manifest_external_dependencies_zero": (
                target[
                    "editable_link_external_manifest_dependency_count"
                ]
                == 0
            ),
            "release_pipeline": (
                release["pipeline_ready"]
                and release["pipeline_digest_ready"]
                and release["pipeline_target_matches"]
                and release["ci_executed"]
                and release["ci_ready"]
                and release["archive_digest_matches"]
                and release["release_git_boundary_ready"]
                and release["archive_source_commit_matches"]
            ),
            "release_archive_custody": release["archive"]["ready"],
            "cleanroom_isolation": release["cleanroom_ready"],
            "strongest_preflight": preflight["ready"],
            "sealed_long_runs": sealed["ready"],
            "full_regression": regression["ready"],
            "source_language_custody": custody["ready"],
            "policy_and_state_owner_contracts": contract["ready"],
        }
        blockers = sorted(key for key, passed in checks.items() if not passed)
        loc = self._loc_buckets(target_commit)
        result: dict[str, Any] = {
            "schema": "zyra.phase2-final-freeze-audit/v1",
            "slice_id": "P2-S06-03",
            "verdict": "PASS" if not blockers else "BLOCKED",
            "ready": not blockers,
            "target_commit": target_commit,
            "target_tree": self._git(
                "rev-parse",
                f"{target_commit}^{{tree}}",
            ).strip(),
            "checks": checks,
            "blockers": blockers,
            "target_audit": target,
            "release_audit": release,
            "preflight_audit": preflight,
            "sealed_audit": sealed,
            "regression_audit": regression,
            "source_language_custody": custody,
            "contract_audit": contract,
            "loc_buckets": loc,
        }
        result["report_digest"] = canonical_digest(result)
        output_root = output_root.resolve()
        output_root.mkdir(parents=True, exist_ok=False)
        (output_root / "final-freeze-audit.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        (output_root / "loc-buckets.json").write_text(
            json.dumps(loc, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        evidence_files: list[dict[str, Any]] = []
        candidates: set[Path] = {
            paths["regression_receipt"],
            paths["custody_report"],
            paths["contract_report"],
            paths["release_root"] / "pipeline-report.json",
            Path(str(release["archive"]["archive"])),
        }
        for root in (paths["sealed_root"], paths["preflight_root"]):
            candidates.update(path for path in root.rglob("*") if path.is_file())
        candidates.add(
            paths["release_root"]
            / "admission"
            / "ci"
            / "clean-install.json"
        )
        for path in sorted(candidates, key=lambda item: str(item)):
            if not path.is_file():
                continue
            evidence_files.append(
                {
                    "path": str(path),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
        evidence_index: dict[str, Any] = {
            "schema": "zyra.phase2-final-evidence-index/v1",
            "target_commit": target_commit,
            "file_count": len(evidence_files),
            "files": evidence_files,
        }
        evidence_index["index_digest"] = canonical_digest(evidence_index)
        (output_root / "evidence-index.json").write_text(
            json.dumps(
                evidence_index,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        checksums = {
            "schema": "zyra.phase2-release-checksums/v1",
            "target_commit": target_commit,
            "release_archive": {
                "path": release["archive"]["archive"],
                "sha256": release["archive"]["archive_sha256"],
            },
            "pipeline_report": {
                "path": str(paths["release_root"] / "pipeline-report.json"),
                "sha256": sha256_file(
                    paths["release_root"] / "pipeline-report.json"
                ),
            },
            "clean_install": {
                "path": release["clean_install_receipt"],
                "sha256": sha256_file(
                    Path(str(release["clean_install_receipt"]))
                ),
            },
        }
        checksums["digest"] = canonical_digest(checksums)
        (output_root / "release-checksums.json").write_text(
            json.dumps(
                checksums,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return result


__all__ = [
    "BANNED_ROOTS",
    "P2_BASE_COMMIT",
    "Phase2FreezeAuditor",
    "Phase2FreezeError",
    "canonical_digest",
    "inspect_release_archive",
    "sha256_file",
]
