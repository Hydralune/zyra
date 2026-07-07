from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable

from .ledger_models import InternalizationLedgerEntry, LedgerLifecycle, LineCountPolicy, MainPathStatus, MigrationStrategy, to_jsonable
from .ledger_store import InternalizationLedger


class MatrixSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class MatrixCode(StrEnum):
    COMBINATION_ALLOWED = "COMBINATION_ALLOWED"
    COMBINATION_REVIEW = "COMBINATION_REVIEW"
    COMBINATION_REJECTED = "COMBINATION_REJECTED"
    CONNECTED_REQUIRES_MATERIALIZED = "CONNECTED_REQUIRES_MATERIALIZED"
    PRODUCTIZED_REQUIRES_STRONG_STRATEGY = "PRODUCTIZED_REQUIRES_STRONG_STRATEGY"
    VENDORED_RUNTIME_NOT_DEEP_INTERNALIZED = "VENDORED_RUNTIME_NOT_DEEP_INTERNALIZED"
    TESTED_STATUS_REQUIRES_TEST = "TESTED_STATUS_REQUIRES_TEST"
    WORKER_STATUS_REQUIRES_RUNTIME = "WORKER_STATUS_REQUIRES_RUNTIME"
    UI_STATUS_REQUIRES_UI_PANEL = "UI_STATUS_REQUIRES_UI_PANEL"
    EVENT_STATUS_REQUIRES_EVENT = "EVENT_STATUS_REQUIRES_EVENT"
    API_STATUS_REQUIRES_ROUTE = "API_STATUS_REQUIRES_ROUTE"
    CONTROL_STATUS_REQUIRES_COMMAND = "CONTROL_STATUS_REQUIRES_COMMAND"
    LINE_POLICY_MISMATCH = "LINE_POLICY_MISMATCH"
    MATRIX_COVERAGE_READY = "MATRIX_COVERAGE_READY"


@dataclass(slots=True)
class PolicyMatrixRule:
    rule_id: str
    lifecycle: LedgerLifecycle | None
    status: MainPathStatus | None
    strategy: MigrationStrategy | None
    allowed: bool
    severity: MatrixSeverity
    rationale: str
    required_fields: list[str] = field(default_factory=list)
    code: MatrixCode = MatrixCode.COMBINATION_ALLOWED

    def matches(self, entry: InternalizationLedgerEntry) -> bool:
        if self.lifecycle is not None and entry.lifecycle != self.lifecycle:
            return False
        if self.status is not None and entry.main_path_status != self.status:
            return False
        if self.strategy is not None and entry.migration_strategy != self.strategy:
            return False
        return True

    def specificity(self) -> int:
        return sum(1 for item in [self.lifecycle, self.status, self.strategy] if item is not None)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class PolicyDecision:
    ledger_id: str
    source_repo: str
    owner_unit: str
    lifecycle: str
    main_path_status: str
    migration_strategy: str
    line_count_policy: str
    allowed: bool
    severity: MatrixSeverity
    rule_id: str
    code: MatrixCode
    rationale: str
    missing_required_fields: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {MatrixSeverity.ERROR, MatrixSeverity.BLOCKER}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class PolicyMatrixFinding:
    code: MatrixCode
    severity: MatrixSeverity
    message: str
    ledger_id: str = ""
    owner_unit: str = ""
    source_repo: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {MatrixSeverity.ERROR, MatrixSeverity.BLOCKER}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class PolicyMatrixCoverage:
    dimension: str
    allowed_values: list[str]
    observed_values: dict[str, int]
    missing_values: list[str]

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class PolicyMatrixReport:
    ok: bool
    owner_unit: str
    total_entries: int
    rules: list[PolicyMatrixRule]
    decisions: list[PolicyDecision]
    coverage: list[PolicyMatrixCoverage]
    findings: list[PolicyMatrixFinding]
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == MatrixSeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == MatrixSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == MatrixSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


CONNECTED_STATUSES = {
    MainPathStatus.API_CONNECTED,
    MainPathStatus.EVENT_LOG_CONNECTED,
    MainPathStatus.CONTROL_COMMAND_CONNECTED,
    MainPathStatus.WORKER_RUNTIME_CONNECTED,
    MainPathStatus.UI_CONNECTED,
    MainPathStatus.TESTED_MAIN_PATH,
}

MATERIALIZED_LIFECYCLES = {
    LedgerLifecycle.ACTIVE,
    LedgerLifecycle.INTERNALIZED,
    LedgerLifecycle.PRODUCTIZED,
}

