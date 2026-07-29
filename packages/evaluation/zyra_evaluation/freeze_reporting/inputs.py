from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import (
    digest,
    file_digest,
    load_json,
    require_commit,
    require_digest,
    require_mapping,
    require_sequence,
    require_text,
    resolve_inside,
    safe_relative_path,
    verify_embedded_digest,
)
from .errors import blocker, fail, require_no_blockers


FORMAL_POINTER = Path(
    "docs/reviews/evidence/M3-S02A-02/formal-current.json"
)

FORMAL_REQUIRED_MEMBERS = {
    "100-point-evidence-index.json": "benchmark-index",
    "benchmark-report.json": "benchmark-report",
    "campaign-store-receipt.json": "benchmark-campaign-store",
    "campaign.json": "benchmark-campaign",
    "current-campaign-evidence.json": "benchmark-current-campaign-evidence",
    "evaluation-summary.json": "benchmark-summary",
    "evidence-manifest.json": "benchmark-manifest",
    "freeze-admission.json": "benchmark-admission",
    "implementation-metadata.json": "benchmark-metadata",
    "protected-deployment-evidence.json": "benchmark-protected-deployment",
    "raw-samples.json": "benchmark-raw-samples",
    "requirement-evidence.json": "benchmark-requirements",
    "run-receipts.json": "benchmark-run-receipts",
    "source-runs.json": "benchmark-source-runs",
    "statistical-evaluation.json": "benchmark-statistics",
    "validation-receipt.json": "benchmark-validation",
    "verification-summary.json": "benchmark-verification",
}

SOURCE_CUSTODY_CANDIDATES = (
    Path("docs/reviews/evidence/M3-S01A-01/source-custody-receipt.json"),
    Path("docs/reviews/evidence/M3-S01A-02/state-owner-reachability-receipt.json"),
    Path("docs/reviews/evidence/M3-S01B-02/config-migration-runtime-receipt.json"),
    Path("docs/reviews/evidence/M3-01-independent-review/aggregate-review-evidence.json"),
)

RELEASE_REQUIRED_MEMBERS = {
    Path(
        "docs/reviews/evidence/M3-S02B-02/verification-summary.json"
    ): "release-summary",
    Path(
        "docs/reviews/evidence/M3-S02B-02/implementation-metadata.json"
    ): "release-metadata",
    Path(
        "docs/reviews/evidence/M3-S02B-02/effective-code-audit.json"
    ): "release-effective-code",
}


