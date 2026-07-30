from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bundle import ReleaseManifest
from .errors import GateFailure, IntegrityViolation
from .integrity import normalize_relative_path, sha256_file, stable_digest
from .policy import DEFAULT_RELEASE_POLICY, ReleasePolicy


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    evidence_id: str
    kind: str
    path: str
    sha256: str
    source_commit: str
    dependencies: tuple[str, ...] = ()
    required: bool = True

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]+", self.evidence_id):
            raise GateFailure(
                "Submission evidence id is invalid.",
                code="submission_evidence_id_invalid",
                details={"evidence_id": self.evidence_id},
            )
        normalize_relative_path(self.path)
        if not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            raise GateFailure(
                "Submission evidence SHA256 is invalid.",
                code="submission_evidence_digest_invalid",
                details={"evidence_id": self.evidence_id},
            )
        if not re.fullmatch(r"[a-f0-9]{40}", self.source_commit):
            raise GateFailure(
                "Submission evidence commit is invalid.",
                code="submission_evidence_commit_invalid",
                details={"evidence_id": self.evidence_id},
            )
        if self.evidence_id in self.dependencies:
            raise GateFailure(
                "Submission evidence cannot depend on itself.",
                code="submission_evidence_self_dependency",
                details={"evidence_id": self.evidence_id},
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "source_commit": self.source_commit,
            "dependencies": list(self.dependencies),
            "required": self.required,
        }


