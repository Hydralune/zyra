from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .ablation import AblationMaterialBuilder
from .algorithms import AlgorithmMaterialBuilder
from .archive import EvidenceArchiveBuilder, EvidenceArchiveVerifier
from .canonical import (
    canonical_text,
    digest,
    file_digest,
    load_json,
    now,
    require_commit,
    require_digest,
    require_mapping,
    require_sequence,
    safe_relative_path,
    write_json,
)
from .cases import CaseStudyBuilder
from .compatibility import CompatibilityMaterialBuilder
from .errors import blocker, fail, require_no_blockers
from .evidence_index import FreezeEvidenceIndexBuilder
from .inputs import FreezeInputSet, load_freeze_input_set
from .langgraph import LangGraphCorrectionMatrixBuilder
from .ledger import InternalizationLedgerBuilder, InternalizationLedgerVerifier
from .replay import ProjectionReplayVerifier
from .report import FreezeReportBuilder
from .scoring import ScoreMatrixVerifier
from .value import ApplicationValueBuilder


GENERATED_MEMBERS = {
    "input-set.json": "input_set",
    "100-point-evidence-index.json": "evidence_index",
    "internalization-ledger.json": "internalization_ledger",
    "algorithm-material.json": "algorithm_material",
    "case-studies.json": "case_studies",
    "ablation-material.json": "ablation_material",
    "compatibility-material.json": "compatibility_material",
    "application-value.json": "application_value",
    "langgraph-correction-matrix.json": "langgraph_correction",
    "freeze-report.json": "freeze_report",
}

SIDE_CAR_MEMBERS = (
    "archive-verification.json",
    "replay-verification.json",
    "generation-receipt.json",
)

ARCHIVE_NAME = "first-stage-evidence.zip"