PRODUCTIZED_LIFECYCLES = {
    LedgerLifecycle.INTERNALIZED,
    LedgerLifecycle.PRODUCTIZED,
}

DEEP_STRATEGIES = {
    MigrationStrategy.DIRECT_PORT,
    MigrationStrategy.ADAPTER,
    MigrationStrategy.REIMPLEMENTED_PATTERN,
}

SOURCE_POOL_STRATEGIES = {
    MigrationStrategy.SIDECAR_RUNTIME,
    MigrationStrategy.VENDORED_RUNTIME,
    MigrationStrategy.PLANNED_ADAPTER,
    MigrationStrategy.CANDIDATE_REVIEW,
}


def default_policy_matrix_rules() -> list[PolicyMatrixRule]:
    rules: list[PolicyMatrixRule] = []
    for status in CONNECTED_STATUSES:
        rules.append(
            PolicyMatrixRule(
                rule_id=f"connected-{status}",
                lifecycle=None,
                status=status,
                strategy=None,
                allowed=True,
                severity=MatrixSeverity.INFO,
                rationale="Connected statuses require materialized lifecycle and concrete main-path evidence.",
                required_fields=required_fields_for_status(status),
                code=MatrixCode.COMBINATION_ALLOWED,
            )
        )
    for lifecycle in [LedgerLifecycle.CANDIDATE, LedgerLifecycle.PLANNED, LedgerLifecycle.DEFERRED, LedgerLifecycle.REJECTED]:
        for status in CONNECTED_STATUSES:
            rules.append(
                PolicyMatrixRule(
                    rule_id=f"{lifecycle}-{status}-rejected",
                    lifecycle=lifecycle,
                    status=status,
                    strategy=None,
                    allowed=False,
                    severity=MatrixSeverity.ERROR,
                    rationale="Connected status cannot be claimed before lifecycle is materialized.",
                    required_fields=required_fields_for_status(status),
                    code=MatrixCode.CONNECTED_REQUIRES_MATERIALIZED,
                )
            )
    for lifecycle in PRODUCTIZED_LIFECYCLES:
        for strategy in SOURCE_POOL_STRATEGIES:
            rules.append(
                PolicyMatrixRule(
                    rule_id=f"{lifecycle}-{strategy}-review",
                    lifecycle=lifecycle,
                    status=None,
                    strategy=strategy,
                    allowed=False,
                    severity=MatrixSeverity.ERROR if strategy in {MigrationStrategy.VENDORED_RUNTIME, MigrationStrategy.SIDECAR_RUNTIME} else MatrixSeverity.WARNING,
                    rationale="Productized/internalized entries require Zyra-owned strategy, not source-pool runtime strategy.",
                    required_fields=["target_bindings", "test_entries", "runtime_entry", "main_path"],
                    code=MatrixCode.PRODUCTIZED_REQUIRES_STRONG_STRATEGY,
                )
            )
    for strategy in [MigrationStrategy.VENDORED_RUNTIME, MigrationStrategy.SIDECAR_RUNTIME]:
        rules.append(
            PolicyMatrixRule(
                rule_id=f"{strategy}-not-deep-internalized",
                lifecycle=None,
                status=None,
                strategy=strategy,
                allowed=True,
                severity=MatrixSeverity.WARNING,
                rationale="Vendored/sidecar strategy is allowed as source pool or reference runtime, but not counted as deep internalization by itself.",
                required_fields=["target_bindings"],
                code=MatrixCode.VENDORED_RUNTIME_NOT_DEEP_INTERNALIZED,
            )
        )
    rules.append(
        PolicyMatrixRule(
            rule_id="default-allowed",
            lifecycle=None,
            status=None,
            strategy=None,
            allowed=True,
            severity=MatrixSeverity.INFO,
            rationale="Default combination is allowed if field-level evidence checks pass.",
            required_fields=["source_repo", "source_path", "target_bindings"],
            code=MatrixCode.COMBINATION_ALLOWED,
        )
    )
    return rules


