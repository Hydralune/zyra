from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .ledger_models import (
    InternalizationLedgerEntry,
    LedgerLifecycle,
    MainPathStatus,
    MigrationStrategy,
    to_jsonable,
)
from .ledger_policy import (
    CONNECTED_STATUSES,
    MATERIALIZED_LIFECYCLES,
    classify_path,
    existing_project_targets,
    minimum_effective_lines_for_unit,
    validate_entry_policy,
)
from .ledger_policy_matrix import evaluate_policy_matrix_entry
from .ledger_store import InternalizationLedger


@dataclass(slots=True)
class TargetAccount:
    path: str
    normalized_path: str
    surface: str
    verdict: str
    exists: bool
    ledger_ids: list[str] = field(default_factory=list)
    owner_units: list[str] = field(default_factory=list)
    source_repos: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    required_for_main_path: bool = False
    primary_count: int = 0
    entry_count: int = 0
    is_conflicting: bool = False
    is_data_only: bool = False
    is_effective: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class SourceAccount:
    source_repo: str
    total_entries: int
    owner_units: dict[str, int] = field(default_factory=dict)
    lifecycles: dict[str, int] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)
    strategies: dict[str, int] = field(default_factory=dict)
    target_surfaces: dict[str, int] = field(default_factory=dict)
    target_verdicts: dict[str, int] = field(default_factory=dict)
    target_prefixes: dict[str, int] = field(default_factory=dict)
    effective_target_entries: int = 0
    data_only_entries: int = 0
    existing_target_entries: int = 0
    runtime_bound_entries: int = 0
    test_bound_entries: int = 0
    main_path_bound_entries: int = 0
    materialized_entries: int = 0
    connected_entries: int = 0
    productized_entries: int = 0
    missing_source_evidence_entries: int = 0
    policy_error_entries: int = 0
    policy_warning_entries: int = 0

    @property
    def effective_target_ratio(self) -> float:
        return self.effective_target_entries / self.total_entries if self.total_entries else 0.0

    @property
    def materialized_ratio(self) -> float:
        return self.materialized_entries / self.total_entries if self.total_entries else 0.0

    @property
    def connected_ratio(self) -> float:
        return self.connected_entries / self.total_entries if self.total_entries else 0.0

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["effective_target_ratio"] = self.effective_target_ratio
        payload["materialized_ratio"] = self.materialized_ratio
        payload["connected_ratio"] = self.connected_ratio
        return payload


@dataclass(slots=True)
class UnitAccount:
    owner_unit: str
    milestone: str
    total_entries: int
    minimum_effective_lines: int
    source_repos: dict[str, int] = field(default_factory=dict)
    dependencies: dict[str, int] = field(default_factory=dict)
    downstream_units: dict[str, int] = field(default_factory=dict)
    target_surfaces: dict[str, int] = field(default_factory=dict)
    target_verdicts: dict[str, int] = field(default_factory=dict)
    effective_target_entries: int = 0
    data_only_entries: int = 0
    existing_target_entries: int = 0
    runtime_bound_entries: int = 0
    test_bound_entries: int = 0
    main_path_bound_entries: int = 0
    materialized_entries: int = 0
    connected_entries: int = 0
    productized_entries: int = 0
    blocked_entries: int = 0
    missing_runtime_entries: int = 0
    missing_test_entries: int = 0
    missing_main_path_entries: int = 0
    missing_target_entries: int = 0
    policy_error_entries: int = 0
    policy_warning_entries: int = 0

    @property
    def wiring_ratio(self) -> float:
        if not self.total_entries:
            return 0.0
        wired = self.runtime_bound_entries + self.test_bound_entries + self.main_path_bound_entries
        return wired / (self.total_entries * 3)

    @property
    def target_materialization_ratio(self) -> float:
        return self.existing_target_entries / self.total_entries if self.total_entries else 0.0

    @property
    def debt_count(self) -> int:
        return (
            self.blocked_entries
            + self.missing_runtime_entries
            + self.missing_test_entries
            + self.missing_main_path_entries
            + self.missing_target_entries
            + self.policy_error_entries
        )

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["wiring_ratio"] = self.wiring_ratio
        payload["target_materialization_ratio"] = self.target_materialization_ratio
        payload["debt_count"] = self.debt_count
        return payload


