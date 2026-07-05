from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .ledger_models import (
    InternalizationLedgerEntry,
    LedgerLifecycle,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    to_jsonable,
)
from .ledger_policy import CountVerdict, classify_path
from .ledger_store import InternalizationLedger


EntryPredicate = Callable[[InternalizationLedgerEntry], bool]


@dataclass(slots=True)
class LedgerSelector:
    source_repo: str = ""
    owner_unit: str = ""
    milestone: str = ""
    lifecycle: str = ""
    status: str = ""
    strategy: str = ""
    runtime_module_contains: str = ""
    runtime_command_contains: str = ""
    runtime_protocol: str = ""
    test_kind: str = ""
    test_path_contains: str = ""
    test_command_contains: str = ""
    api_route_contains: str = ""
    event_type: str = ""
    control_command: str = ""
    surface: str = ""
    worker_runtime: str = ""
    ui_panel: str = ""
    artifact_kind: str = ""
    license_status: str = ""
    target_prefix: str = ""
    target_verdict: str = ""
    target_exists: bool | None = None
    has_blockers: bool | None = None
    has_risk_notes: bool | None = None
    has_replacement_plan: bool | None = None
    tag: str = ""
    dependency: str = ""
    downstream_unit: str = ""
    metadata_key: str = ""
    text: str = ""
    limit: int = 500

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "LedgerSelector":
        return cls(
            source_repo=str(params.get("source_repo") or params.get("repo") or ""),
            owner_unit=str(params.get("owner_unit") or params.get("unit") or ""),
            milestone=str(params.get("milestone") or ""),
            lifecycle=str(params.get("lifecycle") or ""),
            status=str(params.get("main_path_status") or params.get("status") or ""),
            strategy=str(params.get("migration_strategy") or params.get("strategy") or ""),
            runtime_module_contains=str(params.get("runtime_module") or params.get("runtime_module_contains") or ""),
            runtime_command_contains=str(params.get("runtime_command") or params.get("runtime_command_contains") or ""),
            runtime_protocol=str(params.get("runtime_protocol") or ""),
            test_kind=str(params.get("test_kind") or ""),
            test_path_contains=str(params.get("test_path") or params.get("test_path_contains") or ""),
            test_command_contains=str(params.get("test_command") or params.get("test_command_contains") or ""),
            api_route_contains=str(params.get("api_route") or params.get("api_route_contains") or ""),
            event_type=str(params.get("event_type") or ""),
            control_command=str(params.get("control_command") or ""),
            surface=str(params.get("surface") or ""),
            worker_runtime=str(params.get("worker_runtime") or ""),
            ui_panel=str(params.get("ui_panel") or ""),
            artifact_kind=str(params.get("artifact_kind") or ""),
            license_status=str(params.get("license_status") or ""),
            target_prefix=str(params.get("target_prefix") or params.get("target") or ""),
            target_verdict=str(params.get("target_verdict") or ""),
            target_exists=_optional_bool(params.get("target_exists")),
            has_blockers=_optional_bool(params.get("has_blockers")),
            has_risk_notes=_optional_bool(params.get("has_risk_notes")),
            has_replacement_plan=_optional_bool(params.get("has_replacement_plan")),
            tag=str(params.get("tag") or ""),
            dependency=str(params.get("dependency") or ""),
            downstream_unit=str(params.get("downstream_unit") or ""),
            metadata_key=str(params.get("metadata_key") or ""),
            text=str(params.get("q") or params.get("text") or ""),
            limit=_positive_int(params.get("limit"), 500),
        )

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LedgerSelectionReport:
    selector: LedgerSelector
    total_matches: int
    returned: int
    entries: list[dict[str, Any]]
    facets: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


def select_entries(
    ledger: InternalizationLedger,
    selector: LedgerSelector,
    *,
    project_root: Path | None = None,
) -> list[InternalizationLedgerEntry]:
    predicates = build_predicates(selector, project_root=project_root)
    matches: list[InternalizationLedgerEntry] = []
    for entry in ledger.entries():
        if all(predicate(entry) for predicate in predicates):
            matches.append(entry)
    return matches[: max(selector.limit, 0)]


def build_selection_report(
    ledger: InternalizationLedger,
    selector: LedgerSelector,
    *,
    project_root: Path | None = None,
) -> LedgerSelectionReport:
    predicates = build_predicates(selector, project_root=project_root)
    all_matches = [entry for entry in ledger.entries() if all(predicate(entry) for predicate in predicates)]
    returned = all_matches[: max(selector.limit, 0)]
    return LedgerSelectionReport(
        selector=selector,
        total_matches=len(all_matches),
        returned=len(returned),
        entries=[entry.to_dict() for entry in returned],
        facets=selection_facets(all_matches),
    )


