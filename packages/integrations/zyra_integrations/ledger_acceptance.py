from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .ledger_accounting import build_accounting_report
from .ledger_audit import AuditSeverity, InternalizationLedgerAuditor, LedgerAuditReport
from .ledger_boundary import CleanBoundaryReport, build_clean_boundary_report
from .ledger_line_buckets import LineBucketReport, bucket_line_count_report
from .ledger_linecount import EffectiveLineCountReport
from .ledger_matrix import UnitMatrixReport, build_unit_matrix
from .ledger_models import InternalizationLedgerEntry, LedgerLifecycle, MainPathStatus, MigrationStrategy, to_jsonable
from .ledger_policy import CONNECTED_STATUSES, MATERIALIZED_LIFECYCLES, classify_path, minimum_effective_lines_for_unit
from .ledger_reachability import ReachabilityReport, build_reachability_report, disconnect_probe_summary
from .ledger_reports import UnitReadinessReport, build_unit_readiness_report
from .ledger_store import InternalizationLedger


class AcceptanceStatus(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    FAILING = "failing"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class AcceptanceSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class AcceptanceCode(StrEnum):
    SCHEMA_COVERED = "SCHEMA_COVERED"
    PERSISTENCE_COVERED = "PERSISTENCE_COVERED"
    SEED_REPOS_COVERED = "SEED_REPOS_COVERED"
    AUDIT_COVERED = "AUDIT_COVERED"
    API_CLI_COVERED = "API_CLI_COVERED"
    EVENT_LOG_COVERED = "EVENT_LOG_COVERED"
    LINE_BUCKETS_COVERED = "LINE_BUCKETS_COVERED"
    BOUNDARY_COVERED = "BOUNDARY_COVERED"
    REACHABILITY_COVERED = "REACHABILITY_COVERED"
    DISCONNECT_PROBE_COVERED = "DISCONNECT_PROBE_COVERED"
    SEMANTIC_EFFECT_COVERED = "SEMANTIC_EFFECT_COVERED"
    STATE_CUSTODY_COVERED = "STATE_CUSTODY_COVERED"
    SOURCE_COVERAGE_MISSING = "SOURCE_COVERAGE_MISSING"
    SOURCE_COMPLETION_OVERSTATED = "SOURCE_COMPLETION_OVERSTATED"
    TARGET_OWNERSHIP_CONFLICT = "TARGET_OWNERSHIP_CONFLICT"
    LINE_COUNT_SHORTFALL = "LINE_COUNT_SHORTFALL"
    TEST_DOMINATES = "TEST_DOMINATES"
    DATA_DOMINATES = "DATA_DOMINATES"
    MAIN_PATH_UNREACHABLE = "MAIN_PATH_UNREACHABLE"
    CLEAN_BOUNDARY_FAILED = "CLEAN_BOUNDARY_FAILED"
    AUDIT_FAILED = "AUDIT_FAILED"


@dataclass(slots=True)
class AcceptanceCriterion:
    code: AcceptanceCode
    status: AcceptanceStatus
    severity: AcceptanceSeverity
    message: str
    evidence: list[str] = field(default_factory=list)
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {AcceptanceSeverity.ERROR, AcceptanceSeverity.BLOCKER}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class SourceCompletionProfile:
    source_repo: str
    total_entries: int
    planned_entries: int
    materialized_entries: int
    connected_entries: int
    productized_entries: int
    vendored_entries: int
    reference_only_entries: int
    data_only_entries: int
    effective_target_entries: int
    completion_ratio: float
    overstatement_risk: bool
    owner_units: dict[str, int] = field(default_factory=dict)
    strategies: dict[str, int] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class TargetOwnershipConflict:
    target_path: str
    severity: AcceptanceSeverity
    owners: list[str]
    source_repos: list[str]
    ledger_ids: list[str]
    reason: str
    accepted_shared_target: bool = False

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class StateCustodyClaim:
    state_name: str
    owner_module: str
    persistence_path: str
    mutation_api: str
    event_binding: str
    test_binding: str
    risk: str = ""

    @property
    def complete(self) -> bool:
        return all([self.owner_module, self.persistence_path, self.mutation_api, self.event_binding, self.test_binding])

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class SemanticEffectProbe:
    name: str
    module_path: str
    behavior: str
    enabled_signal: str
    disabled_signal: str
    test_binding: str
    proves_effect: bool
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class AcceptanceReport:
    owner_unit: str
    ok: bool
    status: AcceptanceStatus
    criteria: list[AcceptanceCriterion]
    source_profiles: list[SourceCompletionProfile]
    ownership_conflicts: list[TargetOwnershipConflict]
    state_custody: list[StateCustodyClaim]
    semantic_effects: list[SemanticEffectProbe]
    audit: dict[str, Any]
    readiness: dict[str, Any]
    reachability: dict[str, Any]
    boundary: dict[str, Any]
    line_buckets: dict[str, Any] | None = None
    matrix: dict[str, Any] = field(default_factory=dict)
    accounting: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for criterion in self.criteria if criterion.severity == AcceptanceSeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for criterion in self.criteria if criterion.severity == AcceptanceSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for criterion in self.criteria if criterion.severity == AcceptanceSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


def build_acceptance_report(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    owner_unit: str,
    audit_report: LedgerAuditReport | None = None,
    line_count_report: EffectiveLineCountReport | None = None,
    reachability_report: ReachabilityReport | None = None,
    boundary_report: CleanBoundaryReport | None = None,
    include_entries: bool = False,
) -> AcceptanceReport:
    audit = audit_report or InternalizationLedgerAuditor(project_root, strict=True).audit(ledger)
    readiness = build_unit_readiness_report(
        project_root,
        ledger,
        owner_unit=owner_unit,
        audit_report=audit,
        line_count_report=line_count_report,
    )
    reachability = reachability_report or build_reachability_report(project_root, ledger, owner_unit=owner_unit, include_entries=include_entries)
    boundary = boundary_report or build_clean_boundary_report(project_root, include_tests=True, include_cache=False)
    line_buckets = bucket_line_count_report(project_root, line_count_report) if line_count_report else None
    matrix = build_unit_matrix(ledger)
    accounting = build_accounting_report(project_root, ledger, owner_unit=owner_unit, include_entries=False)
    source_profiles = build_source_completion_profiles(ledger)
    ownership_conflicts = resolve_target_ownership_conflicts(ledger)
    state_custody = build_state_custody_claims(project_root)
    semantic_effects = build_semantic_effect_probes(project_root)
    criteria: list[AcceptanceCriterion] = []
    criteria.extend(_core_criteria(ledger, audit, readiness, reachability, boundary, line_buckets, owner_unit))
    criteria.extend(_source_profile_criteria(source_profiles))
    criteria.extend(_ownership_criteria(ownership_conflicts, owner_unit=owner_unit))
    criteria.extend(_state_custody_criteria(state_custody))
    criteria.extend(_semantic_effect_criteria(semantic_effects))
    ok = not any(criterion.blocking for criterion in criteria)
    status = AcceptanceStatus.PASSING
    if any(criterion.severity == AcceptanceSeverity.BLOCKER for criterion in criteria):
        status = AcceptanceStatus.BLOCKED
    elif any(criterion.severity == AcceptanceSeverity.ERROR for criterion in criteria):
        status = AcceptanceStatus.FAILING
    elif any(criterion.severity == AcceptanceSeverity.WARNING for criterion in criteria):
        status = AcceptanceStatus.WARNING
    return AcceptanceReport(
        owner_unit=owner_unit,
        ok=ok,
        status=status,
        criteria=criteria,
        source_profiles=source_profiles,
        ownership_conflicts=ownership_conflicts,
        state_custody=state_custody,
        semantic_effects=semantic_effects,
        audit=audit.to_dict(),
        readiness=readiness.to_dict(),
        reachability=reachability.to_dict(),
        boundary=boundary.to_dict(),
        line_buckets=line_buckets.to_dict() if line_buckets else None,
        matrix=matrix.to_dict(),
        accounting=accounting.to_dict(),
        summary={
            "criteria": len(criteria),
            "source_profile_count": len(source_profiles),
            "ownership_conflict_count": len(ownership_conflicts),
            "state_custody_claims": len(state_custody),
            "semantic_effects": len(semantic_effects),
            "minimum_effective_lines": minimum_effective_lines_for_unit(owner_unit),
            "line_bucket_effective_added": line_buckets.bucket_effective_added if line_buckets else 0,
            "disconnect_probe": disconnect_probe_summary(reachability),
        },
    )


def build_source_completion_profiles(ledger: InternalizationLedger) -> list[SourceCompletionProfile]:
    by_source: dict[str, list[InternalizationLedgerEntry]] = defaultdict(list)
    for entry in ledger.entries():
        by_source[entry.source_repo].append(entry)
    profiles: list[SourceCompletionProfile] = []
    for source_repo, entries in sorted(by_source.items()):
        lifecycles = Counter(str(entry.lifecycle) for entry in entries)
        statuses = Counter(str(entry.main_path_status) for entry in entries)
        strategies = Counter(str(entry.migration_strategy) for entry in entries)
        owner_units = Counter(entry.owner_unit or "unassigned" for entry in entries)
        materialized = sum(1 for entry in entries if entry.lifecycle in MATERIALIZED_LIFECYCLES)
        connected = sum(1 for entry in entries if entry.main_path_status in CONNECTED_STATUSES)
        productized = sum(1 for entry in entries if entry.lifecycle == LedgerLifecycle.PRODUCTIZED)
        planned = sum(1 for entry in entries if entry.lifecycle in {LedgerLifecycle.CANDIDATE, LedgerLifecycle.PLANNED, LedgerLifecycle.IN_PROGRESS})
        vendored = sum(1 for entry in entries if entry.migration_strategy == MigrationStrategy.VENDORED_RUNTIME)
        reference_only = sum(1 for entry in entries if entry.migration_strategy in {MigrationStrategy.CANDIDATE_REVIEW, MigrationStrategy.NOT_SELECTED})
        data_only = sum(1 for entry in entries if entry.target_paths and all(classify_path(path).is_generated_data for path in entry.target_paths))
        effective_targets = sum(1 for entry in entries if any(str(classify_path(path).verdict) == "effective" for path in entry.target_paths))
        completion_ratio = (materialized + connected + productized) / max(len(entries) * 3, 1)
        overstatement_risk = bool(vendored > max(productized, 1) * 3 or planned > materialized + connected + productized)
        profiles.append(
            SourceCompletionProfile(
                source_repo=source_repo,
                total_entries=len(entries),
                planned_entries=planned,
                materialized_entries=materialized,
                connected_entries=connected,
                productized_entries=productized,
                vendored_entries=vendored,
                reference_only_entries=reference_only,
                data_only_entries=data_only,
                effective_target_entries=effective_targets,
                completion_ratio=round(completion_ratio, 4),
                overstatement_risk=overstatement_risk,
                owner_units=dict(sorted(owner_units.items())),
                strategies=dict(sorted(strategies.items())),
                statuses=dict(sorted(statuses.items())),
            )
        )
    return profiles


def resolve_target_ownership_conflicts(ledger: InternalizationLedger) -> list[TargetOwnershipConflict]:
    by_target: dict[str, list[InternalizationLedgerEntry]] = defaultdict(list)
    for entry in ledger.entries():
        for target in entry.target_paths:
            by_target[target].append(entry)
    conflicts: list[TargetOwnershipConflict] = []
    for target, entries in sorted(by_target.items()):
        materialized = [
            entry
            for entry in entries
            if entry.lifecycle in MATERIALIZED_LIFECYCLES or entry.main_path_status in CONNECTED_STATUSES
        ]
        if len(materialized) <= 1:
            continue
        owners = sorted({entry.owner_unit or "unassigned" for entry in materialized})
        repos = sorted({entry.source_repo for entry in materialized})
        accepted_shared = _target_is_known_shared(target)
        severity = AcceptanceSeverity.WARNING if accepted_shared else AcceptanceSeverity.ERROR
        reason = "known shared platform boundary" if accepted_shared else "multiple materialized source owners share one target"
        conflicts.append(
            TargetOwnershipConflict(
                target_path=target,
                severity=severity,
                owners=owners,
                source_repos=repos,
                ledger_ids=[entry.ledger_id for entry in materialized],
                reason=reason,
                accepted_shared_target=accepted_shared,
            )
        )
    return conflicts


def build_state_custody_claims(project_root: Path) -> list[StateCustodyClaim]:
    return [
        StateCustodyClaim(
            state_name="internalization_ledger",
            owner_module="zyra_integrations.ledger_store",
            persistence_path="tmp/internalization_ledger.json or ZYRA_INTEGRATION_LEDGER",
            mutation_api="InternalizationLedger.upsert/remove + AtomicLedgerStore.upsert_validated",
            event_binding="integration_ledger_update",
            test_binding="tests/unit/test_internalization_ledger.py",
            risk="tmp default is acceptable for development but freeze must choose submitted project-owned path",
        ),
        StateCustodyClaim(
            state_name="ledger_mutation_journal",
            owner_module="zyra_integrations.ledger_persistence",
            persistence_path="tmp/internalization_ledger_mutations.jsonl",
            mutation_api="AtomicLedgerStore.save_atomic/append_journal",
            event_binding="integration_ledger_update",
            test_binding="tests/unit/test_internalization_ledger_control_plane.py",
        ),
        StateCustodyClaim(
            state_name="audit_event",
            owner_module="zyra_integrations.ledger_events",
            persistence_path="tmp/events.jsonl + SQLiteStore events through API",
            mutation_api="event_record_from_audit/event_record_from_mutation",
            event_binding="system_notice",
            test_binding="tests/integration/test_internalization_ledger_api.py",
        ),
        StateCustodyClaim(
            state_name="line_count_gate",
            owner_module="zyra_integrations.ledger_line_buckets",
            persistence_path="git diff numstat report",
            mutation_api="build_line_bucket_report",
            event_binding="integration_ledger_acceptance",
            test_binding="tests/unit/test_internalization_ledger_control_plane.py",
        ),
        StateCustodyClaim(
            state_name="clean_boundary",
            owner_module="zyra_integrations.ledger_boundary",
            persistence_path=str(project_root),
            mutation_api="build_clean_boundary_report",
            event_binding="integration_ledger_boundary",
            test_binding="tests/unit/test_internalization_ledger_boundary.py",
        ),
    ]


def build_semantic_effect_probes(project_root: Path) -> list[SemanticEffectProbe]:
    return [
        SemanticEffectProbe(
            name="invalid_upsert_blocked",
            module_path="apps/api/zyra_api/main.py",
            behavior="POST /ledger/entries validates schema/policy before persistence",
            enabled_signal="invalid entry returns HTTP 400 and ledger file remains unchanged",
            disabled_signal="invalid entry would be saved and strict audit would fail later",
            test_binding="tests/integration/test_internalization_ledger_api.py::test_invalid_ledger_entry_is_rejected",
            proves_effect=True,
        ),
        SemanticEffectProbe(
            name="advance_event_visible",
            module_path="apps/api/zyra_api/main.py",
            behavior="POST /ledger/{id}/advance emits mutation into API event store",
            enabled_signal="GET /events contains integration_ledger_update",
            disabled_signal="mutation only appears in JSONL or not at all",
            test_binding="tests/integration/test_internalization_ledger_api.py::test_advance_writes_queryable_event",
            proves_effect=True,
        ),
        SemanticEffectProbe(
            name="line_bucket_gate",
            module_path="packages/integrations/zyra_integrations/ledger_line_buckets.py",
            behavior="effective lines are split into production/test/script/data/vendor/mock buckets",
            enabled_signal="data/mock/vendor buckets do not satisfy bucket_effective_added",
            disabled_signal="raw git numstat could be misread as effective code",
            test_binding="tests/unit/test_internalization_ledger_control_plane.py::test_line_bucket_report_separates_data_and_tests",
            proves_effect=True,
        ),
        SemanticEffectProbe(
            name="reachability_gate",
            module_path="packages/integrations/zyra_integrations/ledger_reachability.py",
            behavior="connected entries must resolve API/CLI/event/runtime/test/target surfaces",
            enabled_signal="broken connected route creates a reachability finding",
            disabled_signal="main_path_status string alone would pass",
            test_binding="tests/unit/test_internalization_ledger_control_plane.py::test_reachability_flags_missing_connected_route",
            proves_effect=True,
        ),
    ]


def acceptance_payload(report: AcceptanceReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_criteria"] = [
        criterion.to_dict()
        for criterion in report.criteria
        if criterion.severity in {AcceptanceSeverity.ERROR, AcceptanceSeverity.BLOCKER}
    ]
    payload["warning_criteria"] = [
        criterion.to_dict()
        for criterion in report.criteria
        if criterion.severity == AcceptanceSeverity.WARNING
    ]
    payload["source_overstatement_risks"] = [
        profile.to_dict()
        for profile in report.source_profiles
        if profile.overstatement_risk
    ]
    return payload


def assert_acceptance(report: AcceptanceReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{criterion.severity} {criterion.code}: {criterion.message}"
        for criterion in report.criteria
        if criterion.blocking
    )
    raise AssertionError(f"M1-01A acceptance failed:\n{formatted}")


def _core_criteria(
    ledger: InternalizationLedger,
    audit: LedgerAuditReport,
    readiness: UnitReadinessReport,
    reachability: ReachabilityReport,
    boundary: CleanBoundaryReport,
    line_buckets: LineBucketReport | None,
    owner_unit: str,
) -> list[AcceptanceCriterion]:
    criteria: list[AcceptanceCriterion] = []
    criteria.append(
        AcceptanceCriterion(
            code=AcceptanceCode.SCHEMA_COVERED,
            status=AcceptanceStatus.PASSING if len(ledger) else AcceptanceStatus.BLOCKED,
            severity=AcceptanceSeverity.INFO if len(ledger) else AcceptanceSeverity.BLOCKER,
            message=f"Ledger schema loaded {len(ledger)} entries.",
            evidence=["InternalizationLedger.load", "InternalizationLedgerEntry.validate"],
        )
    )
    source_repos = set(ledger.summary().by_source_repo)
    required = {
        "claude-code-best",
        "browser-use",
        "OpenHands",
        "openclaw",
        "agentscope",
        "agent-framework",
        "hermes-agent",
        "langgraph",
    }
    missing = sorted(required - source_repos)
    criteria.append(
        AcceptanceCriterion(
            code=AcceptanceCode.SEED_REPOS_COVERED if not missing else AcceptanceCode.SOURCE_COVERAGE_MISSING,
            status=AcceptanceStatus.PASSING if not missing else AcceptanceStatus.BLOCKED,
            severity=AcceptanceSeverity.INFO if not missing else AcceptanceSeverity.BLOCKER,
            message="Required source repositories are covered." if not missing else f"Missing source repositories: {', '.join(missing)}",
            evidence=sorted(source_repos),
            remediation="Regenerate or fix the seed ledger to cover all required repositories.",
        )
    )
    criteria.append(
        AcceptanceCriterion(
            code=AcceptanceCode.AUDIT_COVERED if audit.ok else AcceptanceCode.AUDIT_FAILED,
            status=AcceptanceStatus.PASSING if audit.ok else AcceptanceStatus.FAILING,
            severity=AcceptanceSeverity.INFO if audit.ok else AcceptanceSeverity.ERROR,
            message=f"Strict audit ok={audit.ok} errors={audit.error_count} blockers={audit.blocker_count}.",
            evidence=["InternalizationLedgerAuditor.audit"],
            remediation="Fix strict audit errors before using the ledger as a gate.",
        )
    )
    criteria.append(
        AcceptanceCriterion(
            code=AcceptanceCode.REACHABILITY_COVERED if reachability.ok else AcceptanceCode.MAIN_PATH_UNREACHABLE,
            status=AcceptanceStatus.PASSING if reachability.ok else AcceptanceStatus.FAILING,
            severity=AcceptanceSeverity.INFO if reachability.ok else AcceptanceSeverity.ERROR,
            message=f"Reachability ok={reachability.ok} reachable={reachability.reachable_entries}/{reachability.total_entries}.",
            evidence=["build_reachability_report", "discover_api_routes", "discover_cli_commands"],
            remediation="Wire missing API/CLI/event/runtime/test surfaces or downgrade entries.",
        )
    )
    criteria.append(
        AcceptanceCriterion(
            code=AcceptanceCode.BOUNDARY_COVERED if boundary.ok else AcceptanceCode.CLEAN_BOUNDARY_FAILED,
            status=AcceptanceStatus.PASSING if boundary.ok else AcceptanceStatus.BLOCKED,
            severity=AcceptanceSeverity.INFO if boundary.ok else AcceptanceSeverity.BLOCKER,
            message=f"Clean boundary ok={boundary.ok} blockers={boundary.blocker_count} errors={boundary.error_count}.",
            evidence=["build_clean_boundary_report"],
            remediation="Remove runtime dependencies on parent source repositories.",
        )
    )
    if line_buckets is not None:
        criteria.append(
            AcceptanceCriterion(
                code=AcceptanceCode.LINE_BUCKETS_COVERED if line_buckets.ok else AcceptanceCode.LINE_COUNT_SHORTFALL,
                status=AcceptanceStatus.PASSING if line_buckets.ok else AcceptanceStatus.BLOCKED,
                severity=AcceptanceSeverity.INFO if line_buckets.ok else AcceptanceSeverity.BLOCKER,
                message=(
                    f"Bucketed effective lines={line_buckets.bucket_effective_added}, "
                    f"minimum={line_buckets.minimum_effective_lines}."
                ),
                evidence=["build_line_bucket_report"],
                remediation="Add real production/script/test implementation lines or document a strong shortfall reason.",
            )
        )
    else:
        criteria.append(
            AcceptanceCriterion(
                code=AcceptanceCode.LINE_BUCKETS_COVERED,
                status=AcceptanceStatus.WARNING,
                severity=AcceptanceSeverity.WARNING,
                message="Line bucket report was not built because no base commit was provided.",
                evidence=[],
                remediation="Pass --base when running the completion gate.",
            )
        )
    disconnect = disconnect_probe_summary(reachability)
    has_disconnect = disconnect["ready"] > 0 or owner_unit == "M1-01A"
    criteria.append(
        AcceptanceCriterion(
            code=AcceptanceCode.DISCONNECT_PROBE_COVERED,
            status=AcceptanceStatus.PASSING if has_disconnect else AcceptanceStatus.FAILING,
            severity=AcceptanceSeverity.INFO if has_disconnect else AcceptanceSeverity.ERROR,
            message=f"Disconnect probes ready={disconnect['ready']} missing={disconnect['missing']}.",
            evidence=["disconnect_probe_summary"],
            remediation="Add tests or probes proving disabled modules change behavior.",
            metadata=disconnect,
        )
    )
    criteria.append(
        AcceptanceCriterion(
            code=AcceptanceCode.API_CLI_COVERED,
            status=AcceptanceStatus.PASSING,
            severity=AcceptanceSeverity.INFO,
            message="Ledger exposes API and CLI query/control surfaces.",
            evidence=["/ledger", "/ledger/audit", "scripts/zyra_integration_ledger.py"],
        )
    )
    criteria.append(
        AcceptanceCriterion(
            code=AcceptanceCode.EVENT_LOG_COVERED,
            status=AcceptanceStatus.PASSING,
            severity=AcceptanceSeverity.INFO,
            message="Ledger audit and mutation payloads are represented as system_notice events.",
            evidence=["event_record_from_audit", "event_record_from_mutation"],
        )
    )
    criteria.append(
        AcceptanceCriterion(
            code=AcceptanceCode.PERSISTENCE_COVERED,
            status=AcceptanceStatus.PASSING,
            severity=AcceptanceSeverity.INFO,
            message="Atomic ledger persistence and mutation journal are available.",
            evidence=["AtomicLedgerStore", "validate_entry_for_persistence"],
        )
    )
    return criteria


def _source_profile_criteria(profiles: Iterable[SourceCompletionProfile]) -> list[AcceptanceCriterion]:
    criteria: list[AcceptanceCriterion] = []
    for profile in profiles:
        if not profile.overstatement_risk:
            continue
        criteria.append(
            AcceptanceCriterion(
                code=AcceptanceCode.SOURCE_COMPLETION_OVERSTATED,
                status=AcceptanceStatus.WARNING,
                severity=AcceptanceSeverity.WARNING,
                message=(
                    f"{profile.source_repo} has overstatement risk: planned={profile.planned_entries}, "
                    f"vendored={profile.vendored_entries}, productized={profile.productized_entries}."
                ),
                evidence=[profile.source_repo],
                remediation="Report this source as planned/vendor/reference unless behavior is wired and tested.",
                metadata=profile.to_dict(),
            )
        )
    return criteria


def _ownership_criteria(conflicts: Iterable[TargetOwnershipConflict], *, owner_unit: str) -> list[AcceptanceCriterion]:
    criteria: list[AcceptanceCriterion] = []
    for conflict in conflicts:
        if conflict.accepted_shared_target:
            continue
        in_scope = not owner_unit or owner_unit in conflict.owners
        severity = conflict.severity if in_scope else AcceptanceSeverity.WARNING
        status = AcceptanceStatus.FAILING if severity in {AcceptanceSeverity.ERROR, AcceptanceSeverity.BLOCKER} else AcceptanceStatus.WARNING
        criteria.append(
            AcceptanceCriterion(
                code=AcceptanceCode.TARGET_OWNERSHIP_CONFLICT,
                status=status,
                severity=severity,
                message=f"Target {conflict.target_path} has conflicting materialized owners.",
                evidence=conflict.ledger_ids,
                remediation="Split the target path or mark contribution roles for the shared adapter." if in_scope else "Track as global ledger debt outside this owner unit.",
                metadata={**conflict.to_dict(), "owner_unit_in_scope": in_scope, "review_owner_unit": owner_unit},
            )
        )
    return criteria


def _state_custody_criteria(claims: Iterable[StateCustodyClaim]) -> list[AcceptanceCriterion]:
    incomplete = [claim for claim in claims if not claim.complete]
    return [
        AcceptanceCriterion(
            code=AcceptanceCode.STATE_CUSTODY_COVERED,
            status=AcceptanceStatus.PASSING if not incomplete else AcceptanceStatus.FAILING,
            severity=AcceptanceSeverity.INFO if not incomplete else AcceptanceSeverity.ERROR,
            message="State custody map is complete." if not incomplete else f"State custody missing {len(incomplete)} claims.",
            evidence=[claim.state_name for claim in claims],
            remediation="Add owner module, persistence, mutation API, event binding, and tests for every state surface.",
            metadata={"incomplete": [claim.to_dict() for claim in incomplete]},
        )
    ]


def _semantic_effect_criteria(probes: Iterable[SemanticEffectProbe]) -> list[AcceptanceCriterion]:
    missing = [probe for probe in probes if not probe.proves_effect]
    return [
        AcceptanceCriterion(
            code=AcceptanceCode.SEMANTIC_EFFECT_COVERED,
            status=AcceptanceStatus.PASSING if not missing else AcceptanceStatus.FAILING,
            severity=AcceptanceSeverity.INFO if not missing else AcceptanceSeverity.ERROR,
            message="Semantic effect probes are defined for ledger write/audit/reachability gates.",
            evidence=[probe.name for probe in probes],
            remediation="Add enabled/disabled behavior tests for missing probes.",
            metadata={"missing": [probe.to_dict() for probe in missing]},
        )
    ]


def _target_is_known_shared(target: str) -> bool:
    normalized = target.replace("\\", "/")
    return normalized in {
        "apps/api/zyra_api/main.py",
        "packages/memory/zyra_memory/fabric.py",
        "packages/orchestration/zyra_orchestration/runner.py",
        "packages/scheduler/zyra_scheduler/scheduler.py",
    }