@dataclass(slots=True)
class EntryAccount:
    ledger_id: str
    source_repo: str
    source_path: str
    capability_name: str
    owner_unit: str
    milestone: str
    lifecycle: str
    main_path_status: str
    migration_strategy: str
    target_count: int
    effective_target_count: int
    excluded_target_count: int
    existing_target_count: int
    has_runtime: bool
    has_tests: bool
    has_main_path: bool
    has_notice: bool
    policy_errors: int = 0
    policy_warnings: int = 0
    target_paths: list[str] = field(default_factory=list)
    missing_surfaces: list[str] = field(default_factory=list)

    @property
    def fully_wired(self) -> bool:
        return self.has_runtime and self.has_tests and self.has_main_path

    @property
    def has_effective_code_target(self) -> bool:
        return self.effective_target_count > 0

    @property
    def has_existing_target(self) -> bool:
        return self.existing_target_count > 0

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["fully_wired"] = self.fully_wired
        payload["has_effective_code_target"] = self.has_effective_code_target
        payload["has_existing_target"] = self.has_existing_target
        return payload


@dataclass(slots=True)
class AccountingFinding:
    code: str
    severity: str
    message: str
    owner_unit: str = ""
    source_repo: str = ""
    ledger_id: str = ""
    target_path: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerAccountingReport:
    total_entries: int
    source_accounts: dict[str, SourceAccount]
    unit_accounts: dict[str, UnitAccount]
    target_accounts: dict[str, TargetAccount]
    findings: list[AccountingFinding] = field(default_factory=list)
    entry_accounts: list[EntryAccount] = field(default_factory=list)
    owner_unit: str = ""

    @property
    def ok(self) -> bool:
        return not any(finding.severity in {"error", "blocker"} for finding in self.findings)

    @property
    def has_blocking_findings(self) -> bool:
        return self.blocker_count > 0 or self.error_count > 0

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "blocker")

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "warning")

    @property
    def conflicting_target_count(self) -> int:
        return sum(1 for target in self.target_accounts.values() if target.is_conflicting)

    @property
    def effective_target_count(self) -> int:
        return sum(1 for target in self.target_accounts.values() if target.is_effective)

    @property
    def data_only_target_count(self) -> int:
        return sum(1 for target in self.target_accounts.values() if target.is_data_only)

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "total_entries": self.total_entries,
            "source_repo_count": len(self.source_accounts),
            "owner_unit_count": len(self.unit_accounts),
            "target_path_count": len(self.target_accounts),
            "effective_target_count": self.effective_target_count,
            "data_only_target_count": self.data_only_target_count,
            "conflicting_target_count": self.conflicting_target_count,
            "finding_count": len(self.findings),
            "has_blocking_findings": self.has_blocking_findings,
            "blocker_count": self.blocker_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "owner_unit": self.owner_unit or "all",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "source_accounts": {key: value.to_dict() for key, value in self.source_accounts.items()},
            "unit_accounts": {key: value.to_dict() for key, value in self.unit_accounts.items()},
            "target_accounts": {key: value.to_dict() for key, value in self.target_accounts.items()},
            "findings": [finding.to_dict() for finding in self.findings],
            "entry_accounts": [entry.to_dict() for entry in self.entry_accounts],
        }


def build_accounting_report(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    owner_unit: str = "",
    include_entries: bool = True,
) -> LedgerAccountingReport:
    entries = ledger.by_owner_unit(owner_unit) if owner_unit else ledger.entries()
    entry_accounts = [build_entry_account(project_root, entry) for entry in entries]
    source_accounts = {
        source_repo: build_source_account(project_root, source_repo, grouped)
        for source_repo, grouped in sorted(_group_entries(entries, "source_repo").items())
    }
    unit_accounts = {
        unit: build_unit_account(project_root, unit, grouped)
        for unit, grouped in sorted(_group_entries(entries, "owner_unit").items())
    }
    target_accounts = build_target_accounts(project_root, entries)
    findings = accounting_findings(project_root, entries, target_accounts, entry_accounts)
    return LedgerAccountingReport(
        total_entries=len(entries),
        source_accounts=source_accounts,
        unit_accounts=unit_accounts,
        target_accounts=target_accounts,
        findings=findings,
        entry_accounts=entry_accounts if include_entries else [],
        owner_unit=owner_unit,
    )


