from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .ledger_acceptance import AcceptanceReport, build_acceptance_report
from .ledger_audit import InternalizationLedgerAuditor
from .ledger_boundary import CleanBoundaryReport, build_clean_boundary_report
from .ledger_cleanroom import CleanroomReport, build_cleanroom_report
from .ledger_evidence_graph import EvidenceGraphReport, build_evidence_graph_report
from .ledger_line_buckets import BucketRisk, LineBucketReport, bucket_line_count_report
from .ledger_linecount import EffectiveLineCountReport, build_line_count_report
from .ledger_mutation_consistency import MutationConsistencyReport, build_mutation_consistency_report
from .ledger_reachability import ReachabilityReport, build_reachability_report
from .ledger_schema_contract import LedgerSchemaContractReport, build_schema_contract_report
from .ledger_semantics import SemanticEffectReport, build_semantic_effect_report
from .ledger_state_custody import StateCustodyReport, build_state_custody_report
from .ledger_store import InternalizationLedger
from .ledger_test_quality import TestQualityReport, build_test_quality_report
from .ledger_models import to_jsonable


class ReviewSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ReviewStatus(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    FAILING = "failing"
    BLOCKED = "blocked"
    NOT_RUN = "not_run"


class ReviewCode(StrEnum):
    UNIT_OBJECTIVES_COVERED = "UNIT_OBJECTIVES_COVERED"
    UNIT_OBJECTIVE_MISSING = "UNIT_OBJECTIVE_MISSING"
    SCHEMA_CONTRACT_PASSING = "SCHEMA_CONTRACT_PASSING"
    SCHEMA_CONTRACT_FAILED = "SCHEMA_CONTRACT_FAILED"
    AUDIT_PASSING = "AUDIT_PASSING"
    AUDIT_FAILED = "AUDIT_FAILED"
    EVIDENCE_GRAPH_PASSING = "EVIDENCE_GRAPH_PASSING"
    EVIDENCE_GRAPH_FAILED = "EVIDENCE_GRAPH_FAILED"
    STATE_CUSTODY_PASSING = "STATE_CUSTODY_PASSING"
    STATE_CUSTODY_FAILED = "STATE_CUSTODY_FAILED"
    MUTATION_CONSISTENCY_PASSING = "MUTATION_CONSISTENCY_PASSING"
    MUTATION_CONSISTENCY_FAILED = "MUTATION_CONSISTENCY_FAILED"
    REACHABILITY_PASSING = "REACHABILITY_PASSING"
    REACHABILITY_FAILED = "REACHABILITY_FAILED"
    BOUNDARY_PASSING = "BOUNDARY_PASSING"
    BOUNDARY_FAILED = "BOUNDARY_FAILED"
    CLEANROOM_PASSING = "CLEANROOM_PASSING"
    CLEANROOM_FAILED = "CLEANROOM_FAILED"
    TEST_QUALITY_PASSING = "TEST_QUALITY_PASSING"
    TEST_QUALITY_FAILED = "TEST_QUALITY_FAILED"
    SEMANTIC_EFFECTS_PASSING = "SEMANTIC_EFFECTS_PASSING"
    SEMANTIC_EFFECTS_FAILED = "SEMANTIC_EFFECTS_FAILED"
    ACCEPTANCE_PASSING = "ACCEPTANCE_PASSING"
    ACCEPTANCE_FAILED = "ACCEPTANCE_FAILED"
    LINE_COUNT_PASSING = "LINE_COUNT_PASSING"
    LINE_COUNT_FAILED = "LINE_COUNT_FAILED"
    MAIN_PATH_EVIDENCE_READY = "MAIN_PATH_EVIDENCE_READY"
    MAIN_PATH_EVIDENCE_MISSING = "MAIN_PATH_EVIDENCE_MISSING"


@dataclass(slots=True)
class UnitObjective:
    objective_id: str
    title: str
    required_reports: list[str]
    minimum_signals: list[str]
    failure_condition: str
    source_to_target_anchor: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class ReviewFinding:
    code: ReviewCode
    severity: ReviewSeverity
    message: str
    objective_id: str = ""
    report_name: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {ReviewSeverity.ERROR, ReviewSeverity.BLOCKER}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class ReviewEvidence:
    report_name: str
    ok: bool
    status: ReviewStatus
    summary: dict[str, Any]
    blocking_count: int = 0
    warning_count: int = 0
    runtime_entry: str = ""
    api_routes: list[str] = field(default_factory=list)
    cli_commands: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class ObjectiveReview:
    objective: UnitObjective
    status: ReviewStatus
    severity: ReviewSeverity
    evidence: list[str]
    missing_signals: list[str] = field(default_factory=list)
    blocking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class UnitReviewReport:
    ok: bool
    owner_unit: str
    base_commit: str
    minimum_effective_lines: int
    objectives: list[ObjectiveReview]
    evidence: list[ReviewEvidence]
    findings: list[ReviewFinding]
    reports: dict[str, Any]
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == ReviewSeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == ReviewSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == ReviewSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


def unit_01a_objectives() -> list[UnitObjective]:
    return [
        UnitObjective(
            objective_id="schema_contract",
            title="Ledger schema and transition contract are enforced",
            required_reports=["schema_contract", "mutation_consistency", "audit"],
            minimum_signals=["schema_contract.ok", "mutation_consistency.ok", "audit.ok"],
            failure_condition="Invalid entries can be persisted or lifecycle transitions can bypass policy.",
            source_to_target_anchor="0.4 source-to-target ledger schema/audit/control plane",
        ),
        UnitObjective(
            objective_id="anti_fake_line_count",
            title="Effective line-count buckets exclude vendor/source-pool/data/mock inflation",
            required_reports=["line_count", "line_buckets", "evidence_graph"],
            minimum_signals=["line_buckets.bucket_effective_added", "line_buckets.vendor_like_added"],
            failure_condition="Vendor runtime or generated inventories satisfy completion line counts.",
            source_to_target_anchor="0.4 strict internalization and anti-fake acceptance",
        ),
        UnitObjective(
            objective_id="main_path_reachability",
            title="Ledger/audit controls are reachable through API, CLI, event, and tests",
            required_reports=["reachability", "evidence_graph", "test_quality"],
            minimum_signals=["reachability.route_count", "reachability.cli_command_count", "test_quality.behavior_test_files"],
            failure_condition="Audit code is only importable and cannot be triggered by runtime routes or CLI.",
            source_to_target_anchor="0.4 runtime entry/API/CLI/event log wiring",
        ),
        UnitObjective(
            objective_id="state_custody",
            title="State persistence and mutation custody are Zyra-owned",
            required_reports=["state_custody", "mutation_consistency", "cleanroom"],
            minimum_signals=["state_custody.complete_claims", "mutation_consistency.passing_probes", "cleanroom.commands_to_run"],
            failure_condition="Ledger status or events are kept only in external sidecars/cache/fixtures.",
            source_to_target_anchor="0.4 event log/state custody/clean submission boundary",
        ),
        UnitObjective(
            objective_id="boundary_cleanroom",
            title="Clean submission boundary rejects parent source runtime dependencies",
            required_reports=["boundary", "cleanroom", "semantic_effects"],
            minimum_signals=["boundary.ok", "semantic_effects.ok"],
            failure_condition="A clean zyra copy still needs parent source repositories at runtime.",
            source_to_target_anchor="0.4 source repository isolation and no parent-directory runtime dependency",
        ),
        UnitObjective(
            objective_id="source_target_acceptance",
            title="Source-to-target coverage and acceptance are auditable per unit",
            required_reports=["acceptance", "evidence_graph", "schema_contract"],
            minimum_signals=["acceptance.criteria", "evidence_graph.target_impacts"],
            failure_condition="Ledger records cannot explain what was active, planned, vendored, or deferred.",
            source_to_target_anchor="0.4 source-to-target清单 and milestone internalization ledger",
        ),
    ]


def build_unit_review_report(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    owner_unit: str = "M1-01A",
    base_commit: str = "",
    cached: bool = False,
    minimum_effective_lines: int = 10_000,
    include_reports: bool = False,
    boundary_roots: Iterable[str] | None = ("apps", "packages", "scripts", "tests", "skills"),
) -> UnitReviewReport:
    audit = InternalizationLedgerAuditor(project_root, strict=True).audit(ledger)
    line_count = build_line_count_report(project_root, base=base_commit, cached=cached, minimum_effective_lines=minimum_effective_lines) if base_commit else None
    line_buckets = bucket_line_count_report(project_root, line_count) if line_count else None
    schema = build_schema_contract_report(ledger, owner_unit=owner_unit, include_entries=False)
    evidence_graph = build_evidence_graph_report(project_root, ledger, owner_unit=owner_unit, include_nodes=False)
    state_custody = build_state_custody_report(project_root)
    mutation = build_mutation_consistency_report(project_root)
    boundary = build_clean_boundary_report(project_root, include_tests=True, include_cache=False, scan_roots=boundary_roots)
    reachability = build_reachability_report(project_root, ledger, owner_unit=owner_unit, include_entries=False, strict_audit=False)
    cleanroom = build_cleanroom_report(project_root, ledger, include_source_scan=False)
    semantics = build_semantic_effect_report(project_root)
    test_quality = build_test_quality_report(project_root, ledger, owner_unit=owner_unit, include_entries=False)
    acceptance = build_acceptance_report(
        project_root,
        ledger,
        owner_unit=owner_unit,
        audit_report=audit,
        line_count_report=line_count,
        reachability_report=reachability,
        boundary_report=boundary,
        include_entries=False,
    )
    evidence = [
        evidence_from_audit(audit),
        evidence_from_line_count(line_count),
        evidence_from_line_buckets(line_buckets),
        evidence_from_schema(schema),
        evidence_from_evidence_graph(evidence_graph),
        evidence_from_state_custody(state_custody),
        evidence_from_mutation(mutation),
        evidence_from_boundary(boundary),
        evidence_from_reachability(reachability),
        evidence_from_cleanroom(cleanroom),
        evidence_from_semantics(semantics),
        evidence_from_test_quality(test_quality),
        evidence_from_acceptance(acceptance),
    ]
    evidence = [item for item in evidence if item is not None]
    evidence_by_name = {item.report_name: item for item in evidence}
    objectives = [review_objective(objective, evidence_by_name) for objective in unit_01a_objectives()]
    findings = review_findings(objectives, evidence, owner_unit, minimum_effective_lines)
    report_payloads: dict[str, Any] = {}
    if include_reports:
        report_payloads = {
            "audit": audit.to_dict(),
            "line_count": line_count.to_dict() if line_count else None,
            "line_buckets": line_buckets.to_dict() if line_buckets else None,
            "schema_contract": schema.to_dict(),
            "evidence_graph": evidence_graph.to_dict(),
            "state_custody": state_custody.to_dict(),
            "mutation_consistency": mutation.to_dict(),
            "boundary": boundary.to_dict(),
            "reachability": reachability.to_dict(),
            "cleanroom": cleanroom.to_dict(),
            "semantic_effects": semantics.to_dict(),
            "test_quality": test_quality.to_dict(),
            "acceptance": acceptance.to_dict(),
        }
    ok = not any(finding.blocking for finding in findings)
    return UnitReviewReport(
        ok=ok,
        owner_unit=owner_unit,
        base_commit=base_commit,
        minimum_effective_lines=minimum_effective_lines,
        objectives=objectives,
        evidence=evidence,
        findings=findings,
        reports=report_payloads,
        summary=review_summary(objectives, evidence, findings, owner_unit),
    )


def review_objective(objective: UnitObjective, evidence_by_name: dict[str, ReviewEvidence]) -> ObjectiveReview:
    present = []
    missing_reports = []
    missing_signals = []
    blocking = False
    severity = ReviewSeverity.INFO
    for report_name in objective.required_reports:
        report = evidence_by_name.get(report_name)
        if report is None:
            missing_reports.append(report_name)
            blocking = True
            severity = ReviewSeverity.ERROR
            continue
        present.append(report_name)
        if not report.ok:
            blocking = True
            severity = max_review_severity(severity, ReviewSeverity.ERROR if report.blocking_count else ReviewSeverity.WARNING)
    for signal in objective.minimum_signals:
        report_name, _, field_name = signal.partition(".")
        report = evidence_by_name.get(report_name)
        if report is None:
            missing_signals.append(signal)
            continue
        if not summary_has_signal(report.summary, field_name):
            missing_signals.append(signal)
    if missing_signals:
        severity = max_review_severity(severity, ReviewSeverity.WARNING)
    if missing_reports:
        severity = max_review_severity(severity, ReviewSeverity.ERROR)
    status = ReviewStatus.PASSING
    if blocking and severity == ReviewSeverity.ERROR:
        status = ReviewStatus.FAILING
    elif blocking and severity == ReviewSeverity.BLOCKER:
        status = ReviewStatus.BLOCKED
    elif missing_signals or severity == ReviewSeverity.WARNING:
        status = ReviewStatus.WARNING
    return ObjectiveReview(
        objective=objective,
        status=status,
        severity=severity,
        evidence=present,
        missing_signals=missing_reports + missing_signals,
        blocking=blocking,
    )


def review_findings(
    objectives: list[ObjectiveReview],
    evidence: list[ReviewEvidence],
    owner_unit: str,
    minimum_effective_lines: int,
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for objective in objectives:
        if objective.blocking:
            findings.append(
                ReviewFinding(
                    code=ReviewCode.UNIT_OBJECTIVE_MISSING,
                    severity=ReviewSeverity.ERROR,
                    message=f"Objective is not fully covered: {objective.objective.title}",
                    objective_id=objective.objective.objective_id,
                    remediation=objective.objective.failure_condition,
                    metadata=objective.to_dict(),
                )
            )
        else:
            findings.append(
                ReviewFinding(
                    code=ReviewCode.UNIT_OBJECTIVES_COVERED,
                    severity=ReviewSeverity.INFO if not objective.missing_signals else ReviewSeverity.WARNING,
                    message=f"Objective has required reports: {objective.objective.title}",
                    objective_id=objective.objective.objective_id,
                    remediation="Resolve missing signal warnings before freezing." if objective.missing_signals else "",
                    metadata=objective.to_dict(),
                )
            )
    for item in evidence:
        findings.extend(evidence_findings(item))
    findings.append(
        ReviewFinding(
            code=ReviewCode.MAIN_PATH_EVIDENCE_READY if any(item.report_name == "reachability" and item.ok for item in evidence) else ReviewCode.MAIN_PATH_EVIDENCE_MISSING,
            severity=ReviewSeverity.INFO if any(item.report_name == "reachability" and item.ok for item in evidence) else ReviewSeverity.ERROR,
            message="Main-path evidence was reviewed for the execution unit.",
            remediation="Keep API/CLI/event/test routes connected to production code.",
            metadata={"owner_unit": owner_unit, "minimum_effective_lines": minimum_effective_lines},
        )
    )
    return findings


def evidence_findings(item: ReviewEvidence) -> list[ReviewFinding]:
    mapping: dict[str, tuple[ReviewCode, ReviewCode]] = {
        "schema_contract": (ReviewCode.SCHEMA_CONTRACT_PASSING, ReviewCode.SCHEMA_CONTRACT_FAILED),
        "audit": (ReviewCode.AUDIT_PASSING, ReviewCode.AUDIT_FAILED),
        "evidence_graph": (ReviewCode.EVIDENCE_GRAPH_PASSING, ReviewCode.EVIDENCE_GRAPH_FAILED),
        "state_custody": (ReviewCode.STATE_CUSTODY_PASSING, ReviewCode.STATE_CUSTODY_FAILED),
        "mutation_consistency": (ReviewCode.MUTATION_CONSISTENCY_PASSING, ReviewCode.MUTATION_CONSISTENCY_FAILED),
        "reachability": (ReviewCode.REACHABILITY_PASSING, ReviewCode.REACHABILITY_FAILED),
        "boundary": (ReviewCode.BOUNDARY_PASSING, ReviewCode.BOUNDARY_FAILED),
        "cleanroom": (ReviewCode.CLEANROOM_PASSING, ReviewCode.CLEANROOM_FAILED),
        "test_quality": (ReviewCode.TEST_QUALITY_PASSING, ReviewCode.TEST_QUALITY_FAILED),
        "semantic_effects": (ReviewCode.SEMANTIC_EFFECTS_PASSING, ReviewCode.SEMANTIC_EFFECTS_FAILED),
        "acceptance": (ReviewCode.ACCEPTANCE_PASSING, ReviewCode.ACCEPTANCE_FAILED),
        "line_count": (ReviewCode.LINE_COUNT_PASSING, ReviewCode.LINE_COUNT_FAILED),
        "line_buckets": (ReviewCode.LINE_COUNT_PASSING, ReviewCode.LINE_COUNT_FAILED),
    }
    passing_code, failing_code = mapping.get(item.report_name, (ReviewCode.UNIT_OBJECTIVES_COVERED, ReviewCode.UNIT_OBJECTIVE_MISSING))
    return [
        ReviewFinding(
            code=passing_code if item.ok else failing_code,
            severity=ReviewSeverity.INFO if item.ok else ReviewSeverity.ERROR if item.blocking_count else ReviewSeverity.WARNING,
            message=f"{item.report_name} {'passed' if item.ok else 'reported issues'}.",
            report_name=item.report_name,
            remediation="Inspect report blocking_findings before marking the unit complete." if not item.ok else "",
            metadata=item.to_dict(),
        )
    ]


def evidence_from_audit(report: Any) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="audit",
        ok=bool(report.ok),
        status=status_from_ok(report.ok, getattr(report, "blocker_count", 0), getattr(report, "error_count", 0), getattr(report, "warning_count", 0)),
        summary={"total_entries": report.total_entries, "finding_count": len(report.findings), "ok": report.ok},
        blocking_count=getattr(report, "blocker_count", 0) + getattr(report, "error_count", 0),
        warning_count=getattr(report, "warning_count", 0),
        runtime_entry="zyra_integrations.ledger_audit.InternalizationLedgerAuditor",
        api_routes=["GET /ledger/audit", "POST /ledger/audit"],
        cli_commands=["audit"],
        tests=["tests/integration/test_internalization_ledger_api.py"],
    )


def evidence_from_line_count(report: EffectiveLineCountReport | None) -> ReviewEvidence | None:
    if report is None:
        return None
    return ReviewEvidence(
        report_name="line_count",
        ok=report.ok,
        status=status_from_ok(report.ok, 0, 0, 0 if report.ok else 1),
        summary={"effective_added": report.effective_added, "raw_added": report.raw_added, "minimum_effective_lines": report.minimum_effective_lines, "shortfall": report.shortfall},
        blocking_count=0 if report.ok else 1,
        runtime_entry="zyra_integrations.ledger_linecount.build_line_count_report",
        api_routes=["GET /ledger/linecount"],
        cli_commands=["linecount"],
        tests=["tests/integration/test_internalization_ledger_gate_cli.py"],
    )


def evidence_from_line_buckets(report: LineBucketReport | None) -> ReviewEvidence | None:
    if report is None:
        return None
    blocking = line_bucket_blocking_count(report)
    warnings = line_bucket_warning_count(report)
    return ReviewEvidence(
        report_name="line_buckets",
        ok=report.ok,
        status=status_from_ok(report.ok, blocking, 0, warnings),
        summary={"bucket_effective_added": report.bucket_effective_added, "vendor_like_added": report.vendor_like_added, "data_added": report.data_added, "by_bucket": line_bucket_totals(report)},
        blocking_count=blocking,
        warning_count=warnings,
        runtime_entry="zyra_integrations.ledger_line_buckets.build_line_bucket_report",
        api_routes=["GET /ledger/buckets"],
        cli_commands=["buckets"],
        tests=["tests/integration/test_internalization_ledger_gate_cli.py"],
    )


def line_bucket_blocking_count(report: LineBucketReport) -> int:
    return sum(1 for finding in report.findings if finding.risk in {BucketRisk.ERROR, BucketRisk.BLOCKER})


def line_bucket_warning_count(report: LineBucketReport) -> int:
    return sum(1 for finding in report.findings if finding.risk == BucketRisk.WARNING)


def line_bucket_totals(report: LineBucketReport) -> dict[str, Any]:
    return {str(total.bucket): total.to_dict() for total in report.totals}


def evidence_from_schema(report: LedgerSchemaContractReport) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="schema_contract",
        ok=report.ok,
        status=status_from_ok(report.ok, report.blocker_count, report.error_count, report.warning_count),
        summary=summary_dict(report.summary),
        blocking_count=report.blocker_count + report.error_count,
        warning_count=report.warning_count,
        runtime_entry="zyra_integrations.ledger_schema_contract.build_schema_contract_report",
        api_routes=["GET /ledger/schema-contract"],
        cli_commands=["schema-contract"],
        tests=["tests/unit/test_internalization_ledger_control_plane.py"],
    )


