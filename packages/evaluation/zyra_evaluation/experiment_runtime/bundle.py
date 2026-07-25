from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import (
    canonical_json,
    canonicalize,
    content_digest,
    digest,
    new_identity,
    pretty_json,
    require_digest,
    stable_unique,
    utc_now,
)
from .errors import invalid
from .models import (
    BundleMember,
    DistributionSummary,
    EvidenceBundleManifest,
    ExperimentRun,
    RawMetricSample,
    RequirementEvidence,
    VariantComparison,
)
from .source import SourceArchive


REQUIRED_BUNDLE_PATHS = {
    "report/final-report.json",
    "report/algorithm-entries.json",
    "report/reviewer-navigation.json",
    "metrics/raw-samples.jsonl",
    "metrics/distribution-summaries.json",
    "metrics/variant-comparisons.json",
    "requirements/evidence-matrix.json",
    "sources/source-role-audit.json",
    "sources/live-source-manifest.json",
    "sources/canonical-events.jsonl",
    "sources/owner-receipts.json",
    "sources/artifact-manifest.json",
    "sources/domain-verification.json",
    "verification/receipts.json",
    "visual/screenshot-index.json",
    "configuration/comparison-envelope.json",
    "configuration/variants.json",
}

REQUIRED_CATEGORIES = {
    "report",
    "algorithm",
    "navigation",
    "raw_metric",
    "metric_summary",
    "comparison",
    "requirement",
    "source_role",
    "source_manifest",
    "canonical_event",
    "owner_receipt",
    "artifact",
    "verification",
    "visual_index",
    "configuration",
}


def merkle_root(members: Iterable[BundleMember]) -> str:
    leaves = [
        content_digest(
            f"leaf\0{item.path}\0{item.size}\0{item.sha256}".encode("utf-8")
        )
        for item in sorted(members, key=lambda value: value.path)
    ]
    if not leaves:
        return content_digest(b"empty-evidence-bundle")
    layer = leaves
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [
            content_digest(
                f"node\0{layer[index]}\0{layer[index + 1]}".encode("utf-8")
            )
            for index in range(0, len(layer), 2)
        ]
    return layer[0]