def build_entry_account(project_root: Path, entry: InternalizationLedgerEntry) -> EntryAccount:
    classifications = [classify_path(path) for path in entry.target_paths]
    existing_targets = existing_project_targets(project_root, entry)
    effective_count = sum(1 for item in classifications if str(item.verdict) == "effective")
    excluded_count = sum(1 for item in classifications if str(item.verdict) == "excluded")
    existing_count = sum(1 for exists in existing_targets.values() if exists)
    policy_errors, policy_warnings = _entry_policy_counts(entry)
    missing = []
    if entry.runtime_entry.is_empty():
        missing.append("runtime")
    if not entry.test_entries:
        missing.append("tests")
    if entry.main_path.is_empty():
        missing.append("main_path")
    if not existing_count:
        missing.append("target")
    return EntryAccount(
        ledger_id=entry.ledger_id,
        source_repo=entry.source_repo,
        source_path=entry.source_path,
        capability_name=entry.capability_name,
        owner_unit=entry.owner_unit or "unassigned",
        milestone=entry.milestone,
        lifecycle=str(entry.lifecycle),
        main_path_status=str(entry.main_path_status),
        migration_strategy=str(entry.migration_strategy),
        target_count=len(entry.target_paths),
        effective_target_count=effective_count,
        excluded_target_count=excluded_count,
        existing_target_count=existing_count,
        has_runtime=not entry.runtime_entry.is_empty(),
        has_tests=bool(entry.test_entries),
        has_main_path=not entry.main_path.is_empty(),
        has_notice=entry.license_notice is not None,
        policy_errors=policy_errors,
        policy_warnings=policy_warnings,
        target_paths=list(entry.target_paths),
        missing_surfaces=missing,
    )


def build_source_account(project_root: Path, source_repo: str, entries: Iterable[InternalizationLedgerEntry]) -> SourceAccount:
    grouped = list(entries)
    owner_units: Counter[str] = Counter()
    lifecycles: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    strategies: Counter[str] = Counter()
    surfaces: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    prefixes: Counter[str] = Counter()
    metrics = Counter()
    for entry in grouped:
        owner_units[entry.owner_unit or "unassigned"] += 1
        lifecycles[str(entry.lifecycle)] += 1
        statuses[str(entry.main_path_status)] += 1
        strategies[str(entry.migration_strategy)] += 1
        _count_entry_metrics(project_root, entry, metrics)
        for target in entry.target_paths:
            classification = classify_path(target)
            surfaces[str(classification.surface)] += 1
            verdicts[str(classification.verdict)] += 1
            prefixes[_target_prefix(classification.normalized_path)] += 1
    return SourceAccount(
        source_repo=source_repo,
        total_entries=len(grouped),
        owner_units=dict(sorted(owner_units.items())),
        lifecycles=dict(sorted(lifecycles.items())),
        statuses=dict(sorted(statuses.items())),
        strategies=dict(sorted(strategies.items())),
        target_surfaces=dict(sorted(surfaces.items())),
        target_verdicts=dict(sorted(verdicts.items())),
        target_prefixes=dict(sorted(prefixes.items())),
        effective_target_entries=metrics["effective_target_entries"],
        data_only_entries=metrics["data_only_entries"],
        existing_target_entries=metrics["existing_target_entries"],
        runtime_bound_entries=metrics["runtime_bound_entries"],
        test_bound_entries=metrics["test_bound_entries"],
        main_path_bound_entries=metrics["main_path_bound_entries"],
        materialized_entries=metrics["materialized_entries"],
        connected_entries=metrics["connected_entries"],
        productized_entries=metrics["productized_entries"],
        missing_source_evidence_entries=metrics["missing_source_evidence_entries"],
        policy_error_entries=metrics["policy_error_entries"],
        policy_warning_entries=metrics["policy_warning_entries"],
    )


