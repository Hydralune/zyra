from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from .ledger_models import InternalizationLedgerEntry, LedgerLifecycle, MainPathStatus, to_jsonable
from .ledger_policy import UNIT_BUDGETS, UnitBudget, classify_path
from .ledger_store import InternalizationLedger


UNIT_DEPENDENCIES: dict[str, list[str]] = {
    "M1-01B": ["M1-01A"],
    "M1-02A": ["M1-01A", "M1-01B"],
    "M1-02B": ["M1-02A"],
    "M1-02C": ["M1-02B"],
    "M1-02D": ["M1-02B", "M1-02C"],
    "M1-03A": ["M1-02B", "M1-02C"],
    "M1-03B": ["M1-02B"],
    "M1-03C": ["M1-02B"],
    "M1-03D": ["M1-03A", "M1-03C"],
    "M1-04A": ["M1-03A"],
    "M1-04B": ["M1-04A"],
    "M1-04C": ["M1-04A", "M1-03A"],
    "M1-04D": ["M1-04A", "M1-04B"],
    "M1-05A": ["M1-02B"],
    "M1-05B": ["M1-05A"],
    "M1-05C": ["M1-05A", "M1-05B"],
    "M1-05D": ["M1-05C"],
    "M1-06A": ["M1-02D"],
    "M1-06B": ["M1-06A"],
    "M1-06C": ["M1-06A", "M1-06B", "M1-02D"],
    "M1-07A": ["M1-05C"],
    "M1-07B": ["M1-07A", "M1-04D"],
    "M1-07C": ["M1-07A", "M1-07B", "M1-06C"],
    "M1-08": ["M1-02D", "M1-03D", "M1-04D", "M1-05D", "M1-06C", "M1-07C"],
    "M2-01A": ["M1-08"],
    "M2-01B": ["M2-01A"],
    "M2-02A": ["M2-01B"],
    "M2-02B": ["M2-01B"],
    "M2-03A": ["M2-01B"],
    "M2-03B": ["M2-01B"],
    "M2-04A": ["M2-01B"],
    "M2-04B": ["M2-01B"],
    "M2-05": ["M2-02A", "M2-02B", "M2-03A", "M2-03B", "M2-04A", "M2-04B"],
    "M3-01A": ["M2-05"],
    "M3-01B": ["M3-01A"],
    "M3-02A": ["M2-05"],
    "M3-02B": ["M3-01B", "M3-02A"],
    "M3-03": ["M3-01A", "M3-01B", "M3-02A", "M3-02B"],
}