def evidence_from_evidence_graph(report: EvidenceGraphReport) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="evidence_graph",
        ok=report.ok,
        status=status_from_ok(report.ok, report.blocker_count, report.error_count, report.warning_count),
        summary=summary_dict(report.summary),
        blocking_count=report.blocker_count + report.error_count,
        warning_count=report.warning_count,
        runtime_entry="zyra_integrations.ledger_evidence_graph.build_evidence_graph_report",
        api_routes=["GET /ledger/evidence-graph"],
        cli_commands=["evidence-graph"],
        tests=["tests/unit/test_internalization_ledger_control_plane.py"],
    )


def evidence_from_state_custody(report: StateCustodyReport) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="state_custody",
        ok=report.ok,
        status=status_from_ok(report.ok, report.blocker_count, report.error_count, report.warning_count),
        summary=summary_dict(report.summary),
        blocking_count=report.blocker_count + report.error_count,
        warning_count=report.warning_count,
        runtime_entry="zyra_integrations.ledger_state_custody.build_state_custody_report",
        api_routes=["GET /ledger/state-custody"],
        cli_commands=["state-custody"],
        tests=["tests/unit/test_internalization_ledger_control_plane.py"],
    )


def evidence_from_mutation(report: MutationConsistencyReport) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="mutation_consistency",
        ok=report.ok,
        status=status_from_ok(report.ok, report.blocker_count, report.error_count, report.warning_count),
        summary=summary_dict(report.summary),
        blocking_count=report.blocker_count + report.error_count,
        warning_count=report.warning_count,
        runtime_entry="zyra_integrations.ledger_mutation_consistency.build_mutation_consistency_report",
        api_routes=["GET /ledger/mutation-consistency"],
        cli_commands=["mutation-consistency"],
        tests=["tests/unit/test_internalization_ledger_control_plane.py"],
    )


