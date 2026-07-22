from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .contracts import StructuredObservation, stable_digest


_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_CONTENT_FREE = frozenset(
    {
        "ok",
        "done",
        "continue",
        "no issue",
        "no issues",
        "no concern",
        "no concerns",
        "nothing to report",
        "looks good",
        "all good",
        "healthy",
    }
)


def normalize_summary(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return _NON_WORD.sub(" ", normalized).strip()


@dataclass(frozen=True, slots=True)
class EmissionGuardPolicy:
    history_capacity: int = 4096
    per_window_limit: int = 64
    window_ms: int = 1_000
    duplicate_window_ms: int = 30_000
    suppress_content_free: bool = True

    def __post_init__(self) -> None:
        if self.history_capacity < 1:
            raise ValueError("history_capacity must be positive")
        if self.per_window_limit < 1:
            raise ValueError("per_window_limit must be positive")
        if self.window_ms < 1 or self.duplicate_window_ms < 0:
            raise ValueError("emission guard windows are invalid")


@dataclass(slots=True)
class EmissionGuardState:
    accepted: int = 0
    suppressed_duplicate: int = 0
    suppressed_noise: int = 0
    suppressed_rate: int = 0
    last_accepted_at_ms: int = 0
    seen: OrderedDict[str, int] = field(default_factory=OrderedDict)
    window: deque[int] = field(default_factory=deque)

    def snapshot(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "suppressed_duplicate": self.suppressed_duplicate,
            "suppressed_noise": self.suppressed_noise,
            "suppressed_rate": self.suppressed_rate,
            "last_accepted_at_ms": self.last_accepted_at_ms,
            "history_size": len(self.seen),
            "window_size": len(self.window),
        }


@dataclass(frozen=True, slots=True)
class EmissionDecision:
    accepted: bool
    reason: str
    key: str
    at_ms: int
    state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "key": self.key,
            "at_ms": self.at_ms,
            "state": dict(self.state),
        }


class ObservationEmissionGuard:
    """Bounded observation gate derived from OMP's emission guard.

    Identity fields participate in the key and are never parsed from the
    summary.  Noise suppression therefore cannot turn prose into a worker,
    route, provider or workspace reference.
    """

    def __init__(
        self,
        policy: EmissionGuardPolicy | None = None,
        *,
        monotonic_ms: Callable[[], int] | None = None,
    ) -> None:
        self.policy = policy or EmissionGuardPolicy()
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self._guard = threading.RLock()
        self._states: dict[str, EmissionGuardState] = {}

    def accept(
        self,
        observation: StructuredObservation,
        *,
        at_ms: int | None = None,
    ) -> EmissionDecision:
        now = self.monotonic_ms() if at_ms is None else int(at_ms)
        if now < 0:
            raise ValueError("emission time cannot be negative")
        observer_id = observation.provenance.observer_id
        summary = normalize_summary(observation.summary)
        key = stable_digest(
            {
                "observer_id": observer_id,
                "category": observation.category.value,
                "code": observation.code,
                "refs": observation.refs.to_dict(),
                "status": observation.status,
                "error_type": observation.error_type,
                "status_code": observation.status_code,
                "details": dict(observation.details),
            }
        )
        with self._guard:
            state = self._states.setdefault(observer_id, EmissionGuardState())
            self._prune_window(state, now)
            self._prune_history(state, now)
            if self.policy.suppress_content_free and summary in _CONTENT_FREE:
                state.suppressed_noise += 1
                return self._decision(False, "content_free", key, now, state)
            previous = state.seen.get(key)
            if previous is not None and now - previous <= self.policy.duplicate_window_ms:
                state.suppressed_duplicate += 1
                state.seen.move_to_end(key)
                return self._decision(False, "duplicate", key, now, state)
            if len(state.window) >= self.policy.per_window_limit:
                state.suppressed_rate += 1
                return self._decision(False, "rate_limited", key, now, state)
            state.accepted += 1
            state.last_accepted_at_ms = now
            state.window.append(now)
            state.seen[key] = now
            state.seen.move_to_end(key)
            while len(state.seen) > self.policy.history_capacity:
                state.seen.popitem(last=False)
            return self._decision(True, "accepted", key, now, state)

    def reset(self, observer_id: str = "") -> None:
        with self._guard:
            if observer_id:
                self._states.pop(observer_id, None)
            else:
                self._states.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._guard:
            return {
                "schema": "zyra.watchdog-emission-guard/v1",
                "policy": {
                    "history_capacity": self.policy.history_capacity,
                    "per_window_limit": self.policy.per_window_limit,
                    "window_ms": self.policy.window_ms,
                    "duplicate_window_ms": self.policy.duplicate_window_ms,
                    "suppress_content_free": self.policy.suppress_content_free,
                },
                "observers": {
                    observer_id: state.snapshot()
                    for observer_id, state in sorted(self._states.items())
                },
                "critical_ref_source": "structured_refs_only",
            }

    def _prune_window(self, state: EmissionGuardState, now: int) -> None:
        while state.window and now - state.window[0] >= self.policy.window_ms:
            state.window.popleft()

    def _prune_history(self, state: EmissionGuardState, now: int) -> None:
        if self.policy.duplicate_window_ms == 0:
            state.seen.clear()
            return
        stale: list[str] = []
        for key, accepted_at in state.seen.items():
            if now - accepted_at <= self.policy.duplicate_window_ms:
                break
            stale.append(key)
        for key in stale:
            state.seen.pop(key, None)

    @staticmethod
    def _decision(
        accepted: bool,
        reason: str,
        key: str,
        at_ms: int,
        state: EmissionGuardState,
    ) -> EmissionDecision:
        return EmissionDecision(
            accepted=accepted,
            reason=reason,
            key=key,
            at_ms=at_ms,
            state=state.snapshot(),
        )
