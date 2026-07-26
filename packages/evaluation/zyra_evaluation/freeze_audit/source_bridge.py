from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    AuditSection,
    Disposition,
    EvidencePointer,
    Finding,
    RuleSwitches,
    Severity,
    deduplicate_findings,
    finding,
    section,
    stable_digest,
)


SOURCE_RECEIPT_SCHEMA = "zyra.source-custody-audit-receipt/v1"
RISK_GROUP_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "semantic_port": (
        "source_similarity",
        "structural_capability",
        "vendor_like_shape",
        "source_pool",
        "mechanical",
        "adapter_core",
    ),
    "opaque_bundle": (
        "opaque",
        "minified",
        "binary",
        "archive",
        "generated_runtime",
    ),
    "dynamic_download": (
        "download",
        "dynamic_install",
        "dynamic_import",
        "external_sdk",
        "external_process",
    ),
    "upstream_boundary": (
        "parent_source",
        "root_source",
        "editable",
        "npm_link",
        "source_repo",
        "openclaw",
    ),
}
REQUIRED_SOURCE_RULES = (
    "catalog",
    "roles",
    "dependencies",
    "processes",
    "custody",
    "langgraph",
    "opaque",
    "source_specific",
)


@dataclass(frozen=True, slots=True)
class SourceRiskRecord:
    code: str
    message: str
    severity: str
    blocking: bool
    capability: str
    path: str
    owner_unit: str
    disposition: str
    default_path_impact: str
    remediation: str
    categories: tuple[str, ...]
    fingerprint: str
    attributes: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "blocking": self.blocking,
            "capability": self.capability,
            "path": self.path,
            "owner_unit": self.owner_unit,
            "disposition": self.disposition,
            "default_path_impact": self.default_path_impact,
            "remediation": self.remediation,
            "categories": list(self.categories),
            "fingerprint": self.fingerprint,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class SourceBridgeResult:
    source_receipt_digest: str
    records: tuple[SourceRiskRecord, ...]
    section: AuditSection


