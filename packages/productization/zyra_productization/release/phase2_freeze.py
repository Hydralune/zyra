from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import tarfile
import tomllib
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


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


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Phase2FreezeError(f"invalid JSON evidence: {path}") from error
    if not isinstance(value, Mapping):
        raise Phase2FreezeError(f"JSON evidence is not an object: {path}")
    return value


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
    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                member_names.append(member.name)
                if not member.isfile():
                    continue
                suffix = Path(member.name).suffix.casefold()
                if suffix == ".whl" or "sbom" in member.name.casefold():
                    stream = archive.extractfile(member)
                    if stream is None:
                        continue
                    payload = stream.read()
                    if suffix == ".whl":
                        wheels.append((member.name, payload))
                    if "sbom" in member.name.casefold():
                        sboms.append((member.name, payload))
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                member_names.append(name)
                payload = b""
                if name.casefold().endswith(".whl") or "sbom" in name.casefold():
                    payload = archive.read(name)
                if name.casefold().endswith(".whl"):
                    wheels.append((name, payload))
                if "sbom" in name.casefold():
                    sboms.append((name, payload))
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
        "ready": (
            not release_findings
            and bool(wheels)
            and not wheel_findings
            and bool(sboms)
            and not sbom_findings
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

    def _target_audit(self, target: str) -> dict[str, Any]:
        head = self._git("rev-parse", "HEAD").strip()
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

    @staticmethod
    def _preflight_audit(
        preflight_root: Path,
        target: str,
    ) -> dict[str, Any]:
        report = _load_json(preflight_root / "preflight-report.json")
        readiness = _load_json(
            preflight_root / "MechanismEvidenceReadinessReport.json"
        )
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
            ),
        }

    @staticmethod
    def _sealed_audit(
        sealed_root: Path,
        target: str,
    ) -> dict[str, Any]:
        validation = _load_json(
            sealed_root / "independent-validation.json"
        )
        index = _load_json(sealed_root / "sealed-evidence-index.json")
        blockers: list[str] = []
        if validation.get("valid") is not True:
            blockers.append("independent_validation")
        if validation.get("candidate_commit") != target:
            blockers.append("validation_target")
        if index.get("candidate_commit") != target:
            blockers.append("index_target")
        if index.get("manifest_commit") != target:
            blockers.append("manifest_target")
        runs = index.get("runs")
        if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)):
            runs = ()
        if len(runs) != 2:
            blockers.append("sealed_run_count")
        summaries: list[dict[str, Any]] = []
        for run in runs:
            if not isinstance(run, Mapping):
                blockers.append("sealed_run_invalid")
                continue
            hard_path = sealed_root / str(run.get("hard_gate_bundle") or "")
            verifier_path = sealed_root / str(run.get("final_verifier") or "")
            artifact_path = sealed_root / str(run.get("final_artifact") or "")
            hard = _load_json(hard_path)
            zero_ready = all(int(hard.get(field) or 0) == 0 for field in ZERO_HARD_GATES)
            lanes = (
                hard.get("physical_dispatch", {}).get("lanes", ())
                if isinstance(hard.get("physical_dispatch"), Mapping)
                else ()
            )
            lane_map = {
                str(item.get("lane")): item
                for item in lanes
                if isinstance(item, Mapping)
            }
            lanes_ready = (
                set(lane_map) == {"local", "edge", "cloud"}
                and all(
                    item.get("real_gate_closed") is True
                    and item.get("simulated") is False
                    and item.get("semantic_only") is False
                    for item in lane_map.values()
                )
            )
            continuity = hard.get("continuity")
            continuity_ready = (
                isinstance(continuity, Mapping)
                and continuity.get("stale_rejected") is True
                and continuity.get("poisoned_rejected") is True
                and continuity.get("conflicting_rejected") is True
                and set(continuity.get("verified_transitions", ()))
                >= {
                    "compact_restore",
                    "process_restart",
                    "handoff",
                    "requirement_revision",
                }
            )
            disable = hard.get("disable_evidence")
            disable_ready = (
                isinstance(disable, Mapping)
                and bool(disable)
                and all(
                    isinstance(item, Mapping)
                    and item.get("disabled_changed_outcome") is True
                    for item in disable.values()
                )
            )
            verifier = _load_json(verifier_path)
            transition_count = int(hard.get("valid_transition_count") or 0)
            run_ready = (
                run.get("candidate_commit") == target
                and run.get("manifest_commit") == target
                and int(run.get("failed_attempt_count") or 0) == 0
                and int(run.get("human_intervention_count") or 0) == 0
                and transition_count >= 2000
                and zero_ready
                and hard.get("production_bypass_reachable") is False
                and lanes_ready
                and continuity_ready
                and disable_ready
                and artifact_path.is_file()
                and sha256_file(artifact_path)
                == run.get("final_artifact_digest")
                and verifier_path.is_file()
                and sha256_file(verifier_path)
                == run.get("final_verifier_digest")
                and verifier.get("passed") is True
            )
            if not run_ready:
                blockers.append(
                    f"sealed_run:{run.get('run_key') or 'unknown'}"
                )
            summaries.append(
                {
                    "run_key": run.get("run_key"),
                    "run_id": run.get("run_id"),
                    "valid_transition_count": transition_count,
                    "zero_hard_gates": zero_ready,
                    "continuity_ready": continuity_ready,
                    "disable_evidence_ready": disable_ready,
                    "physical_lanes_ready": lanes_ready,
                    "final_verifier_ready": verifier.get("passed") is True,
                    "ready": run_ready,
                }
            )
        return {
            "candidate_commit": index.get("candidate_commit"),
            "manifest_commit": index.get("manifest_commit"),
            "run_count": len(runs),
            "runs": summaries,
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

    @staticmethod
    def _receipt_ready(path: Path, target: str) -> dict[str, Any]:
        value = _load_json(path)
        commands = value.get("commands")
        commands = (
            commands
            if isinstance(commands, Sequence)
            and not isinstance(commands, (str, bytes))
            else ()
        )
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
            "ready": (
                value.get("ready") is True
                and value.get("target_commit") == target
                and bool(commands)
                and all(
                    isinstance(item, Mapping)
                    and item.get("ready") is True
                    for item in commands
                )
            ),
        }

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
        custody_value = _load_json(paths["custody_report"])
        custody = {
            "ready": (
                custody_value.get("ok") is True
                and custody_value.get("target") == target_commit
                and not custody_value.get("violations")
            ),
            "role_count": len(custody_value.get("roles") or ()),
            "violations": list(custody_value.get("violations") or ()),
        }
        contract_value = _load_json(paths["contract_report"])
        contract = {
            "ready": (
                contract_value.get("valid") is True
                and contract_value.get("target_commit") == target_commit
            ),
            "valid": contract_value.get("valid"),
            "target_commit": contract_value.get("target_commit"),
        }
        checks = {
            "target_head": target["target_matches_head"],
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
                and release["pipeline_target_matches"]
                and release["ci_executed"]
                and release["ci_ready"]
                and release["archive_digest_matches"]
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
