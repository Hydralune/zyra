from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import CorrelationRefs, StructuredObservation, runtime_id
from .observers import DeadlineRecord, ToolDeadlineObserver


class TerminalResultDisposition(StrEnum):
    ACCEPTED = "accepted"
    LATE_AFTER_TIMEOUT = "late_after_timeout"
    DUPLICATE = "duplicate"
    STALE_GENERATION = "stale_generation"
    UNKNOWN_CALL = "unknown_call"


@dataclass(slots=True)
class ToolExecutionLease:
    refs: CorrelationRefs
    generation: int
    token: str
    record: DeadlineRecord
    terminal_result_id: str = ""
    terminal_ok: bool | None = None
    result_disposition: TerminalResultDisposition | None = None
    late_result_ids: list[str] = field(default_factory=list)
    duplicate_result_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TerminalResultReceipt:
    tool_call_id: str
    result_id: str
    generation: int
    disposition: TerminalResultDisposition
    accepted: bool
    timeout_observation_id: str
    detail: str


class ToolDeadlineRuntime:
    """Fences terminal tool results against a real deadline observation.

    ToolDeadlineObserver detects the timeout boundary. This runtime provides
    the complementary execution contract: one generation-scoped terminal
    result may commit, while a result arriving after timeout is retained as
    diagnostic evidence and cannot overwrite the timed-out outcome.
    """

    def __init__(self, observer: ToolDeadlineObserver) -> None:
        self.observer = observer
        self._guard = threading.RLock()
        self._leases: dict[str, ToolExecutionLease] = {}
        self._accepted_results = 0
        self._late_results = 0
        self._duplicate_results = 0
        self._stale_results = 0

    def arm(
        self,
        refs: CorrelationRefs,
        *,
        generation: int,
        deadline_ms: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolExecutionLease:
        if generation < 0:
            raise ValueError("tool execution generation must be non-negative")
        if not refs.tool_call_id:
            raise ValueError("tool execution lease requires tool_call_id")
        with self._guard:
            existing = self._leases.get(refs.tool_call_id)
            if existing is not None:
                if generation < existing.generation:
                    raise RuntimeError("stale tool generation cannot replace execution lease")
                if generation == existing.generation and not existing.record.terminal:
                    raise RuntimeError("tool generation already has an active execution lease")
            token = runtime_id("tool-lease")
            record = self.observer.begin(
                refs,
                deadline_ms=deadline_ms,
                metadata={
                    **dict(metadata or {}),
                    "execution_generation": generation,
                    "execution_lease_token": token,
                },
            )
            lease = ToolExecutionLease(
                refs=refs,
                generation=generation,
                token=token,
                record=record,
            )
            self._leases[refs.tool_call_id] = lease
            return lease

    def accept_result(
        self,
        tool_call_id: str,
        *,
        generation: int,
        token: str,
        result_id: str,
        ok: bool,
        cancelled: bool = False,
    ) -> TerminalResultReceipt:
        if not result_id.strip():
            raise ValueError("tool result_id must not be empty")
        with self._guard:
            lease = self._leases.get(tool_call_id)
            if lease is None:
                return self._receipt(
                    tool_call_id,
                    result_id,
                    generation,
                    TerminalResultDisposition.UNKNOWN_CALL,
                    False,
                    "tool call has no execution lease",
                )
            if generation != lease.generation or token != lease.token:
                self._stale_results += 1
                return self._receipt(
                    tool_call_id,
                    result_id,
                    generation,
                    TerminalResultDisposition.STALE_GENERATION,
                    False,
                    "terminal result failed generation/token fence",
                    lease=lease,
                )
            if lease.record.status == "timed_out":
                if result_id not in lease.late_result_ids:
                    lease.late_result_ids.append(result_id)
                    self._late_results += 1
                lease.result_disposition = TerminalResultDisposition.LATE_AFTER_TIMEOUT
                return self._receipt(
                    tool_call_id,
                    result_id,
                    generation,
                    TerminalResultDisposition.LATE_AFTER_TIMEOUT,
                    False,
                    "deadline won the terminal-result race",
                    lease=lease,
                )
            if lease.terminal_result_id:
                if result_id not in lease.duplicate_result_ids:
                    lease.duplicate_result_ids.append(result_id)
                    self._duplicate_results += 1
                return self._receipt(
                    tool_call_id,
                    result_id,
                    generation,
                    TerminalResultDisposition.DUPLICATE,
                    False,
                    "another terminal result already committed",
                    lease=lease,
                )
            self.observer.settle(tool_call_id, ok=ok, cancelled=cancelled)
            lease.terminal_result_id = result_id
            lease.terminal_ok = ok and not cancelled
            lease.result_disposition = TerminalResultDisposition.ACCEPTED
            self._accepted_results += 1
            return self._receipt(
                tool_call_id,
                result_id,
                generation,
                TerminalResultDisposition.ACCEPTED,
                True,
                "terminal result committed before deadline",
                lease=lease,
            )

    def poll(self, *, at_ms: int | None = None) -> tuple[StructuredObservation, ...]:
        observations = self.observer.poll(at_ms=at_ms)
        with self._guard:
            for observation in observations:
                lease = self._leases.get(observation.refs.tool_call_id)
                if lease is not None:
                    lease.result_disposition = TerminalResultDisposition.LATE_AFTER_TIMEOUT
        return observations

    def cancel_generation(
        self,
        tool_call_id: str,
        *,
        generation: int,
        token: str,
        result_id: str,
    ) -> TerminalResultReceipt:
        return self.accept_result(
            tool_call_id,
            generation=generation,
            token=token,
            result_id=result_id,
            ok=False,
            cancelled=True,
        )

    def active(self) -> tuple[ToolExecutionLease, ...]:
        with self._guard:
            return tuple(
                item
                for item in self._leases.values()
                if not item.record.terminal
            )

    def snapshot(self) -> Mapping[str, Any]:
        with self._guard:
            leases = {
                tool_call_id: {
                    "run_id": item.refs.run_id,
                    "task_id": item.refs.task_id,
                    "generation": item.generation,
                    "status": item.record.status,
                    "started_ms": item.record.started_ms,
                    "deadline_ms": item.record.deadline_ms,
                    "completed_ms": item.record.completed_ms,
                    "timeout_observation_id": item.record.timeout_observation_id,
                    "terminal_result_id": item.terminal_result_id,
                    "terminal_ok": item.terminal_ok,
                    "result_disposition": (
                        item.result_disposition.value if item.result_disposition else ""
                    ),
                    "late_result_ids": list(item.late_result_ids),
                    "duplicate_result_ids": list(item.duplicate_result_ids),
                }
                for tool_call_id, item in sorted(self._leases.items())
            }
            return {
                "schema": "zyra.tool-deadline-runtime/v1",
                "leases": leases,
                "accepted_results": self._accepted_results,
                "late_results": self._late_results,
                "duplicate_results": self._duplicate_results,
                "stale_results": self._stale_results,
                "late_result_can_mutate_terminal_state": False,
            }

    @staticmethod
    def _receipt(
        tool_call_id: str,
        result_id: str,
        generation: int,
        disposition: TerminalResultDisposition,
        accepted: bool,
        detail: str,
        *,
        lease: ToolExecutionLease | None = None,
    ) -> TerminalResultReceipt:
        return TerminalResultReceipt(
            tool_call_id=tool_call_id,
            result_id=result_id,
            generation=generation,
            disposition=disposition,
            accepted=accepted,
            timeout_observation_id=(
                lease.record.timeout_observation_id if lease is not None else ""
            ),
            detail=detail,
        )


def deadline_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.tool-deadline-runtime-contract/v1",
        "deadline_observer": "ToolDeadlineObserver",
        "terminal_result_owner": "ToolDeadlineRuntime",
        "generation_and_token_fenced": True,
        "timeout_wins_late_result_race": True,
        "late_result_retained_as_diagnostic": True,
        "late_result_can_mutate_terminal_state": False,
    }


__all__ = [
    "TerminalResultDisposition",
    "TerminalResultReceipt",
    "ToolDeadlineRuntime",
    "ToolExecutionLease",
    "deadline_runtime_contract",
]
