from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .ledger_audit import AuditSeverity, InternalizationLedgerAuditor, LedgerAuditFinding, LedgerAuditReport
from .ledger_accounting import accounting_summary, build_accounting_report
from .ledger_linecount import EffectiveLineCountReport
from .ledger_matrix import build_unit_matrix
from .ledger_health import health_payload
from .ledger_contracts import build_contract_report, contract_summary
from .ledger_models import InternalizationLedgerEntry, LedgerLifecycle, MainPathStatus, MigrationStrategy, to_jsonable
from .ledger_policy import (
    CONNECTED_STATUSES,
    MATERIALIZED_LIFECYCLES,
    PathClassification,
    classify_path,
    effective_target_paths,
    excluded_target_paths,
    minimum_effective_lines_for_unit,
    validate_entry_policy,
)
from .ledger_store import InternalizationLedger


@dataclass(slots=True)
class LedgerEntryReadiness:
    ledger_id: str
    source_repo: str
    source_path: str
    capability_name: str
    owner_unit: str
    lifecycle: str
    main_path_status: str
    ready_for_advance: bool
    ready_for_internalization: bool
    ready_for_productization: bool
    target_exists: bool
    has_effective_target: bool
    has_runtime: bool
    has_tests: bool
    has_main_path: bool
    has_notice_resolution: bool
    blocking_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    effective_targets: list[str] = field(default_factory=list)
    excluded_targets: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class UnitReadinessReport:
    owner_unit: str
    total_entries: int
    ready_for_advance: int
    ready_for_internalization: int
    ready_for_productization: int
    blocked_entries: int
    planned_entries: int
    materialized_entries: int
    connected_entries: int
    missing_target_entries: int
    missing_runtime_entries: int
    missing_test_entries: int
    missing_main_path_entries: int
    minimum_effective_lines: int
    effective_added: int = 0
    line_count_ok: bool = False
    entries: list[LedgerEntryReadiness] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    debt: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            self.blocked_entries == 0
            and self.missing_runtime_entries == 0
            and self.missing_test_entries == 0
            and self.line_count_ok
        )

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["ok"] = self.ok
        return payload


@dataclass(slots=True)
class SourceCoverage:
    source_repo: str
    total_entries: int
    owner_units: dict[str, int] = field(default_factory=dict)
    lifecycles: dict[str, int] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)
    strategies: dict[str, int] = field(default_factory=dict)
    target_prefixes: dict[str, int] = field(default_factory=dict)
    effective_target_count: int = 0
    data_only_target_count: int = 0
    planned_count: int = 0
    materialized_count: int = 0
    connected_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerCoverageReport:
    total_entries: int
    by_source_repo: dict[str, SourceCoverage] = field(default_factory=dict)
    by_owner_unit: dict[str, int] = field(default_factory=dict)
    by_surface: dict[str, int] = field(default_factory=dict)
    by_line_count_verdict: dict[str, int] = field(default_factory=dict)
    missing_required_repos: list[str] = field(default_factory=list)
    data_only_records: int = 0
    effective_target_records: int = 0
    materialized_records: int = 0
    connected_records: int = 0

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerDebtItem:
    ledger_id: str
    owner_unit: str
    source_repo: str
    capability_name: str
    severity: str
    category: str
    message: str
    remediation: str = ""
    target_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerDebtReport:
    total_debt_items: int
    by_owner_unit: dict[str, int] = field(default_factory=dict)
    by_source_repo: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)
    items: list[LedgerDebtItem] = field(default_factory=list)

    @property
    def blocking_debt_count(self) -> int:
        return sum(1 for item in self.items if item.severity in {"error", "blocker"})

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocking_debt_count"] = self.blocking_debt_count
        return payload


