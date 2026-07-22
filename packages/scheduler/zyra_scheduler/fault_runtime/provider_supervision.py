from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import CorrelationRefs, StructuredObservation
from .observers import ProviderFailureObserver


class ProviderCircuitPhase(StrEnum):
    CLOSED = "closed"
    BACKOFF = "backoff"
    OPEN = "open"
    HALF_OPEN = "half_open"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ProviderRetryPolicy:
    max_attempts: int = 4
    base_backoff_ms: int = 250
    max_backoff_ms: int = 30_000
    jitter_ratio: float = 0.0
    circuit_failure_threshold: int = 4
    circuit_reset_ms: int = 30_000
    retryable_status_codes: tuple[int, ...] = (408, 429, 500, 502, 503, 504, 529)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("provider max_attempts must be positive")
        if self.base_backoff_ms < 1 or self.max_backoff_ms < self.base_backoff_ms:
            raise ValueError("provider backoff bounds are invalid")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("provider jitter ratio must be in range 0..1")
        if self.circuit_failure_threshold < 1 or self.circuit_reset_ms < 1:
            raise ValueError("provider circuit policy is invalid")

    def backoff_ms(self, attempt_number: int, *, jitter_unit: float = 0.5) -> int:
        exponent = max(0, attempt_number - 1)
        base = min(self.base_backoff_ms * (2**exponent), self.max_backoff_ms)
        if self.jitter_ratio == 0:
            return base
        bounded = max(0.0, min(1.0, float(jitter_unit)))
        multiplier = 1.0 + ((bounded * 2.0 - 1.0) * self.jitter_ratio)
        return max(1, min(self.max_backoff_ms, int(round(base * multiplier))))


@dataclass(slots=True)
class ProviderAttemptState:
    refs: CorrelationRefs
    policy: ProviderRetryPolicy
    generation: int
    phase: ProviderCircuitPhase = ProviderCircuitPhase.CLOSED
    attempt_number: int = 0
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    next_attempt_at_ms: int | None = None
    opened_at_ms: int | None = None
    active_attempt_id: str = ""
    last_error_code: str = ""
    last_status_code: int | None = None
    last_observation_id: str = ""
    rejected_attempts: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.refs.run_id}:{self.refs.task_id}:{self.refs.provider_id}"

    @property
    def exhausted(self) -> bool:
        return self.attempt_number >= self.policy.max_attempts


@dataclass(frozen=True, slots=True)
class ProviderAttemptPermit:
    provider_id: str
    generation: int
    attempt_number: int
    attempt_id: str
    half_open_probe: bool
    issued_at_ms: int


@dataclass(frozen=True, slots=True)
class ProviderFailureOutcome:
    observation: StructuredObservation | None
    retryable: bool
    exhausted: bool
    circuit_open: bool
    next_attempt_at_ms: int | None
    attempt_number: int


