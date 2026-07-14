from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .constants import DEFAULT_QUEUE_DEPTH
from .errors import GatewayErrorCode, SandboxGatewayError


@dataclass(slots=True)
class _QueueState:
    condition: threading.Condition
    next_ticket: int = 0
    serving_ticket: int = 0
    waiting: int = 0
    active: bool = False


class SessionActorQueue:
    """OpenClaw-derived per-session FIFO serialization without global blocking."""

    def __init__(self, *, maximum_depth: int = DEFAULT_QUEUE_DEPTH) -> None:
        self.maximum_depth = int(maximum_depth)
        self._states: dict[str, _QueueState] = {}
        self._lock = threading.RLock()

    @contextmanager
    def enter(
        self,
        session_id: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> Iterator[int]:
        state = self._state(session_id)
        deadline = time.monotonic() + timeout_seconds
        with state.condition:
            if state.waiting >= self.maximum_depth:
                raise SandboxGatewayError(
                    GatewayErrorCode.QUEUE_FULL,
                    "sandbox session actor queue is full",
                    operation="session_queue",
                    retryable=True,
                )
            ticket = state.next_ticket
            state.next_ticket += 1
            state.waiting += 1
            try:
                while ticket != state.serving_ticket or state.active:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if ticket == state.serving_ticket:
                            state.serving_ticket += 1
                            state.condition.notify_all()
                        raise SandboxGatewayError(
                            GatewayErrorCode.SESSION_NOT_READY,
                            "timed out waiting for session actor queue",
                            operation="session_queue",
                            retryable=True,
                        )
                    state.condition.wait(timeout=remaining)
                state.active = True
                state.waiting -= 1
            except BaseException:
                if state.waiting > 0:
                    state.waiting -= 1
                raise
        try:
            yield ticket
        finally:
            with state.condition:
                state.active = False
                state.serving_ticket += 1
                state.condition.notify_all()
            self._prune(session_id, state)

    def depth(self, session_id: str) -> int:
        state = self._state(session_id)
        with state.condition:
            return state.waiting + int(state.active)

    def descriptor(self) -> dict[str, object]:
        with self._lock:
            return {
                "maximum_depth": self.maximum_depth,
                "sessions": len(self._states),
                "depths": {
                    key: value.waiting + int(value.active)
                    for key, value in sorted(self._states.items())
                },
            }

    def _state(self, session_id: str) -> _QueueState:
        with self._lock:
            state = self._states.get(session_id)
            if state is None:
                state = _QueueState(condition=threading.Condition(threading.RLock()))
                self._states[session_id] = state
            return state

    def _prune(self, session_id: str, state: _QueueState) -> None:
        with self._lock:
            if not state.active and state.waiting == 0 and state.serving_ticket == state.next_ticket:
                self._states.pop(session_id, None)