def build_predicates(selector: LedgerSelector, *, project_root: Path | None = None) -> list[EntryPredicate]:
    predicates: list[EntryPredicate] = []
    if selector.source_repo:
        predicates.append(lambda entry: entry.source_repo == selector.source_repo)
    if selector.owner_unit:
        predicates.append(lambda entry: entry.owner_unit == selector.owner_unit)
    if selector.milestone:
        predicates.append(lambda entry: entry.milestone == selector.milestone)
    if selector.lifecycle:
        predicates.append(lambda entry: str(entry.lifecycle) == selector.lifecycle)
    if selector.status:
        predicates.append(lambda entry: str(entry.main_path_status) == selector.status)
    if selector.strategy:
        predicates.append(lambda entry: str(entry.migration_strategy) == selector.strategy)
    if selector.runtime_module_contains:
        text = selector.runtime_module_contains.lower()
        predicates.append(lambda entry: text in entry.runtime_entry.module.lower())
    if selector.runtime_command_contains:
        text = selector.runtime_command_contains.lower()
        predicates.append(lambda entry: text in entry.runtime_entry.command.lower())
    if selector.runtime_protocol:
        predicates.append(lambda entry: entry.runtime_entry.protocol == selector.runtime_protocol)
    if selector.test_kind:
        predicates.append(lambda entry: any(test.kind == selector.test_kind for test in entry.test_entries))
    if selector.test_path_contains:
        text = selector.test_path_contains.lower()
        predicates.append(lambda entry: any(text in test.path.lower() for test in entry.test_entries))
    if selector.test_command_contains:
        text = selector.test_command_contains.lower()
        predicates.append(lambda entry: any(text in test.command.lower() for test in entry.test_entries))
    if selector.api_route_contains:
        text = selector.api_route_contains.lower()
        predicates.append(lambda entry: any(text in route.lower() for route in entry.main_path.api_routes))
    if selector.event_type:
        predicates.append(lambda entry: selector.event_type in entry.main_path.event_types)
    if selector.control_command:
        predicates.append(lambda entry: selector.control_command in entry.main_path.control_commands)
    if selector.surface:
        predicates.append(lambda entry: selector.surface in entry.main_path.surfaces)
    if selector.worker_runtime:
        predicates.append(lambda entry: entry.main_path.worker_runtime == selector.worker_runtime)
    if selector.ui_panel:
        predicates.append(lambda entry: selector.ui_panel in entry.main_path.ui_panels)
    if selector.artifact_kind:
        predicates.append(lambda entry: selector.artifact_kind in entry.main_path.artifact_kinds)
    if selector.license_status:
        predicates.append(lambda entry: entry.license_notice is not None and str(entry.license_notice.status) == selector.license_status)
    if selector.target_prefix:
        predicates.append(lambda entry: any(target.startswith(selector.target_prefix) for target in entry.target_paths))
    if selector.target_verdict:
        predicates.append(lambda entry: any(str(classify_path(target).verdict) == selector.target_verdict for target in entry.target_paths))
    if selector.target_exists is not None:
        predicates.append(_target_exists_predicate(project_root, selector.target_exists))
    if selector.has_blockers is not None:
        predicates.append(lambda entry: bool(entry.blockers) is selector.has_blockers)
    if selector.has_risk_notes is not None:
        predicates.append(lambda entry: bool(entry.risk_notes) is selector.has_risk_notes)
    if selector.has_replacement_plan is not None:
        predicates.append(lambda entry: bool(entry.replacement_plan) is selector.has_replacement_plan)
    if selector.tag:
        predicates.append(lambda entry: selector.tag in entry.tags)
    if selector.dependency:
        predicates.append(lambda entry: selector.dependency in entry.dependencies)
    if selector.downstream_unit:
        predicates.append(lambda entry: selector.downstream_unit in entry.downstream_units)
    if selector.metadata_key:
        predicates.append(lambda entry: selector.metadata_key in entry.metadata)
    if selector.text:
        text = selector.text.lower()
        predicates.append(lambda entry: text in entry_search_text(entry))
    return predicates