class ProviderAttemptSupervisor:
    """Deterministic provider attempt/backoff/circuit state around real responses.

    The observer classifies only response facts. This supervisor owns the
    attempt lifecycle needed to decide when a real provider error becomes
    terminal, when another attempt is legal and when a circuit may probe.
    It does not select a replacement provider; route recovery remains 07C.
    """

    _NON_RETRYABLE_CODES = {
        "authentication_error",
        "invalid_api_key",
        "invalid_request",
        "permission_error",
        "content_policy",
        "quota_exhausted",
        "usage_limit_reached",
        "insufficient_quota",
        "credits_exhausted",
    }

    def __init__(
        self,
        observer: ProviderFailureObserver,
        *,
        monotonic_ms: Callable[[], int] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self.observer = observer
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self.jitter = jitter or random.random
        self._guard = threading.RLock()
        self._states: dict[str, ProviderAttemptState] = {}
        self._provider_index: dict[str, str] = {}

    def bind(
        self,
        refs: CorrelationRefs,
        *,
        generation: int,
        policy: ProviderRetryPolicy | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderAttemptState:
        if not refs.provider_id:
            raise ValueError("provider supervision requires explicit provider_id")
        if generation < 0:
            raise ValueError("provider generation must be non-negative")
        key = f"{refs.run_id}:{refs.task_id}:{refs.provider_id}"
        with self._guard:
            current_key = self._provider_index.get(refs.provider_id)
            current = self._states.get(current_key or "")
            if current is not None:
                if generation < current.generation:
                    raise RuntimeError("stale provider generation cannot replace current state")
                if generation == current.generation:
                    if current.key != key:
                        raise RuntimeError("provider generation cannot move across run/task scope")
                    return current
                current.phase = ProviderCircuitPhase.STOPPED
            state = ProviderAttemptState(
                refs=refs,
                policy=policy or ProviderRetryPolicy(),
                generation=generation,
                metadata=dict(metadata or {}),
            )
            self._states[key] = state
            self._provider_index[refs.provider_id] = key
            return state

    def begin_attempt(
        self,
        provider_id: str,
        *,
        generation: int,
        attempt_id: str,
        at_ms: int | None = None,
    ) -> ProviderAttemptPermit:
        if not attempt_id.strip():
            raise ValueError("provider attempt_id must not be empty")
        now = self._now(at_ms)
        with self._guard:
            state = self._require(provider_id, generation)
            self._advance_circuit(state, now)
            if state.phase is ProviderCircuitPhase.STOPPED:
                state.rejected_attempts += 1
                raise RuntimeError("provider supervision is stopped")
            if state.phase is ProviderCircuitPhase.OPEN:
                state.rejected_attempts += 1
                raise RuntimeError("provider circuit is open")
            if state.next_attempt_at_ms is not None and now < state.next_attempt_at_ms:
                state.rejected_attempts += 1
                raise RuntimeError("provider retry backoff has not elapsed")
            if state.active_attempt_id:
                raise RuntimeError("provider already has an active attempt")
            if state.exhausted and state.phase is not ProviderCircuitPhase.HALF_OPEN:
                state.rejected_attempts += 1
                raise RuntimeError("provider attempt budget is exhausted")
            half_open = state.phase is ProviderCircuitPhase.HALF_OPEN
            state.attempt_number += 1
            state.active_attempt_id = attempt_id
            state.next_attempt_at_ms = None
            return ProviderAttemptPermit(
                provider_id=provider_id,
                generation=generation,
                attempt_number=state.attempt_number,
                attempt_id=attempt_id,
                half_open_probe=half_open,
                issued_at_ms=now,
            )

    def succeeded(
        self,
        permit: ProviderAttemptPermit,
        *,
        at_ms: int | None = None,
    ) -> ProviderAttemptState:
        self._now(at_ms)
        with self._guard:
            state = self._require(permit.provider_id, permit.generation)
            self._require_active_permit(state, permit)
            state.active_attempt_id = ""
            state.phase = ProviderCircuitPhase.CLOSED
            state.consecutive_failures = 0
            state.total_successes += 1
            state.next_attempt_at_ms = None
            state.opened_at_ms = None
            state.last_error_code = ""
            state.last_status_code = None
            state.attempt_number = 0
            return state

    def failed(
        self,
        permit: ProviderAttemptPermit,
        *,
        status_code: int | None = None,
        error_type: str = "",
        error_code: str = "provider_error",
        retryable: bool | None = None,
        details: Mapping[str, Any] | None = None,
        at_ms: int | None = None,
    ) -> ProviderFailureOutcome:
        now = self._now(at_ms)
        with self._guard:
            state = self._require(permit.provider_id, permit.generation)
            self._require_active_permit(state, permit)
            normalized_code = error_code.strip().lower() or "provider_error"
            can_retry = self._retryable(state, normalized_code, status_code, retryable)
            exhausted = state.attempt_number >= state.policy.max_attempts
            state.active_attempt_id = ""
            state.consecutive_failures += 1
            state.total_failures += 1
            state.last_error_code = normalized_code
            state.last_status_code = status_code
            should_open = (
                state.consecutive_failures >= state.policy.circuit_failure_threshold
                or permit.half_open_probe
            )
            if should_open:
                state.phase = ProviderCircuitPhase.OPEN
                state.opened_at_ms = now
                state.next_attempt_at_ms = now + state.policy.circuit_reset_ms
            elif can_retry and not exhausted:
                state.phase = ProviderCircuitPhase.BACKOFF
                delay = state.policy.backoff_ms(
                    state.attempt_number,
                    jitter_unit=self.jitter(),
                )
                state.next_attempt_at_ms = now + delay
            else:
                state.phase = ProviderCircuitPhase.OPEN if exhausted else ProviderCircuitPhase.CLOSED
                state.opened_at_ms = now if exhausted else None
                state.next_attempt_at_ms = (
                    now + state.policy.circuit_reset_ms if exhausted else None
                )
            terminal = exhausted or not can_retry or should_open
            refs = CorrelationRefs(
                **{
                    **state.refs.to_dict(),
                    "attempt_id": permit.attempt_id,
                    "observation_id": state.refs.observation_id,
                    "source_state_revision": permit.attempt_number,
                }
            )
            observation = self.observer.observe_response(
                refs,
                ok=False,
                status_code=status_code,
                error_type=error_type,
                error_code=normalized_code,
                retryable=can_retry and not exhausted,
                terminal=terminal,
                details={
                    **state.metadata,
                    **dict(details or {}),
                    "attempt_number": permit.attempt_number,
                    "max_attempts": state.policy.max_attempts,
                    "consecutive_failures": state.consecutive_failures,
                    "circuit_phase": state.phase.value,
                    "circuit_open": state.phase is ProviderCircuitPhase.OPEN,
                    "next_attempt_at_ms": state.next_attempt_at_ms,
                    "attempt_budget_exhausted": exhausted,
                },
            )
            if observation is not None:
                state.last_observation_id = observation.observation_id
            return ProviderFailureOutcome(
                observation=observation,
                retryable=can_retry and not exhausted,
                exhausted=exhausted,
                circuit_open=state.phase is ProviderCircuitPhase.OPEN,
                next_attempt_at_ms=state.next_attempt_at_ms,
                attempt_number=permit.attempt_number,
            )

    def retry_due(self, provider_id: str, *, generation: int, at_ms: int | None = None) -> bool:
        now = self._now(at_ms)
        with self._guard:
            state = self._require(provider_id, generation)
            self._advance_circuit(state, now)
            return (
                state.phase in {ProviderCircuitPhase.CLOSED, ProviderCircuitPhase.HALF_OPEN}
                or (
                    state.phase is ProviderCircuitPhase.BACKOFF
                    and state.next_attempt_at_ms is not None
                    and now >= state.next_attempt_at_ms
                )
            )

    def stop(self, provider_id: str, *, generation: int) -> bool:
        with self._guard:
            state = self._lookup(provider_id)
            if state is None or state.generation != generation:
                return False
            state.phase = ProviderCircuitPhase.STOPPED
            state.active_attempt_id = ""
            state.next_attempt_at_ms = None
            return True

    def snapshot(self) -> Mapping[str, Any]:
        with self._guard:
            states = {
                provider_id: {
                    "run_id": state.refs.run_id,
                    "task_id": state.refs.task_id,
                    "generation": state.generation,
                    "phase": state.phase.value,
                    "attempt_number": state.attempt_number,
                    "max_attempts": state.policy.max_attempts,
                    "consecutive_failures": state.consecutive_failures,
                    "total_failures": state.total_failures,
                    "total_successes": state.total_successes,
                    "next_attempt_at_ms": state.next_attempt_at_ms,
                    "opened_at_ms": state.opened_at_ms,
                    "active_attempt_id": state.active_attempt_id,
                    "last_error_code": state.last_error_code,
                    "last_status_code": state.last_status_code,
                    "last_observation_id": state.last_observation_id,
                    "rejected_attempts": state.rejected_attempts,
                }
                for provider_id in sorted(self._provider_index)
                if (state := self._lookup(provider_id)) is not None
            }
        return {
            "schema": "zyra.provider-attempt-supervisor/v1",
            "states": states,
            "route_recovery_owner": "M1-S07C",
            "provider_selection_owner": False,
        }

    def _advance_circuit(self, state: ProviderAttemptState, now: int) -> None:
        if (
            state.phase is ProviderCircuitPhase.OPEN
            and state.next_attempt_at_ms is not None
            and now >= state.next_attempt_at_ms
        ):
            state.phase = ProviderCircuitPhase.HALF_OPEN
            state.next_attempt_at_ms = None
            state.active_attempt_id = ""
            state.attempt_number = 0

    @staticmethod
    def _retryable(
        state: ProviderAttemptState,
        error_code: str,
        status_code: int | None,
        explicit: bool | None,
    ) -> bool:
        if error_code in ProviderAttemptSupervisor._NON_RETRYABLE_CODES:
            return False
        if explicit is not None:
            return bool(explicit)
        if status_code is not None:
            return status_code in state.policy.retryable_status_codes
        return error_code in {
            "provider_error",
            "rate_limit",
            "rate_limited",
            "overloaded",
            "timeout",
            "connection_error",
        }

    @staticmethod
    def _require_active_permit(
        state: ProviderAttemptState,
        permit: ProviderAttemptPermit,
    ) -> None:
        if state.active_attempt_id != permit.attempt_id:
            raise RuntimeError("provider result does not match active attempt")
        if state.attempt_number != permit.attempt_number:
            raise RuntimeError("provider result carries a stale attempt number")

    def _require(self, provider_id: str, generation: int) -> ProviderAttemptState:
        state = self._lookup(provider_id)
        if state is None:
            raise RuntimeError("provider is not bound to attempt supervision")
        if state.generation != generation:
            raise RuntimeError("stale provider generation")
        return state

    def _lookup(self, provider_id: str) -> ProviderAttemptState | None:
        key = self._provider_index.get(provider_id)
        return self._states.get(key or "")

    def _now(self, value: int | None) -> int:
        now = self.monotonic_ms() if value is None else int(value)
        if now < 0:
            raise ValueError("monotonic time must not be negative")
        return now


def provider_supervision_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.provider-supervision-contract/v1",
        "response_observer": "ProviderFailureObserver",
        "attempt_state_owner": "ProviderAttemptSupervisor",
        "source_role": "oh-my-pi supplementary",
        "backoff": "bounded exponential with configurable jitter",
        "circuit": "closed/backoff/open/half_open/stopped",
        "generation_fenced": True,
        "route_recovery_owner": "M1-S07C",
    }


__all__ = [
    "ProviderAttemptPermit",
    "ProviderAttemptState",
    "ProviderAttemptSupervisor",
    "ProviderCircuitPhase",
    "ProviderFailureOutcome",
    "ProviderRetryPolicy",
    "provider_supervision_contract",
]