def build_unit_account(project_root: Path, owner_unit: str, entries: Iterable[InternalizationLedgerEntry]) -> UnitAccount:
    grouped = list(entries)
    source_repos: Counter[str] = Counter()
    dependencies: Counter[str] = Counter()
    downstream: Counter[str] = Counter()
    surfaces: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    metrics = Counter()
    milestones = Counter(entry.milestone or "unassigned" for entry in grouped)
    for entry in grouped:
        source_repos[entry.source_repo] += 1
        dependencies.update(item for item in entry.dependencies if item)
        downstream.update(item for item in entry.downstream_units if item)
        _count_entry_metrics(project_root, entry, metrics)
        if entry.blockers:
            metrics["blocked_entries"] += 1
        if entry.runtime_entry.is_empty():
            metrics["missing_runtime_entries"] += 1
        if not entry.test_entries:
            metrics["missing_test_entries"] += 1
        if entry.main_path.is_empty():
            metrics["missing_main_path_entries"] += 1
        if not any(existing_project_targets(project_root, entry).values()):
            metrics["missing_target_entries"] += 1
        for target in entry.target_paths:
            classification = classify_path(target)
            surfaces[str(classification.surface)] += 1
            verdicts[str(classification.verdict)] += 1
    return UnitAccount(
        owner_unit=owner_unit or "unassigned",
        milestone=milestones.most_common(1)[0][0] if milestones else "",
        total_entries=len(grouped),
        minimum_effective_lines=minimum_effective_lines_for_unit(owner_unit),
        source_repos=dict(sorted(source_repos.items())),
        dependencies=dict(sorted(dependencies.items())),
        downstream_units=dict(sorted(downstream.items())),
        target_surfaces=dict(sorted(surfaces.items())),
        target_verdicts=dict(sorted(verdicts.items())),
        effective_target_entries=metrics["effective_target_entries"],
        data_only_entries=metrics["data_only_entries"],
        existing_target_entries=metrics["existing_target_entries"],
        runtime_bound_entries=metrics["runtime_bound_entries"],
        test_bound_entries=metrics["test_bound_entries"],
        main_path_bound_entries=metrics["main_path_bound_entries"],
        materialized_entries=metrics["materialized_entries"],
        connected_entries=metrics["connected_entries"],
        productized_entries=metrics["productized_entries"],
        blocked_entries=metrics["blocked_entries"],
        missing_runtime_entries=metrics["missing_runtime_entries"],
        missing_test_entries=metrics["missing_test_entries"],
        missing_main_path_entries=metrics["missing_main_path_entries"],
        missing_target_entries=metrics["missing_target_entries"],
        policy_error_entries=metrics["policy_error_entries"],
        policy_warning_entries=metrics["policy_warning_entries"],
    )


def build_target_accounts(project_root: Path, entries: Iterable[InternalizationLedgerEntry]) -> dict[str, TargetAccount]:
    raw: dict[str, list[tuple[InternalizationLedgerEntry, str]]] = defaultdict(list)
    for entry in entries:
        for binding in entry.target_bindings:
            classification = classify_path(binding.target_path)
            raw[classification.normalized_path].append((entry, binding.role))
    accounts: dict[str, TargetAccount] = {}
    for normalized_path, pairs in sorted(raw.items()):
        classification = classify_path(normalized_path)
        project_path = project_root / classification.normalized_path
        ledger_ids = sorted({entry.ledger_id for entry, _ in pairs})
        owner_units = sorted({entry.owner_unit or "unassigned" for entry, _ in pairs})
        source_repos = sorted({entry.source_repo for entry, _ in pairs})
        roles = sorted({role for _, role in pairs})
        primary_count = sum(1 for _, role in pairs if role == "primary")
        primary_owner_units = {
            entry.owner_unit or "unassigned"
            for entry, role in pairs
            if role == "primary"
        }
        primary_source_repos = {
            entry.source_repo
            for entry, role in pairs
            if role == "primary"
        }
        required_for_main_path = any(
            binding.required_for_main_path
            for entry, _ in pairs
            for binding in entry.target_bindings
            if classify_path(binding.target_path).normalized_path == normalized_path
        )
        # Multiple sources are expected to contribute supporting mechanisms to
        # one Zyra-owned state machine. Only competing *primary* ownership is a
        # conflict; supporting and source-audit bindings preserve provenance.
        is_conflicting = len(primary_owner_units) > 1 or len(primary_source_repos) > 1
        accounts[normalized_path] = TargetAccount(
            path=normalized_path,
            normalized_path=normalized_path,
            surface=str(classification.surface),
            verdict=str(classification.verdict),
            exists=classification.is_project_relative and project_path.exists(),
            ledger_ids=ledger_ids,
            owner_units=owner_units,
            source_repos=source_repos,
            roles=roles,
            required_for_main_path=required_for_main_path,
            primary_count=primary_count,
            entry_count=len(ledger_ids),
            is_conflicting=is_conflicting,
            is_data_only=classification.is_generated_data,
            is_effective=str(classification.verdict) == "effective",
            reason=classification.reason,
        )
    return accounts


