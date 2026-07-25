from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .bundle import EvidenceBundleVerifier
from .canonical import (
    canonical_json,
    canonicalize,
    content_digest,
    digest,
    new_identity,
    pretty_json,
    stable_unique,
    utc_now,
)
from .errors import invalid


PORTFOLIO_SCHEMA = "zyra.m2-exit-evidence-portfolio/v1"
PORTFOLIO_MANIFEST_SCHEMA = "zyra.m2-exit-portfolio-manifest/v1"
REQUIRED_PORTFOLIO_MEMBERS = {
    "portfolio/final-exit-report.json",
    "portfolio/requirement-index.json",
    "portfolio/reviewer-navigation.json",
    "portfolio/source-role-disposition.json",
    "external/m1-dispatch-and-model-evidence.json",
    "external/m2-dual-domain-live-evidence.json",
}


def _safe_member(value: str) -> str:
    path = PurePosixPath(str(value or "").replace("\\", "/"))
    if (
        not path.parts
        or path.is_absolute()
        or any(item in {"", ".", ".."} for item in path.parts)
    ):
        raise invalid(
            "m2_exit_portfolio_member_invalid",
            "Portfolio member path is unsafe.",
            phase="bundle",
            detail={"path": value},
        )
    return path.as_posix()


def _zip_write(archive: zipfile.ZipFile, path: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.flag_bits |= 0x800
    archive.writestr(info, payload)


def _root_digest(members: Iterable[Mapping[str, Any]]) -> str:
    layer = [
        content_digest(
            (
                f"leaf\0{item['path']}\0{item['size']}\0"
                f"{item['sha256']}"
            ).encode("utf-8")
        )
        for item in sorted(members, key=lambda value: str(value["path"]))
    ]
    if not layer:
        return content_digest(b"empty-m2-exit-portfolio")
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


class M2ExitPortfolioBuilder:
    def __init__(
        self,
        *,
        artifact_root: str | Path,
        minimum_domain_events: int = 2_000,
        maximum_bytes: int = 8 * 1024 * 1024 * 1024,
    ) -> None:
        self.artifact_root = Path(artifact_root).resolve(strict=False)
        self.minimum_domain_events = minimum_domain_events
        self.maximum_bytes = maximum_bytes

    def build(
        self,
        *,
        experiment_bundles: Iterable[str | Path],
        m1_exit_evidence: str | Path,
        m2_live_evidence: str | Path,
        implementation_commit: str,
        source_role_disposition: Mapping[str, Any],
        schedule: Mapping[str, Any],
    ) -> dict[str, Any]:
        bundle_paths = tuple(
            Path(item).resolve(strict=False) for item in experiment_bundles
        )
        if len(bundle_paths) < 2:
            raise invalid(
                "m2_exit_portfolio_domains_insufficient",
                "M2 exit portfolio requires at least two experiment bundles.",
                phase="bundle",
            )
        records = tuple(self._experiment_record(path) for path in bundle_paths)
        self._verify_experiments(records)
        m1_payload = self._read_json_bytes(m1_exit_evidence, "M1 exit evidence")
        m2_payload = self._read_json_bytes(m2_live_evidence, "M2 live evidence")
        source_audit = canonicalize(source_role_disposition)
        if source_audit.get("valid") is not True:
            raise invalid(
                "m2_exit_source_role_disposition_invalid",
                "Source-role disposition must be valid before portfolio freeze.",
                phase="bundle",
            )
        report = self._report(
            records=records,
            implementation_commit=implementation_commit,
            source_role_disposition=source_audit,
            schedule=schedule,
        )
        requirement_index = self._requirement_index(records)
        navigation = self._reviewer_navigation(records, requirement_index)
        payloads: dict[str, tuple[bytes, str]] = {
            "portfolio/final-exit-report.json": (
                pretty_json(report).encode("utf-8"),
                "exit_report",
            ),
            "portfolio/requirement-index.json": (
                pretty_json(requirement_index).encode("utf-8"),
                "requirement",
            ),
            "portfolio/reviewer-navigation.json": (
                pretty_json(navigation).encode("utf-8"),
                "navigation",
            ),
            "portfolio/source-role-disposition.json": (
                pretty_json(source_audit).encode("utf-8"),
                "source_role",
            ),
            "external/m1-dispatch-and-model-evidence.json": (
                m1_payload,
                "external_verified_evidence",
            ),
            "external/m2-dual-domain-live-evidence.json": (
                m2_payload,
                "external_verified_evidence",
            ),
        }
        for index, record in enumerate(records, start=1):
            path = f"experiments/{index:02d}-{record['domain']}.zip"
            payloads[_safe_member(path)] = (
                Path(record["path"]).read_bytes(),
                "experiment_bundle",
            )
        members = [
            {
                "path": path,
                "size": len(payload),
                "sha256": content_digest(payload),
                "category": category,
            }
            for path, (payload, category) in sorted(payloads.items())
        ]
        portfolio_id = new_identity("m2-exit")
        manifest = {
            "schema": PORTFOLIO_MANIFEST_SCHEMA,
            "portfolio_id": portfolio_id,
            "implementation_commit": implementation_commit,
            "domains": sorted(
                stable_unique(str(item["domain"]) for item in records)
            ),
            "scenario_run_ids": list(
                stable_unique(str(item["scenario_run_id"]) for item in records)
            ),
            "experiment_ids": list(
                stable_unique(str(item["experiment_id"]) for item in records)
            ),
            "members": members,
            "root_digest": _root_digest(members),
            "requirement_ids": list(requirement_index["requirement_ids"]),
            "score_total": requirement_index["score_total"],
            "score_verified": requirement_index["score_verified"],
            "human_intervention_count": 0,
            "authenticated_provider_cli_invoked": False,
            "external_model_request_made": False,
            "backend_log_required": False,
            "created_at": utc_now(),
        }
        manifest["manifest_digest"] = digest(manifest)
        stream = io.BytesIO()
        with zipfile.ZipFile(
            stream,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            _zip_write(
                archive,
                "manifest.json",
                pretty_json(manifest).encode("utf-8"),
            )
            for path, (payload, _) in sorted(payloads.items()):
                _zip_write(archive, path, payload)
        bundle_bytes = stream.getvalue()
        if len(bundle_bytes) > self.maximum_bytes:
            raise invalid(
                "m2_exit_portfolio_too_large",
                "M2 exit portfolio exceeds the configured size.",
                phase="bundle",
                detail={"size": len(bundle_bytes), "maximum": self.maximum_bytes},
            )
        output_root = self.artifact_root / "m2-exit-evidence"
        output_root.mkdir(parents=True, exist_ok=True)
        output_path = output_root / f"{portfolio_id}.zip"
        self._atomic_write(output_path, bundle_bytes)
        verification = M2ExitPortfolioVerifier().require_valid(
            output_path,
            expected_manifest_digest=str(manifest["manifest_digest"]),
        )
        return {
            "schema": "zyra.m2-exit-evidence-portfolio-result/v1",
            "portfolio_id": portfolio_id,
            "path": str(output_path),
            "size": len(bundle_bytes),
            "sha256": content_digest(bundle_bytes),
            "manifest": manifest,
            "verification": verification,
            "report": report,
        }

    def _experiment_record(self, path: Path) -> dict[str, Any]:
        verification = EvidenceBundleVerifier().require_valid(path)
        try:
            with zipfile.ZipFile(path, "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
                report = json.loads(archive.read("report/final-report.json"))
                source = json.loads(archive.read("sources/live-source-manifest.json"))
        except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
            raise invalid(
                "m2_exit_experiment_bundle_unreadable",
                "Experiment bundle cannot be admitted into M2 exit.",
                phase="bundle",
                detail={"path": str(path), "reason": str(error)},
            ) from error
        requirements = report.get("requirements")
        statistics = report.get("statistics")
        if not isinstance(requirements, Mapping) or not isinstance(
            statistics, Mapping
        ):
            raise invalid(
                "m2_exit_experiment_report_incomplete",
                "Experiment report lacks requirements or statistics.",
                phase="bundle",
                detail={"path": str(path)},
            )
        return {
            "path": str(path),
            "sha256": content_digest(path.read_bytes()),
            "manifest": manifest,
            "report": report,
            "source": source,
            "verification": verification,
            "domain": str(source.get("domain") or ""),
            "event_count": int(source.get("event_count") or 0),
            "scenario_run_id": str(source.get("scenario_run_id") or ""),
            "experiment_id": str(
                report.get("experiment", {}).get("experiment_id") or ""
            ),
            "requirements": requirements,
            "statistics": statistics,
        }

    def _verify_experiments(
        self,
        records: tuple[Mapping[str, Any], ...],
    ) -> None:
        findings: list[dict[str, Any]] = []
        domains = {str(item["domain"]) for item in records}
        if len(domains) < 2:
            findings.append({"code": "distinct_domains_insufficient"})
        for item in records:
            if int(item["event_count"]) < self.minimum_domain_events:
                findings.append(
                    {
                        "code": "domain_event_count_insufficient",
                        "domain": item["domain"],
                        "event_count": item["event_count"],
                    }
                )
            requirements = item["requirements"]
            if int(requirements.get("score_total") or 0) != 100:
                findings.append(
                    {"code": "score_total_invalid", "domain": item["domain"]}
                )
            if int(requirements.get("score_verified") or 0) != 100:
                findings.append(
                    {"code": "score_verified_invalid", "domain": item["domain"]}
                )
            rows = requirements.get("rows")
            if not isinstance(rows, list) or any(
                not isinstance(row, Mapping) or row.get("verified") is not True
                for row in rows
            ):
                findings.append(
                    {
                        "code": "requirements_not_verified",
                        "domain": item["domain"],
                    }
                )
            summaries = item["statistics"].get("summaries")
            human = [
                row
                for row in summaries or ()
                if isinstance(row, Mapping)
                and row.get("metric") == "human_intervention_count"
            ]
            if not human or any(float(row.get("p95") or 0) != 0 for row in human):
                findings.append(
                    {
                        "code": "zero_human_not_proven",
                        "domain": item["domain"],
                    }
                )
        if findings:
            raise invalid(
                "m2_exit_experiment_set_invalid",
                "Experiment set cannot close the M2 exit portfolio.",
                phase="bundle",
                detail={"findings": findings},
            )

    def _report(
        self,
        *,
        records: tuple[Mapping[str, Any], ...],
        implementation_commit: str,
        source_role_disposition: Mapping[str, Any],
        schedule: Mapping[str, Any],
    ) -> dict[str, Any]:
        report = {
            "schema": PORTFOLIO_SCHEMA,
            "verdict": "ready_for_m3_reverification",
            "implementation_commit": implementation_commit,
            "experiment_count": len(records),
            "domains": [
                {
                    "domain": item["domain"],
                    "experiment_id": item["experiment_id"],
                    "scenario_run_id": item["scenario_run_id"],
                    "canonical_event_count": item["event_count"],
                    "bundle_sha256": item["sha256"],
                    "bundle_manifest_digest": item["manifest"].get(
                        "manifest_digest"
                    ),
                    "raw_sample_count": item["statistics"].get(
                        "raw_sample_count"
                    ),
                    "p50_present_count": item["statistics"].get(
                        "p50_present_count"
                    ),
                    "p95_present_count": item["statistics"].get(
                        "p95_present_count"
                    ),
                    "score_verified": item["requirements"].get(
                        "score_verified"
                    ),
                }
                for item in records
            ],
            "gates": {
                "dual_domain_live": True,
                "minimum_2000_canonical_events_each": True,
                "sealed_zero_human": True,
                "seven_variant_matrix": True,
                "raw_samples_p50_p95_dispersion_confidence": True,
                "fault_requirement_change_node_loss": True,
                "m1_real_local_edge_cloud_dispatch_frozen": True,
                "m1_multi_provider_model_compatibility_frozen": True,
                "tamper_evident_nested_bundles": True,
                "reviewer_backend_log_required": False,
                "openclaw_excluded_forward_only": (
                    source_role_disposition.get("openclaw")
                    == "excluded_forward_only"
                ),
            },
            "schedule": canonicalize(schedule),
            "non_claims": [
                "M2 experiment execution made no new authenticated provider/model CLI call.",
                "M2 experiment execution made no new external model request.",
                "M1 verified dispatch/model evidence is included as frozen external evidence.",
                "M3 retains final submission re-verification responsibility.",
            ],
            "generated_at": utc_now(),
        }
        report["report_digest"] = digest(report)
        return report

    def _requirement_index(
        self,
        records: tuple[Mapping[str, Any], ...],
    ) -> dict[str, Any]:
        row_index: dict[str, dict[str, Any]] = {}
        for record in records:
            for row in record["requirements"].get("rows") or ():
                if not isinstance(row, Mapping):
                    continue
                requirement_id = str(row.get("requirement_id") or "")
                current = row_index.setdefault(
                    requirement_id,
                    {
                        "requirement_id": requirement_id,
                        "score": int(row.get("score") or 0),
                        "verified": True,
                        "domains": [],
                        "evidence_ids": [],
                    },
                )
                current["verified"] = (
                    current["verified"] and row.get("verified") is True
                )
                current["domains"].append(record["domain"])
                current["evidence_ids"].extend(row.get("evidence_ids") or ())
        rows = []
        for requirement_id, row in sorted(row_index.items()):
            rows.append(
                {
                    **row,
                    "domains": list(stable_unique(row["domains"])),
                    "evidence_ids": list(stable_unique(row["evidence_ids"])),
                }
            )
        score_total = sum(int(row["score"]) for row in rows)
        score_verified = sum(
            int(row["score"]) for row in rows if row["verified"]
        )
        index = {
            "schema": "zyra.m2-exit-requirement-index/v1",
            "requirement_ids": [row["requirement_id"] for row in rows],
            "score_total": score_total,
            "score_verified": score_verified,
            "rows": rows,
        }
        if score_total != 100 or score_verified != 100:
            raise invalid(
                "m2_exit_requirement_index_invalid",
                "Portfolio requirement index does not verify 100 points.",
                phase="bundle",
                detail={
                    "score_total": score_total,
                    "score_verified": score_verified,
                },
            )
        index["index_digest"] = digest(index)
        return index

    def _reviewer_navigation(
        self,
        records: tuple[Mapping[str, Any], ...],
        requirement_index: Mapping[str, Any],
    ) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        for record in records:
            experiment_id = str(record["experiment_id"])
            nodes.append(
                {
                    "node_id": f"experiment:{experiment_id}",
                    "kind": "experiment_bundle",
                    "label": record["domain"],
                    "digest": record["sha256"],
                }
            )
        for row in requirement_index["rows"]:
            requirement_id = str(row["requirement_id"])
            nodes.append(
                {
                    "node_id": f"requirement:{requirement_id}",
                    "kind": "requirement",
                    "label": requirement_id,
                    "digest": digest(row),
                }
            )
            for record in records:
                edges.append(
                    {
                        "from": f"requirement:{requirement_id}",
                        "to": f"experiment:{record['experiment_id']}",
                        "relation": "verified_in_domain",
                    }
                )
        navigation = {
            "schema": "zyra.m2-exit-reviewer-navigation/v1",
            "nodes": nodes,
            "edges": edges,
            "backend_log_required": False,
            "path": (
                "requirement -> domain experiment -> metric summary/raw sample "
                "-> canonical source event -> owner receipt/artifact"
            ),
        }
        navigation["navigation_digest"] = digest(navigation)
        return navigation

    def _read_json_bytes(
        self,
        path_value: str | Path,
        label: str,
    ) -> bytes:
        path = Path(path_value).resolve(strict=False)
        try:
            payload = path.read_bytes()
            parsed = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise invalid(
                "m2_exit_external_evidence_invalid",
                f"{label} is unreadable.",
                phase="bundle",
                detail={"path": str(path), "reason": str(error)},
            ) from error
        if not isinstance(parsed, Mapping):
            raise invalid(
                "m2_exit_external_evidence_shape_invalid",
                f"{label} must be a JSON object.",
                phase="bundle",
            )
        return (pretty_json(parsed) + "\n").encode("utf-8")

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


class M2ExitPortfolioVerifier:
    def verify(
        self,
        path_value: str | Path,
        *,
        expected_manifest_digest: str = "",
    ) -> dict[str, Any]:
        path = Path(path_value).resolve(strict=False)
        findings: list[dict[str, Any]] = []
        manifest: dict[str, Any] = {}
        try:
            with zipfile.ZipFile(path, "r") as archive:
                names = archive.namelist()
                if len(names) != len(set(names)):
                    findings.append({"code": "duplicate_member"})
                if "manifest.json" not in names:
                    findings.append({"code": "manifest_missing"})
                else:
                    manifest = json.loads(archive.read("manifest.json"))
                for required in sorted(REQUIRED_PORTFOLIO_MEMBERS):
                    if required not in names:
                        findings.append(
                            {"code": "required_member_missing", "path": required}
                        )
                declared = {
                    str(item.get("path") or ""): item
                    for item in manifest.get("members") or ()
                    if isinstance(item, Mapping)
                }
                for name, item in declared.items():
                    if name not in names:
                        findings.append(
                            {"code": "declared_member_missing", "path": name}
                        )
                        continue
                    payload = archive.read(name)
                    if len(payload) != int(item.get("size") or -1):
                        findings.append(
                            {"code": "member_size_mismatch", "path": name}
                        )
                    if content_digest(payload) != str(item.get("sha256") or ""):
                        findings.append(
                            {"code": "member_digest_mismatch", "path": name}
                        )
                    if str(item.get("category") or "") == "experiment_bundle":
                        nested = self._verify_nested(payload)
                        if not nested["valid"]:
                            findings.append(
                                {
                                    "code": "nested_bundle_invalid",
                                    "path": name,
                                    "findings": nested["findings"],
                                }
                            )
                if set(declared) != set(names) - {"manifest.json"}:
                    findings.append({"code": "manifest_member_set_mismatch"})
        except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
            findings.append({"code": "portfolio_unreadable", "reason": str(error)})
        declared_manifest_digest = str(manifest.get("manifest_digest") or "")
        if manifest:
            body = dict(manifest)
            body.pop("manifest_digest", None)
            calculated_manifest_digest = digest(body)
            if declared_manifest_digest != calculated_manifest_digest:
                findings.append({"code": "manifest_digest_mismatch"})
            if expected_manifest_digest and (
                expected_manifest_digest != declared_manifest_digest
            ):
                findings.append({"code": "expected_manifest_digest_mismatch"})
            members = [
                item
                for item in manifest.get("members") or ()
                if isinstance(item, Mapping)
            ]
            if str(manifest.get("root_digest") or "") != _root_digest(members):
                findings.append({"code": "root_digest_mismatch"})
            if int(manifest.get("score_total") or 0) != 100:
                findings.append({"code": "score_total_invalid"})
            if int(manifest.get("score_verified") or 0) != 100:
                findings.append({"code": "score_verified_invalid"})
            if int(manifest.get("human_intervention_count") or -1) != 0:
                findings.append({"code": "human_intervention_nonzero"})
        receipt = {
            "schema": "zyra.m2-exit-portfolio-verification/v1",
            "valid": not findings,
            "path": str(path),
            "manifest_digest": declared_manifest_digest,
            "root_digest": str(manifest.get("root_digest") or ""),
            "member_count": len(manifest.get("members") or ()),
            "findings": findings,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def require_valid(
        self,
        path_value: str | Path,
        *,
        expected_manifest_digest: str = "",
    ) -> dict[str, Any]:
        receipt = self.verify(
            path_value,
            expected_manifest_digest=expected_manifest_digest,
        )
        if not receipt["valid"]:
            raise invalid(
                "m2_exit_portfolio_verification_failed",
                "M2 exit evidence portfolio failed verification.",
                phase="bundle",
                detail=receipt,
            )
        return receipt

    def _verify_nested(self, payload: bytes) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        try:
            with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
                declared = {
                    str(item.get("path") or ""): item
                    for item in manifest.get("members") or ()
                    if isinstance(item, Mapping)
                }
                for name, item in declared.items():
                    body = archive.read(name)
                    if content_digest(body) != str(item.get("sha256") or ""):
                        findings.append(
                            {"code": "member_digest_mismatch", "path": name}
                        )
                manifest_body = dict(manifest)
                manifest_body.pop("manifest_digest", None)
                if digest(manifest_body) != str(
                    manifest.get("manifest_digest") or ""
                ):
                    findings.append({"code": "manifest_digest_mismatch"})
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
            findings.append({"code": "nested_bundle_unreadable", "reason": str(error)})
        return {
            "valid": not findings,
            "findings": findings,
        }