def entry_search_text(entry: InternalizationLedgerEntry) -> str:
    parts = [
        entry.ledger_id,
        entry.source_repo,
        entry.source_path,
        entry.capability_name,
        entry.capability_summary,
        entry.owner_unit,
        entry.milestone,
        entry.runtime_entry.command,
        entry.runtime_entry.module,
        entry.runtime_entry.function,
        entry.runtime_entry.protocol,
        entry.main_path.worker_runtime,
        entry.replacement_plan,
        *entry.target_paths,
        *entry.tags,
        *entry.blockers,
        *entry.risk_notes,
        *entry.dependencies,
        *entry.downstream_units,
        *entry.main_path.surfaces,
        *entry.main_path.event_types,
        *entry.main_path.api_routes,
        *entry.main_path.control_commands,
        *entry.main_path.artifact_kinds,
        *entry.main_path.ui_panels,
        *[test.path for test in entry.test_entries],
        *[test.command for test in entry.test_entries],
        *[str(key) for key in entry.metadata],
        *[str(value) for value in entry.metadata.values()],
    ]
    return " ".join(part for part in parts if part).lower()


def selection_facets(entries: Iterable[InternalizationLedgerEntry]) -> dict[str, dict[str, int]]:
    facets: dict[str, dict[str, int]] = {
        "source_repo": {},
        "owner_unit": {},
        "lifecycle": {},
        "main_path_status": {},
        "migration_strategy": {},
        "target_verdict": {},
        "license_status": {},
    }
    for entry in entries:
        _increment(facets["source_repo"], entry.source_repo)
        _increment(facets["owner_unit"], entry.owner_unit or "unassigned")
        _increment(facets["lifecycle"], str(entry.lifecycle))
        _increment(facets["main_path_status"], str(entry.main_path_status))
        _increment(facets["migration_strategy"], str(entry.migration_strategy))
        _increment(facets["license_status"], str(entry.license_notice.status) if entry.license_notice else "missing")
        for target in entry.target_paths:
            _increment(facets["target_verdict"], str(classify_path(target).verdict))
    return {key: dict(sorted(value.items())) for key, value in facets.items()}


def entries_missing_tests(entries: Iterable[InternalizationLedgerEntry]) -> list[InternalizationLedgerEntry]:
    return [entry for entry in entries if not entry.test_entries]


def entries_missing_runtime(entries: Iterable[InternalizationLedgerEntry]) -> list[InternalizationLedgerEntry]:
    return [entry for entry in entries if entry.runtime_entry.is_empty()]


def entries_with_data_only_targets(entries: Iterable[InternalizationLedgerEntry]) -> list[InternalizationLedgerEntry]:
    result: list[InternalizationLedgerEntry] = []
    for entry in entries:
        classifications = [classify_path(target) for target in entry.target_paths]
        if classifications and all(classification.verdict != CountVerdict.EFFECTIVE for classification in classifications):
            result.append(entry)
    return result


def entries_ready_for_connected_status(entries: Iterable[InternalizationLedgerEntry]) -> list[InternalizationLedgerEntry]:
    result: list[InternalizationLedgerEntry] = []
    for entry in entries:
        if entry.runtime_entry.is_empty() or not entry.test_entries or entry.main_path.is_empty():
            continue
        if entry.lifecycle in {LedgerLifecycle.ACTIVE, LedgerLifecycle.INTERNALIZED, LedgerLifecycle.PRODUCTIZED}:
            result.append(entry)
    return result


def entries_stale_for_unit(entries: Iterable[InternalizationLedgerEntry], owner_unit: str) -> list[InternalizationLedgerEntry]:
    return [
        entry
        for entry in entries
        if entry.owner_unit == owner_unit
        and entry.lifecycle in {LedgerLifecycle.CANDIDATE, LedgerLifecycle.PLANNED}
        and entry.main_path_status == MainPathStatus.PLANNED
    ]


def entries_by_strategy(entries: Iterable[InternalizationLedgerEntry], strategy: MigrationStrategy) -> list[InternalizationLedgerEntry]:
    return [entry for entry in entries if entry.migration_strategy == strategy]


def entries_by_notice_status(entries: Iterable[InternalizationLedgerEntry], status: NoticeStatus) -> list[InternalizationLedgerEntry]:
    return [entry for entry in entries if entry.license_notice is not None and entry.license_notice.status == status]


def _target_exists_predicate(project_root: Path | None, expected: bool) -> EntryPredicate:
    def predicate(entry: InternalizationLedgerEntry) -> bool:
        if project_root is None:
            return False if expected else True
        exists = any((project_root / classify_path(target).normalized_path).exists() for target in entry.target_paths if classify_path(target).is_project_relative)
        return exists is expected

    return predicate


def _increment(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