class EvidenceBundleBuilder:
    def __init__(
        self,
        *,
        artifact_root: str | Path,
        maximum_bundle_bytes: int = 4 * 1024 * 1024 * 1024,
    ) -> None:
        self.artifact_root = Path(artifact_root).resolve(strict=False)
        self.maximum_bundle_bytes = maximum_bundle_bytes

    def build(
        self,
        *,
        run: ExperimentRun,
        source: SourceArchive,
        report: Mapping[str, Any],
        samples: Iterable[RawMetricSample],
        summaries: Iterable[DistributionSummary],
        comparisons: Iterable[VariantComparison],
        requirements: Iterable[RequirementEvidence],
        source_role_audit: Mapping[str, Any],
        verification_receipts: Iterable[Mapping[str, Any]],
        screenshot_index: Mapping[str, Any] | None = None,
        additional_members: Mapping[str, bytes | str] | None = None,
    ) -> dict[str, Any]:
        samples = tuple(samples)
        summaries = tuple(summaries)
        comparisons = tuple(comparisons)
        requirements = tuple(requirements)
        receipts = tuple(dict(item) for item in verification_receipts)
        self._require_ready(
            run=run,
            source=source,
            report=report,
            samples=samples,
            summaries=summaries,
            comparisons=comparisons,
            requirements=requirements,
            source_role_audit=source_role_audit,
            receipts=receipts,
        )
        screenshot = self._screenshot_index(
            screenshot_index or {},
            experiment_id=run.experiment_id,
        )
        payloads: dict[str, tuple[bytes, str, str, tuple[str, ...], tuple[str, ...]]] = {}

        def add_json(
            path: str,
            value: Any,
            *,
            category: str,
            requirements_for_member: Iterable[str] = (),
            source_ids: Iterable[str] = (),
        ) -> None:
            add_bytes(
                path,
                pretty_json(value).encode("utf-8"),
                media_type="application/json",
                category=category,
                requirements_for_member=requirements_for_member,
                source_ids=source_ids,
            )

        def add_jsonl(
            path: str,
            values: Iterable[Any],
            *,
            category: str,
            requirements_for_member: Iterable[str] = (),
            source_ids: Iterable[str] = (),
        ) -> None:
            payload = "".join(
                canonical_json(item) + "\n" for item in values
            ).encode("utf-8")
            add_bytes(
                path,
                payload,
                media_type="application/x-ndjson",
                category=category,
                requirements_for_member=requirements_for_member,
                source_ids=source_ids,
            )

        def add_bytes(
            path: str,
            value: bytes,
            *,
            media_type: str,
            category: str,
            requirements_for_member: Iterable[str] = (),
            source_ids: Iterable[str] = (),
        ) -> None:
            selected = _safe_path(path)
            if selected == "manifest.json" or selected in payloads:
                raise invalid(
                    "experiment_bundle_member_duplicate",
                    "Evidence bundle member path is duplicated or reserved.",
                    detail={"path": selected},
                )
            payloads[selected] = (
                bytes(value),
                media_type,
                category,
                stable_unique(requirements_for_member),
                stable_unique(source_ids),
            )

        all_requirement_ids = tuple(
            item.requirement_id for item in requirements
        )
        add_json(
            "report/final-report.json",
            report,
            category="report",
            requirements_for_member=all_requirement_ids,
            source_ids=(run.experiment_id, source.archive_id),
        )
        add_json(
            "report/algorithm-entries.json",
            report.get("algorithm_entries") or [],
            category="algorithm",
            requirements_for_member=("SCORE-ALGO",),
            source_ids=(run.experiment_id,),
        )
        add_json(
            "report/reviewer-navigation.json",
            report.get("reviewer_navigation") or {},
            category="navigation",
            requirements_for_member=("REQ-TRACE-01", "SCORE-UX"),
            source_ids=(run.experiment_id,),
        )
        add_jsonl(
            "metrics/raw-samples.jsonl",
            (item.to_dict() for item in samples),
            category="raw_metric",
            requirements_for_member=all_requirement_ids,
            source_ids=tuple(item.sample_id for item in samples),
        )
        add_json(
            "metrics/distribution-summaries.json",
            {
                "schema": "zyra.experiment-distribution-summary-set/v1",
                "experiment_id": run.experiment_id,
                "summaries": [item.to_dict() for item in summaries],
            },
            category="metric_summary",
            requirements_for_member=all_requirement_ids,
            source_ids=tuple(item.summary_digest for item in summaries),
        )
        add_json(
            "metrics/variant-comparisons.json",
            {
                "schema": "zyra.experiment-variant-comparison-set/v1",
                "experiment_id": run.experiment_id,
                "comparisons": [item.to_dict() for item in comparisons],
            },
            category="comparison",
            requirements_for_member=all_requirement_ids,
            source_ids=tuple(item.comparison_digest for item in comparisons),
        )
        add_json(
            "requirements/evidence-matrix.json",
            {
                "schema": "zyra.experiment-requirement-evidence-set/v1",
                "experiment_id": run.experiment_id,
                "score_total": sum(item.score for item in requirements),
                "score_verified": sum(
                    item.score for item in requirements if item.verified
                ),
                "rows": [item.to_dict() for item in requirements],
            },
            category="requirement",
            requirements_for_member=all_requirement_ids,
            source_ids=tuple(item.source_digest for item in requirements),
        )
        add_json(
            "sources/source-role-audit.json",
            source_role_audit,
            category="source_role",
            requirements_for_member=("SCORE-ALGO",),
            source_ids=(str(source_role_audit.get("audit_digest") or ""),),
        )
        add_json(
            "sources/live-source-manifest.json",
            source.to_dict(include_events=False),
            category="source_manifest",
            requirements_for_member=(
                "REQ-CLOSE-01",
                "REQ-TRACE-01",
                "SCORE-LOOP",
                "SCORE-TASKS",
            ),
            source_ids=(source.archive_id, source.archive_digest),
        )
        add_jsonl(
            "sources/canonical-events.jsonl",
            (item.to_dict() for item in source.events),
            category="canonical_event",
            requirements_for_member=(
                "REQ-CLOSE-01",
                "REQ-TOPO-01",
                "REQ-COMM-01",
                "REQ-FAULT-01",
                "REQ-TRACE-01",
                "SCORE-LOOP",
                "SCORE-ORG",
                "SCORE-ROBUST",
            ),
            source_ids=tuple(item.event_id for item in source.events),
        )
        add_json(
            "sources/owner-receipts.json",
            {
                "schema": "zyra.experiment-owner-receipt-set/v1",
                "receipts": canonicalize(source.owner_receipts),
            },
            category="owner_receipt",
            requirements_for_member=(
                "REQ-EDGE-01",
                "REQ-FAULT-01",
                "REQ-TRACE-01",
                "SCORE-COMPAT",
            ),
            source_ids=(source.owner_run_id, source.task_id),
        )
        add_json(
            "sources/artifact-manifest.json",
            {
                "schema": "zyra.experiment-source-artifact-set/v1",
                "artifacts": canonicalize(source.artifacts),
            },
            category="artifact",
            requirements_for_member=(
                "REQ-CLOSE-01",
                "REQ-TRACE-01",
                "SCORE-LOOP",
                "SCORE-TASKS",
            ),
            source_ids=tuple(
                str(item.get("artifact_id") or item.get("entry_id") or "")
                for item in source.artifacts
            ),
        )
        add_json(
            "sources/domain-verification.json",
            source.domain_verification,
            category="verification",
            requirements_for_member=(
                "REQ-CLOSE-01",
                "REQ-FAULT-01",
                "SCORE-LOOP",
                "SCORE-ROBUST",
            ),
            source_ids=(source.scenario_run_id,),
        )
        add_json(
            "verification/receipts.json",
            {
                "schema": "zyra.experiment-verification-receipt-set/v1",
                "experiment_id": run.experiment_id,
                "receipts": canonicalize(receipts),
                "all_valid": all(item.get("valid") is True for item in receipts),
            },
            category="verification",
            requirements_for_member=all_requirement_ids,
            source_ids=tuple(
                str(item.get("receipt_id") or item.get("receipt_digest") or "")
                for item in receipts
            ),
        )
        add_json(
            "visual/screenshot-index.json",
            screenshot,
            category="visual_index",
            requirements_for_member=("REQ-TRACE-01", "SCORE-UX"),
            source_ids=tuple(
                str(item.get("screenshot_id") or "")
                for item in screenshot.get("screenshots") or ()
            ),
        )
        add_json(
            "configuration/comparison-envelope.json",
            run.envelope.to_dict(),
            category="configuration",
            requirements_for_member=all_requirement_ids,
            source_ids=(run.envelope.envelope_id,),
        )
        add_json(
            "configuration/variants.json",
            {
                "schema": "zyra.experiment-variant-set/v1",
                "variants": [item.to_dict() for item in run.variants],
                "cells": [item.to_dict() for item in run.cells],
            },
            category="configuration",
            requirements_for_member=all_requirement_ids,
            source_ids=tuple(item.variant_id for item in run.variants),
        )
        for path, value in sorted((additional_members or {}).items()):
            payload = value.encode("utf-8") if isinstance(value, str) else bytes(value)
            add_bytes(
                path,
                payload,
                media_type="application/octet-stream",
                category="supplementary",
                source_ids=(run.experiment_id,),
            )
        members = tuple(
            BundleMember(
                path=path,
                media_type=media_type,
                size=len(payload),
                sha256=content_digest(payload),
                category=category,
                requirement_ids=requirement_ids,
                source_ids=source_ids,
                executable=False,
                redacted=False,
            )
            for path, (
                payload,
                media_type,
                category,
                requirement_ids,
                source_ids,
            ) in sorted(payloads.items())
        )
        root_digest = merkle_root(members)
        manifest = EvidenceBundleManifest(
            bundle_id=new_identity("bundle"),
            experiment_id=run.experiment_id,
            schema_version="1",
            commit_sha=run.envelope.commit_sha,
            envelope_digest=run.envelope.envelope_digest,
            report_digest=str(report["report_digest"]),
            members=members,
            root_digest=root_digest,
            created_at=utc_now(),
            source_run_ids=stable_unique(
                (
                    source.scenario_run_id,
                    source.owner_run_id,
                    source.task_id,
                )
            ),
            requirement_ids=all_requirement_ids,
            source_role_audit_digest=str(source_role_audit["audit_digest"]),
            non_claims=tuple(
                str(item)
                for item in (
                    report.get("method", {}).get("non_claims", ())
                    if isinstance(report.get("method"), Mapping)
                    else ()
                )
            ),
            metadata={
                "deterministic_zip_metadata": True,
                "backend_log_required": False,
                "raw_samples_included": True,
                "canonical_events_included": True,
                "screenshots_included": len(screenshot.get("screenshots") or ()),
            },
        )
        manifest_bytes = pretty_json(manifest.to_dict()).encode("utf-8")
        bundle_bytes = self._zip_bytes(payloads, manifest_bytes)
        if len(bundle_bytes) > self.maximum_bundle_bytes:
            raise invalid(
                "experiment_bundle_too_large",
                "Evidence bundle exceeds the configured size limit.",
                detail={
                    "size": len(bundle_bytes),
                    "maximum": self.maximum_bundle_bytes,
                },
            )
        output_root = self.artifact_root / "experiment-evidence"
        output_root.mkdir(parents=True, exist_ok=True)
        output_path = output_root / f"{manifest.bundle_id}.zip"
        self._atomic_write(output_path, bundle_bytes)
        verification = EvidenceBundleVerifier().verify(
            output_path,
            expected_manifest_digest=manifest.manifest_digest,
        )
        if not verification["valid"]:
            output_path.unlink(missing_ok=True)
            raise invalid(
                "experiment_bundle_verification_failed",
                "Generated evidence bundle failed self-verification.",
                phase="bundle",
                detail=verification,
            )
        return {
            "schema": "zyra.experiment-evidence-bundle-result/v1",
            "bundle_id": manifest.bundle_id,
            "experiment_id": run.experiment_id,
            "path": str(output_path),
            "size": len(bundle_bytes),
            "sha256": content_digest(bundle_bytes),
            "manifest": manifest.to_dict(),
            "verification": verification,
            "created_at": manifest.created_at,
        }

    def _require_ready(
        self,
        *,
        run: ExperimentRun,
        source: SourceArchive,
        report: Mapping[str, Any],
        samples: tuple[RawMetricSample, ...],
        summaries: tuple[DistributionSummary, ...],
        comparisons: tuple[VariantComparison, ...],
        requirements: tuple[RequirementEvidence, ...],
        source_role_audit: Mapping[str, Any],
        receipts: tuple[dict[str, Any], ...],
    ) -> None:
        findings: list[dict[str, Any]] = []
        if run.phase.value not in {"verifying", "succeeded"}:
            findings.append({"code": "experiment_phase_invalid", "phase": run.phase.value})
        if any(item.phase.value != "succeeded" for item in run.cells):
            findings.append({"code": "experiment_cells_incomplete"})
        if source.archive_digest != run.envelope.source_evidence_digest:
            findings.append({"code": "source_digest_mismatch"})
        if str(report.get("report_digest") or "") != digest(
            {key: value for key, value in report.items() if key != "report_digest"}
        ):
            findings.append({"code": "report_digest_invalid"})
        if not samples:
            findings.append({"code": "raw_samples_missing"})
        if not summaries:
            findings.append({"code": "summaries_missing"})
        if not comparisons:
            findings.append({"code": "comparisons_missing"})
        if any(not item.verified for item in requirements):
            findings.append(
                {
                    "code": "requirements_incomplete",
                    "ids": [
                        item.requirement_id
                        for item in requirements
                        if not item.verified
                    ],
                }
            )
        if sum(item.score for item in requirements) != 100:
            findings.append({"code": "score_total_invalid"})
        if source_role_audit.get("valid") is not True:
            findings.append({"code": "source_role_audit_invalid"})
        if not receipts or any(item.get("valid") is not True for item in receipts):
            findings.append({"code": "verification_receipts_invalid"})
        if findings:
            raise invalid(
                "experiment_bundle_input_invalid",
                "Evidence bundle inputs are incomplete.",
                phase="bundle",
                detail={"findings": findings},
            )

    def _screenshot_index(
        self,
        value: Mapping[str, Any],
        *,
        experiment_id: str,
    ) -> dict[str, Any]:
        screenshots = value.get("screenshots")
        if screenshots is None:
            screenshots = []
        if not isinstance(screenshots, list) or not all(
            isinstance(item, Mapping) for item in screenshots
        ):
            raise invalid(
                "experiment_screenshot_index_invalid",
                "Screenshot index must contain screenshot objects.",
            )
        index = {
            "schema": "zyra.experiment-screenshot-index/v1",
            "experiment_id": experiment_id,
            "screenshots": canonicalize(screenshots),
            "capture_required_for_runtime_correctness": False,
            "backend_log_required": False,
            "note": (
                str(value.get("note") or "")
                or "Index may be populated by the M2 reviewer capture flow."
            ),
        }
        index["index_digest"] = digest(index)
        return index

    def _zip_bytes(
        self,
        payloads: Mapping[
            str,
            tuple[bytes, str, str, tuple[str, ...], tuple[str, ...]],
        ],
        manifest_bytes: bytes,
    ) -> bytes:
        stream = io.BytesIO()
        with zipfile.ZipFile(
            stream,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            self._write_zip_member(archive, "manifest.json", manifest_bytes)
            for path, (payload, _, _, _, _) in sorted(payloads.items()):
                self._write_zip_member(archive, path, payload)
        return stream.getvalue()

    def _write_zip_member(
        self,
        archive: zipfile.ZipFile,
        path: str,
        payload: bytes,
    ) -> None:
        info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        info.flag_bits |= 0x800
        archive.writestr(info, payload)

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


class EvidenceBundleVerifier:
    def __init__(
        self,
        *,
        maximum_members: int = 100_000,
        maximum_member_bytes: int = 2 * 1024 * 1024 * 1024,
        maximum_expanded_bytes: int = 8 * 1024 * 1024 * 1024,
    ) -> None:
        self.maximum_members = maximum_members
        self.maximum_member_bytes = maximum_member_bytes
        self.maximum_expanded_bytes = maximum_expanded_bytes

    def verify(
        self,
        path: str | Path,
        *,
        expected_manifest_digest: str = "",
    ) -> dict[str, Any]:
        selected = Path(path).resolve(strict=False)
        findings: list[dict[str, Any]] = []
        members: dict[str, bytes] = {}
        manifest: dict[str, Any] = {}
        if not selected.is_file():
            findings.append({"code": "bundle_missing", "path": str(selected)})
        else:
            try:
                with zipfile.ZipFile(selected, "r") as archive:
                    infos = archive.infolist()
                    if len(infos) > self.maximum_members:
                        findings.append(
                            {
                                "code": "member_count_exceeded",
                                "count": len(infos),
                            }
                        )
                    names: set[str] = set()
                    expanded = 0
                    for info in infos:
                        if info.is_dir():
                            continue
                        try:
                            name = _safe_path(info.filename)
                        except Exception as error:
                            findings.append(
                                {
                                    "code": "member_path_invalid",
                                    "path": info.filename,
                                    "reason": str(error),
                                }
                            )
                            continue
                        if name in names:
                            findings.append(
                                {"code": "member_duplicate", "path": name}
                            )
                            continue
                        names.add(name)
                        if info.file_size > self.maximum_member_bytes:
                            findings.append(
                                {
                                    "code": "member_size_exceeded",
                                    "path": name,
                                    "size": info.file_size,
                                }
                            )
                            continue
                        expanded += info.file_size
                        if expanded > self.maximum_expanded_bytes:
                            findings.append(
                                {
                                    "code": "expanded_size_exceeded",
                                    "size": expanded,
                                }
                            )
                            break
                        members[name] = archive.read(info)
            except (OSError, zipfile.BadZipFile) as error:
                findings.append(
                    {"code": "bundle_invalid_zip", "reason": str(error)}
                )
        manifest_bytes = members.pop("manifest.json", None)
        if manifest_bytes is None:
            findings.append({"code": "manifest_missing"})
        else:
            try:
                parsed = json.loads(manifest_bytes.decode("utf-8"))
                if not isinstance(parsed, Mapping):
                    raise TypeError("manifest must be an object")
                manifest = dict(parsed)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
                findings.append(
                    {"code": "manifest_invalid", "reason": str(error)}
                )
        declared_members: dict[str, Mapping[str, Any]] = {}
        manifest_digest = ""
        if manifest:
            body = dict(manifest)
            manifest_digest = str(body.pop("manifest_digest", "") or "")
            calculated_manifest_digest = digest(body)
            if manifest_digest != calculated_manifest_digest:
                findings.append(
                    {
                        "code": "manifest_digest_mismatch",
                        "declared": manifest_digest,
                        "calculated": calculated_manifest_digest,
                    }
                )
            if (
                expected_manifest_digest
                and manifest_digest != expected_manifest_digest
            ):
                findings.append(
                    {
                        "code": "manifest_expected_digest_mismatch",
                        "expected": expected_manifest_digest,
                        "observed": manifest_digest,
                    }
                )
            rows = manifest.get("members")
            if not isinstance(rows, list):
                findings.append({"code": "manifest_members_invalid"})
            else:
                for row in rows:
                    if not isinstance(row, Mapping):
                        findings.append({"code": "manifest_member_shape_invalid"})
                        continue
                    path_value = str(row.get("path") or "")
                    if path_value in declared_members:
                        findings.append(
                            {
                                "code": "manifest_member_duplicate",
                                "path": path_value,
                            }
                        )
                    declared_members[path_value] = row
            missing = sorted(set(declared_members) - set(members))
            extra = sorted(set(members) - set(declared_members))
            if missing:
                findings.append(
                    {"code": "declared_members_missing", "paths": missing}
                )
            if extra:
                findings.append(
                    {"code": "undeclared_members_present", "paths": extra}
                )
            for path_value, row in declared_members.items():
                payload = members.get(path_value)
                if payload is None:
                    continue
                calculated = content_digest(payload)
                if str(row.get("sha256") or "") != calculated:
                    findings.append(
                        {
                            "code": "member_digest_mismatch",
                            "path": path_value,
                            "declared": row.get("sha256"),
                            "calculated": calculated,
                        }
                    )
                if int(row.get("size") or -1) != len(payload):
                    findings.append(
                        {
                            "code": "member_size_mismatch",
                            "path": path_value,
                            "declared": row.get("size"),
                            "calculated": len(payload),
                        }
                    )
            member_objects: list[BundleMember] = []
            for row in declared_members.values():
                try:
                    member_objects.append(
                        BundleMember(
                            path=str(row["path"]),
                            media_type=str(row["media_type"]),
                            size=int(row["size"]),
                            sha256=require_digest(
                                row["sha256"],
                                "bundle member digest",
                            ),
                            category=str(row["category"]),
                            requirement_ids=tuple(
                                str(item)
                                for item in row.get("requirement_ids") or ()
                            ),
                            source_ids=tuple(
                                str(item) for item in row.get("source_ids") or ()
                            ),
                            executable=row.get("executable") is True,
                            redacted=row.get("redacted") is True,
                        )
                    )
                except Exception as error:
                    findings.append(
                        {
                            "code": "manifest_member_invalid",
                            "path": row.get("path"),
                            "reason": str(error),
                        }
                    )
            calculated_root = merkle_root(member_objects)
            if str(manifest.get("root_digest") or "") != calculated_root:
                findings.append(
                    {
                        "code": "root_digest_mismatch",
                        "declared": manifest.get("root_digest"),
                        "calculated": calculated_root,
                    }
                )
            missing_required = sorted(
                REQUIRED_BUNDLE_PATHS - set(declared_members)
            )
            if missing_required:
                findings.append(
                    {
                        "code": "required_members_missing",
                        "paths": missing_required,
                    }
                )
            categories = {
                str(row.get("category") or "") for row in declared_members.values()
            }
            missing_categories = sorted(REQUIRED_CATEGORIES - categories)
            if missing_categories:
                findings.append(
                    {
                        "code": "required_categories_missing",
                        "categories": missing_categories,
                    }
                )
            requirement_ids = set(
                str(item) for item in manifest.get("requirement_ids") or ()
            )
            if not requirement_ids:
                findings.append({"code": "requirement_ids_missing"})
            for requirement_id in requirement_ids:
                if not any(
                    requirement_id in {
                        str(item)
                        for item in row.get("requirement_ids") or ()
                    }
                    for row in declared_members.values()
                ):
                    findings.append(
                        {
                            "code": "requirement_member_binding_missing",
                            "requirement_id": requirement_id,
                        }
                    )
        receipt = {
            "schema": "zyra.experiment-evidence-bundle-verification/v1",
            "valid": not findings,
            "path": str(selected),
            "bundle_size": selected.stat().st_size if selected.is_file() else 0,
            "bundle_sha256": (
                content_digest(selected.read_bytes()) if selected.is_file() else ""
            ),
            "manifest_digest": manifest_digest,
            "member_count": len(members),
            "findings": findings,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def require_valid(
        self,
        path: str | Path,
        *,
        expected_manifest_digest: str = "",
    ) -> dict[str, Any]:
        receipt = self.verify(
            path,
            expected_manifest_digest=expected_manifest_digest,
        )
        if not receipt["valid"]:
            raise invalid(
                "experiment_bundle_invalid",
                "Evidence bundle failed verification.",
                phase="bundle",
                detail=receipt,
            )
        return receipt


def _safe_path(value: str) -> str:
    selected = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(selected)
    if (
        not selected
        or path.is_absolute()
        or ".." in path.parts
        or ":" in path.parts[0]
    ):
        raise invalid(
            "experiment_bundle_path_invalid",
            "Evidence bundle member path is unsafe.",
            detail={"path": selected},
        )
    return selected