class FreezeInputSet:
    """Admits immutable, cross-linked M3 inputs without taking their ownership."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        expected_commit: str | None = None,
    ) -> None:
        self.repository_root = Path(repository_root).resolve(strict=True)
        self.expected_commit = (
            require_commit(expected_commit, "expected repository commit")
            if expected_commit
            else ""
        )
        self._documents: dict[str, dict[str, Any]] = {}
        self._paths: dict[str, Path] = {}
        self._digests: dict[str, str] = {}

    @property
    def documents(self) -> Mapping[str, Mapping[str, Any]]:
        return dict(self._documents)

    @property
    def paths(self) -> Mapping[str, Path]:
        return dict(self._paths)

    @property
    def digests(self) -> Mapping[str, str]:
        return dict(self._digests)

    def discover(self, *, require_release: bool = True) -> dict[str, Any]:
        pointer_path = self._file(FORMAL_POINTER, "formal benchmark pointer")
        pointer = self._load("formal-pointer", pointer_path)
        self._verify_formal_pointer(pointer)
        evidence_root = self._resolve_formal_root(pointer)
        self._load_formal_members(evidence_root)
        self._verify_formal_manifest_members(evidence_root)
        self._verify_formal_cross_links(pointer)
        self._load_optional_custody()
        release_id = self._load_release(required=require_release)
        receipt = {
            "schema": "zyra.freeze-input-set/v1",
            "repository_root": ".",
            "expected_commit": self.expected_commit,
            "formal_evidence_root": evidence_root.relative_to(
                self.repository_root
            ).as_posix(),
            "release_input": release_id,
            "members": [
                {
                    "input_id": input_id,
                    "path": path.relative_to(self.repository_root).as_posix(),
                    "sha256": self._digests[input_id],
                    "schema": str(self._documents[input_id].get("schema") or ""),
                }
                for input_id, path in sorted(self._paths.items())
            ],
        }
        receipt["input_set_digest"] = digest(receipt)
        return receipt

    def document(self, input_id: str) -> dict[str, Any]:
        try:
            return dict(self._documents[input_id])
        except KeyError as error:
            raise fail(
                "freeze-input-not-loaded",
                "Requested freeze input was not admitted.",
                phase="input",
                detail={"input_id": input_id},
            ) from error

    def path(self, input_id: str) -> Path:
        try:
            return self._paths[input_id]
        except KeyError as error:
            raise fail(
                "freeze-input-path-not-loaded",
                "Requested freeze input path was not admitted.",
                phase="input",
                detail={"input_id": input_id},
            ) from error

    def relative_path(self, input_id: str) -> str:
        return self.path(input_id).relative_to(self.repository_root).as_posix()

    def member_reference(
        self,
        input_id: str,
        *,
        kind: str,
        label: str,
        selector: str = "",
        commit: str = "",
    ) -> dict[str, Any]:
        reference = {
            "reference_id": f"ref-{input_id}-{kind}".replace("_", "-"),
            "kind": kind,
            "path": self.relative_path(input_id),
            "sha256": self._digests[input_id],
            "label": label,
        }
        if selector:
            reference["selector"] = selector
        if commit:
            reference["commit"] = require_commit(commit, "reference commit")
        return reference

    def find_by_schema(self, prefix: str) -> list[str]:
        return sorted(
            input_id
            for input_id, document in self._documents.items()
            if str(document.get("schema") or "").startswith(prefix)
        )

    def _file(self, relative: Path, label: str) -> Path:
        try:
            selected = resolve_inside(
                self.repository_root,
                relative.as_posix(),
                must_exist=True,
            )
        except Exception as error:
            raise fail(
                "freeze-input-required-member-missing",
                f"{label} is missing.",
                phase="input",
                detail={"path": relative.as_posix()},
            ) from error
        if not selected.is_file():
            raise fail(
                "freeze-input-member-not-file",
                f"{label} must be a regular file.",
                phase="input",
                detail={"path": relative.as_posix()},
            )
        return selected

    def _load(self, input_id: str, path: Path) -> dict[str, Any]:
        if input_id in self._documents:
            raise fail(
                "freeze-input-duplicate-id",
                "Freeze input identifier is duplicated.",
                phase="input",
                detail={"input_id": input_id},
            )
        document = load_json(path)
        self._documents[input_id] = document
        self._paths[input_id] = path
        self._digests[input_id] = file_digest(path)
        return document

    def _load_binary(
        self,
        input_id: str,
        path: Path,
        *,
        schema: str,
        manifest_path: str,
    ) -> None:
        if input_id in self._documents:
            raise fail(
                "freeze-input-duplicate-id",
                "Freeze input identifier is duplicated.",
                phase="input",
                detail={"input_id": input_id},
            )
        self._documents[input_id] = {
            "schema": schema,
            "manifest_path": manifest_path,
        }
        self._paths[input_id] = path
        self._digests[input_id] = file_digest(path)

    def _verify_formal_pointer(self, pointer: Mapping[str, Any]) -> None:
        schema = require_text(
            pointer.get("schema"),
            "formal pointer schema",
            maximum=128,
        )
        supported_schemas = {
            "zyra.formal-evidence-pointer/v1",
            "zyra.m3-s02a02-formal-evidence-pointer/v1",
        }
        if schema not in supported_schemas:
            raise fail(
                "formal-pointer-schema-mismatch",
                "Formal benchmark pointer has an unsupported schema.",
                phase="input",
                detail={"schema": schema},
            )
        implementation = require_commit(
            pointer.get("implementation_commit"),
            "formal benchmark implementation commit",
        )
        if (
            self.expected_commit
            and implementation != self.expected_commit
            and not self._commit_is_ancestor(
                implementation,
                self.expected_commit,
            )
        ):
            raise fail(
                "formal-pointer-commit-not-ancestor",
                "Formal benchmark evidence is not an ancestor of the target commit.",
                phase="input",
                detail={
                    "formal_commit": implementation,
                    "expected_commit": self.expected_commit,
                },
            )
        projection = dict(pointer)
        declared = projection.pop("pointer_digest", "")
        observed = digest(projection)
        if require_digest(declared, "formal pointer digest") != observed:
            raise fail(
                "formal-pointer-digest-mismatch",
                "Formal benchmark pointer digest is invalid.",
                phase="integrity",
            )

    def _resolve_formal_root(self, pointer: Mapping[str, Any]) -> Path:
        relative = safe_relative_path(
            pointer.get("relative_evidence_root"),
            "formal evidence root",
        )
        selected = resolve_inside(
            self.repository_root,
            relative,
            must_exist=True,
        )
        if not selected.is_dir():
            raise fail(
                "formal-evidence-root-not-directory",
                "Formal evidence root must be a directory.",
                phase="input",
                detail={"path": relative},
            )
        return selected

    def _load_formal_members(self, root: Path) -> None:
        for filename, input_id in FORMAL_REQUIRED_MEMBERS.items():
            path = root / filename
            if not path.is_file():
                raise fail(
                    "formal-evidence-member-missing",
                    "Formal benchmark member is missing.",
                    phase="input",
                    detail={"member": filename},
                )
            self._load(input_id, path)

    def _verify_formal_manifest_members(self, root: Path) -> None:
        manifest = self.document("benchmark-manifest")
        members = [
            require_mapping(item, "formal manifest member")
            for item in require_sequence(
                manifest.get("members"),
                "formal manifest members",
            )
        ]
        findings: list[dict[str, Any]] = []
        if manifest.get("member_count") != len(members):
            findings.append(
                blocker(
                    "formal-manifest-member-count-mismatch",
                    "Formal evidence manifest member count is inconsistent.",
                    declared=manifest.get("member_count"),
                    observed=len(members),
                )
            )
        known_by_path = {
            path.relative_to(root).as_posix(): input_id
            for input_id, path in self._paths.items()
            if path == root or root in path.parents
        }
        identities: set[str] = set()
        source_archive_index = 0
        for member in members:
            relative = safe_relative_path(
                member.get("path"),
                "formal manifest member path",
            )
            if relative in identities:
                findings.append(
                    blocker(
                        "formal-manifest-member-duplicate",
                        "Formal evidence manifest path is duplicated.",
                        path=relative,
                    )
                )
                continue
            identities.add(relative)
            try:
                path = resolve_inside(root, relative, must_exist=True)
            except Exception:
                findings.append(
                    blocker(
                        "formal-manifest-member-missing",
                        "Formal evidence manifest member is missing.",
                        path=relative,
                    )
                )
                continue
            if not path.is_file() or path.is_symlink():
                findings.append(
                    blocker(
                        "formal-manifest-member-not-regular-file",
                        "Formal evidence member must be a regular non-symlink file.",
                        path=relative,
                    )
                )
                continue
            observed_digest = file_digest(path)
            observed_bytes = path.stat().st_size
            if member.get("sha256") != observed_digest:
                findings.append(
                    blocker(
                        "formal-manifest-member-digest-mismatch",
                        "Formal evidence member checksum does not match its manifest.",
                        path=relative,
                    )
                )
            if member.get("bytes") != observed_bytes:
                findings.append(
                    blocker(
                        "formal-manifest-member-size-mismatch",
                        "Formal evidence member size does not match its manifest.",
                        path=relative,
                    )
                )
            if member.get("required") is not True:
                findings.append(
                    blocker(
                        "formal-manifest-member-not-required",
                        "Formal evidence manifest cannot downgrade a member to optional.",
                        path=relative,
                    )
                )
            if relative.startswith("source-archives/"):
                source_archive_index += 1
                self._load_binary(
                    f"benchmark-source-archive-{source_archive_index:02d}",
                    path,
                    schema="application/zip",
                    manifest_path=relative,
                )
            elif relative not in known_by_path:
                findings.append(
                    blocker(
                        "formal-manifest-json-not-admitted",
                        "Formal manifest JSON member is not part of the admitted input set.",
                        path=relative,
                    )
                )
        require_no_blockers(
            findings,
            code="formal-manifest-members-invalid",
            message="Formal benchmark manifest members failed admission.",
            phase="input",
        )

    def _verify_formal_cross_links(self, pointer: Mapping[str, Any]) -> None:
        report = self.document("benchmark-report")
        index = self.document("benchmark-index")
        manifest = self.document("benchmark-manifest")
        summary = self.document("benchmark-verification")
        admission = self.document("benchmark-admission")
        findings: list[dict[str, Any]] = []
        report_digest = verify_embedded_digest(report, "report_digest")
        index_digest = verify_embedded_digest(index, "index_digest")
        manifest_digest = verify_embedded_digest(manifest, "manifest_digest")
        expected_links = (
            ("pointer.report_digest", pointer.get("report_digest"), report_digest),
            (
                "pointer.evidence_index_digest",
                pointer.get("evidence_index_digest"),
                index_digest,
            ),
            (
                "pointer.manifest_digest",
                pointer.get("manifest_digest"),
                manifest_digest,
            ),
            ("index.report_digest", index.get("report_digest"), report_digest),
            ("summary.report_digest", summary.get("report_digest"), report_digest),
            (
                "summary.evidence_index_digest",
                summary.get("evidence_index_digest"),
                index_digest,
            ),
            (
                "summary.manifest_digest",
                summary.get("manifest_digest"),
                manifest_digest,
            ),
            ("admission.report_digest", admission.get("report_digest"), report_digest),
            (
                "admission.evidence_index_digest",
                admission.get("evidence_index_digest"),
                index_digest,
            ),
            (
                "admission.manifest_digest",
                admission.get("manifest_digest"),
                manifest_digest,
            ),
        )
        for label, declared, observed in expected_links:
            if declared != observed:
                findings.append(
                    blocker(
                        "formal-cross-link-mismatch",
                        "Formal evidence cross-link does not match.",
                        label=label,
                        declared=declared,
                        observed=observed,
                    )
                )
        commit = require_commit(report.get("commit_sha"), "benchmark report commit")
        for label, value in (
            ("index", index.get("commit_sha")),
            ("pointer", pointer.get("implementation_commit")),
            ("summary", summary.get("target_commit")),
            ("admission", admission.get("target_commit")),
        ):
            if value != commit:
                findings.append(
                    blocker(
                        "formal-commit-cross-link-mismatch",
                        "Formal evidence commit identities disagree.",
                        member=label,
                        expected=commit,
                        observed=value,
                    )
                )
        score = require_mapping(report.get("score"), "benchmark report score")
        if score.get("complete") is not True or score.get("verified") != 100:
            findings.append(
                blocker(
                    "formal-score-incomplete",
                    "Formal benchmark does not close its source score matrix.",
                )
            )
        if summary.get("verdict") != "PASS":
            findings.append(
                blocker(
                    "formal-verification-not-pass",
                    "Formal benchmark verification is not PASS.",
                    verdict=summary.get("verdict"),
                )
            )
        require_no_blockers(
            findings,
            code="formal-evidence-cross-links-invalid",
            message="Formal benchmark evidence failed cross-link admission.",
            phase="input",
        )

    def _load_optional_custody(self) -> None:
        for index, relative in enumerate(SOURCE_CUSTODY_CANDIDATES, 1):
            path = self.repository_root / relative
            if path.is_file():
                self._load(f"custody-{index:02d}", path)

    def _load_release(self, *, required: bool) -> str:
        missing = [
            relative.as_posix()
            for relative in RELEASE_REQUIRED_MEMBERS
            if not (self.repository_root / relative).is_file()
        ]
        if missing and required:
            raise fail(
                "release-input-missing",
                "Reviewed M3-S02B release evidence is required.",
                phase="input",
                detail={"missing": missing},
            )
        if missing:
            return ""
        for relative, input_id in RELEASE_REQUIRED_MEMBERS.items():
            self._load(input_id, self.repository_root / relative)
        self._verify_release(
            self.document("release-summary"),
            self.document("release-metadata"),
            self.document("release-effective-code"),
        )
        return "release-summary"

    def _verify_release(
        self,
        summary: Mapping[str, Any],
        metadata: Mapping[str, Any],
        effective_code: Mapping[str, Any],
    ) -> None:
        findings: list[dict[str, Any]] = []
        if summary.get("schema") != "zyra.m3-s02b-02.verification-summary/v1":
            findings.append(
                blocker(
                    "release-summary-schema-mismatch",
                    "M3-S02B-02 release summary schema is unsupported.",
                    schema=summary.get("schema"),
                )
            )
        if metadata.get("schema") != (
            "zyra.m3-s02b-02.implementation-metadata/v1"
        ):
            findings.append(
                blocker(
                    "release-metadata-schema-mismatch",
                    "M3-S02B-02 implementation metadata schema is unsupported.",
                    schema=metadata.get("schema"),
                )
            )
        if effective_code.get("schema") != (
            "zyra.effective-code-language-gate-audit/v1"
        ):
            findings.append(
                blocker(
                    "release-effective-code-schema-mismatch",
                    "M3-S02B-02 effective-code audit schema is unsupported.",
                    schema=effective_code.get("schema"),
                )
            )
        if (
            summary.get("slice_id") != "M3-S02B-02"
            or metadata.get("slice_id") != "M3-S02B-02"
        ):
            findings.append(
                blocker(
                    "release-slice-identity-mismatch",
                    "Release inputs do not identify M3-S02B-02.",
                )
            )
        target = require_commit(
            summary.get("target_commit"),
            "M3-S02B-02 target commit",
        )
        metadata_target = require_commit(
            metadata.get("implementation_commit"),
            "M3-S02B-02 implementation commit",
        )
        if target != metadata_target:
            findings.append(
                blocker(
                    "release-target-cross-link-mismatch",
                    "Release summary and implementation metadata disagree.",
                    summary_target=target,
                    metadata_target=metadata_target,
                )
            )
        if self.expected_commit and not self._commit_is_ancestor(
            target,
            self.expected_commit,
        ):
            findings.append(
                blocker(
                    "release-target-not-ancestor",
                    "Reviewed release target is not an ancestor of the report target.",
                    release_target=target,
                    report_target=self.expected_commit,
                )
            )
        pipeline = require_mapping(
            summary.get("release_pipeline"),
            "M3-S02B-02 release pipeline",
        )
        benchmark = require_mapping(
            summary.get("benchmark_binding"),
            "M3-S02B-02 benchmark binding",
        )
        effective = require_mapping(
            summary.get("effective_code"),
            "M3-S02B-02 effective-code summary",
        )
        if summary.get("verdict") != "PASS":
            findings.append(
                blocker(
                    "release-verdict-not-pass",
                    "M3-S02B-02 reviewed verdict is not PASS.",
                    verdict=summary.get("verdict"),
                )
            )
        if (
            pipeline.get("ready") is not True
            or int(pipeline.get("failed_gate_count") or 0) != 0
            or int(pipeline.get("blocked_gate_count") or 0) != 0
            or pipeline.get("required_failures")
        ):
            findings.append(
                blocker(
                    "release-pipeline-not-ready",
                    "M3-S02B-02 release pipeline is not fully admitted.",
                )
            )
        if (
            benchmark.get("ready") is not True
            or benchmark.get("score") != 100
            or benchmark.get("human_intervention_count") != 0
        ):
            findings.append(
                blocker(
                    "release-benchmark-binding-invalid",
                    "Release evidence is not bound to the formal zero-human score.",
                )
            )
        if (
            summary.get("execution_state_update_authorized") is not True
            or effective.get("accounted_effective_production", 0)
            < effective.get("minimum", 1)
            or not isinstance(effective_code.get("gate"), Mapping)
            or effective_code.get("gate", {}).get("ok") is not True
            or effective_code.get("gate", {}).get("blockers")
        ):
            findings.append(
                blocker(
                    "release-completion-gate-invalid",
                    "M3-S02B-02 completion/effective-code gate is not closed.",
                )
            )
        require_no_blockers(
            findings,
            code="release-input-not-admitted",
            message="Reviewed M3-S02B-02 release evidence is not admitted.",
            phase="input",
        )

    def _commit_is_ancestor(self, ancestor: str, descendant: str) -> bool:
        import subprocess

        completed = subprocess.run(
            [
                "git",
                "-C",
                str(self.repository_root),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        return completed.returncode == 0


def load_freeze_input_set(
    repository_root: str | Path,
    *,
    expected_commit: str | None = None,
    require_release: bool = True,
) -> tuple[FreezeInputSet, dict[str, Any]]:
    selected = FreezeInputSet(
        repository_root,
        expected_commit=expected_commit,
    )
    receipt = selected.discover(require_release=require_release)
    return selected, receipt