UNIT_REQUIRED_SURFACES: dict[str, list[str]] = {
    "M1-01A": ["packages/integrations", "apps/api", "scripts", "tests"],
    "M1-01B": ["packages/integrations", "scripts", "vendor-runtimes"],
    "M1-02A": ["packages/workers", "vendor-runtimes", "tests"],
    "M1-02B": ["packages/workers", "packages/runtime", "apps/api", "tests"],
    "M1-02C": ["packages/runtime", "packages/workers", "tests"],
    "M1-02D": ["packages/memory", "packages/workers", "apps/api", "tests"],
    "M1-03A": ["packages/runtime", "packages/commands", "apps/api", "tests"],
    "M1-03B": ["packages/integrations", "packages/runtime", "apps/api", "tests"],
    "M1-03C": ["packages/skills", "packages/runtime", "apps/api", "tests"],
    "M1-03D": ["packages/workers", "packages/commands", "packages/skills", "tests"],
    "M1-04A": ["packages/workers", "vendor-runtimes", "tests"],
    "M1-04B": ["packages/workers", "packages/memory", "tests"],
    "M1-04C": ["packages/workers", "packages/runtime", "tests"],
    "M1-04D": ["packages/workers", "packages/scheduler", "packages/runtime", "tests"],
    "M1-05A": ["packages/scheduler", "packages/runtime", "apps/api", "tests"],
    "M1-05B": ["packages/scheduler", "packages/runtime", "vendor-runtimes", "tests"],
    "M1-05C": ["packages/runtime", "packages/scheduler", "apps/api", "tests"],
    "M1-05D": ["packages/runtime", "packages/scheduler", "tests"],
    "M1-06A": ["packages/memory", "packages/integrations", "tests"],
    "M1-06B": ["packages/memory", "packages/workers", "tests"],
    "M1-06C": ["packages/memory", "packages/skills", "packages/workers", "tests"],
    "M1-07A": ["packages/scheduler", "packages/workers", "tests"],
    "M1-07B": ["packages/scheduler", "packages/workers", "tests"],
    "M1-07C": ["packages/scheduler", "packages/memory", "tests"],
    "M1-08": ["packages/runtime", "packages/workers", "packages/scheduler", "apps/api", "tests"],
    "M2-01A": ["apps/web", "apps/api", "tests"],
    "M2-01B": ["apps/web", "apps/api", "tests"],
    "M2-02A": ["apps/web", "apps/api", "tests"],
    "M2-02B": ["apps/web", "apps/api", "tests"],
    "M2-03A": ["apps/web", "apps/api", "tests"],
    "M2-03B": ["apps/web", "apps/api", "tests"],
    "M2-04A": ["apps/web", "apps/api", "tests"],
    "M2-04B": ["apps/web", "apps/api", "tests"],
    "M2-05": ["apps/web", "apps/api", "packages/evaluation", "tests"],
    "M3-01A": ["third_party", "packages/integrations", "tests"],
    "M3-01B": ["vendor-runtimes", "packages", "tests"],
    "M3-02A": ["packages/evaluation", "tests", "scripts"],
    "M3-02B": ["scripts", "apps/api", "tests"],
    "M3-03": ["scripts", "packages/integrations", "tests"],
}


@dataclass(slots=True)
class UnitMatrixRow:
    owner_unit: str
    milestone: str
    minimum_effective_lines: int
    dependency_units: list[str]
    downstream_units: list[str]
    required_surfaces: list[str]
    entry_count: int
    source_repos: dict[str, int]
    target_surfaces: dict[str, int]
    lifecycles: dict[str, int]
    statuses: dict[str, int]
    planned_entries: int
    materialized_entries: int
    connected_entries: int
    effective_target_entries: int
    data_only_entries: int
    missing_required_surfaces: list[str]

    @property
    def has_ledger_coverage(self) -> bool:
        return self.entry_count > 0

    @property
    def surface_coverage_ok(self) -> bool:
        return not self.missing_required_surfaces

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["has_ledger_coverage"] = self.has_ledger_coverage
        payload["surface_coverage_ok"] = self.surface_coverage_ok
        return payload


@dataclass(slots=True)
class UnitMatrixReport:
    unit_count: int
    covered_units: int
    uncovered_units: int
    dependency_order: list[str]
    rows: list[UnitMatrixRow]
    missing_units: list[str] = field(default_factory=list)
    cyclic_or_unresolved_units: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.uncovered_units == 0 and not self.cyclic_or_unresolved_units

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["ok"] = self.ok
        return payload


def build_unit_matrix(ledger: InternalizationLedger) -> UnitMatrixReport:
    rows = [_build_unit_row(unit, budget, ledger) for unit, budget in sorted(UNIT_BUDGETS.items())]
    missing = [row.owner_unit for row in rows if not row.has_ledger_coverage]
    order, unresolved = topological_unit_order(UNIT_BUDGETS, UNIT_DEPENDENCIES)
    return UnitMatrixReport(
        unit_count=len(rows),
        covered_units=sum(1 for row in rows if row.has_ledger_coverage),
        uncovered_units=len(missing),
        dependency_order=order,
        rows=rows,
        missing_units=missing,
        cyclic_or_unresolved_units=unresolved,
    )


def unit_downstreams(unit: str) -> list[str]:
    return sorted(key for key, deps in UNIT_DEPENDENCIES.items() if unit in deps)