def evidence_from_boundary(report: CleanBoundaryReport) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="boundary",
        ok=report.ok,
        status=status_from_ok(report.ok, report.blocker_count, report.error_count, report.warning_count),
        summary=summary_dict(report.summary),
        blocking_count=report.blocker_count + report.error_count,
        warning_count=report.warning_count,
        runtime_entry="zyra_integrations.ledger_boundary.build_clean_boundary_report",
        api_routes=["GET /ledger/boundary"],
        cli_commands=["boundary"],
        tests=["tests/unit/test_internalization_ledger_control_plane.py"],
    )


def evidence_from_reachability(report: ReachabilityReport) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="reachability",
        ok=report.ok,
        status=status_from_ok(report.ok, report.blocker_count, report.error_count, report.warning_count),
        summary=summary_dict(report.summary) | {"route_count": report.route_count, "cli_command_count": report.cli_command_count},
        blocking_count=report.blocker_count + report.error_count,
        warning_count=report.warning_count,
        runtime_entry="zyra_integrations.ledger_reachability.build_reachability_report",
        api_routes=["GET /ledger/reachability"],
        cli_commands=["reachability"],
        tests=["tests/unit/test_internalization_ledger_control_plane.py"],
    )


def evidence_from_cleanroom(report: CleanroomReport) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="cleanroom",
        ok=report.ok,
        status=status_from_ok(report.ok, report.blocker_count, report.error_count, report.warning_count),
        summary=summary_dict(report.summary) | {"commands_to_run": len(report.commands)},
        blocking_count=report.blocker_count + report.error_count,
        warning_count=report.warning_count,
        runtime_entry="zyra_integrations.ledger_cleanroom.build_cleanroom_report",
        api_routes=["GET /ledger/cleanroom"],
        cli_commands=["cleanroom"],
        tests=["tests/unit/test_internalization_ledger_control_plane.py"],
    )


