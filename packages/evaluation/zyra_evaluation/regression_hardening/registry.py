from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import (
    CaseExecutionBuffer,
    CaseSpec,
    ContractError,
    SuiteProfile,
    stable_digest,
    validate_identifier,
)


class CaseContextLike(Protocol):
    case_id: str


CaseExecutor = Callable[[CaseContextLike], CaseExecutionBuffer | Mapping[str, Any] | None]


@dataclass(frozen=True, slots=True)
class RegisteredCase:
    spec: CaseSpec
    executor: CaseExecutor
    source: str
    registration_order: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.spec.to_dict(),
            "source": self.source,
            "registration_order": self.registration_order,
            "executor": f"{self.executor.__module__}:{getattr(self.executor, '__qualname__', self.executor.__name__)}",
        }


@dataclass(frozen=True, slots=True)
class CapabilityState:
    capability_id: str
    available: bool
    owner: str
    revision: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        validate_identifier(self.capability_id, field_name="capability_id")
        if not self.owner.strip():
            raise ContractError(f"{self.capability_id} requires an owner label")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "available": self.available,
            "owner": self.owner,
            "revision": self.revision,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RegistrySelection:
    profile: SuiteProfile
    cases: tuple[RegisteredCase, ...]
    ordered_case_ids: tuple[str, ...]
    dependency_closure: Mapping[str, tuple[str, ...]]
    missing_capabilities: Mapping[str, tuple[str, ...]]
    shard_assignments: Mapping[str, str]
    registry_digest: str

    def case(self, case_id: str) -> RegisteredCase:
        for item in self.cases:
            if item.spec.case_id == case_id:
                return item
        raise KeyError(case_id)

    def shard_cases(self, shard_id: str) -> tuple[RegisteredCase, ...]:
        selected = {
            case_id
            for case_id, assigned in self.shard_assignments.items()
            if assigned == shard_id
        }
        return tuple(item for item in self.cases if item.spec.case_id in selected)

    @property
    def shard_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.shard_assignments.values())))

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "cases": [item.to_dict() for item in self.cases],
            "ordered_case_ids": list(self.ordered_case_ids),
            "dependency_closure": {
                key: list(value)
                for key, value in sorted(self.dependency_closure.items())
            },
            "missing_capabilities": {
                key: list(value)
                for key, value in sorted(self.missing_capabilities.items())
            },
            "shard_assignments": dict(sorted(self.shard_assignments.items())),
            "registry_digest": self.registry_digest,
        }


