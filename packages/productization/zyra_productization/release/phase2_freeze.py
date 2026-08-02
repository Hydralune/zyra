from __future__ import annotations

import hashlib
import io
import json
import os
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
FINAL_REGRESSION_RESUME_RERUN_IDS = (
    "phase2-policy-contracts",
    "internalization-ledger",
    "loopx-cross-version-restart",
)
FINAL_REGRESSION_RESUME_ALLOWED_PATHS = (
    "packages/productization/zyra_productization/release/phase2_freeze.py",
    "scripts/audit/verify_phase2_freeze.py",
    "scripts/release/run_phase2_final_regression.py",
    "scripts/release/verify_loopx_cross_version_upgrade.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
)
FINAL_REGRESSION_RESUME_REQUIRED_FIRST_COMMIT = (
    "363e011ff899e76cdb2c16e7246294f333cb5b9f"
)
FINAL_REGRESSION_SUPPLEMENT_SOURCE_TARGET = (
    "7995b64e8fd48028f1e12d4c60a23f6df4784a2e"
)
FINAL_REGRESSION_SUPPLEMENT_SOURCE_SHA256 = (
    "b8d9ff131cd1206044160a7cea590de1ea6bdd668e8e428fceb70fef143c4df4"
)
FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FIRST_COMMIT = (
    "751dbe2c2aff2172ad3bd82946486d09ca415f3c"
)
FINAL_REGRESSION_SUPPLEMENT_REQUIRED_SECOND_COMMIT = (
    "cb0328abcbeef3dc7957954a7eaece19288f50e9"
)
FINAL_REGRESSION_SUPPLEMENT_REQUIRED_THIRD_COMMIT = (
    "3290fe66b1f4018b49086212ff71b3b6f029bc97"
)
FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FOURTH_COMMIT = (
    "361a9ac947150e31ba6cccfd54101e13fab1ca78"
)
FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FIFTH_COMMIT = (
    "2710c7565fd0ef8d543f2f796f4dafb68b2873b3"
)
FINAL_REGRESSION_SUPPLEMENT_FIRST_ALLOWED_PATHS = (
    "packages/evaluation/zyra_evaluation/policy_benchmark/sealed_physical.py",
    "packages/productization/zyra_productization/release/phase2_freeze.py",
    "scripts/release/run_phase2_final_regression.py",
    "tests/scenarios/test_phase2_sealed_long_runs.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
)
FINAL_REGRESSION_SUPPLEMENT_SECOND_ALLOWED_PATHS = (
    "packages/orchestration/zyra_orchestration/topology_policy/production.py",
    "packages/productization/zyra_productization/release/phase2_freeze.py",
    "scripts/release/run_phase2_final_regression.py",
    "tests/integration/test_phase2_production_policy_main_path.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
)
FINAL_REGRESSION_SUPPLEMENT_THIRD_ALLOWED_PATHS = (
    "packages/orchestration/zyra_orchestration/task_graph.py",
    "packages/productization/zyra_productization/release/phase2_freeze.py",
    "packages/symbolic/zyra_symbolic/topology.py",
    "scripts/release/run_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
    "tests/unit/test_symbolic_control.py",
    "tests/unit/test_task_graph.py",
)
FINAL_REGRESSION_SUPPLEMENT_FOURTH_ALLOWED_PATHS = (
    "packages/productization/zyra_productization/release/cleanroom.py",
    "packages/productization/zyra_productization/release/phase2_freeze.py",
    "scripts/release/run_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
    "tests/unit/test_release_productization.py",
)
FINAL_REGRESSION_SUPPLEMENT_FIFTH_ALLOWED_PATHS = (
    "config/phase2/policies.yaml",
    "packages/productization/zyra_productization/release/phase2_freeze.py",
    "scripts/release/run_phase2_final_regression.py",
    "tests/unit/orchestration/test_policy_registry.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
)
FINAL_REGRESSION_SUPPLEMENT_FINAL_ALLOWED_PATHS = (
    "packages/productization/zyra_productization/release/cleanroom.py",
    "packages/productization/zyra_productization/release/phase2_freeze.py",
    "scripts/release/run_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
    "tests/unit/test_release_productization.py",
)
FINAL_REGRESSION_SUPPLEMENT_TESTS = (
    "tests/scenarios/test_phase2_sealed_long_runs.py",
    "tests/unit/test_deployment_profiles_runtime.py",
    "tests/unit/orchestration/test_agentprune_optimizer.py",
    "tests/integration/test_spatial_temporal_pruning.py",
    "tests/integration/test_mechanism_diagnostic_activation_rollback.py",
    "tests/integration/test_phase2_production_policy_main_path.py",
    "tests/integration/test_topology_policy_default_path.py",
    "tests/integration/test_topology_route_placement_projection.py",
    "tests/unit/orchestration/test_policy_registry.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
    "tests/unit/test_symbolic_control.py",
    "tests/unit/test_task_graph.py",
    "tests/unit/test_release_productization.py",
)
FINAL_REGRESSION_SUPPLEMENT_RERUN_IDS = (
    "phase2-policy-contracts",
    "internalization-ledger",
)
LOOPX_CROSS_VERSION_BASE_COMMIT = "3c4d1092187b1777468cad0ce2a772244d012197"
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

    @staticmethod
    def _require_full_commit(value: str, *, label: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise Phase2FreezeError(f"{label} must be a full lowercase commit id")
        return value

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
        self._require_full_commit(target, label="target commit")
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
        loopx_base_checkout: Path,
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
                    str(
                        self.root
                        / "docs"
                        / "reviews"
                        / "evidence"
                        / "M3-S03-02"
                        / "final-freeze"
                    ),
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
                    "--base-checkout",
                    str(loopx_base_checkout.resolve()),
                    "--target-checkout",
                    str(self.root),
                    "--workspace",
                    str(output_root / "loopx-cross-version-workspace"),
                    "--output",
                    str(output_root / "loopx-cross-version-upgrade.json"),
                ),
            ),
        )
        return dict(commands)

    def _expected_regression_environment(self, output_root: Path) -> dict[str, str]:
        with (self.root / "pyproject.toml").open("rb") as stream:
            pyproject = tomllib.load(stream)
        entries = (
            pyproject.get("tool", {})
            .get("setuptools", {})
            .get("packages", {})
            .get("find", {})
            .get("where", ())
        )
        python_path = os.pathsep.join(
            str((self.root / str(entry)).resolve()) for entry in entries
        )
        return {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "BUN_INSTALL_CACHE_DIR": str(output_root / "cache" / "bun"),
            "PIP_CACHE_DIR": str(output_root / "cache" / "pip"),
            "UV_CACHE_DIR": str(output_root / "cache" / "uv"),
            "ZYRA_STATE_ROOT": str(output_root / "state"),
            "PYTHONPATH": python_path,
        }

    def _loopx_base_boundary_audit(
        self,
        supplied: Mapping[str, Any],
        checkout: Path,
    ) -> dict[str, Any]:
        checkout = checkout.resolve()
        completed_trusted_common = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "--git-common-dir"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        completed_common = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--git-common-dir"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        def resolve_common(raw: str, root: Path) -> Path:
            selected = Path(raw.strip())
            return (
                selected.resolve()
                if selected.is_absolute()
                else (root / selected).resolve()
            )

        trusted_common = resolve_common(
            completed_trusted_common.stdout,
            self.root,
        )
        common = resolve_common(completed_common.stdout, checkout)
        common_ready = (
            completed_trusted_common.returncode == 0
            and completed_common.returncode == 0
            and common == trusted_common
        )
        if not common_ready:
            observed: dict[str, Any] = {
                "schema": "zyra.loopx-cross-version-base-boundary/v1",
                "ready": False,
                "checkout": str(checkout),
                "git_common_dir": str(common),
                "trusted_git_common_dir": str(trusted_common),
                "trusted_common_dir_matches": False,
                "rejected_before_worktree_commands": True,
            }
            observed["boundary_digest"] = canonical_digest(observed)
            return {
                "ready": False,
                "supplied_matches": dict(supplied) == observed,
                "observed": observed,
            }
        completed_head = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        completed_tree = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        completed_expected_tree = subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "rev-parse",
                f"{LOOPX_CROSS_VERSION_BASE_COMMIT}^{{tree}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        completed_toplevel = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        completed_status = subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            check=False,
            capture_output=True,
        )
        completed_flags = subprocess.run(
            ["git", "-C", str(checkout), "ls-files", "-v", "-z"],
            check=False,
            capture_output=True,
        )
        completed_index = subprocess.run(
            ["git", "-C", str(checkout), "ls-files", "-s", "-z"],
            check=False,
            capture_output=True,
        )
        completed_object_format = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--show-object-format"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        completed_sparse = subprocess.run(
            ["git", "-C", str(checkout), "config", "--bool", "core.sparseCheckout"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        status = completed_status.stdout
        flags = completed_flags.stdout
        index = completed_index.stdout
        head = completed_head.stdout.strip()
        tree = completed_tree.stdout.strip()
        expected_tree = completed_expected_tree.stdout.strip()
        object_format = completed_object_format.stdout.strip()
        try:
            toplevel = Path(completed_toplevel.stdout.strip()).resolve()
        except OSError:
            toplevel = Path()
        sparse = completed_sparse.stdout.strip().casefold() == "true"
        unsafe_flags = tuple(
            entry.decode("utf-8", errors="replace")
            for entry in flags.split(b"\0")
            if entry and not entry.startswith(b"H ")
        )
        blob_manifest = hashlib.sha256()
        blob_mismatches: list[str] = []
        unsupported_entries: list[str] = []
        tracked_paths: list[str] = []
        expected_blobs: list[str] = []
        tracked_count = 0
        for entry in index.split(b"\0"):
            if not entry:
                continue
            metadata, separator, path_bytes = entry.partition(b"\t")
            parts = metadata.split()
            path = path_bytes.decode("utf-8", errors="surrogateescape")
            if separator != b"\t" or len(parts) != 3:
                unsupported_entries.append(path or "<invalid-index-entry>")
                continue
            mode, expected_blob, stage = (
                parts[0].decode("ascii", errors="replace"),
                parts[1].decode("ascii", errors="replace"),
                parts[2].decode("ascii", errors="replace"),
            )
            tracked_count += 1
            blob_manifest.update(path_bytes)
            blob_manifest.update(b"\0" + parts[0] + b"\0" + parts[1] + b"\n")
            candidate = checkout / path
            if (
                mode not in {"100644", "100755"}
                or stage != "0"
                or not candidate.is_file()
                or "\n" in path
                or "\r" in path
            ):
                unsupported_entries.append(path)
                continue
            tracked_paths.append(path)
            expected_blobs.append(expected_blob)
        completed_hashes = subprocess.run(
            ["git", "-C", str(checkout), "hash-object", "--stdin-paths"],
            check=False,
            input="".join(f"{path}\n" for path in tracked_paths),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        canonical_hashes = completed_hashes.stdout.splitlines()
        if len(canonical_hashes) != len(tracked_paths):
            unsupported_entries.append("<canonical-hash-count-mismatch>")
        else:
            blob_mismatches.extend(
                path
                for path, expected_blob, actual_blob in zip(
                    tracked_paths,
                    expected_blobs,
                    canonical_hashes,
                    strict=True,
                )
                if actual_blob != expected_blob
            )
        commands_ready = all(
            completed.returncode == 0
            for completed in (
                completed_head,
                completed_tree,
                completed_expected_tree,
                completed_toplevel,
                completed_status,
                completed_flags,
                completed_index,
                completed_object_format,
                completed_hashes,
            )
        )
        observed: dict[str, Any] = {
            "schema": "zyra.loopx-cross-version-base-boundary/v1",
            "ready": (
                commands_ready
                and head == LOOPX_CROSS_VERSION_BASE_COMMIT
                and tree == expected_tree
                and toplevel == checkout
                and object_format == "sha1"
                and not status
                and not unsafe_flags
                and not sparse
                and not blob_mismatches
                and not unsupported_entries
            ),
            "checkout": str(checkout),
            "git_common_dir": str(common),
            "trusted_git_common_dir": str(trusted_common),
            "trusted_common_dir_matches": common == trusted_common,
            "rejected_before_worktree_commands": False,
            "head_commit": head,
            "head_tree": tree,
            "repository_toplevel": str(toplevel),
            "checkout_is_repository_toplevel": toplevel == checkout,
            "object_format": object_format,
            "tracked_untracked_and_ignored_clean": not status,
            "status_sha256": hashlib.sha256(status).hexdigest(),
            "status_entry_count": 0 if not status else status.count(b"\0"),
            "index_flags_sha256": hashlib.sha256(flags).hexdigest(),
            "unsafe_index_flag_count": len(unsafe_flags),
            "unsafe_index_flags": list(unsafe_flags[:20]),
            "sparse_checkout": sparse,
            "tracked_file_count": tracked_count,
            "tracked_blob_manifest_sha256": blob_manifest.hexdigest(),
            "tracked_blob_mismatch_count": len(blob_mismatches),
            "tracked_blob_mismatches": blob_mismatches[:20],
            "unsupported_index_entry_count": len(unsupported_entries),
            "unsupported_index_entries": unsupported_entries[:20],
        }
        observed["boundary_digest"] = canonical_digest(observed)
        return {
            "ready": observed["ready"] is True and dict(supplied) == observed,
            "supplied_matches": dict(supplied) == observed,
            "observed": observed,
        }

    @staticmethod
    def _roots_are_disjoint(first: Path, second: Path) -> bool:
        first = first.resolve()
        second = second.resolve()
        for candidate, root in ((first, second), (second, first)):
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            return False
        return True

    def _regression_command_policy_blockers(
        self,
        commands: Sequence[Mapping[str, Any]],
        *,
        output_root: Path,
        target: str,
        loopx_base_checkout: Path,
    ) -> list[str]:
        expected = self._expected_regression_commands(
            output_root=output_root,
            target=target,
            loopx_base_checkout=loopx_base_checkout,
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

    @staticmethod
    def _regression_boundary_ready(
        boundary: Mapping[str, Any],
        *,
        target: str,
        target_tree: str,
    ) -> bool:
        return (
            boundary.get("schema") == "zyra.release-worktree-boundary/v1"
            and boundary.get("ready") is True
            and boundary.get("head_commit") == target
            and boundary.get("head_tree") == target_tree
            and boundary.get("expected_head") == target
            and boundary.get("head_matches") is True
            and boundary.get("tracked_dirty_entries") in ([], ())
            and boundary.get("unexpected_untracked_entries") in ([], ())
            and _embedded_digest_ready(boundary, "boundary_digest")
        )

    def _failed_v1_resume_source_ready(
        self,
        path: Path,
        *,
        expected_sha256: str,
        source_target: str,
    ) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]]]:
        self._require_full_commit(source_target, label="resume source target")
        value = _load_json(path)
        commands_value = value.get("commands")
        commands = (
            tuple(item for item in commands_value if isinstance(item, Mapping))
            if isinstance(commands_value, Sequence)
            and not isinstance(commands_value, (str, bytes))
            else ()
        )
        blockers: list[str] = []
        if sha256_file(path) != expected_sha256:
            blockers.append("source_external_digest")
        if (
            value.get("schema") != "zyra.phase2-final-regression/v1"
            or value.get("ready") is not False
            or value.get("target_commit") != source_target
            or not _embedded_digest_ready(value, "receipt_digest")
            or int(value.get("passed_count") or 0) != 13
            or int(value.get("failed_count") or 0) != 1
        ):
            blockers.append("source_receipt_integrity")
        command_ids = tuple(str(item.get("command_id") or "") for item in commands)
        if command_ids != FINAL_REGRESSION_COMMAND_IDS:
            blockers.append("source_command_set")
        expected = self._expected_regression_commands(
            output_root=path.parent,
            target=source_target,
            loopx_base_checkout=self.root,
        )
        inherited: dict[str, Mapping[str, Any]] = {}
        failed_ids: list[str] = []
        for item in commands:
            command_id = str(item.get("command_id") or "")
            argv_value = item.get("argv")
            argv = (
                tuple(str(part) for part in argv_value)
                if isinstance(argv_value, Sequence)
                and not isinstance(argv_value, (str, bytes))
                else ()
            )
            expected_argv = expected.get(command_id, ())
            if command_id == "loopx-cross-version-restart":
                expected_argv = expected_argv[:2]
            if argv != expected_argv:
                blockers.append(f"source_command_argv:{command_id}")
            try:
                cwd_ready = Path(str(item.get("cwd") or "")).resolve() == self.root
            except OSError:
                cwd_ready = False
            if not cwd_ready:
                blockers.append(f"source_command_cwd:{command_id}")
            for field in ("stdout", "stderr"):
                try:
                    member = _member(path.parent, item.get(field))
                except Phase2FreezeError:
                    blockers.append(f"source_log:{command_id}:{field}")
                    continue
                if sha256_file(member) != item.get(f"{field}_sha256"):
                    blockers.append(f"source_log_digest:{command_id}:{field}")
            if item.get("ready") is not True or int(item.get("returncode") or 0) != 0:
                failed_ids.append(command_id)
            elif command_id not in FINAL_REGRESSION_RESUME_RERUN_IDS:
                inherited[command_id] = item
        if failed_ids != ["loopx-cross-version-restart"]:
            blockers.append("source_failure_scope")
        if commands:
            try:
                stderr = _member(
                    path.parent,
                    commands[-1].get("stderr"),
                ).read_text(encoding="utf-8", errors="replace")
            except Phase2FreezeError:
                stderr = ""
            required_error = (
                "--base-checkout, --target-checkout, --workspace and --output "
                "are required"
            )
            if required_error not in stderr:
                blockers.append("source_failure_reason")
        source_tree = self._git("rev-parse", f"{source_target}^{{tree}}").strip()
        before = value.get("worktree_boundary_before")
        after = value.get("worktree_boundary_after")
        if (
            not isinstance(before, Mapping)
            or not isinstance(after, Mapping)
            or not self._regression_boundary_ready(
                before,
                target=source_target,
                target_tree=source_tree,
            )
            or not self._regression_boundary_ready(
                after,
                target=source_target,
                target_tree=source_tree,
            )
        ):
            blockers.append("source_target_boundary")
        policy = value.get("python_test_policy")
        policy = policy if isinstance(policy, Mapping) else {}
        explicit_environment = value.get("explicit_environment")
        if (
            policy.get("path") != "config/release-python-tests.json"
            or policy.get("sha256")
            != sha256_file(self.root / "config" / "release-python-tests.json")
            or value.get("p2_base_commit") != P2_BASE_COMMIT
        ):
            blockers.append("source_test_policy")
        if (
            not isinstance(explicit_environment, Mapping)
            or dict(explicit_environment)
            != self._expected_regression_environment(path.parent)
        ):
            blockers.append("source_explicit_environment")
        return {
            "ready": not blockers,
            "blockers": sorted(set(blockers)),
            "target_commit": source_target,
            "target_tree": source_tree,
            "receipt_sha256": sha256_file(path),
            "receipt_digest": value.get("receipt_digest"),
            "inherited_command_ids": sorted(inherited),
        }, inherited

    def _resume_delta_audit(
        self,
        supplied: Mapping[str, Any],
        *,
        source_target: str,
        target: str,
    ) -> dict[str, Any]:
        self._require_full_commit(source_target, label="resume source target")
        self._require_full_commit(target, label="resume target")
        blockers: list[str] = []
        allowed = set(FINAL_REGRESSION_RESUME_ALLOWED_PATHS)
        allowlist_payload = json.dumps(
            FINAL_REGRESSION_RESUME_ALLOWED_PATHS,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        commit_chain = (
            FINAL_REGRESSION_RESUME_REQUIRED_FIRST_COMMIT,
            target,
        )
        expected_parents = (
            source_target,
            FINAL_REGRESSION_RESUME_REQUIRED_FIRST_COMMIT,
        )
        observed_parents = tuple(
            self._git("rev-list", "--parents", "-n", "1", commit).split()
            for commit in commit_chain
        )
        exact_chain = all(
            parents == [commit, expected_parent]
            for commit, expected_parent, parents in zip(
                commit_chain,
                expected_parents,
                observed_parents,
                strict=True,
            )
        )
        if not exact_chain:
            observed = {
                "source_target_commit": source_target,
                "source_target_tree": "",
                "target_commit": target,
                "target_tree": "",
                "direct_single_parent": False,
                "linear_single_parent_chain": False,
                "required_first_commit": (
                    FINAL_REGRESSION_RESUME_REQUIRED_FIRST_COMMIT
                ),
                "commit_count": 2,
                "commit_chain": list(commit_chain),
                "commit_path_changes": [],
                "changed_paths": [],
                "change_statuses": [],
                "blob_transitions": [],
                "allowed_paths": list(FINAL_REGRESSION_RESUME_ALLOWED_PATHS),
                "allowlist_sha256": hashlib.sha256(allowlist_payload).hexdigest(),
                "diff_sha256": "",
                "production_or_configuration_changed": False,
                "rename_symlink_or_submodule_changed": False,
            }
            blockers.append("target_not_exact_two_commit_remediation_chain")
            if dict(supplied) != observed:
                blockers.append("target_delta_receipt_mismatch")
            return {"ready": False, "blockers": blockers, **observed}

        status_lines = tuple(
            line
            for line in self._git(
                "diff",
                "--name-status",
                "--no-renames",
                source_target,
                target,
            ).splitlines()
            if line
        )
        changed_paths: list[str] = []
        transitions: list[dict[str, str]] = []
        if not status_lines:
            blockers.append("target_delta_missing")
        for line in status_lines:
            status, separator, path = line.partition("\t")
            if separator != "\t" or status != "M" or path not in allowed:
                blockers.append(f"forbidden_target_change:{line}")
                continue
            source_entry = self._git("ls-tree", source_target, "--", path).split()
            target_entry = self._git("ls-tree", target, "--", path).split()
            if (
                len(source_entry) < 3
                or len(target_entry) < 3
                or source_entry[0] != target_entry[0]
                or source_entry[0] not in {"100644", "100755"}
                or source_entry[1] != "blob"
                or target_entry[1] != "blob"
            ):
                blockers.append(f"target_mode_or_type:{path}")
                continue
            changed_paths.append(path)
            transitions.append(
                {
                    "path": path,
                    "mode": source_entry[0],
                    "source_blob": source_entry[2],
                    "target_blob": target_entry[2],
                }
            )
        if set(changed_paths) != set(FINAL_REGRESSION_RESUME_ALLOWED_PATHS) or len(
            changed_paths
        ) != len(FINAL_REGRESSION_RESUME_ALLOWED_PATHS):
            blockers.append("required_control_plane_delta_incomplete")

        previous = source_target
        commit_path_changes: list[dict[str, Any]] = []
        for commit in commit_chain:
            commit_statuses = tuple(
                line
                for line in self._git(
                    "diff",
                    "--name-status",
                    "--no-renames",
                    previous,
                    commit,
                ).splitlines()
                if line
            )
            if not commit_statuses:
                blockers.append(f"empty_remediation_commit:{commit}")
            segment_transitions: list[dict[str, str]] = []
            for line in commit_statuses:
                status, separator, path = line.partition("\t")
                if separator != "\t" or status != "M" or path not in allowed:
                    blockers.append(f"forbidden_remediation_commit_change:{commit}:{line}")
                    continue
                parent_entry = self._git("ls-tree", previous, "--", path).split()
                commit_entry = self._git("ls-tree", commit, "--", path).split()
                if (
                    len(parent_entry) < 3
                    or len(commit_entry) < 3
                    or parent_entry[0] != commit_entry[0]
                    or parent_entry[0] not in {"100644", "100755"}
                    or parent_entry[1] != "blob"
                    or commit_entry[1] != "blob"
                ):
                    blockers.append(f"remediation_commit_mode_or_type:{commit}:{path}")
                    continue
                segment_transitions.append(
                    {
                        "path": path,
                        "mode": parent_entry[0],
                        "source_blob": parent_entry[2],
                        "target_blob": commit_entry[2],
                    }
                )
            segment_diff = subprocess.run(
                ["git", "diff", "--binary", "--no-renames", previous, commit],
                cwd=self.root,
                check=True,
                capture_output=True,
            ).stdout
            commit_path_changes.append(
                {
                    "commit": commit,
                    "parent": previous,
                    "change_statuses": list(commit_statuses),
                    "blob_transitions": segment_transitions,
                    "diff_sha256": hashlib.sha256(segment_diff).hexdigest(),
                }
            )
            previous = commit
        diff = subprocess.run(
            ["git", "diff", "--binary", "--no-renames", source_target, target],
            cwd=self.root,
            check=True,
            capture_output=True,
        ).stdout
        observed = {
            "source_target_commit": source_target,
            "source_target_tree": self._git(
                "rev-parse", f"{source_target}^{{tree}}"
            ).strip(),
            "target_commit": target,
            "target_tree": self._git("rev-parse", f"{target}^{{tree}}").strip(),
            "direct_single_parent": False,
            "linear_single_parent_chain": True,
            "required_first_commit": FINAL_REGRESSION_RESUME_REQUIRED_FIRST_COMMIT,
            "commit_count": len(commit_chain),
            "commit_chain": list(commit_chain),
            "commit_path_changes": commit_path_changes,
            "changed_paths": changed_paths,
            "change_statuses": list(status_lines),
            "blob_transitions": transitions,
            "allowed_paths": list(FINAL_REGRESSION_RESUME_ALLOWED_PATHS),
            "allowlist_sha256": hashlib.sha256(allowlist_payload).hexdigest(),
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "production_or_configuration_changed": False,
            "rename_symlink_or_submodule_changed": False,
        }
        if dict(supplied) != observed:
            blockers.append("target_delta_receipt_mismatch")
        return {"ready": not blockers, "blockers": blockers, **observed}

    def _supplement_delta_audit(
        self,
        supplied: Mapping[str, Any],
        *,
        target: str,
    ) -> dict[str, Any]:
        self._require_full_commit(target, label="supplement target")
        source = FINAL_REGRESSION_SUPPLEMENT_SOURCE_TARGET
        blockers: list[str] = []
        commit_chain = (
            FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FIRST_COMMIT,
            FINAL_REGRESSION_SUPPLEMENT_REQUIRED_SECOND_COMMIT,
            FINAL_REGRESSION_SUPPLEMENT_REQUIRED_THIRD_COMMIT,
            FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FOURTH_COMMIT,
            FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FIFTH_COMMIT,
            target,
        )
        expected_parents = (
            source,
            FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FIRST_COMMIT,
            FINAL_REGRESSION_SUPPLEMENT_REQUIRED_SECOND_COMMIT,
            FINAL_REGRESSION_SUPPLEMENT_REQUIRED_THIRD_COMMIT,
            FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FOURTH_COMMIT,
            FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FIFTH_COMMIT,
        )
        exact_chain = True
        for commit, expected_parent in zip(
            commit_chain,
            expected_parents,
            strict=True,
        ):
            parents = self._git(
                "rev-list", "--parents", "-n", "1", commit
            ).split()
            if parents != [commit, expected_parent]:
                exact_chain = False
                blockers.append("supplement_target_not_exact_six_commit_chain")
                break
        segment_allowlists = (
            FINAL_REGRESSION_SUPPLEMENT_FIRST_ALLOWED_PATHS,
            FINAL_REGRESSION_SUPPLEMENT_SECOND_ALLOWED_PATHS,
            FINAL_REGRESSION_SUPPLEMENT_THIRD_ALLOWED_PATHS,
            FINAL_REGRESSION_SUPPLEMENT_FOURTH_ALLOWED_PATHS,
            FINAL_REGRESSION_SUPPLEMENT_FIFTH_ALLOWED_PATHS,
            FINAL_REGRESSION_SUPPLEMENT_FINAL_ALLOWED_PATHS,
        )
        cumulative_allowed = tuple(
            dict.fromkeys(
                (
                    *FINAL_REGRESSION_SUPPLEMENT_FIRST_ALLOWED_PATHS,
                    *FINAL_REGRESSION_SUPPLEMENT_SECOND_ALLOWED_PATHS,
                    *FINAL_REGRESSION_SUPPLEMENT_THIRD_ALLOWED_PATHS,
                    *FINAL_REGRESSION_SUPPLEMENT_FOURTH_ALLOWED_PATHS,
                    *FINAL_REGRESSION_SUPPLEMENT_FIFTH_ALLOWED_PATHS,
                    *FINAL_REGRESSION_SUPPLEMENT_FINAL_ALLOWED_PATHS,
                )
            )
        )
        commit_path_changes: list[dict[str, Any]] = []
        status_lines: tuple[str, ...] = ()
        changed_paths: list[str] = []
        transitions: list[dict[str, str]] = []
        diff = b""
        if exact_chain:
            previous = source
            for commit, allowed_paths in zip(
                commit_chain,
                segment_allowlists,
                strict=True,
            ):
                allowed = set(allowed_paths)
                segment_statuses = tuple(
                    line
                    for line in self._git(
                        "diff",
                        "--name-status",
                        "--no-renames",
                        previous,
                        commit,
                    ).splitlines()
                    if line
                )
                segment_paths: list[str] = []
                segment_transitions: list[dict[str, str]] = []
                if not segment_statuses:
                    blockers.append("supplement_target_delta_missing")
                for line in segment_statuses:
                    status, separator, path = line.partition("\t")
                    if separator != "\t" or status != "M" or path not in allowed:
                        blockers.append(f"forbidden_supplement_change:{line}")
                        continue
                    source_entry = self._git(
                        "ls-tree", previous, "--", path
                    ).split()
                    target_entry = self._git(
                        "ls-tree", commit, "--", path
                    ).split()
                    if (
                        len(source_entry) < 3
                        or len(target_entry) < 3
                        or source_entry[0] != target_entry[0]
                        or source_entry[0] not in {"100644", "100755"}
                        or source_entry[1] != "blob"
                        or target_entry[1] != "blob"
                    ):
                        blockers.append(f"supplement_mode_or_type:{path}")
                        continue
                    segment_paths.append(path)
                    segment_transitions.append(
                        {
                            "path": path,
                            "mode": source_entry[0],
                            "source_blob": source_entry[2],
                            "target_blob": target_entry[2],
                        }
                    )
                if (
                    set(segment_paths) != allowed
                    or len(segment_paths) != len(allowed)
                ):
                    blockers.append("supplement_required_delta_incomplete")
                segment_diff = subprocess.run(
                    [
                        "git",
                        "diff",
                        "--binary",
                        "--no-renames",
                        previous,
                        commit,
                    ],
                    cwd=self.root,
                    check=True,
                    capture_output=True,
                ).stdout
                commit_path_changes.append(
                    {
                        "commit": commit,
                        "parent": previous,
                        "change_statuses": list(segment_statuses),
                        "blob_transitions": segment_transitions,
                        "allowed_paths": list(allowed_paths),
                        "diff_sha256": hashlib.sha256(
                            segment_diff
                        ).hexdigest(),
                    }
                )
                previous = commit

            status_lines = tuple(
                line
                for line in self._git(
                    "diff",
                    "--name-status",
                    "--no-renames",
                    source,
                    target,
                ).splitlines()
                if line
            )
            if not status_lines:
                blockers.append("supplement_target_delta_missing")
            for line in status_lines:
                status, separator, path = line.partition("\t")
                if (
                    separator != "\t"
                    or status != "M"
                    or path not in cumulative_allowed
                ):
                    blockers.append(f"forbidden_supplement_cumulative:{line}")
                    continue
                source_entry = self._git("ls-tree", source, "--", path).split()
                target_entry = self._git("ls-tree", target, "--", path).split()
                if (
                    len(source_entry) < 3
                    or len(target_entry) < 3
                    or source_entry[0] != target_entry[0]
                    or source_entry[0] not in {"100644", "100755"}
                    or source_entry[1] != "blob"
                    or target_entry[1] != "blob"
                ):
                    blockers.append(f"supplement_mode_or_type:{path}")
                    continue
                changed_paths.append(path)
                transitions.append(
                    {
                        "path": path,
                        "mode": source_entry[0],
                        "source_blob": source_entry[2],
                        "target_blob": target_entry[2],
                    }
                )
            if set(changed_paths) != set(cumulative_allowed):
                blockers.append("supplement_cumulative_delta_incomplete")
            diff = subprocess.run(
                ["git", "diff", "--binary", "--no-renames", source, target],
                cwd=self.root,
                check=True,
                capture_output=True,
            ).stdout
        allowlist_payload = json.dumps(
            cumulative_allowed,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        diff_sha = hashlib.sha256(diff).hexdigest() if exact_chain else ""
        observed = {
            "source_target_commit": source,
            "source_target_tree": (
                self._git("rev-parse", f"{source}^{{tree}}").strip()
                if exact_chain
                else ""
            ),
            "target_commit": target,
            "target_tree": (
                self._git("rev-parse", f"{target}^{{tree}}").strip()
                if exact_chain
                else ""
            ),
            "direct_single_parent": False,
            "linear_single_parent_chain": exact_chain,
            "required_first_commit": (
                FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FIRST_COMMIT
            ),
            "required_second_commit": (
                FINAL_REGRESSION_SUPPLEMENT_REQUIRED_SECOND_COMMIT
            ),
            "required_third_commit": (
                FINAL_REGRESSION_SUPPLEMENT_REQUIRED_THIRD_COMMIT
            ),
            "required_fourth_commit": (
                FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FOURTH_COMMIT
            ),
            "required_fifth_commit": (
                FINAL_REGRESSION_SUPPLEMENT_REQUIRED_FIFTH_COMMIT
            ),
            "commit_count": len(commit_chain),
            "commit_chain": list(commit_chain),
            "commit_path_changes": commit_path_changes,
            "changed_paths": changed_paths,
            "change_statuses": list(status_lines),
            "blob_transitions": transitions,
            "allowed_paths": list(cumulative_allowed),
            "allowlist_sha256": hashlib.sha256(allowlist_payload).hexdigest(),
            "diff_sha256": diff_sha,
            "production_or_configuration_changed": True,
            "bounded_production_change": True,
            "rename_symlink_or_submodule_changed": False,
        }
        if dict(supplied) != observed:
            blockers.append("supplement_delta_receipt_mismatch")
        return {"ready": not blockers, "blockers": sorted(set(blockers)), **observed}

    def _resume_receipt_ready(
        self,
        path: Path,
        value: Mapping[str, Any],
        *,
        target: str,
        actual_receipt_sha256: str,
        expected_receipt_sha256: str,
        require_current_boundary: bool = True,
    ) -> dict[str, Any]:
        self._require_full_commit(target, label="resume target")
        blockers: list[str] = []
        expected_digest = expected_receipt_sha256.strip().casefold()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
            or actual_receipt_sha256 != expected_digest
        ):
            blockers.append("external_receipt_digest")
        source_target = str(value.get("source_target_commit") or "")
        if re.fullmatch(r"[0-9a-f]{40}", source_target) is None:
            blockers.append("source_target_commit")
            return {
                "schema": value.get("schema"),
                "target_commit": value.get("target_commit"),
                "target_matches": value.get("target_commit") == target,
                "command_count": 0,
                "all_commands_passed": False,
                "source_boundary_ready": False,
                "current_source_boundary_ready": False,
                "receipt_sha256": actual_receipt_sha256,
                "external_receipt_digest_required": True,
                "external_receipt_digest_matches": (
                    actual_receipt_sha256 == expected_digest
                ),
                "source_receipt_audit": {
                    "ready": False,
                    "blockers": ["source_target_commit"],
                },
                "target_delta_audit": {
                    "ready": False,
                    "blockers": ["source_target_commit"],
                },
                "loopx_result_ready": False,
                "blockers": sorted(set(blockers)),
                "ready": False,
            }
        if (
            value.get("schema") != "zyra.phase2-final-regression-resume/v1"
            or value.get("ready") is not True
            or value.get("target_commit") != target
            or value.get("target_tree")
            != self._git("rev-parse", f"{target}^{{tree}}").strip()
            or not _embedded_digest_ready(value, "receipt_digest")
            or int(value.get("passed_count") or 0) != len(FINAL_REGRESSION_COMMAND_IDS)
            or int(value.get("failed_count") or 0) != 0
            or value.get("p2_base_commit") != P2_BASE_COMMIT
            or value.get("loopx_cross_version_base_commit")
            != LOOPX_CROSS_VERSION_BASE_COMMIT
            or value.get("duplicate_heavy_execution_avoided") is not True
        ):
            blockers.append("resume_receipt_integrity")
        source_sha = str(value.get("source_receipt_sha256") or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha):
            blockers.append("source_receipt_digest")
        source_target_valid = re.fullmatch(r"[0-9a-f]{40}", source_target) is not None
        if not source_target_valid:
            source_audit = {
                "ready": False,
                "blockers": ["source_target_commit"],
            }
            inherited = {}
            delta_audit = {
                "ready": False,
                "blockers": ["source_target_commit"],
            }
            blockers.append("source_target_commit")
        else:
            try:
                source_path = _member(
                    self.root,
                    value.get("source_receipt"),
                    repository_relative=True,
                )
                source_audit, inherited = self._failed_v1_resume_source_ready(
                    source_path,
                    expected_sha256=source_sha,
                    source_target=source_target,
                )
                if source_audit["ready"] is not True:
                    blockers.extend(source_audit["blockers"])
                if (
                    value.get("source_receipt_digest")
                    != source_audit.get("receipt_digest")
                    or value.get("source_target_tree")
                    != source_audit.get("target_tree")
                ):
                    blockers.append("source_receipt_cross_binding")
            except Phase2FreezeError:
                source_audit = {
                    "ready": False,
                    "blockers": ["source_receipt_path"],
                }
                inherited = {}
                blockers.append("source_receipt_path")
            delta_value = value.get("target_delta")
            delta_audit = self._resume_delta_audit(
                delta_value if isinstance(delta_value, Mapping) else {},
                source_target=source_target,
                target=target,
            )
            if delta_audit["ready"] is not True:
                blockers.extend(delta_audit["blockers"])
        loopx_base = Path(str(value.get("loopx_base_checkout") or "."))
        base_boundary_before_value = value.get("loopx_base_boundary_before")
        base_boundary_after_value = value.get("loopx_base_boundary_after")
        base_boundary_before_audit = self._loopx_base_boundary_audit(
            base_boundary_before_value
            if isinstance(base_boundary_before_value, Mapping)
            else {},
            loopx_base,
        )
        live_base_boundary = base_boundary_before_audit["observed"]
        after_supplied = (
            dict(base_boundary_after_value)
            if isinstance(base_boundary_after_value, Mapping)
            else {}
        )
        base_boundary_after_audit = {
            "ready": live_base_boundary.get("ready") is True
            and after_supplied == live_base_boundary,
            "supplied_matches": after_supplied == live_base_boundary,
            "observed": live_base_boundary,
        }
        if (
            base_boundary_before_audit["ready"] is not True
            or base_boundary_after_audit["ready"] is not True
            or base_boundary_before_value != base_boundary_after_value
        ):
            blockers.append("resume_loopx_base_boundary")
        if not self._roots_are_disjoint(path.parent, loopx_base):
            blockers.append("resume_loopx_base_output_overlap")
        expected_full = self._expected_regression_commands(
            output_root=path.parent,
            target=target,
            loopx_base_checkout=loopx_base,
        )
        remediation_argv = (
            str(Path(sys.executable).resolve()),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(path.parent / "remediation-pytest"),
            str(
                self.root
                / "tests"
                / "unit"
                / "productization"
                / "test_phase2_final_regression.py"
            ),
            str(
                self.root
                / "tests"
                / "unit"
                / "productization"
                / "test_phase2_freeze_audit.py"
            ),
        )
        expected_reruns = {
            "remediation-targeted-python": remediation_argv,
            **{
                command_id: expected_full[command_id]
                for command_id in FINAL_REGRESSION_RESUME_RERUN_IDS
            },
        }
        reruns_value = value.get("rerun_commands")
        reruns = (
            tuple(item for item in reruns_value if isinstance(item, Mapping))
            if isinstance(reruns_value, Sequence)
            and not isinstance(reruns_value, (str, bytes))
            else ()
        )
        rerun_ids = tuple(str(item.get("command_id") or "") for item in reruns)
        if rerun_ids != tuple(expected_reruns):
            blockers.append("resume_rerun_set")
        if value.get("remediation_command_ids") != [
            "remediation-targeted-python"
        ]:
            blockers.append("resume_remediation_set")
        rerun_by_id: dict[str, Mapping[str, Any]] = {}
        for item in reruns:
            command_id = str(item.get("command_id") or "")
            argv_value = item.get("argv")
            argv = (
                tuple(str(part) for part in argv_value)
                if isinstance(argv_value, Sequence)
                and not isinstance(argv_value, (str, bytes))
                else ()
            )
            if argv != expected_reruns.get(command_id):
                blockers.append(f"resume_command_argv:{command_id}")
            try:
                cwd_ready = Path(str(item.get("cwd") or "")).resolve() == self.root
            except OSError:
                cwd_ready = False
            if (
                not cwd_ready
                or item.get("ready") is not True
                or int(item.get("returncode") or 0) != 0
            ):
                blockers.append(f"resume_command_failed:{command_id}")
            for field in ("stdout", "stderr"):
                try:
                    member = _member(path.parent, item.get(field))
                except Phase2FreezeError:
                    blockers.append(f"resume_command_log:{command_id}:{field}")
                    continue
                if sha256_file(member) != item.get(f"{field}_sha256"):
                    blockers.append(f"resume_command_log_digest:{command_id}:{field}")
            rerun_by_id[command_id] = item
        logical_value = value.get("logical_gates")
        logical = (
            tuple(item for item in logical_value if isinstance(item, Mapping))
            if isinstance(logical_value, Sequence)
            and not isinstance(logical_value, (str, bytes))
            else ()
        )
        if tuple(str(item.get("command_id") or "") for item in logical) != FINAL_REGRESSION_COMMAND_IDS:
            blockers.append("resume_logical_gate_set")
        for item in logical:
            command_id = str(item.get("command_id") or "")
            source = str(item.get("source") or "")
            if item.get("ready") is not True:
                blockers.append(f"resume_logical_gate_failed:{command_id}")
            if command_id in FINAL_REGRESSION_RESUME_RERUN_IDS:
                rerun = rerun_by_id.get(command_id, {})
                if (
                    source != "rerun"
                    or item.get("stdout_sha256") != rerun.get("stdout_sha256")
                    or item.get("stderr_sha256") != rerun.get("stderr_sha256")
                ):
                    blockers.append(f"resume_logical_rerun:{command_id}")
            else:
                parent = inherited.get(command_id, {})
                if (
                    source != "inherited"
                    or item.get("source_target_commit") != source_target
                    or item.get("source_receipt_sha256") != source_sha
                    or item.get("source_stdout_sha256") != parent.get("stdout_sha256")
                    or item.get("source_stderr_sha256") != parent.get("stderr_sha256")
                ):
                    blockers.append(f"resume_logical_inheritance:{command_id}")
        try:
            loopx_path = _member(path.parent, value.get("loopx_result"))
            loopx = _load_json(loopx_path)
        except Phase2FreezeError:
            loopx = {}
            blockers.append("resume_loopx_result")
        invariants = loopx.get("invariants")
        restarts = loopx.get("target_restarts")
        baseline = loopx.get("baseline")
        phases = (
            (baseline, loopx_base.resolve()),
            *((item, self.root) for item in restarts),
        ) if (
            isinstance(baseline, Mapping)
            and isinstance(restarts, Sequence)
            and not isinstance(restarts, (str, bytes))
            and len(restarts) == 2
            and all(isinstance(item, Mapping) for item in restarts)
        ) else ()
        isolation_paths: list[Path] = []
        isolation_ready = len(phases) == 3
        for phase, expected_checkout in phases:
            isolation = phase.get("deployment_state_isolation")
            if not isinstance(isolation, Mapping):
                isolation_ready = False
                continue
            isolation_path = Path(str(isolation.get("path") or ".")).resolve()
            expected_checkout = expected_checkout.resolve()
            isolation_ready = isolation_ready and (
                isolation.get("strategy")
                == "owned_checkout_temporary_directory"
                and isolation.get("cleaned") is True
                and Path(str(isolation.get("checkout") or ".")).resolve()
                == expected_checkout
                and expected_checkout in isolation_path.parents
                and not isolation_path.exists()
            )
            isolation_paths.append(isolation_path)
        isolation_ready = (
            isolation_ready
            and len(isolation_paths) == 3
            and len({os.path.normcase(str(item)) for item in isolation_paths}) == 3
        )
        loopx_ready = (
            sha256_file(loopx_path) == value.get("loopx_result_sha256")
            if "loopx_path" in locals() and loopx_path.is_file()
            else False
        ) and (
            loopx.get("schema") == "zyra.loopx-cross-version-upgrade/v1"
            and loopx.get("ready") is True
            and loopx.get("base_commit") == LOOPX_CROSS_VERSION_BASE_COMMIT
            and loopx.get("target_commit") == target
            and isinstance(restarts, Sequence)
            and not isinstance(restarts, (str, bytes))
            and len(restarts) == 2
            and all(
                isinstance(item, Mapping) and item.get("commit") == target
                for item in restarts
            )
            and isinstance(invariants, Mapping)
            and invariants.get("semantic_state_preserved") is True
            and invariants.get("cursor_monotonic") is True
            and invariants.get("duplicate_claim") is False
            and invariants.get("duplicate_spend") is False
            and invariants.get("duplicate_interaction") is False
            and invariants.get("duplicate_canonical_commit") is False
            and invariants.get("historical_install_preserved_and_ignored") is True
            and invariants.get("new_install_or_extraction") is False
            and invariants.get("independent_target_restart_count") == 2
            and invariants.get("checkout_runtime_state_cleaned") is True
            and invariants.get("checkout_runtime_state_paths_distinct") is True
            and isolation_ready
        )
        if not loopx_ready or value.get("loopx_result_ready") is not True:
            blockers.append("resume_loopx_invariants")
        policy = value.get("python_test_policy")
        policy = policy if isinstance(policy, Mapping) else {}
        explicit_environment = value.get("explicit_environment")
        if (
            policy.get("path") != "config/release-python-tests.json"
            or policy.get("sha256")
            != sha256_file(self.root / "config" / "release-python-tests.json")
        ):
            blockers.append("resume_test_policy")
        if (
            not isinstance(explicit_environment, Mapping)
            or dict(explicit_environment)
            != self._expected_regression_environment(path.parent)
        ):
            blockers.append("resume_explicit_environment")
        target_tree = self._git("rev-parse", f"{target}^{{tree}}").strip()
        before = value.get("worktree_boundary_before")
        after = value.get("worktree_boundary_after")
        current = (
            inspect_worktree(self.root, expected_head=target)
            if require_current_boundary
            else None
        )
        boundaries_ready = (
            isinstance(before, Mapping)
            and isinstance(after, Mapping)
            and self._regression_boundary_ready(
                before,
                target=target,
                target_tree=target_tree,
            )
            and self._regression_boundary_ready(
                after,
                target=target,
                target_tree=target_tree,
            )
            and (
                not require_current_boundary
                or (
                    isinstance(current, Mapping)
                    and self._regression_boundary_ready(
                        current,
                        target=target,
                        target_tree=target_tree,
                    )
                )
            )
        )
        if not boundaries_ready:
            blockers.append("resume_target_boundary")
        return {
            "schema": value.get("schema"),
            "target_commit": value.get("target_commit"),
            "target_matches": value.get("target_commit") == target,
            "command_count": len(logical),
            "all_commands_passed": len(logical) == len(FINAL_REGRESSION_COMMAND_IDS)
            and all(item.get("ready") is True for item in logical),
            "source_boundary_ready": source_audit.get("ready") is True,
            "current_source_boundary_ready": boundaries_ready,
            "current_target_boundary_required": require_current_boundary,
            "receipt_sha256": actual_receipt_sha256,
            "external_receipt_digest_required": True,
            "external_receipt_digest_matches": actual_receipt_sha256 == expected_digest,
            "source_receipt_audit": source_audit,
            "target_delta_audit": delta_audit,
            "loopx_result_ready": loopx_ready,
            "loopx_base_boundary_before": base_boundary_before_audit,
            "loopx_base_boundary_after": base_boundary_after_audit,
            "blockers": sorted(set(blockers)),
            "ready": value.get("ready") is True and not blockers,
        }

    def _supplement_receipt_ready(
        self,
        path: Path,
        value: Mapping[str, Any],
        *,
        target: str,
        actual_receipt_sha256: str,
        expected_receipt_sha256: str,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        expected_digest = expected_receipt_sha256.strip().casefold()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
            or actual_receipt_sha256 != expected_digest
        ):
            blockers.append("supplement_external_receipt_digest")
        source_sha = str(value.get("source_receipt_sha256") or "").casefold()
        if source_sha != FINAL_REGRESSION_SUPPLEMENT_SOURCE_SHA256:
            blockers.append("supplement_source_anchor")
        try:
            source_path = _member(
                self.root,
                value.get("source_receipt"),
                repository_relative=True,
            )
            source_raw = source_path.read_bytes()
            source_actual_sha = hashlib.sha256(source_raw).hexdigest()
            source_value = json.loads(source_raw.decode("utf-8"))
            if not isinstance(source_value, Mapping):
                raise Phase2FreezeError("supplement source receipt is not an object")
            source_audit = self._resume_receipt_ready(
                source_path,
                source_value,
                target=FINAL_REGRESSION_SUPPLEMENT_SOURCE_TARGET,
                actual_receipt_sha256=source_actual_sha,
                expected_receipt_sha256=FINAL_REGRESSION_SUPPLEMENT_SOURCE_SHA256,
                require_current_boundary=False,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, Phase2FreezeError):
            source_value = {}
            source_audit = {
                "ready": False,
                "blockers": ["supplement_source_receipt"],
            }
            blockers.append("supplement_source_receipt")
        if source_audit.get("ready") is not True:
            blockers.append("supplement_source_receipt_not_ready")
        if (
            value.get("source_target_commit")
            != FINAL_REGRESSION_SUPPLEMENT_SOURCE_TARGET
            or value.get("source_target_tree")
            != self._git(
                "rev-parse",
                f"{FINAL_REGRESSION_SUPPLEMENT_SOURCE_TARGET}^{{tree}}",
            ).strip()
            or value.get("source_receipt_digest")
            != source_value.get("receipt_digest")
        ):
            blockers.append("supplement_source_cross_binding")
        delta_value = value.get("target_delta")
        delta_audit = self._supplement_delta_audit(
            delta_value if isinstance(delta_value, Mapping) else {},
            target=target,
        )
        if delta_audit["ready"] is not True:
            blockers.extend(delta_audit["blockers"])
        loopx_base = Path(str(value.get("loopx_base_checkout") or "."))
        base_before_value = value.get("loopx_base_boundary_before")
        base_after_value = value.get("loopx_base_boundary_after")
        base_before = self._loopx_base_boundary_audit(
            base_before_value if isinstance(base_before_value, Mapping) else {},
            loopx_base,
        )
        live_base = base_before["observed"]
        base_after_supplied = (
            dict(base_after_value) if isinstance(base_after_value, Mapping) else {}
        )
        base_after = {
            "ready": live_base.get("ready") is True
            and base_after_supplied == live_base,
            "supplied_matches": base_after_supplied == live_base,
            "observed": live_base,
        }
        if (
            base_before.get("ready") is not True
            or base_after.get("ready") is not True
            or base_before_value != base_after_value
        ):
            blockers.append("supplement_loopx_base_boundary")
        if not self._roots_are_disjoint(path.parent, loopx_base):
            blockers.append("supplement_loopx_base_output_overlap")
        expected_full = self._expected_regression_commands(
            output_root=path.parent,
            target=target,
            loopx_base_checkout=loopx_base,
        )
        targeted_argv = (
            str(Path(sys.executable).resolve()),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(path.parent / "supplement-pytest"),
            *(
                str(self.root / relative)
                for relative in FINAL_REGRESSION_SUPPLEMENT_TESTS
            ),
        )
        expected_reruns = {
            "supplement-targeted-python": targeted_argv,
            **{
                command_id: expected_full[command_id]
                for command_id in FINAL_REGRESSION_SUPPLEMENT_RERUN_IDS
            },
        }
        reruns_value = value.get("rerun_commands")
        reruns = (
            tuple(item for item in reruns_value if isinstance(item, Mapping))
            if isinstance(reruns_value, Sequence)
            and not isinstance(reruns_value, (str, bytes))
            else ()
        )
        if tuple(str(item.get("command_id") or "") for item in reruns) != tuple(
            expected_reruns
        ):
            blockers.append("supplement_rerun_set")
        if value.get("remediation_command_ids") != ["supplement-targeted-python"]:
            blockers.append("supplement_remediation_set")
        rerun_by_id: dict[str, Mapping[str, Any]] = {}
        for item in reruns:
            command_id = str(item.get("command_id") or "")
            argv_value = item.get("argv")
            argv = (
                tuple(str(part) for part in argv_value)
                if isinstance(argv_value, Sequence)
                and not isinstance(argv_value, (str, bytes))
                else ()
            )
            if argv != expected_reruns.get(command_id):
                blockers.append(f"supplement_command_argv:{command_id}")
            try:
                cwd_ready = Path(str(item.get("cwd") or "")).resolve() == self.root
            except OSError:
                cwd_ready = False
            if (
                not cwd_ready
                or item.get("ready") is not True
                or int(item.get("returncode") or 0) != 0
            ):
                blockers.append(f"supplement_command_failed:{command_id}")
            for field in ("stdout", "stderr"):
                try:
                    member = _member(path.parent, item.get(field))
                except Phase2FreezeError:
                    blockers.append(f"supplement_command_log:{command_id}:{field}")
                    continue
                if sha256_file(member) != item.get(f"{field}_sha256"):
                    blockers.append(
                        f"supplement_command_log_digest:{command_id}:{field}"
                    )
            rerun_by_id[command_id] = item
        source_logical_value = source_value.get("logical_gates")
        source_logical = {
            str(item.get("command_id") or ""): item
            for item in source_logical_value
            if isinstance(item, Mapping)
        } if (
            isinstance(source_logical_value, Sequence)
            and not isinstance(source_logical_value, (str, bytes))
        ) else {}
        logical_value = value.get("logical_gates")
        logical = (
            tuple(item for item in logical_value if isinstance(item, Mapping))
            if isinstance(logical_value, Sequence)
            and not isinstance(logical_value, (str, bytes))
            else ()
        )
        if tuple(str(item.get("command_id") or "") for item in logical) != FINAL_REGRESSION_COMMAND_IDS:
            blockers.append("supplement_logical_gate_set")
        for item in logical:
            command_id = str(item.get("command_id") or "")
            if item.get("ready") is not True:
                blockers.append(f"supplement_logical_gate_failed:{command_id}")
            if command_id in FINAL_REGRESSION_SUPPLEMENT_RERUN_IDS:
                rerun = rerun_by_id.get(command_id, {})
                if (
                    item.get("source") != "rerun"
                    or item.get("stdout_sha256") != rerun.get("stdout_sha256")
                    or item.get("stderr_sha256") != rerun.get("stderr_sha256")
                ):
                    blockers.append(f"supplement_logical_rerun:{command_id}")
            else:
                source_gate = source_logical.get(command_id, {})
                if (
                    item.get("source") != "inherited_v2"
                    or item.get("source_target_commit")
                    != FINAL_REGRESSION_SUPPLEMENT_SOURCE_TARGET
                    or item.get("source_receipt_sha256")
                    != FINAL_REGRESSION_SUPPLEMENT_SOURCE_SHA256
                    or item.get("source_gate_digest")
                    != canonical_digest(source_gate)
                ):
                    blockers.append(f"supplement_logical_inheritance:{command_id}")
        loopx_ready = (
            source_audit.get("loopx_result_ready") is True
            and value.get("loopx_result_ready") is True
            and value.get("source_loopx_result_sha256")
            == source_value.get("loopx_result_sha256")
        )
        if not loopx_ready:
            blockers.append("supplement_loopx_inheritance")
        policy = value.get("python_test_policy")
        policy = policy if isinstance(policy, Mapping) else {}
        if (
            policy.get("path") != "config/release-python-tests.json"
            or policy.get("sha256")
            != sha256_file(self.root / "config" / "release-python-tests.json")
        ):
            blockers.append("supplement_test_policy")
        explicit_environment = value.get("explicit_environment")
        if (
            not isinstance(explicit_environment, Mapping)
            or dict(explicit_environment)
            != self._expected_regression_environment(path.parent)
        ):
            blockers.append("supplement_explicit_environment")
        target_tree = self._git("rev-parse", f"{target}^{{tree}}").strip()
        before = value.get("worktree_boundary_before")
        after = value.get("worktree_boundary_after")
        current = inspect_worktree(self.root, expected_head=target)
        boundaries_ready = (
            isinstance(before, Mapping)
            and isinstance(after, Mapping)
            and self._regression_boundary_ready(
                before,
                target=target,
                target_tree=target_tree,
            )
            and self._regression_boundary_ready(
                after,
                target=target,
                target_tree=target_tree,
            )
            and self._regression_boundary_ready(
                current,
                target=target,
                target_tree=target_tree,
            )
        )
        if not boundaries_ready:
            blockers.append("supplement_target_boundary")
        if (
            value.get("schema") != "zyra.phase2-final-regression-supplement/v1"
            or value.get("ready") is not True
            or value.get("target_commit") != target
            or value.get("target_tree") != target_tree
            or not _embedded_digest_ready(value, "receipt_digest")
            or int(value.get("passed_count") or 0)
            != len(FINAL_REGRESSION_COMMAND_IDS)
            or int(value.get("failed_count") or 0) != 0
            or value.get("p2_base_commit") != P2_BASE_COMMIT
            or value.get("loopx_cross_version_base_commit")
            != LOOPX_CROSS_VERSION_BASE_COMMIT
            or value.get("duplicate_heavy_execution_avoided") is not True
        ):
            blockers.append("supplement_receipt_integrity")
        return {
            "schema": value.get("schema"),
            "target_commit": value.get("target_commit"),
            "target_matches": value.get("target_commit") == target,
            "command_count": len(logical),
            "all_commands_passed": len(logical) == len(FINAL_REGRESSION_COMMAND_IDS)
            and all(item.get("ready") is True for item in logical),
            "source_boundary_ready": source_audit.get("ready") is True,
            "current_source_boundary_ready": boundaries_ready,
            "receipt_sha256": actual_receipt_sha256,
            "external_receipt_digest_required": True,
            "external_receipt_digest_matches": actual_receipt_sha256
            == expected_digest,
            "source_receipt_audit": source_audit,
            "target_delta_audit": delta_audit,
            "loopx_result_ready": loopx_ready,
            "loopx_base_boundary_before": base_before,
            "loopx_base_boundary_after": base_after,
            "blockers": sorted(set(blockers)),
            "ready": value.get("ready") is True and not blockers,
        }

    def _receipt_ready(
        self,
        path: Path,
        target: str,
        *,
        expected_receipt_sha256: str = "",
    ) -> dict[str, Any]:
        self._require_full_commit(target, label="regression target")
        raw_receipt = path.read_bytes()
        actual_receipt_sha256 = hashlib.sha256(raw_receipt).hexdigest()
        try:
            value = json.loads(raw_receipt.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Phase2FreezeError(
                f"invalid final regression receipt: {path}"
            ) from error
        if not isinstance(value, Mapping):
            raise Phase2FreezeError(
                f"final regression receipt must be an object: {path}"
            )
        if value.get("schema") == "zyra.phase2-final-regression-resume/v1":
            return self._resume_receipt_ready(
                path,
                value,
                target=target,
                actual_receipt_sha256=actual_receipt_sha256,
                expected_receipt_sha256=expected_receipt_sha256,
            )
        if value.get("schema") == "zyra.phase2-final-regression-supplement/v1":
            return self._supplement_receipt_ready(
                path,
                value,
                target=target,
                actual_receipt_sha256=actual_receipt_sha256,
                expected_receipt_sha256=expected_receipt_sha256,
            )
        commands = value.get("commands")
        commands = (
            commands
            if isinstance(commands, Sequence)
            and not isinstance(commands, (str, bytes))
            else ()
        )
        blockers: list[str] = []
        loopx_base = Path(str(value.get("loopx_base_checkout") or "."))
        base_boundary_before_value = value.get("loopx_base_boundary_before")
        base_boundary_after_value = value.get("loopx_base_boundary_after")
        base_boundary_before_audit = self._loopx_base_boundary_audit(
            base_boundary_before_value
            if isinstance(base_boundary_before_value, Mapping)
            else {},
            loopx_base,
        )
        live_base_boundary = base_boundary_before_audit["observed"]
        after_supplied = (
            dict(base_boundary_after_value)
            if isinstance(base_boundary_after_value, Mapping)
            else {}
        )
        base_boundary_after_audit = {
            "ready": live_base_boundary.get("ready") is True
            and after_supplied == live_base_boundary,
            "supplied_matches": after_supplied == live_base_boundary,
            "observed": live_base_boundary,
        }
        if (
            base_boundary_before_audit["ready"] is not True
            or base_boundary_after_audit["ready"] is not True
            or base_boundary_before_value != base_boundary_after_value
        ):
            blockers.append("loopx_base_boundary")
        if not self._roots_are_disjoint(path.parent, loopx_base):
            blockers.append("loopx_base_output_overlap")
        expected_digest = expected_receipt_sha256.strip().casefold()
        if expected_digest and (
            not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
            or actual_receipt_sha256 != expected_digest
        ):
            blockers.append("external_receipt_digest")
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
                loopx_base_checkout=Path(
                    str(value.get("loopx_base_checkout") or ".")
                ),
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
        target_tree = self._git("rev-parse", f"{target}^{{tree}}").strip()
        current_boundary = inspect_worktree(
            self.root,
            expected_head=target,
        )

        def boundary_ready(boundary: Mapping[str, Any]) -> bool:
            return (
                boundary.get("schema")
                == "zyra.release-worktree-boundary/v1"
                and boundary.get("ready") is True
                and boundary.get("head_commit") == target
                and boundary.get("head_tree") == target_tree
                and boundary.get("expected_head") == target
                and boundary.get("head_matches") is True
                and boundary.get("tracked_dirty_entries") in ([], ())
                and boundary.get("unexpected_untracked_entries") in ([], ())
                and _embedded_digest_ready(boundary, "boundary_digest")
            )

        source_boundary_ready = (
            boundary_ready(before)
            and boundary_ready(after)
            and before.get("head_tree") == after.get("head_tree")
        )
        if not source_boundary_ready:
            blockers.append("target_source_boundary")
        current_source_boundary_ready = boundary_ready(current_boundary)
        if not current_source_boundary_ready:
            blockers.append("current_target_source_boundary")
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
            "current_source_boundary_ready": current_source_boundary_ready,
            "receipt_sha256": actual_receipt_sha256,
            "external_receipt_digest_required": bool(expected_digest),
            "external_receipt_digest_matches": bool(
                expected_digest and actual_receipt_sha256 == expected_digest
            ),
            "blockers": sorted(set(blockers)),
            "loopx_base_boundary_before": base_boundary_before_audit,
            "loopx_base_boundary_after": base_boundary_after_audit,
            "ready": ready,
        }

    def verify_final_regression_receipt(
        self,
        path: Path,
        *,
        target_commit: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        """Verify an immutable final-regression receipt for gate reuse."""

        self._require_full_commit(target_commit, label="final regression target")
        target = self._git("rev-parse", f"{target_commit}^{{commit}}").strip()
        if target != target_commit:
            raise Phase2FreezeError(
                "final regression reuse requires a full target commit"
            )
        if not expected_sha256.strip():
            raise Phase2FreezeError(
                "final regression reuse requires an external SHA-256 anchor"
            )
        return self._receipt_ready(
            path.resolve(),
            target,
            expected_receipt_sha256=expected_sha256,
        )

    def audit(
        self,
        *,
        target_commit: str,
        release_root: Path,
        sealed_root: Path,
        preflight_root: Path,
        regression_receipt: Path,
        regression_receipt_sha256: str,
        custody_report: Path,
        contract_report: Path,
        output_root: Path,
    ) -> dict[str, Any]:
        self._require_full_commit(target_commit, label="freeze target")
        if not re.fullmatch(r"[0-9a-f]{64}", regression_receipt_sha256.casefold()):
            raise Phase2FreezeError(
                "freeze regression receipt SHA-256 must be a full lowercase digest"
            )
        paths = {
            "release_root": release_root.resolve(),
            "sealed_root": sealed_root.resolve(),
            "preflight_root": preflight_root.resolve(),
            "regression_receipt": regression_receipt.resolve(),
            "custody_report": custody_report.resolve(),
            "contract_report": contract_report.resolve(),
        }
        if not paths["regression_receipt"].is_file():
            raise Phase2FreezeError("freeze regression receipt is missing")
        expected_regression_sha = regression_receipt_sha256.casefold()
        if sha256_file(paths["regression_receipt"]) != expected_regression_sha:
            raise Phase2FreezeError(
                "freeze regression receipt does not match its external SHA-256 anchor"
            )
        target = self._target_audit(target_commit)
        regression = self._receipt_ready(
            paths["regression_receipt"],
            target_commit,
            expected_receipt_sha256=expected_regression_sha,
        )
        release = self._release_audit(paths["release_root"], target_commit)
        preflight = self._preflight_audit(
            paths["preflight_root"],
            target_commit,
        )
        sealed = self._sealed_audit(paths["sealed_root"], target_commit)
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