def evidence_from_semantics(report: SemanticEffectReport) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="semantic_effects",
        ok=report.ok,
        status=status_from_ok(report.ok, report.error_probes, 0, report.failing_probes),
        summary=summary_dict(report.summary) | {"total_probes": report.total_probes, "passing_probes": report.passing_probes, "ok": report.ok},
        blocking_count=report.error_probes + report.failing_probes,
        runtime_entry="zyra_integrations.ledger_semantics.build_semantic_effect_report",
        api_routes=["GET /ledger/semantic-effects"],
        cli_commands=["semantic-effects"],
        tests=["tests/unit/test_internalization_ledger_control_plane.py"],
    )


def evidence_from_test_quality(report: TestQualityReport) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="test_quality",
        ok=report.ok,
        status=status_from_ok(report.ok, report.blocker_count, report.error_count, report.warning_count),
        summary=summary_dict(report.summary) | {"behavior_test_files": report.behavior_test_files, "checked_test_entries": report.checked_test_entries},
        blocking_count=report.blocker_count + report.error_count,
        warning_count=report.warning_count,
        runtime_entry="zyra_integrations.ledger_test_quality.build_test_quality_report",
        api_routes=["GET /ledger/test-quality"],
        cli_commands=["test-quality"],
        tests=["tests/unit/test_internalization_ledger_control_plane.py"],
    )