def accounting_findings(
    project_root: Path,
    entries: Iterable[InternalizationLedgerEntry],
    target_accounts: dict[str, TargetAccount],
    entry_accounts: list[EntryAccount],
) -> list[AccountingFinding]:
    findings: list[AccountingFinding] = []
    for target in target_accounts.values():
        if target.is_conflicting:
            findings.append(
                AccountingFinding(
                    code="TARGET_OWNERSHIP_CONFLICT",
                    severity="warning",
                    message=f"Target path is claimed by multiple source repos or owner units: {target.normalized_path}",
                    target_path=target.normalized_path,
                    remediation="Split the target binding, mark secondary roles, or record the integration boundary explicitly.",
                    metadata={
                        "owner_units": target.owner_units,
                        "source_repos": target.source_repos,
                        "ledger_ids": target.ledger_ids,
                    },
                )
            )
        if target.is_data_only and target.entry_count:
            findings.append(
                AccountingFinding(
                    code="TARGET_COUNTS_AS_DATA_ONLY",
                    severity="warning",
                    message=f"Target path is data/seed/inventory and cannot count as effective implementation: {target.normalized_path}",
                    target_path=target.normalized_path,
                    remediation="Pair this record with runtime, audit, API, CLI, or test code before claiming implementation progress.",
                )
            )
    entry_by_id = {entry.ledger_id: entry for entry in entries}
    for account in entry_accounts:
        source_entry = entry_by_id[account.ledger_id]
        if source_entry.lifecycle in MATERIALIZED_LIFECYCLES and not account.has_existing_target:
            findings.append(
                AccountingFinding(
                    code="MATERIALIZED_TARGET_MISSING",
                    severity="error",
                    message="Materialized entry has no existing target path.",
                    owner_unit=account.owner_unit,
                    source_repo=account.source_repo,
                    ledger_id=account.ledger_id,
                    remediation="Create the target module inside Zyra or downgrade lifecycle before advancing.",
                )
            )
        if source_entry.lifecycle in MATERIALIZED_LIFECYCLES and not account.has_effective_code_target:
            findings.append(
                AccountingFinding(
                    code="MATERIALIZED_WITHOUT_EFFECTIVE_TARGET",
                    severity="error",
                    message="Materialized entry is backed only by non-countable targets.",
                    owner_unit=account.owner_unit,
                    source_repo=account.source_repo,
                    ledger_id=account.ledger_id,
                    remediation="Add source/runtime/test target code; seed and inventory files are not enough.",
                )
            )
        if source_entry.main_path_status in CONNECTED_STATUSES and not account.has_main_path:
            findings.append(
                AccountingFinding(
                    code="CONNECTED_WITHOUT_MAIN_PATH",
                    severity="error",
                    message="Connected status lacks API/event/control/worker/UI binding.",
                    owner_unit=account.owner_unit,
                    source_repo=account.source_repo,
                    ledger_id=account.ledger_id,
                    remediation="Record exact main_path surfaces before using connected status.",
                )
            )
        if account.policy_errors:
            findings.append(
                AccountingFinding(
                    code="ENTRY_POLICY_ERRORS",
                    severity="error",
                    message=f"Entry has {account.policy_errors} policy errors.",
                    owner_unit=account.owner_unit,
                    source_repo=account.source_repo,
                    ledger_id=account.ledger_id,
                    remediation="Run ledger audit and fix the policy violations before advancement.",
                )
            )
        matrix_decision = evaluate_policy_matrix_entry(source_entry)
        if matrix_decision.blocking:
            findings.append(
                AccountingFinding(
                    code=str(matrix_decision.code),
                    severity=str(matrix_decision.severity),
                    message=matrix_decision.rationale,
                    owner_unit=account.owner_unit,
                    source_repo=account.source_repo,
                    ledger_id=account.ledger_id,
                    remediation="Downgrade the ledger status or convert the capability into Zyra-owned runtime/main-path code.",
                    metadata=matrix_decision.to_dict(),
                )
            )
        if not source_entry.source_evidence:
            findings.append(
                AccountingFinding(
                    code="SOURCE_EVIDENCE_MISSING",
                    severity="warning",
                    message="Entry has no source_evidence records.",
                    owner_unit=account.owner_unit,
                    source_repo=account.source_repo,
                    ledger_id=account.ledger_id,
                    remediation="Record source files, symbols, or directory evidence used for migration decisions.",
                )
            )
    return sorted(findings, key=lambda item: (item.severity, item.code, item.owner_unit, item.source_repo, item.ledger_id))


