from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import FaultKind, FaultSeverity, FaultSignal, SignalOrigin, utc_now


class PressureLevel(StrEnum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    SATURATED = "saturated"
    STORM = "storm"


@dataclass(frozen=True, slots=True)
class PressurePolicy:
    window_ms: int = 60_000
    elevated_count: int = 3
    saturated_count: int = 6
    storm_count: int = 12
    distinct_kind_storm_count: int = 5
    critical_weight: int = 4
    error_weight: int = 2
    warning_weight: int = 1
    injection_weight: int = 0

    def __post_init__(self) -> None:
        if self.window_ms < 1:
            raise ValueError("pressure window must be positive")
        thresholds = (self.elevated_count, self.saturated_count, self.storm_count)
        if not (0 < thresholds[0] < thresholds[1] < thresholds[2]):
            raise ValueError("pressure thresholds must be positive and strictly increasing")
        if self.distinct_kind_storm_count < 2:
            raise ValueError("distinct kind storm threshold must be at least two")
        if min(self.critical_weight, self.error_weight, self.warning_weight, self.injection_weight) < 0:
            raise ValueError("pressure weights must be non-negative")


@dataclass(frozen=True, slots=True)
class PressureSample:
    signal_id: str
    task_id: str
    run_id: str
    fault_kind: FaultKind
    severity: FaultSeverity
    origin: SignalOrigin
    weight: int
    observed_ms: int
    observer_id: str
    target_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "fault_kind": self.fault_kind.value,
            "severity": self.severity.value,
            "origin": self.origin.value,
            "weight": self.weight,
            "observed_ms": self.observed_ms,
            "observer_id": self.observer_id,
            "target_key": self.target_key,
        }


@dataclass(frozen=True, slots=True)
class PressureDecision:
    task_id: str
    run_id: str
    level: PressureLevel
    previous_level: PressureLevel
    weighted_count: int
    real_signal_count: int
    injection_signal_count: int
    distinct_kinds: tuple[FaultKind, ...]
    distinct_observers: tuple[str, ...]
    window_started_ms: int
    window_ends_ms: int
    changed: bool
    force_recovery_handoff: bool
    reason: str
    signal_id: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.fault-pressure-decision/v1",
            "task_id": self.task_id,
            "run_id": self.run_id,
            "level": self.level.value,
            "previous_level": self.previous_level.value,
            "weighted_count": self.weighted_count,
            "real_signal_count": self.real_signal_count,
            "injection_signal_count": self.injection_signal_count,
            "distinct_kinds": [item.value for item in self.distinct_kinds],
            "distinct_observers": list(self.distinct_observers),
            "window_started_ms": self.window_started_ms,
            "window_ends_ms": self.window_ends_ms,
            "changed": self.changed,
            "force_recovery_handoff": self.force_recovery_handoff,
            "reason": self.reason,
            "signal_id": self.signal_id,
            "created_at": self.created_at,
            "injection_contributes_to_pressure": False,
        }


@dataclass(slots=True)
class TaskPressureState:
    task_id: str
    run_id: str
    level: PressureLevel = PressureLevel.NORMAL
    revision: int = 0
    samples: deque[PressureSample] = field(default_factory=deque)
    transition_count: int = 0
    last_signal_id: str = ""
    last_changed_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "level": self.level.value,
            "revision": self.revision,
            "transition_count": self.transition_count,
            "last_signal_id": self.last_signal_id,
            "last_changed_at": self.last_changed_at,
            "sample_count": len(self.samples),
            "samples": [item.to_dict() for item in self.samples],
        }