def evidence_from_acceptance(report: AcceptanceReport) -> ReviewEvidence:
    return ReviewEvidence(
        report_name="acceptance",
        ok=report.ok,
        status=status_from_ok(report.ok, report.blocker_count, report.error_count, report.warning_count),
        summary=summary_dict(report.summary),
        blocking_count=report.blocker_count + report.error_count,
        warning_count=report.warning_count,
        runtime_entry="zyra_integrations.ledger_acceptance.build_acceptance_report",
        api_routes=["GET /ledger/acceptance"],
        cli_commands=["acceptance"],
        tests=["tests/unit/test_internalization_ledger_control_plane.py"],
    )


def status_from_ok(ok: bool, blockers: int, errors: int, warnings: int) -> ReviewStatus:
    if blockers:
        return ReviewStatus.BLOCKED
    if errors or not ok:
        return ReviewStatus.FAILING
    if warnings:
        return ReviewStatus.WARNING
    return ReviewStatus.PASSING


def summary_has_signal(summary: Any, field_name: str) -> bool:
    summary = summary_dict(summary)
    if field_name in summary:
        value = summary[field_name]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value > 0
        return bool(value)
    for value in summary.values():
        if isinstance(value, dict) and summary_has_signal(value, field_name):
            return True
    return False


def summary_dict(summary: Any) -> dict[str, Any]:
    if isinstance(summary, dict):
        return summary
    converted = to_jsonable(summary)
    return converted if isinstance(converted, dict) else {}