class EvidenceIndex:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._references: dict[str, EvidenceReference] = {}

    def add(self, reference: EvidenceReference) -> None:
        reference.validate()
        if reference.evidence_id in self._references:
            raise GateFailure(
                "Submission evidence id is duplicated.",
                code="submission_evidence_duplicate",
                details={"evidence_id": reference.evidence_id},
            )
        path = self.root.joinpath(
            *normalize_relative_path(reference.path).split("/")
        ).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise GateFailure(
                "Submission evidence path escapes root.",
                code="submission_evidence_path_escape",
                details={"path": reference.path},
            ) from error
        if not path.is_file():
            raise GateFailure(
                "Submission evidence file is missing.",
                code="submission_evidence_file_missing",
                details={
                    "evidence_id": reference.evidence_id,
                    "path": reference.path,
                },
            )
        actual = sha256_file(path)
        if actual != reference.sha256:
            raise GateFailure(
                "Submission evidence checksum does not match.",
                code="submission_evidence_checksum_mismatch",
                details={
                    "evidence_id": reference.evidence_id,
                    "expected": reference.sha256,
                    "actual": actual,
                },
            )
        self._references[reference.evidence_id] = reference

    def validate(
        self,
        *,
        release_commit: str,
        allowed_historical_commits: Iterable[str] = (),
    ) -> dict[str, Any]:
        allowed = {release_commit, *allowed_historical_commits}
        missing_dependencies: list[dict[str, Any]] = []
        stale: list[dict[str, str]] = []
        for reference in self._references.values():
            missing = [
                dependency
                for dependency in reference.dependencies
                if dependency not in self._references
            ]
            if missing:
                missing_dependencies.append(
                    {
                        "evidence_id": reference.evidence_id,
                        "missing": missing,
                    }
                )
            if reference.source_commit not in allowed:
                stale.append(
                    {
                        "evidence_id": reference.evidence_id,
                        "source_commit": reference.source_commit,
                    }
                )
        order = self._topological_order()
        required = [
            reference.evidence_id
            for reference in self._references.values()
            if reference.required
        ]
        ready = not missing_dependencies and not stale
        report = {
            "schema": "zyra.submission-evidence-index/v1",
            "ready": ready,
            "release_commit": release_commit,
            "allowed_historical_commits": sorted(allowed - {release_commit}),
            "evidence_count": len(self._references),
            "required_count": len(required),
            "missing_dependencies": missing_dependencies,
            "stale": stale,
            "order": list(order),
            "references": [
                self._references[evidence_id].to_dict()
                for evidence_id in order
            ],
        }
        report["digest"] = stable_digest(report)
        if not ready:
            raise GateFailure(
                "Submission evidence index is not admissible.",
                code="submission_evidence_index_failed",
                details=report,
            )
        return report

    def _topological_order(self) -> tuple[str, ...]:
        indegree = {identifier: 0 for identifier in self._references}
        outgoing: dict[str, set[str]] = defaultdict(set)
        for reference in self._references.values():
            for dependency in reference.dependencies:
                if dependency not in self._references:
                    continue
                if reference.evidence_id not in outgoing[dependency]:
                    outgoing[dependency].add(reference.evidence_id)
                    indegree[reference.evidence_id] += 1
        queue = deque(
            sorted(
                identifier
                for identifier, degree in indegree.items()
                if degree == 0
            )
        )
        output: list[str] = []
        while queue:
            current = queue.popleft()
            output.append(current)
            for target in sorted(outgoing[current]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if len(output) != len(self._references):
            cycle = sorted(
                identifier
                for identifier, degree in indegree.items()
                if degree > 0
            )
            raise GateFailure(
                "Submission evidence graph contains a cycle.",
                code="submission_evidence_cycle",
                details={"evidence_ids": cycle},
            )
        return tuple(output)


class ReproducibilityVerifier:
    def compare(
        self,
        first: Path,
        second: Path,
        *,
        expected_commit: str,
    ) -> dict[str, Any]:
        if not first.is_file() or not second.is_file():
            raise IntegrityViolation(
                "Reproducibility comparison requires two archives.",
                code="reproducibility_archive_missing",
                details={
                    "first": str(first),
                    "second": str(second),
                },
            )
        first_digest = sha256_file(first)
        second_digest = sha256_file(second)
        byte_identical = first_digest == second_digest
        first_size = first.stat().st_size
        second_size = second.stat().st_size
        report = {
            "schema": "zyra.release-reproducibility/v1",
            "ready": byte_identical,
            "source_commit": expected_commit,
            "first": {
                "path": str(first.resolve()),
                "sha256": first_digest,
                "size": first_size,
            },
            "second": {
                "path": str(second.resolve()),
                "sha256": second_digest,
                "size": second_size,
            },
            "byte_identical": byte_identical,
            "size_delta": second_size - first_size,
        }
        report["digest"] = stable_digest(report)
        if not byte_identical:
            raise IntegrityViolation(
                "Release builds are not byte reproducible.",
                code="release_not_reproducible",
                details=report,
            )
        return report


class CleanInstallReceiptVerifier:
    def verify(
        self,
        value: Mapping[str, Any],
        *,
        expected_commit: str,
        require_product_lifecycle: bool = True,
    ) -> dict[str, Any]:
        if value.get("schema") != "zyra.clean-install-receipt/v1":
            raise GateFailure(
                "Clean-install receipt schema is unsupported.",
                code="clean_install_receipt_schema",
            )
        failures: list[str] = []
        if value.get("ready") is not True:
            failures.append("receipt_not_ready")
        if value.get("source_commit") != expected_commit:
            failures.append("source_commit_mismatch")
        if value.get("workspace_isolated") is not True:
            failures.append("workspace_not_isolated")
        if value.get("parent_source_repositories_present") is not False:
            failures.append("parent_source_repository_present")
        if require_product_lifecycle and value.get("product_lifecycle_exercised") is not True:
            failures.append("product_lifecycle_not_exercised")
        commands = value.get("commands")
        if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes)):
            failures.append("commands_invalid")
            commands = ()
        command_failures = [
            index
            for index, command in enumerate(commands)
            if not isinstance(command, Mapping) or command.get("ready") is not True
        ]
        if command_failures:
            failures.append("command_failed")
        receipts = value.get("receipts")
        if not isinstance(receipts, Mapping):
            failures.append("nested_receipts_invalid")
            receipts = {}
        install = receipts.get("install")
        uninstall = receipts.get("uninstall")
        if not isinstance(install, Mapping) or install.get("state") != "committed":
            failures.append("install_not_committed")
        if not isinstance(uninstall, Mapping) or uninstall.get("state") != "uninstalled":
            failures.append("uninstall_not_completed")
        if require_product_lifecycle:
            lifecycle = receipts.get("lifecycle")
            if not isinstance(lifecycle, Mapping) or lifecycle.get("ready") is not True:
                failures.append("lifecycle_not_ready")
        isolation_audit = value.get("isolation_audit")
        if (
            not isinstance(isolation_audit, Mapping)
            or isolation_audit.get("ready") is not True
        ):
            failures.append("isolation_audit_not_ready")
        else:
            zero_count_fields = (
                "implicit_cache_or_user_state_dependency_count",
                "editable_or_link_install_count",
                "external_build_context_count",
                "undeclared_process_count",
                "undeclared_port_count",
            )
            if any(
                int(isolation_audit.get(field) or 0) != 0
                for field in zero_count_fields
            ):
                failures.append("isolation_audit_finding_present")
            declared_ports = sorted(
                int(item)
                for item in isolation_audit.get("declared_ports", ())
            )
            released_ports = sorted(
                int(item)
                for item in isolation_audit.get("released_ports", ())
            )
            if not declared_ports or declared_ports != released_ports:
                failures.append("cleanroom_port_not_released")
            required_actions = {
                "release-doctor",
                "product-start",
                "semantic-health",
                "product-restart",
                "post-restart-health",
                "product-stop",
                "post-stop-status",
            }
            actions = {
                str(item)
                for item in isolation_audit.get(
                    "declared_process_actions",
                    (),
                )
            }
            if not required_actions.issubset(actions):
                failures.append("cleanroom_lifecycle_action_missing")
        report = {
            "schema": "zyra.clean-install-admission/v1",
            "ready": not failures,
            "source_commit": expected_commit,
            "failures": failures,
            "command_failures": command_failures,
            "isolation_audit_digest": (
                stable_digest(isolation_audit)
                if isinstance(isolation_audit, Mapping)
                else ""
            ),
            "receipt_digest": stable_digest(value),
        }
        report["digest"] = stable_digest(report)
        if failures:
            raise GateFailure(
                "Clean-install receipt failed admission.",
                code="clean_install_receipt_failed",
                details=report,
            )
        return report