def accounting_summary(report: LedgerAccountingReport) -> dict[str, Any]:
    source_rows = [
        {
            "source_repo": source.source_repo,
            "entries": source.total_entries,
            "effective_target_entries": source.effective_target_entries,
            "materialized_entries": source.materialized_entries,
            "connected_entries": source.connected_entries,
            "policy_error_entries": source.policy_error_entries,
        }
        for source in report.source_accounts.values()
    ]
    unit_rows = [
        {
            "owner_unit": unit.owner_unit,
            "entries": unit.total_entries,
            "minimum_effective_lines": unit.minimum_effective_lines,
            "debt_count": unit.debt_count,
            "wiring_ratio": unit.wiring_ratio,
        }
        for unit in report.unit_accounts.values()
    ]
    return {
        **report.summary(),
        "source_rows": source_rows,
        "unit_rows": unit_rows,
    }


def source_account_rows(report: LedgerAccountingReport) -> list[dict[str, Any]]:
    return [
        {
            "source_repo": source.source_repo,
            "total_entries": source.total_entries,
            "effective_target_ratio": source.effective_target_ratio,
            "materialized_ratio": source.materialized_ratio,
            "connected_ratio": source.connected_ratio,
            "runtime_bound_entries": source.runtime_bound_entries,
            "test_bound_entries": source.test_bound_entries,
            "main_path_bound_entries": source.main_path_bound_entries,
            "policy_error_entries": source.policy_error_entries,
            "policy_warning_entries": source.policy_warning_entries,
        }
        for source in report.source_accounts.values()
    ]


def unit_account_rows(report: LedgerAccountingReport) -> list[dict[str, Any]]:
    return [
        {
            "owner_unit": unit.owner_unit,
            "milestone": unit.milestone,
            "total_entries": unit.total_entries,
            "minimum_effective_lines": unit.minimum_effective_lines,
            "effective_target_entries": unit.effective_target_entries,
            "existing_target_entries": unit.existing_target_entries,
            "runtime_bound_entries": unit.runtime_bound_entries,
            "test_bound_entries": unit.test_bound_entries,
            "main_path_bound_entries": unit.main_path_bound_entries,
            "wiring_ratio": unit.wiring_ratio,
            "target_materialization_ratio": unit.target_materialization_ratio,
            "debt_count": unit.debt_count,
        }
        for unit in report.unit_accounts.values()
    ]


def target_conflict_rows(report: LedgerAccountingReport) -> list[dict[str, Any]]:
    rows = []
    for target in report.target_accounts.values():
        if not target.is_conflicting:
            continue
        rows.append(
            {
                "target_path": target.normalized_path,
                "owner_units": target.owner_units,
                "source_repos": target.source_repos,
                "ledger_ids": target.ledger_ids,
                "verdict": target.verdict,
                "exists": target.exists,
            }
        )
    return rows


def unit_completion_profiles(report: LedgerAccountingReport) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for unit in report.unit_accounts.values():
        denominator = max(unit.total_entries, 1)
        profile = {
            "owner_unit": unit.owner_unit,
            "milestone": unit.milestone,
            "entries": unit.total_entries,
            "minimum_effective_lines": unit.minimum_effective_lines,
            "target_materialization_ratio": unit.target_materialization_ratio,
            "wiring_ratio": unit.wiring_ratio,
            "effective_target_ratio": unit.effective_target_entries / denominator,
            "runtime_ratio": unit.runtime_bound_entries / denominator,
            "test_ratio": unit.test_bound_entries / denominator,
            "main_path_ratio": unit.main_path_bound_entries / denominator,
            "materialized_ratio": unit.materialized_entries / denominator,
            "connected_ratio": unit.connected_entries / denominator,
            "debt_count": unit.debt_count,
            "dominant_source_repo": _dominant_key(unit.source_repos),
            "dominant_target_surface": _dominant_key(unit.target_surfaces),
            "dominant_target_verdict": _dominant_key(unit.target_verdicts),
        }
        profile["completion_band"] = _completion_band(
            min(
                profile["target_materialization_ratio"],
                profile["runtime_ratio"],
                profile["test_ratio"],
                profile["main_path_ratio"],
            )
        )
        profiles.append(profile)
    return sorted(profiles, key=lambda item: (str(item["owner_unit"])))