def max_review_severity(left: ReviewSeverity, right: ReviewSeverity) -> ReviewSeverity:
    order = {
        ReviewSeverity.INFO: 0,
        ReviewSeverity.WARNING: 1,
        ReviewSeverity.ERROR: 2,
        ReviewSeverity.BLOCKER: 3,
    }
    return left if order[left] >= order[right] else right


def review_summary(
    objectives: list[ObjectiveReview],
    evidence: list[ReviewEvidence],
    findings: list[ReviewFinding],
    owner_unit: str,
) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_evidence_status: dict[str, int] = {}
    by_finding_code: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for objective in objectives:
        by_status[str(objective.status)] = by_status.get(str(objective.status), 0) + 1
    for item in evidence:
        by_evidence_status[str(item.status)] = by_evidence_status.get(str(item.status), 0) + 1
    for finding in findings:
        by_finding_code[str(finding.code)] = by_finding_code.get(str(finding.code), 0) + 1
        by_severity[str(finding.severity)] = by_severity.get(str(finding.severity), 0) + 1
    return {
        "owner_unit": owner_unit,
        "objective_count": len(objectives),
        "passing_objectives": sum(1 for objective in objectives if objective.status == ReviewStatus.PASSING),
        "blocking_objectives": sum(1 for objective in objectives if objective.blocking),
        "evidence_count": len(evidence),
        "passing_evidence": sum(1 for item in evidence if item.ok),
        "blocking_evidence": sum(1 for item in evidence if item.blocking_count),
        "by_objective_status": dict(sorted(by_status.items())),
        "by_evidence_status": dict(sorted(by_evidence_status.items())),
        "findings_by_code": dict(sorted(by_finding_code.items())),
        "findings_by_severity": dict(sorted(by_severity.items())),
    }