def build_entry_readiness(project_root: Path, entry: InternalizationLedgerEntry) -> LedgerEntryReadiness:
    target_exists_map = {
        target: (project_root / classify_path(target).normalized_path).exists()
        for target in entry.target_paths
        if classify_path(target).is_project_relative
    }
    target_exists = any(target_exists_map.values())
    has_effective_target = bool(effective_target_paths(entry))
    has_runtime = not entry.runtime_entry.is_empty()
    has_tests = bool(entry.test_entries)
    has_main_path = not entry.main_path.is_empty()
    notice_status = entry.license_notice.status if entry.license_notice else None
    has_notice_resolution = notice_status is not None and str(notice_status) in {"recorded", "not_required"}
    blocking: list[str] = []
    warnings: list[str] = []
    if entry.lifecycle in MATERIALIZED_LIFECYCLES and not target_exists:
        blocking.append("materialized lifecycle requires existing target path")
    if entry.lifecycle in MATERIALIZED_LIFECYCLES and not has_runtime:
        blocking.append("materialized lifecycle requires runtime entry")
    if entry.lifecycle in MATERIALIZED_LIFECYCLES and not has_tests:
        blocking.append("materialized lifecycle requires tests")
    if entry.main_path_status in CONNECTED_STATUSES and not has_main_path:
        blocking.append("connected status requires main-path binding")
    if not has_effective_target:
        warnings.append("entry has no effective source/runtime/test target path")
    if excluded_target_paths(entry):
        warnings.append("entry has data/documentation/review-only target paths")
    policy_findings = validate_entry_policy(entry)
    for finding in policy_findings:
        if str(finding.severity) in {"error", "blocker"}:
            blocking.append(f"{finding.code}: {finding.message}")
        else:
            warnings.append(f"{finding.code}: {finding.message}")
    ready_for_internalization = target_exists and has_effective_target and has_runtime and has_tests and not blocking
    ready_for_productization = ready_for_internalization and has_main_path and has_notice_resolution
    ready_for_advance = not blocking
    return LedgerEntryReadiness(
        ledger_id=entry.ledger_id,
        source_repo=entry.source_repo,
        source_path=entry.source_path,
        capability_name=entry.capability_name,
        owner_unit=entry.owner_unit,
        lifecycle=str(entry.lifecycle),
        main_path_status=str(entry.main_path_status),
        ready_for_advance=ready_for_advance,
        ready_for_internalization=ready_for_internalization,
        ready_for_productization=ready_for_productization,
        target_exists=target_exists,
        has_effective_target=has_effective_target,
        has_runtime=has_runtime,
        has_tests=has_tests,
        has_main_path=has_main_path,
        has_notice_resolution=has_notice_resolution,
        blocking_reasons=sorted(set(blocking)),
        warnings=sorted(set(warnings)),
        effective_targets=effective_target_paths(entry),
        excluded_targets=[item.to_dict() for item in excluded_target_paths(entry)],
    )


def build_unit_readiness_report(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    owner_unit: str,
    audit_report: LedgerAuditReport | None = None,
    line_count_report: EffectiveLineCountReport | None = None,
) -> UnitReadinessReport:
    entries = ledger.by_owner_unit(owner_unit) if owner_unit else ledger.entries()
    readiness = [build_entry_readiness(project_root, entry) for entry in entries]
    materialized = sum(1 for entry in entries if entry.lifecycle in MATERIALIZED_LIFECYCLES)
    connected = sum(1 for entry in entries if entry.main_path_status in CONNECTED_STATUSES)
    planned = sum(1 for entry in entries if entry.lifecycle in {LedgerLifecycle.CANDIDATE, LedgerLifecycle.PLANNED, LedgerLifecycle.IN_PROGRESS})
    blocked = sum(1 for item in readiness if item.blocking_reasons)
    debt = Counter()
    for item in readiness:
        if not item.target_exists:
            debt["missing_target"] += 1
        if not item.has_runtime:
            debt["missing_runtime"] += 1
        if not item.has_tests:
            debt["missing_test"] += 1
        if not item.has_main_path:
            debt["missing_main_path"] += 1
        if not item.has_effective_target:
            debt["no_effective_target"] += 1
    minimum = minimum_effective_lines_for_unit(owner_unit)
    effective_added = line_count_report.effective_added if line_count_report else 0
    line_count_ok = True if minimum <= 0 else bool(line_count_report and line_count_report.ok)
    return UnitReadinessReport(
        owner_unit=owner_unit or "all",
        total_entries=len(entries),
        ready_for_advance=sum(1 for item in readiness if item.ready_for_advance),
        ready_for_internalization=sum(1 for item in readiness if item.ready_for_internalization),
        ready_for_productization=sum(1 for item in readiness if item.ready_for_productization),
        blocked_entries=blocked,
        planned_entries=planned,
        materialized_entries=materialized,
        connected_entries=connected,
        missing_target_entries=debt["missing_target"],
        missing_runtime_entries=debt["missing_runtime"],
        missing_test_entries=debt["missing_test"],
        missing_main_path_entries=debt["missing_main_path"],
        minimum_effective_lines=minimum,
        effective_added=effective_added,
        line_count_ok=line_count_ok,
        entries=readiness,
        audit=_audit_scope_payload(audit_report, owner_unit),
        debt=dict(sorted(debt.items())),
    )