def source_completion_profiles(report: LedgerAccountingReport) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for source in report.source_accounts.values():
        denominator = max(source.total_entries, 1)
        effective = source.effective_target_entries / denominator
        runtime = source.runtime_bound_entries / denominator
        tests = source.test_bound_entries / denominator
        main_path = source.main_path_bound_entries / denominator
        materialized = source.materialized_entries / denominator
        connected = source.connected_entries / denominator
        profile = {
            "source_repo": source.source_repo,
            "entries": source.total_entries,
            "effective_target_ratio": effective,
            "runtime_ratio": runtime,
            "test_ratio": tests,
            "main_path_ratio": main_path,
            "materialized_ratio": materialized,
            "connected_ratio": connected,
            "policy_error_entries": source.policy_error_entries,
            "policy_warning_entries": source.policy_warning_entries,
            "dominant_owner_unit": _dominant_key(source.owner_units),
            "dominant_strategy": _dominant_key(source.strategies),
            "dominant_target_surface": _dominant_key(source.target_surfaces),
        }
        profile["completion_band"] = _completion_band(min(effective, runtime, tests, main_path))
        profiles.append(profile)
    return sorted(profiles, key=lambda item: (str(item["source_repo"])))


def entry_debt_queue(report: LedgerAccountingReport, *, limit: int = 50) -> list[dict[str, Any]]:
    queued = []
    finding_count_by_entry: Counter[str] = Counter(
        finding.ledger_id for finding in report.findings if finding.ledger_id
    )
    for account in report.entry_accounts:
        debt_score = (
            len(account.missing_surfaces) * 10
            + account.policy_errors * 25
            + account.policy_warnings * 5
            + finding_count_by_entry[account.ledger_id] * 8
        )
        if not account.has_effective_code_target:
            debt_score += 20
        if not account.has_existing_target:
            debt_score += 15
        if debt_score <= 0:
            continue
        queued.append(
            {
                "ledger_id": account.ledger_id,
                "owner_unit": account.owner_unit,
                "source_repo": account.source_repo,
                "capability_name": account.capability_name,
                "debt_score": debt_score,
                "missing_surfaces": list(account.missing_surfaces),
                "policy_errors": account.policy_errors,
                "policy_warnings": account.policy_warnings,
                "finding_count": finding_count_by_entry[account.ledger_id],
                "target_paths": list(account.target_paths),
                "recommended_next_action": _recommended_next_action(account),
            }
        )
    return sorted(queued, key=lambda item: (-int(item["debt_score"]), str(item["owner_unit"]), str(item["ledger_id"])))[:limit]


