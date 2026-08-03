from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .ledger_audit import InternalizationLedgerAuditor, LedgerAuditReport
from .ledger_linecount import EffectiveLineCountReport, build_line_count_report
from .ledger_line_buckets import LineBucketReport, bucket_line_count_report
from .ledger_matrix import UnitMatrixReport, build_unit_matrix
from .ledger_models import to_jsonable
from .ledger_policy_matrix import PolicyMatrixReport, build_policy_matrix_report
from .ledger_reports import UnitReadinessReport, build_unit_readiness_report
from .ledger_source_scan import SourceScanReport, build_source_scan_report
from .ledger_store import InternalizationLedger


class GateSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class GateCode(StrEnum):
    AUDIT_FAILED = "AUDIT_FAILED"
    AUDIT_HAS_WARNINGS = "AUDIT_HAS_WARNINGS"
    LINE_COUNT_SHORTFALL = "LINE_COUNT_SHORTFALL"
    LINE_COUNT_BASE_MISSING = "LINE_COUNT_BASE_MISSING"
    EXCLUDED_DATA_DOMINATES_DIFF = "EXCLUDED_DATA_DOMINATES_DIFF"
    LINE_BUCKET_FAILED = "LINE_BUCKET_FAILED"
    READINESS_BLOCKED = "READINESS_BLOCKED"
    POLICY_MATRIX_FAILED = "POLICY_MATRIX_FAILED"
    UNIT_LEDGER_COVERAGE_MISSING = "UNIT_LEDGER_COVERAGE_MISSING"
    SOURCE_DEPENDENCY_FORBIDDEN = "SOURCE_DEPENDENCY_FORBIDDEN"
    SOURCE_EVIDENCE_MISSING = "SOURCE_EVIDENCE_MISSING"
    SOURCE_TARGET_MISSING = "SOURCE_TARGET_MISSING"
    SNAPSHOT_REQUIRED = "SNAPSHOT_REQUIRED"


@dataclass(slots=True)
class GateFinding:
    code: GateCode
    severity: GateSeverity
    message: str
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class CompletionGateReport:
    owner_unit: str
    ok: bool
    disposition: str
    findings: list[GateFinding]
    audit: dict[str, Any]
    line_count: dict[str, Any] | None
    line_buckets: dict[str, Any] | None
    readiness: dict[str, Any]
    matrix: dict[str, Any]
    policy_matrix: dict[str, Any]
    source_scan: dict[str, Any] | None = None

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == GateSeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == GateSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == GateSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


def build_completion_gate_report(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    owner_unit: str,
    base_commit: str = "",
    cached: bool = False,
    minimum_effective_lines: int = 0,
    source_root: Path | None = None,
    include_source_scan: bool = False,
    require_snapshot: bool = False,
) -> CompletionGateReport:
    audit = InternalizationLedgerAuditor(project_root, strict=True).audit(ledger)
    line_count = (
        build_line_count_report(
            project_root,
            base=base_commit,
            cached=cached,
            minimum_effective_lines=minimum_effective_lines,
        )
        if base_commit
        else None
    )
    line_buckets = bucket_line_count_report(project_root, line_count) if line_count else None
    readiness = build_unit_readiness_report(
        project_root,
        ledger,
        owner_unit=owner_unit,
        audit_report=audit,
        line_count_report=line_count,
    )
    matrix = build_unit_matrix(ledger)
    policy_matrix = build_policy_matrix_report(ledger, owner_unit=owner_unit)
    source_scan = (
        build_source_scan_report(
            project_root,
            source_root or project_root / "provenance",
            ledger,
        )
        if include_source_scan
        else None
    )
    findings: list[GateFinding] = []
    findings.extend(_audit_gate_findings(audit))
    if line_count is not None:
        findings.extend(_line_count_gate_findings(line_count))
    if line_buckets is not None:
        findings.extend(_line_bucket_gate_findings(line_buckets))
    elif minimum_effective_lines > 0:
        findings.append(
            GateFinding(
                code=GateCode.LINE_COUNT_BASE_MISSING,
                severity=GateSeverity.ERROR,
                message="Completion gate has a minimum effective line-count requirement but no base commit was provided.",
                remediation="Pass base_commit/--base so seed and inventory data can be excluded from the diff.",
                metadata={"minimum_effective_lines": minimum_effective_lines},
            )
        )
    findings.extend(_readiness_gate_findings(readiness))
    findings.extend(_policy_matrix_gate_findings(policy_matrix))
    findings.extend(_matrix_gate_findings(matrix, owner_unit))
    if source_scan is not None:
        findings.extend(_source_scan_gate_findings(source_scan))
    if require_snapshot:
        findings.append(
            GateFinding(
                code=GateCode.SNAPSHOT_REQUIRED,
                severity=GateSeverity.INFO,
                message="Completion gate was configured to require a snapshot; create one after the final audit.",
                remediation="Run ledger CLI snapshot or POST /ledger/snapshots before closing the execution unit.",
            )
        )
    ok = not any(finding.severity in {GateSeverity.ERROR, GateSeverity.BLOCKER} for finding in findings)
    disposition = "passing" if ok and not any(finding.severity == GateSeverity.WARNING for finding in findings) else "warning" if ok else "failing"
    return CompletionGateReport(
        owner_unit=owner_unit,
        ok=ok,
        disposition=disposition,
        findings=findings,
        audit=audit.to_dict(),
        line_count=line_count.to_dict() if line_count else None,
        line_buckets=line_buckets.to_dict() if line_buckets else None,
        readiness=readiness.to_dict(),
        matrix=matrix.to_dict(),
        policy_matrix=policy_matrix.to_dict(),
        source_scan=source_scan.to_dict() if source_scan else None,
    )