def build_coverage_report(ledger: InternalizationLedger, *, required_repos: Iterable[str] = ()) -> LedgerCoverageReport:
    required = set(required_repos)
    by_source: dict[str, list[InternalizationLedgerEntry]] = defaultdict(list)
    by_owner: Counter[str] = Counter()
    by_surface: Counter[str] = Counter()
    by_verdict: Counter[str] = Counter()
    data_only_records = 0
    effective_records = 0
    materialized_records = 0
    connected_records = 0
    for entry in ledger.entries():
        by_source[entry.source_repo].append(entry)
        by_owner[entry.owner_unit or "unassigned"] += 1
        classifications = [classify_path(path) for path in entry.target_paths]
        for classification in classifications:
            by_surface[str(classification.surface)] += 1
            by_verdict[str(classification.verdict)] += 1
        if classifications and all(classification.is_generated_data for classification in classifications):
            data_only_records += 1
        if any(str(classification.verdict) == "effective" for classification in classifications):
            effective_records += 1
        if entry.lifecycle in MATERIALIZED_LIFECYCLES:
            materialized_records += 1
        if entry.main_path_status in CONNECTED_STATUSES:
            connected_records += 1
    source_payload = {
        repo: _build_source_coverage(repo, entries)
        for repo, entries in sorted(by_source.items())
    }
    missing = sorted(required - set(by_source)) if required else []
    return LedgerCoverageReport(
        total_entries=len(ledger),
        by_source_repo=source_payload,
        by_owner_unit=dict(sorted(by_owner.items())),
        by_surface=dict(sorted(by_surface.items())),
        by_line_count_verdict=dict(sorted(by_verdict.items())),
        missing_required_repos=missing,
        data_only_records=data_only_records,
        effective_target_records=effective_records,
        materialized_records=materialized_records,
        connected_records=connected_records,
    )


def build_debt_report(audit_report: LedgerAuditReport, *, owner_unit: str = "", source_repo: str = "") -> LedgerDebtReport:
    items: list[LedgerDebtItem] = []
    for finding in audit_report.findings:
        if owner_unit and finding.owner_unit != owner_unit:
            continue
        if source_repo and finding.source_repo != source_repo:
            continue
        items.append(_finding_to_debt_item(finding))
    by_owner = Counter(item.owner_unit or "unassigned" for item in items)
    by_repo = Counter(item.source_repo or "global" for item in items)
    by_category = Counter(item.category for item in items)
    by_severity = Counter(item.severity for item in items)
    return LedgerDebtReport(
        total_debt_items=len(items),
        by_owner_unit=dict(sorted(by_owner.items())),
        by_source_repo=dict(sorted(by_repo.items())),
        by_category=dict(sorted(by_category.items())),
        by_severity=dict(sorted(by_severity.items())),
        items=items,
    )