def accounting_markdown(report: LedgerAccountingReport, *, limit: int = 20) -> str:
    summary = report.summary()
    lines = [
        "# Internalization Ledger Accounting",
        "",
        f"- ok: {summary['ok']}",
        f"- owner_unit: {summary['owner_unit']}",
        f"- entries: {summary['total_entries']}",
        f"- source repos: {summary['source_repo_count']}",
        f"- execution units: {summary['owner_unit_count']}",
        f"- target paths: {summary['target_path_count']}",
        f"- findings: {summary['finding_count']} ({summary['error_count']} errors, {summary['warning_count']} warnings)",
        "",
        "## Source Profiles",
        "",
        "| source_repo | entries | effective | runtime | tests | main_path | band |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for profile in source_completion_profiles(report)[:limit]:
        lines.append(
            "| {source_repo} | {entries} | {effective_target_ratio:.2f} | {runtime_ratio:.2f} | "
            "{test_ratio:.2f} | {main_path_ratio:.2f} | {completion_band} |".format(**profile)
        )
    lines.extend(
        [
            "",
            "## Unit Profiles",
            "",
            "| owner_unit | entries | targets | wiring | debt | band |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for profile in unit_completion_profiles(report)[:limit]:
        lines.append(
            "| {owner_unit} | {entries} | {target_materialization_ratio:.2f} | "
            "{wiring_ratio:.2f} | {debt_count} | {completion_band} |".format(**profile)
        )
    debt = entry_debt_queue(report, limit=limit)
    if debt:
        lines.extend(
            [
                "",
                "## Debt Queue",
                "",
                "| ledger_id | owner_unit | source_repo | score | next_action |",
                "| --- | --- | --- | ---: | --- |",
            ]
        )
        for item in debt:
            lines.append(
                f"| {item['ledger_id']} | {item['owner_unit']} | {item['source_repo']} | "
                f"{item['debt_score']} | {item['recommended_next_action']} |"
            )
    return "\n".join(lines) + "\n"


def assert_accounting_report(report: LedgerAccountingReport) -> None:
    if report.ok:
        return
    messages = [
        f"{finding.severity} {finding.code} {finding.ledger_id or finding.target_path}: {finding.message}".strip()
        for finding in report.findings
        if finding.severity in {"error", "blocker"}
    ]
    raise AssertionError("\n".join(messages) or "ledger accounting report failed")


def _group_entries(entries: Iterable[InternalizationLedgerEntry], field_name: str) -> dict[str, list[InternalizationLedgerEntry]]:
    grouped: dict[str, list[InternalizationLedgerEntry]] = defaultdict(list)
    for entry in entries:
        value = str(getattr(entry, field_name) or "unassigned")
        grouped[value].append(entry)
    return grouped


def _count_entry_metrics(project_root: Path, entry: InternalizationLedgerEntry, metrics: Counter[str]) -> None:
    classifications = [classify_path(path) for path in entry.target_paths]
    existing_targets = existing_project_targets(project_root, entry)
    if any(str(classification.verdict) == "effective" for classification in classifications):
        metrics["effective_target_entries"] += 1
    if classifications and all(classification.is_generated_data for classification in classifications):
        metrics["data_only_entries"] += 1
    if any(existing_targets.values()):
        metrics["existing_target_entries"] += 1
    if not entry.runtime_entry.is_empty():
        metrics["runtime_bound_entries"] += 1
    if entry.test_entries:
        metrics["test_bound_entries"] += 1
    if not entry.main_path.is_empty():
        metrics["main_path_bound_entries"] += 1
    if entry.lifecycle in MATERIALIZED_LIFECYCLES:
        metrics["materialized_entries"] += 1
    if entry.main_path_status in CONNECTED_STATUSES:
        metrics["connected_entries"] += 1
    if entry.lifecycle == LedgerLifecycle.PRODUCTIZED:
        metrics["productized_entries"] += 1
    if not entry.source_evidence:
        metrics["missing_source_evidence_entries"] += 1
    policy_errors, policy_warnings = _entry_policy_counts(entry)
    if policy_errors:
        metrics["policy_error_entries"] += 1
    if policy_warnings:
        metrics["policy_warning_entries"] += 1


def _entry_policy_counts(entry: InternalizationLedgerEntry) -> tuple[int, int]:
    policy_findings = validate_entry_policy(entry)
    policy_errors = sum(1 for finding in policy_findings if str(finding.severity) in {"error", "blocker"})
    policy_warnings = sum(1 for finding in policy_findings if str(finding.severity) == "warning")
    matrix_decision = evaluate_policy_matrix_entry(entry)
    if matrix_decision.blocking:
        policy_errors += 1
    elif str(matrix_decision.severity) == "warning":
        policy_warnings += 1
    return policy_errors, policy_warnings


def _target_prefix(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "missing"
    return "/".join(parts[:2])


def _dominant_key(counts: dict[str, int]) -> str:
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _completion_band(value: float) -> str:
    if value >= 0.9:
        return "complete"
    if value >= 0.66:
        return "strong"
    if value >= 0.33:
        return "partial"
    if value > 0:
        return "thin"
    return "empty"


def _recommended_next_action(account: EntryAccount) -> str:
    if not account.has_effective_code_target:
        return "add effective source/runtime/test target"
    if not account.has_existing_target:
        return "materialize recorded target inside zyra"
    if account.policy_errors:
        return "fix blocking ledger policy errors"
    if "runtime" in account.missing_surfaces:
        return "record runtime entry"
    if "tests" in account.missing_surfaces:
        return "add test entry"
    if "main_path" in account.missing_surfaces:
        return "bind API/event/control/worker/UI surface"
    if account.policy_warnings:
        return "resolve policy warnings"
    return "review accounting finding"