def build_policy_matrix_report(
    ledger: InternalizationLedger,
    *,
    owner_unit: str = "",
    include_decisions: bool = False,
) -> PolicyMatrixReport:
    entries = ledger.by_owner_unit(owner_unit) if owner_unit else ledger.entries()
    rules = default_policy_matrix_rules()
    decisions = [evaluate_policy_matrix_entry(entry, rules) for entry in entries]
    findings = build_policy_matrix_findings(decisions)
    coverage = build_policy_matrix_coverage(entries)
    findings.append(
        PolicyMatrixFinding(
            code=MatrixCode.MATRIX_COVERAGE_READY,
            severity=MatrixSeverity.INFO,
            message="Policy matrix coverage was built for lifecycle/status/strategy/line policy dimensions.",
            metadata={"entry_count": len(entries), "rule_count": len(rules)},
        )
    )
    ok = not any(finding.blocking for finding in findings)
    return PolicyMatrixReport(
        ok=ok,
        owner_unit=owner_unit or "all",
        total_entries=len(entries),
        rules=rules,
        decisions=decisions if include_decisions else [],
        coverage=coverage,
        findings=findings,
        summary=policy_matrix_summary(entries, decisions, findings, coverage),
    )


def evaluate_policy_matrix_entry(entry: InternalizationLedgerEntry, rules: list[PolicyMatrixRule] | None = None) -> PolicyDecision:
    rules = rules or default_policy_matrix_rules()
    matching = sorted([rule for rule in rules if rule.matches(entry)], key=lambda rule: rule.specificity(), reverse=True)
    rule = matching[0] if matching else default_policy_matrix_rules()[-1]
    missing = missing_required_fields(entry, rule.required_fields)
    severity = rule.severity
    allowed = rule.allowed
    code = rule.code
    rationale = rule.rationale
    metadata: dict[str, Any] = {"matched_rule_count": len(matching)}
    extra_code, extra_severity, extra_rationale, extra_allowed = status_specific_decision(entry)
    if extra_code is not None:
        code = extra_code
        severity = max_matrix_severity(severity, extra_severity)
        rationale = f"{rationale} {extra_rationale}".strip()
        allowed = allowed and extra_allowed
    if missing:
        severity = max_matrix_severity(severity, MatrixSeverity.ERROR if entry.main_path_status in CONNECTED_STATUSES else MatrixSeverity.WARNING)
        allowed = False if entry.main_path_status in CONNECTED_STATUSES else allowed
    line_code, line_severity, line_rationale = line_policy_decision(entry)
    if line_code is not None:
        code = line_code
        severity = max_matrix_severity(severity, line_severity)
        rationale = f"{rationale} {line_rationale}".strip()
        metadata["line_policy_rationale"] = line_rationale
    return PolicyDecision(
        ledger_id=entry.ledger_id,
        source_repo=entry.source_repo,
        owner_unit=entry.owner_unit,
        lifecycle=str(entry.lifecycle),
        main_path_status=str(entry.main_path_status),
        migration_strategy=str(entry.migration_strategy),
        line_count_policy=str(entry.line_count_policy),
        allowed=allowed and severity not in {MatrixSeverity.ERROR, MatrixSeverity.BLOCKER},
        severity=severity,
        rule_id=rule.rule_id,
        code=code,
        rationale=rationale,
        missing_required_fields=missing,
        metadata=metadata,
    )


def status_specific_decision(entry: InternalizationLedgerEntry) -> tuple[MatrixCode | None, MatrixSeverity, str, bool]:
    status = entry.main_path_status
    if status == MainPathStatus.API_CONNECTED and not entry.main_path.api_routes:
        return MatrixCode.API_STATUS_REQUIRES_ROUTE, MatrixSeverity.ERROR, "API-connected entries require api_routes.", False
    if status == MainPathStatus.EVENT_LOG_CONNECTED and not entry.main_path.event_types:
        return MatrixCode.EVENT_STATUS_REQUIRES_EVENT, MatrixSeverity.ERROR, "Event-log-connected entries require event_types.", False
    if status == MainPathStatus.CONTROL_COMMAND_CONNECTED and not entry.main_path.control_commands:
        return MatrixCode.CONTROL_STATUS_REQUIRES_COMMAND, MatrixSeverity.ERROR, "Control-command-connected entries require control_commands.", False
    if status == MainPathStatus.WORKER_RUNTIME_CONNECTED and not entry.main_path.worker_runtime:
        return MatrixCode.WORKER_STATUS_REQUIRES_RUNTIME, MatrixSeverity.ERROR, "Worker-runtime-connected entries require worker_runtime.", False
    if status == MainPathStatus.UI_CONNECTED and not entry.main_path.ui_panels:
        return MatrixCode.UI_STATUS_REQUIRES_UI_PANEL, MatrixSeverity.ERROR, "UI-connected entries require ui_panels.", False
    if status == MainPathStatus.TESTED_MAIN_PATH and not entry.test_entries:
        return MatrixCode.TESTED_STATUS_REQUIRES_TEST, MatrixSeverity.ERROR, "Tested main path entries require test_entries.", False
    if status in CONNECTED_STATUSES and entry.lifecycle not in MATERIALIZED_LIFECYCLES:
        return MatrixCode.CONNECTED_REQUIRES_MATERIALIZED, MatrixSeverity.ERROR, "Connected entries require active/internalized/productized lifecycle.", False
    return None, MatrixSeverity.INFO, "", True