def upstream_closure(unit: str) -> list[str]:
    seen: set[str] = set()
    stack = list(UNIT_DEPENDENCIES.get(unit, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(UNIT_DEPENDENCIES.get(current, []))
    return sorted(seen)


def downstream_closure(unit: str) -> list[str]:
    seen: set[str] = set()
    stack = unit_downstreams(unit)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(unit_downstreams(current))
    return sorted(seen)


def topological_unit_order(
    budgets: dict[str, UnitBudget],
    dependencies: dict[str, list[str]],
) -> tuple[list[str], list[str]]:
    incoming: dict[str, set[str]] = {unit: set(dependencies.get(unit, [])) for unit in budgets}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for unit, deps in incoming.items():
        for dep in deps:
            outgoing[dep].add(unit)
    ready = deque(sorted(unit for unit, deps in incoming.items() if not deps))
    order: list[str] = []
    while ready:
        unit = ready.popleft()
        order.append(unit)
        for downstream in sorted(outgoing.get(unit, set())):
            incoming[downstream].discard(unit)
            if not incoming[downstream]:
                ready.append(downstream)
    unresolved = sorted(unit for unit, deps in incoming.items() if deps)
    return order, unresolved


def entries_for_unit_and_dependencies(ledger: InternalizationLedger, unit: str) -> list[InternalizationLedgerEntry]:
    units = [unit, *upstream_closure(unit)]
    entries: list[InternalizationLedgerEntry] = []
    for owner_unit in units:
        entries.extend(ledger.by_owner_unit(owner_unit))
    return sorted(entries, key=lambda entry: (entry.owner_unit, entry.source_repo, entry.capability_name))


def _build_unit_row(unit: str, budget: UnitBudget, ledger: InternalizationLedger) -> UnitMatrixRow:
    entries = ledger.by_owner_unit(unit)
    source_repos = Counter(entry.source_repo for entry in entries)
    lifecycles = Counter(str(entry.lifecycle) for entry in entries)
    statuses = Counter(str(entry.main_path_status) for entry in entries)
    target_surfaces: Counter[str] = Counter()
    effective_target_entries = 0
    data_only_entries = 0
    for entry in entries:
        classifications = [classify_path(path) for path in entry.target_paths]
        if any(str(classification.verdict) == "effective" for classification in classifications):
            effective_target_entries += 1
        if classifications and all(classification.is_generated_data for classification in classifications):
            data_only_entries += 1
        for classification in classifications:
            surface = "/".join(classification.normalized_path.split("/")[:2]) if classification.normalized_path else "missing"
            target_surfaces[surface] += 1
    required = UNIT_REQUIRED_SURFACES.get(unit, [])
    missing_surfaces = [
        surface
        for surface in required
        if not any(target.startswith(surface) for target in target_surfaces)
    ]
    return UnitMatrixRow(
        owner_unit=unit,
        milestone=budget.milestone,
        minimum_effective_lines=budget.minimum_effective_lines,
        dependency_units=UNIT_DEPENDENCIES.get(unit, []),
        downstream_units=unit_downstreams(unit),
        required_surfaces=required,
        entry_count=len(entries),
        source_repos=dict(sorted(source_repos.items())),
        target_surfaces=dict(sorted(target_surfaces.items())),
        lifecycles=dict(sorted(lifecycles.items())),
        statuses=dict(sorted(statuses.items())),
        planned_entries=sum(1 for entry in entries if entry.lifecycle in {LedgerLifecycle.CANDIDATE, LedgerLifecycle.PLANNED, LedgerLifecycle.IN_PROGRESS}),
        materialized_entries=sum(1 for entry in entries if entry.lifecycle in {LedgerLifecycle.ACTIVE, LedgerLifecycle.INTERNALIZED, LedgerLifecycle.PRODUCTIZED}),
        connected_entries=sum(
            1
            for entry in entries
            if entry.main_path_status
            in {
                MainPathStatus.API_CONNECTED,
                MainPathStatus.EVENT_LOG_CONNECTED,
                MainPathStatus.CONTROL_COMMAND_CONNECTED,
                MainPathStatus.WORKER_RUNTIME_CONNECTED,
                MainPathStatus.UI_CONNECTED,
                MainPathStatus.TESTED_MAIN_PATH,
            }
        ),
        effective_target_entries=effective_target_entries,
        data_only_entries=data_only_entries,
        missing_required_surfaces=missing_surfaces,
    )