class SubmissionAssembler:
    def __init__(
        self,
        project_root: Path,
        output_root: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.project_root = project_root.resolve()
        self.output_root = output_root.resolve()
        self.policy = policy

    def assemble(
        self,
        *,
        release_manifest: Path,
        release_archive: Path,
        ci_report: Path,
        admission: Path,
        clean_install: Path,
        benchmark_link: Path,
        source_commit: str,
        historical_benchmark_commit: str,
    ) -> dict[str, Any]:
        manifest = ReleaseManifest.load(release_manifest)
        if manifest.source_commit != source_commit:
            raise GateFailure(
                "Submission release manifest targets a different commit.",
                code="submission_manifest_commit_mismatch",
                details={
                    "expected": source_commit,
                    "actual": manifest.source_commit,
                },
            )
        required_files = {
            "release-archive": release_archive,
            "release-manifest": release_manifest,
            "ci-report": ci_report,
            "release-admission": admission,
            "clean-install": clean_install,
            "benchmark-link": benchmark_link,
        }
        for identifier, path in required_files.items():
            if not path.is_file():
                raise GateFailure(
                    "Submission input is missing.",
                    code="submission_input_missing",
                    details={"input": identifier, "path": str(path)},
                )
        clean_value = self._load_mapping(clean_install)
        clean_admission = CleanInstallReceiptVerifier().verify(
            clean_value,
            expected_commit=source_commit,
            require_product_lifecycle=True,
        )
        ci_value = self._load_mapping(ci_report)
        admission_value = self._load_mapping(admission)
        if ci_value.get("ready") is not True:
            raise GateFailure(
                "Submission CI report is not ready.",
                code="submission_ci_not_ready",
            )
        if admission_value.get("ready") is not True:
            raise GateFailure(
                "Submission admission verdict is not ready.",
                code="submission_admission_not_ready",
            )
        index = EvidenceIndex(self.output_root)
        copies: dict[str, Path] = {}
        for identifier, source in required_files.items():
            destination = self.output_root / source.name
            if source.resolve() != destination.resolve():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            copies[identifier] = destination
        index.add(
            EvidenceReference(
                evidence_id="release-archive",
                kind="release_bundle",
                path=copies["release-archive"].name,
                sha256=sha256_file(copies["release-archive"]),
                source_commit=source_commit,
            )
        )
        index.add(
            EvidenceReference(
                evidence_id="release-manifest",
                kind="release_manifest",
                path=copies["release-manifest"].name,
                sha256=sha256_file(copies["release-manifest"]),
                source_commit=source_commit,
                dependencies=("release-archive",),
            )
        )
        index.add(
            EvidenceReference(
                evidence_id="ci-report",
                kind="ci_report",
                path=copies["ci-report"].name,
                sha256=sha256_file(copies["ci-report"]),
                source_commit=source_commit,
                dependencies=("release-manifest",),
            )
        )
        index.add(
            EvidenceReference(
                evidence_id="release-admission",
                kind="release_admission",
                path=copies["release-admission"].name,
                sha256=sha256_file(copies["release-admission"]),
                source_commit=source_commit,
                dependencies=("ci-report",),
            )
        )
        index.add(
            EvidenceReference(
                evidence_id="clean-install",
                kind="clean_install",
                path=copies["clean-install"].name,
                sha256=sha256_file(copies["clean-install"]),
                source_commit=source_commit,
                dependencies=("release-archive",),
            )
        )
        index.add(
            EvidenceReference(
                evidence_id="benchmark-link",
                kind="formal_benchmark",
                path=copies["benchmark-link"].name,
                sha256=sha256_file(copies["benchmark-link"]),
                source_commit=historical_benchmark_commit,
                dependencies=(),
            )
        )
        evidence = index.validate(
            release_commit=source_commit,
            allowed_historical_commits=(historical_benchmark_commit,),
        )
        result = {
            "schema": "zyra.submission-bundle-index/v1",
            "ready": True,
            "source_commit": source_commit,
            "release_id": manifest.release_id,
            "release_archive_sha256": sha256_file(copies["release-archive"]),
            "manifest_digest": manifest.digest,
            "clean_install_admission": clean_admission,
            "ci_report_digest": stable_digest(ci_value),
            "release_admission_digest": stable_digest(admission_value),
            "evidence": evidence,
        }
        result["digest"] = stable_digest(result)
        output = self.output_root / "submission-index.json"
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        result["path"] = str(output)
        result["sha256"] = sha256_file(output)
        return result

    @staticmethod
    def _load_mapping(path: Path) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise GateFailure(
                "Submission JSON input cannot be parsed.",
                code="submission_json_invalid",
                details={"path": str(path), "error": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise GateFailure(
                "Submission JSON root must be an object.",
                code="submission_json_root",
                details={"path": str(path)},
            )
        return value


__all__ = [
    "CleanInstallReceiptVerifier",
    "EvidenceIndex",
    "EvidenceReference",
    "ReproducibilityVerifier",
    "SubmissionAssembler",
]