class FaultPressureMonitor:
    """Detects real-source fault storms without treating injections as health."""

    def __init__(
        self,
        policy: PressurePolicy | None = None,
        *,
        monotonic_ms: Callable[[], int] | None = None,
    ) -> None:
        self.policy = policy or PressurePolicy()
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self._guard = threading.RLock()
        self._states: dict[str, TaskPressureState] = {}
        self._decisions: deque[PressureDecision] = deque(maxlen=5_000)

    def record(self, signal: FaultSignal, *, at_ms: int | None = None) -> PressureDecision:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        key = f"{signal.refs.run_id}:{signal.refs.task_id}"
        sample = PressureSample(
            signal_id=signal.signal_id,
            task_id=signal.refs.task_id,
            run_id=signal.refs.run_id,
            fault_kind=signal.kind,
            severity=signal.severity,
            origin=signal.origin,
            weight=self._weight(signal),
            observed_ms=now,
            observer_id=signal.provenance.observer_id,
            target_key=self._target_key(signal),
        )
        with self._guard:
            state = self._states.setdefault(
                key,
                TaskPressureState(task_id=signal.refs.task_id, run_id=signal.refs.run_id),
            )
            self._prune(state, now)
            if any(item.signal_id == signal.signal_id for item in state.samples):
                return self._decision(state, signal, now, changed=False, reason="signal already sampled")
            state.samples.append(sample)
            state.revision += 1
            state.last_signal_id = signal.signal_id
            previous = state.level
            state.level = self._level(state)
            changed = state.level is not previous
            if changed:
                state.transition_count += 1
                state.last_changed_at = utc_now()
            decision = self._decision(
                state,
                signal,
                now,
                changed=changed,
                previous=previous,
                reason=self._reason(state),
            )
            self._decisions.append(decision)
            return decision

    def sweep(self, *, at_ms: int | None = None) -> tuple[PressureDecision, ...]:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        decisions: list[PressureDecision] = []
        with self._guard:
            states = tuple(self._states.values())
            for state in states:
                previous = state.level
                self._prune(state, now)
                state.level = self._level(state)
                if state.level is previous:
                    continue
                state.revision += 1
                state.transition_count += 1
                state.last_changed_at = utc_now()
                placeholder = self._last_signal(state)
                if placeholder is None:
                    continue
                decision = self._decision(
                    state,
                    placeholder,
                    now,
                    changed=True,
                    previous=previous,
                    reason="fault pressure decayed after old samples left the window",
                )
                self._decisions.append(decision)
                decisions.append(decision)
        return tuple(decisions)

    def level(self, task_id: str, *, run_id: str = "") -> PressureLevel:
        with self._guard:
            candidates = [
                item
                for item in self._states.values()
                if item.task_id == task_id and (not run_id or item.run_id == run_id)
            ]
            if not candidates:
                return PressureLevel.NORMAL
            return max(candidates, key=lambda item: self._rank(item.level)).level

    def snapshot(self, *, task_id: str = "") -> Mapping[str, Any]:
        with self._guard:
            states = {
                key: value.to_dict()
                for key, value in sorted(self._states.items())
                if not task_id or value.task_id == task_id
            }
            decisions = [
                item.to_dict()
                for item in self._decisions
                if not task_id or item.task_id == task_id
            ]
        return {
            "schema": "zyra.fault-pressure-monitor/v1",
            "policy": {
                "window_ms": self.policy.window_ms,
                "elevated_count": self.policy.elevated_count,
                "saturated_count": self.policy.saturated_count,
                "storm_count": self.policy.storm_count,
                "distinct_kind_storm_count": self.policy.distinct_kind_storm_count,
                "injection_weight": self.policy.injection_weight,
            },
            "states": states,
            "decisions": decisions[-200:],
            "injection_masks_observer_pressure": False,
        }

    def _prune(self, state: TaskPressureState, now: int) -> None:
        oldest = now - self.policy.window_ms
        while state.samples and state.samples[0].observed_ms < oldest:
            state.samples.popleft()

    def _weight(self, signal: FaultSignal) -> int:
        if signal.origin is SignalOrigin.INJECTION:
            return self.policy.injection_weight
        return {
            FaultSeverity.INFO: 0,
            FaultSeverity.WARNING: self.policy.warning_weight,
            FaultSeverity.ERROR: self.policy.error_weight,
            FaultSeverity.CRITICAL: self.policy.critical_weight,
        }[signal.severity]

    def _level(self, state: TaskPressureState) -> PressureLevel:
        real = [item for item in state.samples if item.origin is SignalOrigin.OBSERVER]
        weighted = sum(item.weight for item in real)
        kinds = {item.fault_kind for item in real}
        if weighted >= self.policy.storm_count or len(kinds) >= self.policy.distinct_kind_storm_count:
            return PressureLevel.STORM
        if weighted >= self.policy.saturated_count:
            return PressureLevel.SATURATED
        if weighted >= self.policy.elevated_count:
            return PressureLevel.ELEVATED
        return PressureLevel.NORMAL

    def _decision(
        self,
        state: TaskPressureState,
        signal: FaultSignal | PressureSample,
        now: int,
        *,
        changed: bool,
        reason: str,
        previous: PressureLevel | None = None,
    ) -> PressureDecision:
        real = [item for item in state.samples if item.origin is SignalOrigin.OBSERVER]
        injected = [item for item in state.samples if item.origin is SignalOrigin.INJECTION]
        return PressureDecision(
            task_id=state.task_id,
            run_id=state.run_id,
            level=state.level,
            previous_level=previous or state.level,
            weighted_count=sum(item.weight for item in real),
            real_signal_count=len(real),
            injection_signal_count=len(injected),
            distinct_kinds=tuple(sorted({item.fault_kind for item in real}, key=lambda item: item.value)),
            distinct_observers=tuple(sorted({item.observer_id for item in real})),
            window_started_ms=max(0, now - self.policy.window_ms),
            window_ends_ms=now,
            changed=changed,
            force_recovery_handoff=state.level in {PressureLevel.SATURATED, PressureLevel.STORM},
            reason=reason,
            signal_id=signal.signal_id,
        )

    def _reason(self, state: TaskPressureState) -> str:
        real = [item for item in state.samples if item.origin is SignalOrigin.OBSERVER]
        weighted = sum(item.weight for item in real)
        kinds = len({item.fault_kind for item in real})
        return (
            f"real-source fault pressure is {state.level.value}: "
            f"weighted_count={weighted}, signals={len(real)}, distinct_kinds={kinds}"
        )

    @staticmethod
    def _target_key(signal: FaultSignal) -> str:
        refs = signal.refs
        return next(
            (
                value
                for value in (
                    refs.tool_call_id,
                    refs.worker_id,
                    refs.browser_session_id,
                    refs.provider_id,
                    refs.workspace_id,
                    refs.mcp_server_id,
                    refs.subagent_task_id,
                    refs.attempt_id,
                )
                if value
            ),
            refs.task_id,
        )

    @staticmethod
    def _rank(level: PressureLevel) -> int:
        return {
            PressureLevel.NORMAL: 0,
            PressureLevel.ELEVATED: 1,
            PressureLevel.SATURATED: 2,
            PressureLevel.STORM: 3,
        }[level]

    @staticmethod
    def _last_signal(state: TaskPressureState) -> PressureSample | None:
        return state.samples[-1] if state.samples else None


__all__ = [
    "FaultPressureMonitor",
    "PressureDecision",
    "PressureLevel",
    "PressurePolicy",
    "PressureSample",
    "TaskPressureState",
]