def line_policy_decision(entry: InternalizationLedgerEntry) -> tuple[MatrixCode | None, MatrixSeverity, str]:
    if entry.migration_strategy in {MigrationStrategy.VENDORED_RUNTIME, MigrationStrategy.SIDECAR_RUNTIME}:
        if entry.line_count_policy not in {LineCountPolicy.EXCLUDED_INVENTORY_ONLY, LineCountPolicy.COUNTS_WHEN_PRODUCTIZED}:
            return MatrixCode.LINE_POLICY_MISMATCH, MatrixSeverity.WARNING, "Vendor/sidecar source pools should not use direct runtime/test/script line policies."
    if entry.lifecycle in PRODUCTIZED_LIFECYCLES and entry.line_count_policy == LineCountPolicy.EXCLUDED_INVENTORY_ONLY:
        return MatrixCode.LINE_POLICY_MISMATCH, MatrixSeverity.WARNING, "Productized/internalized entries need an explicit countable policy or documented exclusion."
    return None, MatrixSeverity.INFO, ""


def missing_required_fields(entry: InternalizationLedgerEntry, names: Iterable[str]) -> list[str]:
    missing: list[str] = []
    for name in names:
        value = getattr(entry, name, None)
        if name == "runtime_entry":
            if entry.runtime_entry.is_empty():
                missing.append(name)
            continue
        if name == "main_path":
            if entry.main_path.is_empty():
                missing.append(name)
            continue
        if value is None:
            missing.append(name)
        elif isinstance(value, str) and not value.strip():
            missing.append(name)
        elif isinstance(value, (list, dict, tuple, set)) and not value:
            missing.append(name)
    return missing


def required_fields_for_status(status: MainPathStatus) -> list[str]:
    base = ["target_bindings", "test_entries", "runtime_entry", "main_path"]
    if status == MainPathStatus.API_CONNECTED:
        return base + ["main_path.api_routes"]
    if status == MainPathStatus.EVENT_LOG_CONNECTED:
        return base + ["main_path.event_types"]
    if status == MainPathStatus.CONTROL_COMMAND_CONNECTED:
        return base + ["main_path.control_commands"]
    if status == MainPathStatus.WORKER_RUNTIME_CONNECTED:
        return base + ["main_path.worker_runtime"]
    if status == MainPathStatus.UI_CONNECTED:
        return base + ["main_path.ui_panels"]
    return base


def build_policy_matrix_findings(decisions: Iterable[PolicyDecision]) -> list[PolicyMatrixFinding]:
    findings: list[PolicyMatrixFinding] = []
    for decision in decisions:
        severity = decision.severity
        if decision.allowed and severity == MatrixSeverity.INFO:
            findings.append(
                PolicyMatrixFinding(
                    code=MatrixCode.COMBINATION_ALLOWED,
                    severity=MatrixSeverity.INFO,
                    message="Policy matrix combination is allowed.",
                    ledger_id=decision.ledger_id,
                    owner_unit=decision.owner_unit,
                    source_repo=decision.source_repo,
                    metadata=decision.to_dict(),
                )
            )
            continue
        findings.append(
            PolicyMatrixFinding(
                code=decision.code if not decision.allowed else MatrixCode.COMBINATION_REVIEW,
                severity=severity,
                message=decision.rationale,
                ledger_id=decision.ledger_id,
                owner_unit=decision.owner_unit,
                source_repo=decision.source_repo,
                remediation=remediation_for_decision(decision),
                metadata=decision.to_dict(),
            )
        )
    return findings