def build_full_ledger_report(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    owner_unit: str = "",
    line_count_report: EffectiveLineCountReport | None = None,
) -> dict[str, Any]:
    audit = InternalizationLedgerAuditor(project_root, strict=True).audit(ledger)
    coverage = build_coverage_report(ledger)
    debt = build_debt_report(audit, owner_unit=owner_unit)
    accounting = build_accounting_report(project_root, ledger, owner_unit=owner_unit, include_entries=False)
    readiness = build_unit_readiness_report(
        project_root,
        ledger,
        owner_unit=owner_unit,
        audit_report=audit,
        line_count_report=line_count_report,
    )
    return {
        "summary": ledger.summary().to_dict(),
        "contracts": contract_summary(build_contract_report()),
        "coverage": coverage.to_dict(),
        "accounting": accounting_summary(accounting),
        "unit_matrix": build_unit_matrix(ledger).to_dict(),
        "audit": audit.to_dict(),
        "debt": debt.to_dict(),
        "readiness": readiness.to_dict(),
        "health": health_payload(
            audit=audit,
            coverage=coverage,
            debt=debt,
            readiness=readiness,
            matrix=build_unit_matrix(ledger),
            line_count=line_count_report,
        ),
        "line_count": line_count_report.to_dict() if line_count_report else None,
    }


def _build_source_coverage(source_repo: str, entries: list[InternalizationLedgerEntry]) -> SourceCoverage:
    owner_units = Counter(entry.owner_unit or "unassigned" for entry in entries)
    lifecycles = Counter(str(entry.lifecycle) for entry in entries)
    statuses = Counter(str(entry.main_path_status) for entry in entries)
    strategies = Counter(str(entry.migration_strategy) for entry in entries)
    prefixes: Counter[str] = Counter()
    effective_target_count = 0
    data_only_target_count = 0
    planned = 0
    materialized = 0
    connected = 0
    for entry in entries:
        if entry.lifecycle in {LedgerLifecycle.CANDIDATE, LedgerLifecycle.PLANNED, LedgerLifecycle.IN_PROGRESS}:
            planned += 1
        if entry.lifecycle in MATERIALIZED_LIFECYCLES:
            materialized += 1
        if entry.main_path_status in CONNECTED_STATUSES:
            connected += 1
        classifications = [classify_path(path) for path in entry.target_paths]
        for classification in classifications:
            prefix = "/".join(classification.normalized_path.split("/")[:2]) if classification.normalized_path else "missing"
            prefixes[prefix] += 1
            if str(classification.verdict) == "effective":
                effective_target_count += 1
            if classification.is_generated_data:
                data_only_target_count += 1
    return SourceCoverage(
        source_repo=source_repo,
        total_entries=len(entries),
        owner_units=dict(sorted(owner_units.items())),
        lifecycles=dict(sorted(lifecycles.items())),
        statuses=dict(sorted(statuses.items())),
        strategies=dict(sorted(strategies.items())),
        target_prefixes=dict(sorted(prefixes.items())),
        effective_target_count=effective_target_count,
        data_only_target_count=data_only_target_count,
        planned_count=planned,
        materialized_count=materialized,
        connected_count=connected,
    )


def _finding_to_debt_item(finding: LedgerAuditFinding) -> LedgerDebtItem:
    severity = str(finding.severity)
    category = str(finding.code)
    return LedgerDebtItem(
        ledger_id=finding.ledger_id,
        owner_unit=finding.owner_unit,
        source_repo=finding.source_repo,
        capability_name=finding.metadata.get("capability_name", ""),
        severity=severity,
        category=category,
        message=finding.message,
        remediation=finding.remediation,
        target_path=finding.target_path,
    )


def _audit_scope_payload(report: LedgerAuditReport | None, owner_unit: str) -> dict[str, Any]:
    if report is None:
        return {}
    scoped = [
        finding
        for finding in report.findings
        if not owner_unit or finding.owner_unit == owner_unit or not finding.owner_unit
    ]
    return {
        "ok": report.ok,
        "disposition": str(report.disposition),
        "strict": report.strict,
        "finding_count": len(scoped),
        "error_count": sum(1 for finding in scoped if finding.severity == AuditSeverity.ERROR),
        "blocker_count": sum(1 for finding in scoped if finding.severity == AuditSeverity.BLOCKER),
        "warning_count": sum(1 for finding in scoped if finding.severity == AuditSeverity.WARNING),
    }