def assert_completion_gate(report: CompletionGateReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code}: {finding.message}"
        for finding in report.findings
        if finding.severity in {GateSeverity.ERROR, GateSeverity.BLOCKER}
    )
    raise AssertionError(f"Completion gate failed for {report.owner_unit}:\n{formatted}")


def _audit_gate_findings(report: LedgerAuditReport) -> list[GateFinding]:
    findings: list[GateFinding] = []
    if not report.ok:
        findings.append(
            GateFinding(
                code=GateCode.AUDIT_FAILED,
                severity=GateSeverity.BLOCKER if report.blocker_count else GateSeverity.ERROR,
                message=f"Ledger audit failed with {report.error_count} errors and {report.blocker_count} blockers.",
                remediation="Fix audit findings before closing the execution unit.",
                metadata={"error_count": report.error_count, "blocker_count": report.blocker_count},
            )
        )
    elif report.warning_count:
        findings.append(
            GateFinding(
                code=GateCode.AUDIT_HAS_WARNINGS,
                severity=GateSeverity.WARNING,
                message=f"Ledger audit has {report.warning_count} warnings.",
                remediation="Warnings may be acceptable for planned entries, but must be reviewed in the execution record.",
                metadata={"warning_count": report.warning_count},
            )
        )
    return findings


def _line_count_gate_findings(report: EffectiveLineCountReport) -> list[GateFinding]:
    findings: list[GateFinding] = []
    if not report.ok:
        findings.append(
            GateFinding(
                code=GateCode.LINE_COUNT_SHORTFALL,
                severity=GateSeverity.BLOCKER,
                message=(
                    f"Effective line count {report.effective_added} is below minimum "
                    f"{report.minimum_effective_lines}; raw added was {report.raw_added}."
                ),
                remediation="Add real implementation/test/runtime code or revise the unit only with a strong documented reason.",
                metadata={
                    "effective_added": report.effective_added,
                    "minimum_effective_lines": report.minimum_effective_lines,
                    "shortfall": report.shortfall,
                    "excluded_added": report.excluded_added,
                },
            )
        )
    if report.raw_added and report.excluded_added / max(report.raw_added, 1) > 0.5:
        findings.append(
            GateFinding(
                code=GateCode.EXCLUDED_DATA_DOMINATES_DIFF,
                severity=GateSeverity.WARNING,
                message="Excluded seed/inventory/data lines dominate the raw diff.",
                remediation="Report excluded data separately and do not use raw line counts as completion evidence.",
                metadata={"raw_added": report.raw_added, "excluded_added": report.excluded_added},
            )
        )
    return findings