def unit_review_payload(report: UnitReviewReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["summary_rows"] = unit_review_summary_rows(report)
    payload["gate_table"] = unit_review_gate_table(report)
    payload["status_matrix"] = unit_review_status_matrix(report)
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity in {ReviewSeverity.ERROR, ReviewSeverity.BLOCKER}
    ]
    payload["warning_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity == ReviewSeverity.WARNING
    ]
    return payload


def unit_review_status_matrix(report: UnitReviewReport) -> dict[str, Any]:
    objective_rows = unit_review_summary_rows(report)
    evidence_rows = [row for row in objective_rows if row["kind"] == "evidence"]
    objective_rows = [row for row in objective_rows if row["kind"] == "objective"]
    return {
        "objectives_by_status": count_rows_by_field(objective_rows, "status"),
        "evidence_by_status": count_rows_by_field(evidence_rows, "status"),
        "objectives_missing_signals": sum(1 for row in objective_rows if row.get("missing")),
        "evidence_with_api_routes": sum(1 for row in evidence_rows if row.get("api_routes")),
        "evidence_with_cli_commands": sum(1 for row in evidence_rows if row.get("cli_commands")),
        "evidence_with_tests": sum(1 for row in evidence_rows if row.get("tests")),
    }


def count_rows_by_field(rows: list[dict[str, Any]], field_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field_name) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def assert_unit_review(report: UnitReviewReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code} {finding.objective_id or finding.report_name}: {finding.message}"
        for finding in report.findings
        if finding.blocking
    )
    raise AssertionError(f"Unit review failed:\n{formatted}")


def unit_review_markdown(report: UnitReviewReport) -> str:
    lines: list[str] = []
    lines.append(f"# {report.owner_unit} Internalization Review")
    lines.append("")
    lines.append(f"- ok: {str(report.ok).lower()}")
    lines.append(f"- base_commit: {report.base_commit or 'not supplied'}")
    lines.append(f"- minimum_effective_lines: {report.minimum_effective_lines}")
    lines.append(f"- objectives: {len(report.objectives)}")
    lines.append(f"- evidence_reports: {len(report.evidence)}")
    lines.append(f"- blockers: {report.blocker_count}")
    lines.append(f"- errors: {report.error_count}")
    lines.append(f"- warnings: {report.warning_count}")
    lines.append("")
    lines.extend(objectives_markdown(report.objectives))
    lines.append("")
    lines.extend(evidence_markdown(report.evidence))
    lines.append("")
    lines.extend(findings_markdown(report.findings))
    lines.append("")
    lines.extend(gate_markdown(report))
    return "\n".join(lines).rstrip() + "\n"


def objectives_markdown(objectives: list[ObjectiveReview]) -> list[str]:
    rows = [
        [
            "objective",
            "status",
            "severity",
            "blocking",
            "evidence",
            "missing",
        ]
    ]
    for objective in objectives:
        rows.append(
            [
                objective.objective.objective_id,
                str(objective.status),
                str(objective.severity),
                "yes" if objective.blocking else "no",
                ", ".join(objective.evidence) or "-",
                ", ".join(objective.missing_signals) or "-",
            ]
        )
    return ["## Objectives", "", *markdown_table(rows)]


