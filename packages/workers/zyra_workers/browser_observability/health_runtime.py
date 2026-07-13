from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .models import (
    HealthStatus,
    ObservationScope,
    Severity,
    WatchdogSignal,
    digest_value,
    new_observation_id,
    utc_now,
)


_STATUS_RANK: dict[HealthStatus, int] = {
    HealthStatus.UNKNOWN: 0,
    HealthStatus.HEALTHY: 1,
    HealthStatus.DEGRADED: 2,
    HealthStatus.UNHEALTHY: 3,
    HealthStatus.TERMINATED: 4,
}

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.ERROR: 2,
    Severity.CRITICAL: 3,
}


@dataclass(frozen=True, slots=True)
class HealthAggregationPolicy:
    recovery_candidate_statuses: frozenset[HealthStatus] = frozenset(
        {
            HealthStatus.UNHEALTHY,
            HealthStatus.TERMINATED,
        }
    )
    retain_signal_ids: int = 2_000
    retain_transitions: int = 500
    allow_recovery_to_healthy: bool = True

    def __post_init__(self) -> None:
        if self.retain_signal_ids < 1:
            raise ValueError("health signal retention must be positive")
        if self.retain_transitions < 1:
            raise ValueError("health transition retention must be positive")


@dataclass(frozen=True, slots=True)
class BrowserHealthTransition:
    scope: ObservationScope
    previous: HealthStatus
    current: HealthStatus
    signal_ids: tuple[str, ...]
    transition_id: str = field(
        default_factory=lambda: new_observation_id("browser-health-transition")
    )
    created_at: str = field(default_factory=utc_now)
    terminal: bool = False
    summary: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict(include_digest=False))

    def to_dict(
        self,
        *,
        include_digest: bool = True,
    ) -> dict[str, Any]:
        value = {
            "schema": "zyra.browser-observability.health-transition.v1",
            "transition_id": self.transition_id,
            "scope": self.scope.to_dict(),
            "previous": str(self.previous),
            "current": str(self.current),
            "signal_ids": list(self.signal_ids),
            "created_at": self.created_at,
            "terminal": self.terminal,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }
        if include_digest:
            value["digest"] = self.digest
        return value


@dataclass(frozen=True, slots=True)
class BrowserSessionHealth:
    scope: ObservationScope
    status: HealthStatus = HealthStatus.UNKNOWN
    revision: int = 0
    updated_at: str = field(default_factory=utc_now)
    latest_signal_id: str = ""
    terminal_signal_id: str = ""
    signal_ids: tuple[str, ...] = ()
    recovery_candidate_signal_ids: tuple[str, ...] = ()
    transitions: tuple[BrowserHealthTransition, ...] = ()
    watchdog_statuses: Mapping[str, str] = field(default_factory=dict)
    counters: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.session-health.v1",
            "scope": self.scope.to_dict(),
            "status": str(self.status),
            "revision": self.revision,
            "updated_at": self.updated_at,
            "latest_signal_id": self.latest_signal_id,
            "terminal_signal_id": self.terminal_signal_id,
            "signal_ids": list(self.signal_ids),
            "recovery_candidate_signal_ids": list(
                self.recovery_candidate_signal_ids
            ),
            "transitions": [item.to_dict() for item in self.transitions],
            "watchdog_statuses": dict(self.watchdog_statuses),
            "counters": dict(self.counters),
            "canonical": False,
            "durable_source": "BrowserHistoryStore",
            "recovery_planner_owner": "M1-07C",
        }