class FreezeEvidencePipeline:
    """Generates a fail-closed report, evidence index, and immutable archive."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        target_commit: str,
        require_release: bool = True,
    ) -> None:
        self.repository_root = Path(repository_root).resolve(strict=True)
        self.target_commit = require_commit(target_commit, "pipeline target commit")
        self.require_release = bool(require_release)

    def build(self, output_directory: str | Path) -> dict[str, Any]:
        output = self._output_path(output_directory)
        if output.exists():
            raise fail(
                "freeze-output-exists",
                "Freeze evidence output directory already exists.",
                phase="pipeline",
                detail={"path": str(output)},
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{output.name}.",
                dir=output.parent,
            )
        )
        try:
            generated = staging / "generated"
            generated.mkdir()
            inputs, input_receipt = load_freeze_input_set(
                self.repository_root,
                expected_commit=self.target_commit,
                require_release=self.require_release,
            )
            materials = self._materials(inputs, input_receipt)
            generated_files = self._write_generated(generated, materials)
            report_markdown = FreezeReportBuilder().markdown(
                materials["freeze_report"]
            )
            report_path = staging / "freeze-report.md"
            report_path.write_text(report_markdown, encoding="utf-8", newline="\n")
            archive_build = self._archive(
                staging=staging,
                generated_files=generated_files,
                report_path=report_path,
                inputs=inputs,
            )
            archive_path = staging / ARCHIVE_NAME
            archive_verification = require_mapping(
                archive_build.get("verification_receipt"),
                "archive verification receipt",
            )
            replay_verification = ProjectionReplayVerifier().verify_archive(
                archive_path,
                expected_commit=self.target_commit,
            )
            archive_build = dict(archive_build)
            archive_build["path"] = ARCHIVE_NAME
            write_json(
                staging / "archive-verification.json",
                archive_verification,
            )
            write_json(
                staging / "replay-verification.json",
                replay_verification,
            )
            receipt = self._generation_receipt(
                input_receipt=input_receipt,
                materials=materials,
                generated_files=generated_files,
                report_path=report_path,
                archive_path=archive_path,
                archive_build=archive_build,
                archive_verification=archive_verification,
                replay_verification=replay_verification,
            )
            write_json(staging / "generation-receipt.json", receipt)
            self._verify_staging(staging, receipt)
            os.replace(staging, output)
            staging = Path()
        except Exception:
            self._remove_staging(staging, output.parent)
            raise
        return verify_freeze_output(
            output,
            expected_commit=self.target_commit,
        )

    def _materials(
        self,
        inputs: FreezeInputSet,
        input_receipt: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        evidence_index = FreezeEvidenceIndexBuilder(
            inputs,
            target_commit=self.target_commit,
        ).build()
        ledger = InternalizationLedgerBuilder(inputs).build()
        algorithms = AlgorithmMaterialBuilder(self.repository_root).build()
        cases = CaseStudyBuilder(inputs).build()
        ablation = AblationMaterialBuilder(inputs).build()
        compatibility = CompatibilityMaterialBuilder(inputs).build()
        application_value = ApplicationValueBuilder(inputs).build()
        langgraph_correction = LangGraphCorrectionMatrixBuilder(
            self.repository_root
        ).build()
        report = FreezeReportBuilder().build(
            target_commit=self.target_commit,
            input_receipt=input_receipt,
            evidence_index=evidence_index,
            ledger=ledger,
            algorithms=algorithms,
            cases=cases,
            ablation=ablation,
            compatibility=compatibility,
            application_value=application_value,
            langgraph_correction=langgraph_correction,
        )
        return {
            "input_set": dict(input_receipt),
            "evidence_index": evidence_index,
            "internalization_ledger": ledger,
            "algorithm_material": algorithms,
            "case_studies": cases,
            "ablation_material": ablation,
            "compatibility_material": compatibility,
            "application_value": application_value,
            "langgraph_correction": langgraph_correction,
            "freeze_report": report,
        }

    @staticmethod
    def _write_generated(
        generated: Path,
        materials: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Path]:
        paths = {}
        for filename, material_id in GENERATED_MEMBERS.items():
            path = generated / filename
            write_json(path, materials[material_id])
            paths[filename] = path
        return paths

    def _archive(
        self,
        *,
        staging: Path,
        generated_files: Mapping[str, Path],
        report_path: Path,
        inputs: FreezeInputSet,
    ) -> dict[str, Any]:
        builder = EvidenceArchiveBuilder(
            self.repository_root,
            target_commit=self.target_commit,
        )
        for filename, path in sorted(generated_files.items()):
            builder.add_file(
                path,
                archive_path=f"generated/{filename}",
                kind="generated-evidence",
            )
        builder.add_file(
            report_path,
            archive_path="freeze-report.md",
            kind="generated-report",
        )
        for input_id, path in sorted(inputs.paths.items()):
            category = (
                "benchmark"
                if input_id.startswith("benchmark-") or input_id == "formal-pointer"
                else "release"
                if input_id.startswith("release-")
                else "custody"
            )
            filename = (
                "formal-current.json"
                if input_id == "formal-pointer"
                else path.name
            )
            builder.add_file(
                path,
                archive_path=f"inputs/{category}/{filename}",
                kind=f"admitted-{category}-input",
            )
        return builder.build(staging / ARCHIVE_NAME)

    def _generation_receipt(
        self,
        *,
        input_receipt: Mapping[str, Any],
        materials: Mapping[str, Mapping[str, Any]],
        generated_files: Mapping[str, Path],
        report_path: Path,
        archive_path: Path,
        archive_build: Mapping[str, Any],
        archive_verification: Mapping[str, Any],
        replay_verification: Mapping[str, Any],
    ) -> dict[str, Any]:
        generated = [
            {
                "path": f"generated/{filename}",
                "sha256": file_digest(path),
                "bytes": path.stat().st_size,
                "object_digest": self._object_digest(materials[material_id]),
            }
            for filename, material_id in sorted(GENERATED_MEMBERS.items())
            for path in [generated_files[filename]]
        ]
        generated.append(
            {
                "path": "freeze-report.md",
                "sha256": file_digest(report_path),
                "bytes": report_path.stat().st_size,
                "object_digest": "",
            }
        )
        receipt = {
            "schema": "zyra.m3-s03-01-generation-receipt/v1",
            "target_commit": self.target_commit,
            "slice_id": "M3-S03-01",
            "final_freeze_claimed": False,
            "next_freeze_owner": "M3-S03-02",
            "input_set_digest": input_receipt.get("input_set_digest"),
            "release_input": input_receipt.get("release_input"),
            "release_required": self.require_release,
            "score": materials["evidence_index"].get("score"),
            "generated": generated,
            "archive": {
                "path": ARCHIVE_NAME,
                "sha256": file_digest(archive_path),
                "bytes": archive_path.stat().st_size,
                "manifest_digest": archive_build.get("manifest_digest"),
                "chain_root": archive_build.get("chain_root"),
                "member_count": archive_build.get("member_count"),
            },
            "archive_verification_digest": archive_verification.get(
                "receipt_digest"
            ),
            "replay_verification_digest": replay_verification.get(
                "receipt_digest"
            ),
            "task_success_recomputed": False,
            "generated_at": now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _verify_staging(
        self,
        staging: Path,
        receipt: Mapping[str, Any],
    ) -> None:
        verification = FreezeOutputVerifier(
            staging,
            expected_commit=self.target_commit,
        ).verify(receipt=receipt)
        if verification.get("valid") is not True:
            raise fail(
                "freeze-staging-verification-failed",
                "Freeze staging output did not verify.",
                phase="pipeline",
            )

    def _output_path(self, value: str | Path) -> Path:
        selected = Path(value)
        if not selected.is_absolute():
            selected = self.repository_root / selected
        selected = selected.resolve(strict=False)
        try:
            selected.relative_to(self.repository_root)
        except ValueError as error:
            raise fail(
                "freeze-output-outside-repository",
                "Freeze output must remain inside the Zyra repository.",
                phase="pipeline",
                detail={"path": str(selected)},
            ) from error
        if selected == self.repository_root:
            raise fail(
                "freeze-output-root-forbidden",
                "Repository root cannot be used as a freeze output directory.",
                phase="pipeline",
            )
        return selected

    @staticmethod
    def _object_digest(value: Mapping[str, Any]) -> str:
        for field in (
            "report_digest",
            "matrix_digest",
            "material_digest",
            "ledger_digest",
            "index_digest",
            "input_set_digest",
        ):
            if value.get(field):
                return require_digest(value[field], f"{field} value")
        return digest(value)

    @staticmethod
    def _remove_staging(staging: Path, parent: Path) -> None:
        if not staging or str(staging) in {"", "."}:
            return
        try:
            resolved = staging.resolve(strict=False)
            resolved.relative_to(parent.resolve(strict=True))
        except (OSError, ValueError):
            return
        if resolved.name.startswith(".") and resolved.is_dir():
            shutil.rmtree(resolved)


class FreezeOutputVerifier:
    """Verifies a materialized M3-S03-01 directory without trusting its receipt."""

    def __init__(
        self,
        output_directory: str | Path,
        *,
        expected_commit: str | None = None,
    ) -> None:
        self.output = Path(output_directory).resolve(strict=True)
        self.expected_commit = (
            require_commit(expected_commit, "expected output commit")
            if expected_commit
            else ""
        )

    def verify(
        self,
        *,
        receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = (
            dict(receipt)
            if receipt is not None
            else load_json(self.output / "generation-receipt.json")
        )
        findings: list[dict[str, Any]] = []
        if selected.get("schema") != "zyra.m3-s03-01-generation-receipt/v1":
            findings.append(
                blocker(
                    "freeze-output-receipt-schema-invalid",
                    "Generation receipt schema is unsupported.",
                    schema=selected.get("schema"),
                )
            )
        declared_receipt = selected.get("receipt_digest")
        projection = dict(selected)
        projection.pop("receipt_digest", None)
        if declared_receipt != digest(projection):
            findings.append(
                blocker(
                    "freeze-output-receipt-digest-invalid",
                    "Generation receipt digest does not match.",
                )
            )
        target_commit = str(selected.get("target_commit") or "")
        if self.expected_commit and target_commit != self.expected_commit:
            findings.append(
                blocker(
                    "freeze-output-target-commit-mismatch",
                    "Generation receipt targets another commit.",
                    expected=self.expected_commit,
                    observed=target_commit,
                )
            )
        if selected.get("final_freeze_claimed") is not False:
            findings.append(
                blocker(
                    "freeze-output-premature-freeze-claim",
                    "M3-S03-01 output cannot claim final freeze.",
                )
            )
        if selected.get("release_required") is True and not selected.get(
            "release_input"
        ):
            findings.append(
                blocker(
                    "freeze-output-release-input-missing",
                    "A release-required output lacks reviewed M3-S02B-02 evidence.",
                )
            )
        generated = [
            require_mapping(item, "generated output member")
            for item in require_sequence(
                selected.get("generated"),
                "generated output members",
            )
        ]
        expected_generated = {
            f"generated/{name}" for name in GENERATED_MEMBERS
        } | {"freeze-report.md"}
        observed_generated = {str(item.get("path") or "") for item in generated}
        if observed_generated != expected_generated:
            findings.append(
                blocker(
                    "freeze-output-generated-set-mismatch",
                    "Generated output set does not match the contract.",
                    missing=sorted(expected_generated - observed_generated),
                    extra=sorted(observed_generated - expected_generated),
                )
            )
        for row in generated:
            findings.extend(self._file_findings(row))
            findings.extend(self._object_digest_findings(row))
        archive = require_mapping(selected.get("archive"), "archive output")
        findings.extend(self._file_findings(archive))
        archive_path = self.output / ARCHIVE_NAME
        archive_receipt: dict[str, Any] = {}
        replay_receipt: dict[str, Any] = {}
        if archive_path.is_file():
            try:
                archive_receipt = EvidenceArchiveVerifier().verify(
                    archive_path,
                    expected_commit=target_commit,
                )
                replay_receipt = ProjectionReplayVerifier().verify_archive(
                    archive_path,
                    expected_commit=target_commit,
                )
                if archive_receipt.get("manifest_digest") != archive.get(
                    "manifest_digest"
                ):
                    findings.append(
                        blocker(
                            "freeze-output-manifest-cross-link-mismatch",
                            "Archive manifest digest disagrees with generation receipt.",
                        )
                    )
                findings.extend(
                    self._archive_projection_findings(
                        archive_path,
                        generated,
                    )
                )
            except Exception as error:
                findings.append(
                    blocker(
                        "freeze-output-archive-verification-failed",
                        "Archive or projection replay verification failed.",
                        error=str(error),
                    )
                )
        else:
            findings.append(
                blocker(
                    "freeze-output-archive-missing",
                    "Evidence archive is missing.",
                )
            )
        score_path = self.output / "generated" / "100-point-evidence-index.json"
        ledger_path = self.output / "generated" / "internalization-ledger.json"
        try:
            score_receipt = ScoreMatrixVerifier().verify(load_json(score_path))
        except Exception as error:
            score_receipt = {}
            findings.append(
                blocker(
                    "freeze-output-score-verification-failed",
                    "100-point evidence index failed verification.",
                    error=str(error),
                )
            )
        try:
            ledger_receipt = InternalizationLedgerVerifier(
                self._repository_root()
            ).verify(load_json(ledger_path))
        except Exception as error:
            ledger_receipt = {}
            findings.append(
                blocker(
                    "freeze-output-ledger-verification-failed",
                    "Internalization ledger failed verification.",
                    error=str(error),
                )
            )
        findings.extend(
            self._material_findings(
                selected,
                target_commit=target_commit,
            )
        )
        for filename, digest_field in (
            (
                "archive-verification.json",
                "archive_verification_digest",
            ),
            (
                "replay-verification.json",
                "replay_verification_digest",
            ),
        ):
            path = self.output / filename
            if not path.is_file():
                findings.append(
                    blocker(
                        "freeze-output-sidecar-missing",
                        "Verification sidecar is missing.",
                        path=filename,
                    )
                )
                continue
            sidecar = load_json(path)
            if sidecar.get("receipt_digest") != selected.get(digest_field):
                findings.append(
                    blocker(
                        "freeze-output-sidecar-digest-mismatch",
                        "Verification sidecar digest disagrees with generation receipt.",
                        path=filename,
                    )
                )
            recomputed = (
                archive_receipt
                if filename == "archive-verification.json"
                else replay_receipt
            )
            if (
                recomputed
                and sidecar.get("receipt_digest")
                != recomputed.get("receipt_digest")
            ):
                findings.append(
                    blocker(
                        "freeze-output-sidecar-recomputation-mismatch",
                        "Stored verification sidecar disagrees with recomputation.",
                        path=filename,
                    )
                )
        if score_receipt and (
            selected.get("score", {}).get("verified")
            != score_receipt.get("verified_score")
        ):
            findings.append(
                blocker(
                    "freeze-output-score-cross-link-mismatch",
                    "Generation receipt score disagrees with evidence index.",
                )
            )
        require_no_blockers(
            findings,
            code="freeze-output-invalid",
            message="Generated first-stage evidence output is invalid.",
            phase="verification",
        )
        verification = {
            "schema": "zyra.m3-s03-01-output-verification/v1",
            "valid": True,
            "target_commit": target_commit,
            "score": score_receipt.get("verified_score"),
            "archive_manifest_digest": archive_receipt.get("manifest_digest"),
            "archive_sha256": archive_receipt.get("archive_sha256"),
            "replay_projection_count": replay_receipt.get("projection_count"),
            "task_success_recomputed": replay_receipt.get(
                "task_success_recomputed"
            ),
            "ledger_row_count": ledger_receipt.get("summary", {}).get(
                "row_count"
            ),
            "output_digest": self._output_digest(selected),
        }
        verification["receipt_digest"] = digest(verification)
        return verification

    def _file_findings(self, row: Mapping[str, Any]) -> list[dict[str, Any]]:
        findings = []
        try:
            relative = safe_relative_path(row.get("path"), "output member path")
        except Exception as error:
            return [
                blocker(
                    "freeze-output-member-path-invalid",
                    "Output member path is invalid.",
                    error=str(error),
                )
            ]
        path = self.output / relative
        if not path.is_file():
            return [
                blocker(
                    "freeze-output-member-missing",
                    "Declared output member is missing.",
                    path=relative,
                )
            ]
        observed = file_digest(path)
        if observed != row.get("sha256"):
            findings.append(
                blocker(
                    "freeze-output-member-digest-mismatch",
                    "Output member digest does not match.",
                    path=relative,
                    declared=row.get("sha256"),
                    observed=observed,
                )
            )
        if int(row.get("bytes") or -1) != path.stat().st_size:
            findings.append(
                blocker(
                    "freeze-output-member-size-mismatch",
                    "Output member size does not match.",
                    path=relative,
                )
            )
        return findings

    def _object_digest_findings(
        self,
        row: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        relative = str(row.get("path") or "")
        if not relative.endswith(".json"):
            return []
        path = self.output / relative
        if not path.is_file():
            return []
        document = load_json(path)
        fields = (
            "report_digest",
            "matrix_digest",
            "material_digest",
            "ledger_digest",
            "index_digest",
            "input_set_digest",
        )
        field = next((item for item in fields if document.get(item)), "")
        if not field:
            return [
                blocker(
                    "freeze-output-object-digest-missing",
                    "Generated JSON has no embedded object digest.",
                    path=relative,
                )
            ]
        projection = dict(document)
        declared = projection.pop(field)
        observed = digest(projection)
        findings = []
        if declared != observed:
            findings.append(
                blocker(
                    "freeze-output-object-digest-invalid",
                    "Generated JSON embedded digest is invalid.",
                    path=relative,
                    field=field,
                )
            )
        if row.get("object_digest") != declared:
            findings.append(
                blocker(
                    "freeze-output-object-digest-cross-link-mismatch",
                    "Generation receipt object digest disagrees with generated JSON.",
                    path=relative,
                    field=field,
                )
            )
        return findings

    def _material_findings(
        self,
        receipt: Mapping[str, Any],
        *,
        target_commit: str,
    ) -> list[dict[str, Any]]:
        findings = []
        root = self._repository_root()
        try:
            inputs, actual_input_receipt = load_freeze_input_set(
                root,
                expected_commit=target_commit,
                require_release=receipt.get("release_required") is True,
            )
        except Exception as error:
            return [
                blocker(
                    "freeze-output-input-readmission-failed",
                    "Original evidence inputs could not be readmitted.",
                    error=str(error),
                )
            ]
        documents = {
            material_id: load_json(
                self.output / "generated" / filename
            )
            for filename, material_id in GENERATED_MEMBERS.items()
        }
        if documents["input_set"].get(
            "input_set_digest"
        ) != actual_input_receipt.get("input_set_digest"):
            findings.append(
                blocker(
                    "freeze-output-input-set-drift",
                    "Generated input set no longer matches readmitted source evidence.",
                )
            )
        checks = (
            (
                "evidence_index",
                lambda value: FreezeEvidenceIndexBuilder(
                    inputs,
                    target_commit=target_commit,
                ).verify(value),
            ),
            (
                "internalization_ledger",
                lambda value: InternalizationLedgerVerifier(root).verify(value),
            ),
            (
                "algorithm_material",
                lambda value: AlgorithmMaterialBuilder(root).verify(value),
            ),
            (
                "case_studies",
                lambda value: CaseStudyBuilder(inputs).verify(value),
            ),
            (
                "ablation_material",
                lambda value: AblationMaterialBuilder(inputs).verify(value),
            ),
            (
                "compatibility_material",
                lambda value: CompatibilityMaterialBuilder(inputs).verify(value),
            ),
            (
                "application_value",
                lambda value: ApplicationValueBuilder(inputs).verify(value),
            ),
            (
                "langgraph_correction",
                lambda value: LangGraphCorrectionMatrixBuilder(root).verify(value),
            ),
            (
                "freeze_report",
                lambda value: FreezeReportBuilder().verify(value),
            ),
        )
        for material_id, check in checks:
            try:
                check(documents[material_id])
            except Exception as error:
                findings.append(
                    blocker(
                        "freeze-output-material-verification-failed",
                        "Generated material failed independent semantic verification.",
                        material_id=material_id,
                        error=str(error),
                    )
                )
        report = documents["freeze_report"]
        cross_links = {
            "input_set_digest": documents["input_set"].get("input_set_digest"),
            "evidence_index_digest": documents["evidence_index"].get(
                "index_digest"
            ),
            "internalization_ledger_digest": documents[
                "internalization_ledger"
            ].get("ledger_digest"),
            "algorithm_material_digest": documents["algorithm_material"].get(
                "material_digest"
            ),
            "case_material_digest": documents["case_studies"].get(
                "material_digest"
            ),
            "ablation_material_digest": documents["ablation_material"].get(
                "material_digest"
            ),
            "compatibility_material_digest": documents[
                "compatibility_material"
            ].get("material_digest"),
            "application_value_digest": documents["application_value"].get(
                "material_digest"
            ),
            "langgraph_correction_digest": documents[
                "langgraph_correction"
            ].get("matrix_digest"),
        }
        for field, expected in cross_links.items():
            if report.get(field) != expected:
                findings.append(
                    blocker(
                        "freeze-output-report-cross-link-mismatch",
                        "Freeze report digest link disagrees with generated material.",
                        field=field,
                        expected=expected,
                        observed=report.get(field),
                    )
                )
        return findings

    @staticmethod
    def _archive_projection_findings(
        archive_path: Path,
        generated: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        findings = []
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            manifest = require_mapping(
                load_json_from_bytes(archive.read("manifest.json")),
                "archive manifest",
            )
        by_path = {
            str(item.get("path") or ""): item
            for item in require_sequence(
                manifest.get("entries"),
                "archive manifest entries",
            )
            if isinstance(item, Mapping)
        }
        for row in generated:
            output_path = str(row.get("path") or "")
            archive_member = (
                output_path
                if output_path == "freeze-report.md"
                else output_path
            )
            archived = by_path.get(archive_member)
            if archived is None:
                findings.append(
                    blocker(
                        "freeze-output-archive-projection-missing",
                        "Generated output is absent from the evidence archive.",
                        path=output_path,
                    )
                )
            elif archived.get("sha256") != row.get("sha256"):
                findings.append(
                    blocker(
                        "freeze-output-archive-projection-mismatch",
                        "Generated output differs from its archived projection.",
                        path=output_path,
                    )
                )
        return findings

    def _repository_root(self) -> Path:
        for parent in (self.output, *self.output.parents):
            if (parent / "pyproject.toml").is_file() and (
                parent / "packages"
            ).is_dir():
                return parent
        raise fail(
            "freeze-output-repository-root-missing",
            "Could not resolve the Zyra repository for ledger verification.",
            phase="verification",
        )

    @staticmethod
    def _output_digest(receipt: Mapping[str, Any]) -> str:
        archive = require_mapping(receipt.get("archive"), "archive output")
        generated = [
            require_mapping(item, "generated output member")
            for item in require_sequence(
                receipt.get("generated"),
                "generated output members",
            )
        ]
        return digest(
            {
                "target_commit": receipt.get("target_commit"),
                "archive_sha256": archive.get("sha256"),
                "generated": [
                    {
                        "path": item.get("path"),
                        "sha256": item.get("sha256"),
                    }
                    for item in generated
                ],
            }
        )


def verify_freeze_output(
    output_directory: str | Path,
    *,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    return FreezeOutputVerifier(
        output_directory,
        expected_commit=expected_commit,
    ).verify()


def load_json_from_bytes(value: bytes) -> dict[str, Any]:
    import json

    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise fail(
            "freeze-archive-json-invalid",
            "Archived JSON member is invalid.",
            phase="verification",
            detail={"error": str(error)},
        ) from error
    return require_mapping(decoded, "archived JSON document")