class CaseRegistry:
    """Validated catalog for executable regression cases.

    Registration is intentionally explicit: importing a module never mutates
    the global suite. A service constructs a registry, registers cases, seals
    it, and then selects one immutable profile.
    """

    def __init__(self) -> None:
        self._cases: dict[str, RegisteredCase] = {}
        self._capabilities: dict[str, CapabilityState] = {}
        self._sealed = False
        self._generation = 0

    def register(
        self,
        spec: CaseSpec,
        executor: CaseExecutor,
        *,
        source: str = "zyra",
    ) -> RegisteredCase:
        self._require_open()
        if not callable(executor):
            raise ContractError(f"{spec.case_id} executor is not callable")
        if spec.case_id in self._cases:
            previous = self._cases[spec.case_id]
            raise ContractError(
                f"duplicate case ID {spec.case_id}; already registered by {previous.source}"
            )
        self._generation += 1
        registered = RegisteredCase(
            spec=spec,
            executor=executor,
            source=str(source).strip() or "zyra",
            registration_order=self._generation,
        )
        self._cases[spec.case_id] = registered
        return registered

    def register_capability(
        self,
        capability_id: str,
        *,
        available: bool,
        owner: str,
        revision: str = "",
        detail: str = "",
    ) -> CapabilityState:
        self._require_open()
        state = CapabilityState(
            capability_id=capability_id,
            available=bool(available),
            owner=owner,
            revision=revision,
            detail=detail,
        )
        prior = self._capabilities.get(state.capability_id)
        if prior and prior != state:
            raise ContractError(
                f"capability {state.capability_id} has conflicting registrations: "
                f"{prior.owner!r} and {state.owner!r}"
            )
        self._capabilities[state.capability_id] = state
        return state

    def seal(self) -> str:
        self._validate_dependencies()
        self._validate_capability_names()
        self._sealed = True
        return self.digest

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def digest(self) -> str:
        return stable_digest(
            {
                "cases": [
                    self._cases[key].to_dict()
                    for key in sorted(self._cases)
                ],
                "capabilities": [
                    self._capabilities[key].to_dict()
                    for key in sorted(self._capabilities)
                ],
            }
        )

    def cases(self) -> tuple[RegisteredCase, ...]:
        return tuple(self._cases[key] for key in sorted(self._cases))

    def capabilities(self) -> tuple[CapabilityState, ...]:
        return tuple(self._capabilities[key] for key in sorted(self._capabilities))

    def select(self, profile: SuiteProfile) -> RegistrySelection:
        if not self._sealed:
            raise ContractError("registry must be sealed before selection")
        roots = self._selected_roots(profile)
        if not roots:
            raise ContractError(f"profile {profile.profile_id} selected no cases")
        selected_ids = self._dependency_expansion(roots)
        ordered = self._topological_order(selected_ids)
        cases = tuple(self._cases[case_id] for case_id in ordered)
        missing = {
            item.spec.case_id: tuple(
                capability
                for capability in item.spec.required_capabilities
                if (
                    capability not in self._capabilities
                    or not self._capabilities[capability].available
                )
            )
            for item in cases
        }
        missing = {key: value for key, value in missing.items() if value}
        closure = {
            case_id: tuple(sorted(self._dependency_expansion((case_id,)) - {case_id}))
            for case_id in ordered
        }
        shards = self._assign_shards(cases, profile.shard_count)
        material = {
            "registry": self.digest,
            "profile": profile.to_dict(),
            "cases": [item.spec.digest for item in cases],
            "ordered": ordered,
            "closure": closure,
            "missing": missing,
            "shards": shards,
        }
        return RegistrySelection(
            profile=profile,
            cases=cases,
            ordered_case_ids=tuple(ordered),
            dependency_closure=closure,
            missing_capabilities=missing,
            shard_assignments=shards,
            registry_digest=stable_digest(material),
        )

    def dependency_layers(
        self,
        selection: RegistrySelection,
    ) -> tuple[tuple[RegisteredCase, ...], ...]:
        selected = set(selection.ordered_case_ids)
        indegree = {
            case_id: len(set(self._cases[case_id].spec.dependencies) & selected)
            for case_id in selected
        }
        dependents: dict[str, set[str]] = defaultdict(set)
        for case_id in selected:
            for dependency in self._cases[case_id].spec.dependencies:
                if dependency in selected:
                    dependents[dependency].add(case_id)
        ready = sorted(case_id for case_id, degree in indegree.items() if degree == 0)
        layers: list[tuple[RegisteredCase, ...]] = []
        visited = 0
        while ready:
            current = tuple(ready)
            layers.append(tuple(self._cases[case_id] for case_id in current))
            visited += len(current)
            following: list[str] = []
            for case_id in current:
                for dependent in sorted(dependents[case_id]):
                    indegree[dependent] -= 1
                    if indegree[dependent] == 0:
                        following.append(dependent)
            ready = sorted(following)
        if visited != len(selected):
            raise ContractError("selected registry contains a dependency cycle")
        return tuple(layers)

    def describe(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-regression-case-registry/v1",
            "sealed": self.sealed,
            "case_count": len(self._cases),
            "capability_count": len(self._capabilities),
            "cases": [item.to_dict() for item in self.cases()],
            "capabilities": [item.to_dict() for item in self.capabilities()],
            "digest": self.digest,
        }

    def _selected_roots(self, profile: SuiteProfile) -> set[str]:
        unknown = set(profile.case_ids) - set(self._cases)
        if unknown:
            raise ContractError(f"profile references unknown cases: {sorted(unknown)}")
        if profile.case_ids:
            roots = set(profile.case_ids)
        else:
            roots = set(self._cases)
        include = set(profile.include_tags)
        exclude = set(profile.exclude_tags)
        selected: set[str] = set()
        for case_id in roots:
            tags = set(self._cases[case_id].spec.tags)
            if include and not tags.intersection(include):
                continue
            if tags.intersection(exclude):
                continue
            selected.add(case_id)
        return selected

    def _dependency_expansion(self, roots: Iterable[str]) -> set[str]:
        expanded: set[str] = set()
        pending = list(roots)
        while pending:
            case_id = pending.pop()
            if case_id in expanded:
                continue
            if case_id not in self._cases:
                raise ContractError(f"unknown dependency {case_id}")
            expanded.add(case_id)
            pending.extend(self._cases[case_id].spec.dependencies)
        return expanded

    def _topological_order(self, selected: set[str]) -> list[str]:
        indegree: dict[str, int] = {case_id: 0 for case_id in selected}
        dependents: dict[str, set[str]] = defaultdict(set)
        for case_id in selected:
            for dependency in self._cases[case_id].spec.dependencies:
                if dependency not in selected:
                    continue
                indegree[case_id] += 1
                dependents[dependency].add(case_id)
        queue = deque(sorted(case_id for case_id, degree in indegree.items() if degree == 0))
        ordered: list[str] = []
        while queue:
            case_id = queue.popleft()
            ordered.append(case_id)
            for dependent in sorted(dependents[case_id]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    queue.append(dependent)
        if len(ordered) != len(selected):
            cycle = self._find_cycle(selected)
            raise ContractError(f"case dependency cycle: {' -> '.join(cycle)}")
        return ordered

    def _find_cycle(self, selected: set[str]) -> tuple[str, ...]:
        visiting: set[str] = set()
        visited: set[str] = set()
        stack: list[str] = []

        def walk(case_id: str) -> tuple[str, ...] | None:
            if case_id in visiting:
                start = stack.index(case_id)
                return tuple(stack[start:] + [case_id])
            if case_id in visited:
                return None
            visiting.add(case_id)
            stack.append(case_id)
            for dependency in self._cases[case_id].spec.dependencies:
                if dependency not in selected:
                    continue
                cycle = walk(dependency)
                if cycle:
                    return cycle
            stack.pop()
            visiting.remove(case_id)
            visited.add(case_id)
            return None

        for case_id in sorted(selected):
            cycle = walk(case_id)
            if cycle:
                return cycle
        return ("unknown",)

    def _validate_dependencies(self) -> None:
        unknown: dict[str, list[str]] = {}
        for case_id, registered in self._cases.items():
            missing = [
                dependency
                for dependency in registered.spec.dependencies
                if dependency not in self._cases
            ]
            if missing:
                unknown[case_id] = missing
        if unknown:
            raise ContractError(f"unknown case dependencies: {unknown}")
        self._topological_order(set(self._cases))

    def _validate_capability_names(self) -> None:
        known = set(self._capabilities)
        declared = {
            capability
            for item in self._cases.values()
            for capability in item.spec.required_capabilities
        }
        unknown = declared - known
        if unknown:
            raise ContractError(
                f"case specs reference unregistered capabilities: {sorted(unknown)}"
            )

    @staticmethod
    def _assign_shards(
        cases: Sequence[RegisteredCase],
        shard_count: int,
    ) -> dict[str, str]:
        """Consistent assignment keeps a case stable when registry order changes."""

        result: dict[str, str] = {}
        for item in cases:
            digest = hashlib.sha256(item.spec.identity.encode("utf-8")).digest()
            number = int.from_bytes(digest[:8], "big") % shard_count
            result[item.spec.case_id] = f"shard-{number + 1:03d}-of-{shard_count:03d}"
        return result

    def _require_open(self) -> None:
        if self._sealed:
            raise ContractError("registry is sealed")


def shard_filter(
    selection: RegistrySelection,
    *,
    shard_index: int,
    shard_count: int,
) -> RegistrySelection:
    if shard_count != selection.profile.shard_count:
        raise ContractError(
            f"requested shard count {shard_count} does not match profile "
            f"{selection.profile.shard_count}"
        )
    if shard_index < 1 or shard_index > shard_count:
        raise ContractError("shard index is out of range")
    target = f"shard-{shard_index:03d}-of-{shard_count:03d}"
    roots = {
        case_id
        for case_id, shard_id in selection.shard_assignments.items()
        if shard_id == target
    }
    required = set(roots)
    for case_id in tuple(roots):
        required.update(selection.dependency_closure.get(case_id, ()))
    cases = tuple(
        item
        for item in selection.cases
        if item.spec.case_id in required
    )
    ordered = tuple(
        case_id
        for case_id in selection.ordered_case_ids
        if case_id in required
    )
    assignments = {
        case_id: selection.shard_assignments[case_id]
        for case_id in required
    }
    digest = stable_digest(
        selection.registry_digest,
        target,
        ordered,
        assignments,
    )
    return RegistrySelection(
        profile=selection.profile,
        cases=cases,
        ordered_case_ids=ordered,
        dependency_closure={
            case_id: tuple(
                dependency
                for dependency in selection.dependency_closure.get(case_id, ())
                if dependency in required
            )
            for case_id in ordered
        },
        missing_capabilities={
            case_id: value
            for case_id, value in selection.missing_capabilities.items()
            if case_id in required
        },
        shard_assignments=assignments,
        registry_digest=digest,
    )


__all__ = [
    "CapabilityState",
    "CaseExecutor",
    "CaseRegistry",
    "RegisteredCase",
    "RegistrySelection",
    "shard_filter",
]