def _line_bucket_gate_findings(report: LineBucketReport) -> list[GateFinding]:
    findings: list[GateFinding] = []
    if not report.ok:
        findings.append(
            GateFinding(
                code=GateCode.LINE_BUCKET_FAILED,
                severity=GateSeverity.BLOCKER,
                message=(
                    f"Bucketed effective line count {report.bucket_effective_added} is below minimum "
                    f"{report.minimum_effective_lines} or has blocking bucket findings."
                ),
                remediation="Use production/script/test behavior code; report data/vendor/mock/generated buckets separately.",
                metadata={
                    "bucket_effective_added": report.bucket_effective_added,
                    "minimum_effective_lines": report.minimum_effective_lines,
                    "shortfall": report.shortfall,
                    "production_added": report.production_added,
                    "test_added": report.test_added,
                    "script_added": report.script_added,
                    "vendor_like_added": report.vendor_like_added,
                    "mock_fixture_added": report.mock_fixture_added,
                    "data_added": report.data_added,
                    "generated_added": report.generated_added,
                },
            )
        )
    return findings


def _readiness_gate_findings(report: UnitReadinessReport) -> list[GateFinding]:
    findings: list[GateFinding] = []
    if report.blocked_entries:
        findings.append(
            GateFinding(
                code=GateCode.READINESS_BLOCKED,
                severity=GateSeverity.ERROR,
                message=f"{report.blocked_entries} ledger entries have blocking readiness issues.",
                remediation="Resolve readiness blockers or downgrade entries before closing the unit.",
                metadata=report.debt,
            )
        )
    return findings


def _policy_matrix_gate_findings(report: PolicyMatrixReport) -> list[GateFinding]:
    if report.ok:
        return []
    blocking = [finding.to_dict() for finding in report.findings if finding.blocking]
    return [
        GateFinding(
            code=GateCode.POLICY_MATRIX_FAILED,
            severity=GateSeverity.BLOCKER if report.blocker_count else GateSeverity.ERROR,
            message=(
                f"Policy matrix rejected {len(blocking)} ledger status/strategy combinations "
                f"for {report.owner_unit}."
            ),
            remediation=(
                "Downgrade source-pool/vendor entries or add concrete Zyra-owned runtime, "
                "main-path, test, and line-count evidence before closing the unit."
            ),
            metadata={
                "owner_unit": report.owner_unit,
                "error_count": report.error_count,
                "blocker_count": report.blocker_count,
                "blocking_findings": blocking[:20],
            },
        )
    ]


def _matrix_gate_findings(report: UnitMatrixReport, owner_unit: str) -> list[GateFinding]:
    findings: list[GateFinding] = []
    row = next((item for item in report.rows if item.owner_unit == owner_unit), None)
    if row is not None and not row.coverage_ok:
        findings.append(
            GateFinding(
                code=GateCode.UNIT_LEDGER_COVERAGE_MISSING,
                severity=GateSeverity.ERROR,
                message=f"{owner_unit} has no ledger records.",
                remediation="Add planned source-to-target records before executing the migration unit.",
            )
        )
    return findings


def _source_scan_gate_findings(report: SourceScanReport) -> list[GateFinding]:
    findings: list[GateFinding] = []
    if report.forbidden_hits:
        findings.append(
            GateFinding(
                code=GateCode.SOURCE_DEPENDENCY_FORBIDDEN,
                severity=GateSeverity.BLOCKER,
                message=f"Found {len(report.forbidden_hits)} forbidden parent source repository dependencies.",
                remediation="Move source code into zyra or use productized vendor runtime boundaries.",
                metadata={"hits": [hit.to_dict() for hit in report.forbidden_hits[:20]]},
            )
        )
    if report.missing_source_count:
        findings.append(
            GateFinding(
                code=GateCode.SOURCE_EVIDENCE_MISSING,
                severity=GateSeverity.WARNING,
                message=f"{report.missing_source_count} source evidence paths were not found under source_root.",
                remediation="Refresh source evidence with the source mapper, or explain why the source path is conceptual.",
                metadata={"missing_source_count": report.missing_source_count},
            )
        )
    if report.missing_target_count:
        findings.append(
            GateFinding(
                code=GateCode.SOURCE_TARGET_MISSING,
                severity=GateSeverity.ERROR,
                message=f"{report.missing_target_count} materialized or connected target paths are missing.",
                remediation="Create the target path or downgrade the ledger lifecycle/status before closing the unit.",
                metadata={"missing_target_count": report.missing_target_count},
            )
        )
    return findings