class BrowserHealthRuntime:
    """Reconstructible health projection over durable watchdog facts.

    This process-live cache is not a canonical state owner. Its inputs are
    durable WatchdogSignal history records, and it can be rebuilt by replay.
    """

    def __init__(
        self,
        *,
        policy: HealthAggregationPolicy | None = None,
    ) -> None:
        self.policy = policy or HealthAggregationPolicy()
        self._guard = threading.RLock()
        self._states: dict[str, BrowserSessionHealth] = {}

    def ingest(
        self,
        scope: ObservationScope,
        signals: Sequence[WatchdogSignal],
    ) -> BrowserSessionHealth:
        with self._guard:
            state = self._states.get(scope.key)
            if state is None:
                state = BrowserSessionHealth(scope=scope)
            elif state.scope != scope:
                raise RuntimeError("browser health scope key collision")
            seen = set(state.signal_ids)
            fresh = tuple(
                item
                for item in signals
                if item.signal_id not in seen
            )
            if any(item.scope != scope for item in fresh):
                raise ValueError("browser health signals cross observation scopes")
            if not fresh:
                return state
            next_status = self._aggregate_status(
                state.status,
                fresh,
            )
            watchdog_statuses = dict(state.watchdog_statuses)
            counters = dict(state.counters)
            recovery_candidates = list(state.recovery_candidate_signal_ids)
            terminal_signal_id = state.terminal_signal_id
            for signal in fresh:
                watchdog_statuses[str(signal.watchdog)] = str(signal.status)
                counters[str(signal.kind)] = counters.get(str(signal.kind), 0) + 1
                if signal.status in self.policy.recovery_candidate_statuses:
                    recovery_candidates.append(signal.signal_id)
                if signal.terminal:
                    terminal_signal_id = signal.signal_id
            signal_ids = (
                *state.signal_ids,
                *(item.signal_id for item in fresh),
            )[-self.policy.retain_signal_ids:]
            transition_values = list(state.transitions)
            if next_status != state.status:
                transition_values.append(
                    BrowserHealthTransition(
                        scope=scope,
                        previous=state.status,
                        current=next_status,
                        signal_ids=tuple(item.signal_id for item in fresh),
                        terminal=any(item.terminal for item in fresh),
                        summary=self._transition_summary(
                            state.status,
                            next_status,
                            fresh,
                        ),
                        metadata={
                            "watchdogs": sorted(
                                {str(item.watchdog) for item in fresh}
                            ),
                            "signal_kinds": sorted(
                                {str(item.kind) for item in fresh}
                            ),
                        },
                    )
                )
            updated = BrowserSessionHealth(
                scope=scope,
                status=next_status,
                revision=state.revision + 1,
                updated_at=utc_now(),
                latest_signal_id=fresh[-1].signal_id,
                terminal_signal_id=terminal_signal_id,
                signal_ids=tuple(signal_ids),
                recovery_candidate_signal_ids=tuple(
                    dict.fromkeys(recovery_candidates)
                )[-self.policy.retain_signal_ids:],
                transitions=tuple(
                    transition_values[-self.policy.retain_transitions:]
                ),
                watchdog_statuses=watchdog_statuses,
                counters=counters,
            )
            self._states[scope.key] = updated
            return updated

    def mark_healthy(
        self,
        scope: ObservationScope,
        *,
        evidence_signal_ids: Sequence[str],
        summary: str,
    ) -> BrowserSessionHealth:
        if not self.policy.allow_recovery_to_healthy:
            raise RuntimeError("health policy prohibits explicit recovery transition")
        if not evidence_signal_ids:
            raise ValueError("healthy transition requires evidence signal ids")
        with self._guard:
            state = self.require(scope)
            transition = BrowserHealthTransition(
                scope=scope,
                previous=state.status,
                current=HealthStatus.HEALTHY,
                signal_ids=tuple(evidence_signal_ids),
                terminal=False,
                summary=summary,
                metadata={
                    "explicit_recovery_observation": True,
                    "planner_owner": "M1-07C",
                },
            )
            updated = replace(
                state,
                status=HealthStatus.HEALTHY,
                revision=state.revision + 1,
                updated_at=utc_now(),
                transitions=(
                    *state.transitions,
                    transition,
                )[-self.policy.retain_transitions:],
            )
            self._states[scope.key] = updated
            return updated

    def rebuild(
        self,
        scope: ObservationScope,
        signals: Sequence[WatchdogSignal],
    ) -> BrowserSessionHealth:
        with self._guard:
            self._states.pop(scope.key, None)
        ordered = tuple(
            sorted(
                signals,
                key=lambda item: (
                    item.sequence,
                    item.detected_at,
                    item.signal_id,
                ),
            )
        )
        return self.ingest(scope, ordered)

    def require(
        self,
        scope: ObservationScope,
    ) -> BrowserSessionHealth:
        state = self._states.get(scope.key)
        if state is None:
            raise KeyError(scope.key)
        if state.scope != scope:
            raise RuntimeError("browser health scope key collision")
        return state

    def projection(
        self,
        *,
        scope: ObservationScope | None = None,
        task_id: str = "",
    ) -> dict[str, Any]:
        with self._guard:
            if scope is not None:
                values = (
                    (self._states[scope.key],)
                    if scope.key in self._states
                    else ()
                )
            else:
                values = tuple(
                    item
                    for item in self._states.values()
                    if not task_id or item.scope.task_id == task_id
                )
            return {
                "schema": "zyra.browser-observability.health-projection.v1",
                "scope_count": len(values),
                "healthy": sum(
                    1
                    for item in values
                    if item.status == HealthStatus.HEALTHY
                ),
                "degraded": sum(
                    1
                    for item in values
                    if item.status == HealthStatus.DEGRADED
                ),
                "unhealthy": sum(
                    1
                    for item in values
                    if item.status == HealthStatus.UNHEALTHY
                ),
                "terminated": sum(
                    1
                    for item in values
                    if item.status == HealthStatus.TERMINATED
                ),
                "sessions": [item.to_dict() for item in values],
                "canonical": False,
                "durable_source": "BrowserHistoryStore",
            }

    @staticmethod
    def _aggregate_status(
        current: HealthStatus,
        signals: Sequence[WatchdogSignal],
    ) -> HealthStatus:
        candidate = max(
            (item.status for item in signals),
            key=lambda item: _STATUS_RANK[item],
            default=current,
        )
        if _STATUS_RANK[candidate] >= _STATUS_RANK[current]:
            return candidate
        explicit_healthy = all(
            item.status == HealthStatus.HEALTHY
            and item.severity == Severity.INFO
            for item in signals
        )
        return candidate if explicit_healthy else current

    @staticmethod
    def _transition_summary(
        previous: HealthStatus,
        current: HealthStatus,
        signals: Sequence[WatchdogSignal],
    ) -> str:
        strongest = max(
            signals,
            key=lambda item: (
                _STATUS_RANK[item.status],
                _SEVERITY_RANK[item.severity],
                item.sequence,
            ),
        )
        return (
            f"Browser health changed from {previous} to {current}: "
            f"{strongest.summary}"
        )