class SourceRiskBridge:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()

    def audit(
        self,
        source_result: Any | None = None,
        *,
        receipt_path: str | Path | None = None,
        expected_revision: str = "",
    ) -> SourceBridgeResult:
        if not self.switches.source_risks:
            return SourceBridgeResult(
                source_receipt_digest="",
                records=(),
                section=section(
                    "source_risk_bridge",
                    metrics={"rule_enabled": False},
                ),
            )
        payload, input_path, input_error = self._payload(
            source_result,
            receipt_path,
        )
        findings: list[Finding] = []
        evidence: list[EvidencePointer] = []
        if input_error:
            findings.append(
                self._blocker(
                    "source_custody_input_unavailable",
                    input_error,
                    remediation=(
                        "Run the protected M3-S01A-01 source custody auditor on "
                        "the same target revision or provide its full receipt."
                    ),
                )
            )
            return SourceBridgeResult(
                source_receipt_digest="",
                records=(),
                section=section(
                    "source_risk_bridge",
                    metrics={"rule_enabled": True, "input_valid": False},
                    findings=findings,
                ),
            )
        assert payload is not None
        schema = str(payload.get("schema", ""))
        if schema != SOURCE_RECEIPT_SCHEMA:
            findings.append(
                self._blocker(
                    "source_custody_schema_invalid",
                    (
                        f"Source custody receipt schema is {schema!r}; expected "
                        f"{SOURCE_RECEIPT_SCHEMA!r}."
                    ),
                    remediation="Regenerate the receipt with the protected auditor.",
                )
            )
        revision = str(payload.get("revision", "")).casefold()
        if expected_revision and revision != expected_revision.casefold():
            findings.append(
                self._blocker(
                    "source_custody_revision_mismatch",
                    (
                        "Source risk evidence was produced for a different "
                        f"revision: expected={expected_revision}, actual={revision}."
                    ),
                    remediation=(
                        "Run source custody against the exact implementation target."
                    ),
                    attributes={
                        "expected_revision": expected_revision,
                        "actual_revision": revision,
                    },
                )
            )
        config = payload.get("config")
        rule_switches = (
            config.get("rule_switches")
            if isinstance(config, Mapping)
            and isinstance(config.get("rule_switches"), Mapping)
            else {}
        )
        for name in REQUIRED_SOURCE_RULES:
            if rule_switches.get(name) is True:
                continue
            findings.append(
                self._blocker(
                    "source_custody_rule_disabled",
                    f"Required source custody rule was not enabled: {name}.",
                    remediation=(
                        "Enable all source/dependency/custody rules for release evidence."
                    ),
                    attributes={"rule": name},
                )
            )
        records = self._records(payload)
        categories = Counter(
            category for record in records for category in record.categories
        )
        risk_summary = payload.get("risk_summary")
        if not isinstance(risk_summary, Mapping):
            findings.append(
                self._blocker(
                    "source_custody_risk_summary_missing",
                    "Source custody receipt has no structured risk summary.",
                    remediation="Regenerate a full receipt rather than a compact summary.",
                )
            )
            risk_summary = {}
        required_summary_fields = {
            "similarity_pairs",
            "source_specific_hits",
            "langgraph_forbidden_hits",
            "opaque_runtime_hits",
            "digest",
        }
        for name in sorted(required_summary_fields - set(risk_summary)):
            findings.append(
                self._blocker(
                    "source_custody_risk_field_missing",
                    f"Source custody risk summary omits field {name!r}.",
                    remediation=(
                        "Use an M3-S01A-01 receipt that includes similarity, "
                        "opaque, source-specific and LangGraph audit output."
                    ),
                    attributes={"field": name},
                )
            )
        repository_manifest = payload.get("repository_manifest")
        if not isinstance(repository_manifest, Sequence) or isinstance(
            repository_manifest, (str, bytes, bytearray)
        ):
            findings.append(
                self._blocker(
                    "source_custody_manifest_missing",
                    "Source custody receipt has no repository artifact manifest.",
                    remediation="Include the vendor-aware repository manifest.",
                )
            )
            repository_manifest = ()
        else:
            findings.extend(self._manifest_cross_check(repository_manifest, records))
        work_queue = payload.get("work_queue")
        if not isinstance(work_queue, Sequence) or isinstance(
            work_queue, (str, bytes, bytearray)
        ):
            findings.append(
                self._blocker(
                    "source_custody_work_queue_missing",
                    "Source custody receipt has no normalized remediation queue.",
                    remediation="Include the deterministic M3-01B work queue.",
                )
            )
            work_queue = ()
        receipt_digest = str(payload.get("receipt_digest", ""))
        if not receipt_digest.startswith("sha256:"):
            findings.append(
                self._blocker(
                    "source_custody_digest_missing",
                    "Source custody receipt lacks a SHA-256 receipt digest.",
                    remediation="Regenerate checksum-bound source evidence.",
                )
            )
        input_valid = not any(item.blocking for item in findings)
        findings.extend(self._record_findings(records))
        evidence.append(
            EvidencePointer(
                kind="source_custody_receipt",
                path=input_path,
                digest=receipt_digest,
                attributes={
                    "revision": revision,
                    "records": len(records),
                    "work_items": len(work_queue),
                    "categories": dict(sorted(categories.items())),
                },
            )
        )
        metrics = {
            "rule_enabled": True,
            "input_valid": input_valid,
            "source_revision": revision,
            "source_receipt_digest": receipt_digest,
            "risk_records": len(records),
            "blocking_risk_records": sum(item.blocking for item in records),
            "risk_categories": dict(sorted(categories.items())),
            "work_queue_items": len(work_queue),
            "repository_manifest_items": len(repository_manifest),
            "similarity_pairs": int(risk_summary.get("similarity_pairs", 0) or 0),
            "opaque_runtime_hits": int(
                risk_summary.get("opaque_runtime_hits", 0) or 0
            ),
            "langgraph_forbidden_hits": int(
                risk_summary.get("langgraph_forbidden_hits", 0) or 0
            ),
            "bridge_digest": stable_digest(
                {
                    "source_receipt_digest": receipt_digest,
                    "records": [item.to_dict() for item in records],
                }
            ),
        }
        return SourceBridgeResult(
            source_receipt_digest=receipt_digest,
            records=records,
            section=section(
                "source_risk_bridge",
                metrics=metrics,
                findings=findings,
                evidence=evidence,
                records=(item.to_dict() for item in records),
            ),
        )

    def _payload(
        self,
        source_result: Any | None,
        receipt_path: str | Path | None,
    ) -> tuple[Mapping[str, Any] | None, str, str]:
        if source_result is not None:
            try:
                payload = source_result.receipt()
            except (AttributeError, TypeError, ValueError) as exc:
                return None, "<in-memory>", f"Source result cannot emit receipt: {exc}"
            if not isinstance(payload, Mapping):
                return None, "<in-memory>", "Source result receipt is not an object."
            return payload, "<in-memory>", ""
        if receipt_path is None:
            return (
                None,
                "",
                "No in-memory source custody result or receipt path was provided.",
            )
        selected = Path(receipt_path)
        if not selected.is_absolute():
            selected = self.root / selected
        selected = selected.resolve(strict=False)
        try:
            payload = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, selected.as_posix(), f"Source receipt cannot be read: {exc}"
        if not isinstance(payload, Mapping):
            return None, selected.as_posix(), "Source receipt root is not an object."
        return payload, selected.as_posix(), ""

    def _records(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[SourceRiskRecord, ...]:
        records: list[SourceRiskRecord] = []
        sections = payload.get("sections")
        if not isinstance(sections, Sequence) or isinstance(
            sections, (str, bytes, bytearray)
        ):
            return ()
        for section_payload in sections:
            if not isinstance(section_payload, Mapping):
                continue
            raw_findings = section_payload.get("findings")
            if not isinstance(raw_findings, Sequence) or isinstance(
                raw_findings, (str, bytes, bytearray)
            ):
                continue
            for raw in raw_findings:
                if not isinstance(raw, Mapping):
                    continue
                code = str(raw.get("code", ""))
                categories = tuple(
                    category
                    for category, patterns in RISK_GROUP_PATTERNS.items()
                    if any(pattern in code.casefold() for pattern in patterns)
                )
                if not categories:
                    continue
                records.append(
                    SourceRiskRecord(
                        code=code,
                        message=str(
                            raw.get(
                                "message",
                                f"Source custody risk {code} remains unresolved.",
                            )
                        ),
                        severity=str(raw.get("severity", "warning")),
                        blocking=bool(raw.get("blocking", False)),
                        capability=str(raw.get("capability", "")),
                        path=str(raw.get("path", "")),
                        owner_unit=str(raw.get("owner_unit", "M3-01B")),
                        disposition=str(raw.get("disposition", "track")),
                        default_path_impact=str(
                            raw.get("default_path_impact", "")
                        ),
                        remediation=str(raw.get("remediation", "")),
                        categories=categories,
                        fingerprint=str(raw.get("fingerprint", "")),
                        attributes=(
                            dict(raw.get("attributes", {}))
                            if isinstance(raw.get("attributes"), Mapping)
                            else {}
                        ),
                    )
                )
        unique: dict[tuple[str, str, str], SourceRiskRecord] = {}
        for item in records:
            unique.setdefault((item.code, item.path, item.fingerprint), item)
        return tuple(
            sorted(
                unique.values(),
                key=lambda item: (
                    not item.blocking,
                    item.code,
                    item.path,
                    item.fingerprint,
                ),
            )
        )

    @staticmethod
    def _record_findings(
        records: Sequence[SourceRiskRecord],
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        for record in records:
            try:
                severity = Severity(record.severity)
            except ValueError:
                severity = (
                    Severity.BLOCKER if record.blocking else Severity.WARNING
                )
            try:
                disposition = Disposition(record.disposition)
            except ValueError:
                disposition = (
                    Disposition.BLOCK_RELEASE
                    if record.blocking
                    else Disposition.TRACK
                )
            if record.blocking and not (
                severity is Severity.BLOCKER
                or disposition is Disposition.BLOCK_RELEASE
            ):
                disposition = Disposition.BLOCK_RELEASE
            attributes = dict(record.attributes)
            attributes.update(
                {
                    "source_risk_categories": list(record.categories),
                    "source_fingerprint": record.fingerprint,
                    "bridged_from": "M3-S01A-01",
                }
            )
            findings.append(
                finding(
                    record.code,
                    record.message,
                    "source_risks",
                    severity=severity,
                    domain=record.capability,
                    path=record.path,
                    owner_unit=record.owner_unit or "M3-01B",
                    disposition=disposition,
                    default_path_impact=record.default_path_impact,
                    remediation=record.remediation,
                    attributes=attributes,
                )
            )
        return deduplicate_findings(findings)

    def _manifest_cross_check(
        self,
        manifest: Sequence[Any],
        records: Sequence[SourceRiskRecord],
    ) -> tuple[Finding, ...]:
        record_paths = {item.path for item in records}
        findings: list[Finding] = []
        for raw in manifest:
            if not isinstance(raw, Mapping):
                continue
            path = str(raw.get("path", ""))
            production = path.startswith(("apps/", "packages/", "scripts/", "skills/"))
            opaque = (
                bool(raw.get("binary"))
                or bool(raw.get("minified"))
                or bool(raw.get("generated"))
                or bool(raw.get("vendor_like"))
                or str(raw.get("kind", "")) == "archive"
            )
            if not production or not opaque or path in record_paths:
                continue
            findings.append(
                self._blocker(
                    "source_risk_manifest_artifact_unclassified",
                    (
                        "Production opaque/generated/vendor-like artifact has no "
                        f"source custody finding or explicit disposition: {path}."
                    ),
                    path=path,
                    remediation=(
                        "Classify the artifact as runtime asset, generated output, "
                        "vendor/source pool, opaque dependency, or remove it."
                    ),
                    attributes={
                        "kind": raw.get("kind"),
                        "binary": raw.get("binary"),
                        "minified": raw.get("minified"),
                        "generated": raw.get("generated"),
                        "vendor_like": raw.get("vendor_like"),
                    },
                )
            )
        return deduplicate_findings(findings)

    @staticmethod
    def _blocker(
        code: str,
        message: str,
        *,
        path: str = "",
        remediation: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> Finding:
        return finding(
            code,
            message,
            "source_risks",
            severity=Severity.BLOCKER,
            path=path,
            owner_unit="M3-01B",
            disposition=Disposition.BLOCK_RELEASE,
            default_path_impact=(
                "Semantic port, upstream custody, opaque runtime or source "
                "similarity risk is absent from freeze evidence."
            ),
            remediation=remediation,
            attributes=dict(attributes or {}),
        )