def evidence_markdown(evidence: list[ReviewEvidence]) -> list[str]:
    rows = [["report", "ok", "status", "blocking", "warnings", "api", "cli", "tests"]]
    for item in evidence:
        rows.append(
            [
                item.report_name,
                "yes" if item.ok else "no",
                str(item.status),
                str(item.blocking_count),
                str(item.warning_count),
                ", ".join(item.api_routes) or "-",
                ", ".join(item.cli_commands) or "-",
                ", ".join(item.tests) or "-",
            ]
        )
    return ["## Evidence", "", *markdown_table(rows)]


def findings_markdown(findings: list[ReviewFinding]) -> list[str]:
    blocking = [finding for finding in findings if finding.blocking]
    warnings = [finding for finding in findings if finding.severity == ReviewSeverity.WARNING]
    rows = [["severity", "code", "scope", "message", "remediation"]]
    for finding in [*blocking, *warnings][:80]:
        rows.append(
            [
                str(finding.severity),
                str(finding.code),
                finding.objective_id or finding.report_name or "-",
                finding.message,
                finding.remediation or "-",
            ]
        )
    if len(rows) == 1:
        rows.append(["info", "none", "-", "No blocking or warning findings.", "-"])
    return ["## Findings", "", *markdown_table(rows)]


def gate_markdown(report: UnitReviewReport) -> list[str]:
    rows = [["gate", "result", "evidence"]]
    rows.append(["all objectives non-blocking", "pass" if not any(item.blocking for item in report.objectives) else "fail", f"{report.summary.get('passing_objectives', 0)}/{report.summary.get('objective_count', 0)} passing"])
    rows.append(["all evidence non-blocking", "pass" if not any(item.blocking_count for item in report.evidence) else "fail", f"{report.summary.get('passing_evidence', 0)}/{report.summary.get('evidence_count', 0)} passing"])
    rows.append(["line minimum declared", "pass" if report.minimum_effective_lines >= 10_000 else "fail", str(report.minimum_effective_lines)])
    rows.append(["main path evidence", "pass" if any(item.report_name == "reachability" and item.ok for item in report.evidence) else "fail", "reachability report"])
    return ["## Gates", "", *markdown_table(rows)]


def markdown_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    widths = [0 for _ in rows[0]]
    normalized: list[list[str]] = []
    for row in rows:
        normalized_row = [markdown_cell(cell) for cell in row]
        normalized.append(normalized_row)
        for index, cell in enumerate(normalized_row):
            widths[index] = max(widths[index], len(cell))
    lines: list[str] = []
    header = normalized[0]
    lines.append("| " + " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(header)) + " |")
    lines.append("| " + " | ".join("-" * widths[index] for index in range(len(widths))) + " |")
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row[index].ljust(widths[index]) for index in range(len(widths))) + " |")
    return lines


def markdown_cell(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("|", "\\|").strip()
    if len(text) > 140:
        text = text[:137] + "..."
    return text or "-"


def unit_review_summary_rows(report: UnitReviewReport) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for objective in report.objectives:
        rows.append(
            {
                "kind": "objective",
                "name": objective.objective.objective_id,
                "ok": not objective.blocking,
                "status": str(objective.status),
                "severity": str(objective.severity),
                "evidence": list(objective.evidence),
                "missing": list(objective.missing_signals),
            }
        )
    for item in report.evidence:
        rows.append(
            {
                "kind": "evidence",
                "name": item.report_name,
                "ok": item.ok,
                "status": str(item.status),
                "severity": "error" if item.blocking_count else "warning" if item.warning_count else "info",
                "api_routes": list(item.api_routes),
                "cli_commands": list(item.cli_commands),
                "tests": list(item.tests),
            }
        )
    return rows


def unit_review_gate_table(report: UnitReviewReport) -> list[dict[str, Any]]:
    return [
        {
            "gate": "objectives",
            "ok": not any(item.blocking for item in report.objectives),
            "passing": report.summary.get("passing_objectives", 0),
            "total": report.summary.get("objective_count", 0),
        },
        {
            "gate": "evidence",
            "ok": not any(item.blocking_count for item in report.evidence),
            "passing": report.summary.get("passing_evidence", 0),
            "total": report.summary.get("evidence_count", 0),
        },
        {
            "gate": "line_minimum",
            "ok": report.minimum_effective_lines >= 10_000,
            "passing": report.minimum_effective_lines,
            "total": 10_000,
        },
        {
            "gate": "main_path",
            "ok": any(item.report_name == "reachability" and item.ok for item in report.evidence),
            "passing": 1 if any(item.report_name == "reachability" and item.ok for item in report.evidence) else 0,
            "total": 1,
        },
    ]