def remediation_for_decision(decision: PolicyDecision) -> str:
    if decision.code == MatrixCode.CONNECTED_REQUIRES_MATERIALIZED:
        return "Advance lifecycle only after target/runtime/test/main-path evidence exists, or downgrade main_path_status."
    if decision.code == MatrixCode.PRODUCTIZED_REQUIRES_STRONG_STRATEGY:
        return "Convert vendor/source-pool strategy into Zyra-owned adapter/direct port, or keep lifecycle below productized."
    if decision.code == MatrixCode.LINE_POLICY_MISMATCH:
        return "Use a policy that matches whether the implementation is Zyra-owned production/test/script code."
    if decision.missing_required_fields:
        return f"Populate required fields: {', '.join(decision.missing_required_fields)}."
    return "Review policy matrix decision and adjust ledger status or evidence."


def build_policy_matrix_coverage(entries: Iterable[InternalizationLedgerEntry]) -> list[PolicyMatrixCoverage]:
    items = list(entries)
    return [
        coverage_for_dimension("lifecycle", [str(item) for item in LedgerLifecycle], [str(entry.lifecycle) for entry in items]),
        coverage_for_dimension("main_path_status", [str(item) for item in MainPathStatus], [str(entry.main_path_status) for entry in items]),
        coverage_for_dimension("migration_strategy", [str(item) for item in MigrationStrategy], [str(entry.migration_strategy) for entry in items]),
        coverage_for_dimension("line_count_policy", [str(item) for item in LineCountPolicy], [str(entry.line_count_policy) for entry in items]),
    ]


def coverage_for_dimension(dimension: str, allowed: list[str], observed: Iterable[str]) -> PolicyMatrixCoverage:
    counts: dict[str, int] = {}
    for value in observed:
        counts[value] = counts.get(value, 0) + 1
    return PolicyMatrixCoverage(
        dimension=dimension,
        allowed_values=allowed,
        observed_values=dict(sorted(counts.items())),
        missing_values=[value for value in allowed if value not in counts],
    )


def policy_matrix_summary(
    entries: list[InternalizationLedgerEntry],
    decisions: list[PolicyDecision],
    findings: list[PolicyMatrixFinding],
    coverage: list[PolicyMatrixCoverage],
) -> dict[str, Any]:
    by_code: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for finding in findings:
        by_code[str(finding.code)] = by_code.get(str(finding.code), 0) + 1
        by_severity[str(finding.severity)] = by_severity.get(str(finding.severity), 0) + 1
    for decision in decisions:
        by_rule[decision.rule_id] = by_rule.get(decision.rule_id, 0) + 1
        by_status[decision.main_path_status] = by_status.get(decision.main_path_status, 0) + 1
    return {
        "entry_count": len(entries),
        "decision_count": len(decisions),
        "allowed_decisions": sum(1 for decision in decisions if decision.allowed),
        "rejected_decisions": sum(1 for decision in decisions if not decision.allowed),
        "connected_entries": sum(1 for entry in entries if entry.main_path_status in CONNECTED_STATUSES),
        "materialized_entries": sum(1 for entry in entries if entry.lifecycle in MATERIALIZED_LIFECYCLES),
        "productized_entries": sum(1 for entry in entries if entry.lifecycle in PRODUCTIZED_LIFECYCLES),
        "source_pool_strategy_entries": sum(1 for entry in entries if entry.migration_strategy in SOURCE_POOL_STRATEGIES),
        "findings_by_code": dict(sorted(by_code.items())),
        "findings_by_severity": dict(sorted(by_severity.items())),
        "decisions_by_rule": dict(sorted(by_rule.items())),
        "decisions_by_status": dict(sorted(by_status.items())),
        "coverage": {item.dimension: item.observed_values for item in coverage},
    }


def max_matrix_severity(left: MatrixSeverity, right: MatrixSeverity) -> MatrixSeverity:
    order = {
        MatrixSeverity.INFO: 0,
        MatrixSeverity.WARNING: 1,
        MatrixSeverity.ERROR: 2,
        MatrixSeverity.BLOCKER: 3,
    }
    return left if order[left] >= order[right] else right


def policy_matrix_payload(report: PolicyMatrixReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity in {MatrixSeverity.ERROR, MatrixSeverity.BLOCKER}
    ]
    payload["warning_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity == MatrixSeverity.WARNING
    ]
    return payload


def assert_policy_matrix(report: PolicyMatrixReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code} {finding.ledger_id}: {finding.message}"
        for finding in report.findings
        if finding.blocking
    )
    raise AssertionError(f"Policy matrix failed:\n{formatted}")
