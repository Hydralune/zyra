from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Mapping


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class TerminalPhase(StrEnum):
    OPENING = "opening"
    RUNNING = "running"
    PERMISSION_PENDING = "permission_pending"
    EXITED = "exited"
    KILLED = "killed"
    TIMED_OUT = "timed_out"
    CRASHED = "crashed"
    REJECTED = "rejected"


TERMINAL_PHASE_TRANSITIONS: Mapping[TerminalPhase, frozenset[TerminalPhase]] = {
    TerminalPhase.OPENING: frozenset(
        {
            TerminalPhase.RUNNING,
            TerminalPhase.PERMISSION_PENDING,
            TerminalPhase.REJECTED,
            TerminalPhase.CRASHED,
        }
    ),
    TerminalPhase.PERMISSION_PENDING: frozenset(
        {
            TerminalPhase.OPENING,
            TerminalPhase.RUNNING,
            TerminalPhase.REJECTED,
            TerminalPhase.CRASHED,
        }
    ),
    TerminalPhase.RUNNING: frozenset(
        {
            TerminalPhase.EXITED,
            TerminalPhase.KILLED,
            TerminalPhase.TIMED_OUT,
            TerminalPhase.CRASHED,
        }
    ),
    TerminalPhase.EXITED: frozenset(),
    TerminalPhase.KILLED: frozenset(),
    TerminalPhase.TIMED_OUT: frozenset(),
    TerminalPhase.CRASHED: frozenset(),
    TerminalPhase.REJECTED: frozenset(),
}


class TerminalError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.details = dict(details or {})

    def response(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "fallback": False,
            **({"details": dict(self.details)} if self.details else {}),
        }


@dataclass(frozen=True, slots=True)
class TerminalBinding:
    task_id: str
    run_id: str
    terminal_id: str
    session_id: str
    workspace_id: str
    workspace_revision: int
    worker_id: str
    command_id: str
    tool_call_id: str
    span_id: str

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "terminal_id": self.terminal_id,
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "worker_id": self.worker_id,
            "command_id": self.command_id,
            "tool_call_id": self.tool_call_id,
            "span_id": self.span_id,
        }


@dataclass(frozen=True, slots=True)
class TerminalPermission:
    effect: str
    decision_id: str
    reason_code: str
    reason: str
    request_id: str = ""
    permit_id: str = ""
    canonical_owner: str = "typescript.PermissionCoordinator"

    def to_json(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "decision_id": self.decision_id,
            "request_id": self.request_id or None,
            "permit_id": self.permit_id or None,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "canonical_owner": self.canonical_owner,
        }


@dataclass(frozen=True, slots=True)
class TerminalSpill:
    artifact_id: str
    revision: str
    media_type: str
    sha256: str
    byte_length: int
    first_cursor: int
    next_cursor: int
    binary: bool
    redacted: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "first_cursor": self.first_cursor,
            "next_cursor": self.next_cursor,
            "binary": self.binary,
            "redacted": self.redacted,
        }


@dataclass(frozen=True, slots=True)
class TerminalStatus:
    phase: TerminalPhase
    cwd: str
    title: str
    rows: int
    cols: int
    cursor: int
    earliest_cursor: int
    started_at: str
    updated_at: str
    state_mutation_id: str
    pid: int | None = None
    exit_code: int | None = None
    signal: str = ""
    exited_at: str = ""
    viewers: int = 0
    spilled_bytes: int = 0
    binary_bytes: int = 0
    input_sequence: int = 0
    resize_sequence: int = 0

    def transition(
        self,
        phase: TerminalPhase,
        *,
        state_mutation_id: str,
        exit_code: int | None = None,
        signal: str = "",
    ) -> TerminalStatus:
        if phase == self.phase:
            return replace(
                self,
                updated_at=now_iso(),
                state_mutation_id=state_mutation_id,
                exit_code=exit_code if exit_code is not None else self.exit_code,
                signal=signal or self.signal,
            )
        if phase not in TERMINAL_PHASE_TRANSITIONS[self.phase]:
            raise TerminalError(
                "terminal_phase_transition_invalid",
                f"Terminal cannot transition from {self.phase} to {phase}.",
            )
        terminal = phase in {
            TerminalPhase.EXITED,
            TerminalPhase.KILLED,
            TerminalPhase.TIMED_OUT,
            TerminalPhase.CRASHED,
            TerminalPhase.REJECTED,
        }
        timestamp = now_iso()
        return replace(
            self,
            phase=phase,
            updated_at=timestamp,
            exited_at=timestamp if terminal else "",
            state_mutation_id=state_mutation_id,
            exit_code=exit_code,
            signal=signal,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase),
            "pid": self.pid,
            "cwd": self.cwd,
            "title": self.title,
            "rows": self.rows,
            "cols": self.cols,
            "cursor": self.cursor,
            "earliest_cursor": self.earliest_cursor,
            "exit_code": self.exit_code,
            "signal": self.signal or None,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "exited_at": self.exited_at or None,
            "viewers": self.viewers,
            "spilled_bytes": self.spilled_bytes,
            "binary_bytes": self.binary_bytes,
            "input_sequence": self.input_sequence,
            "resize_sequence": self.resize_sequence,
            "state_mutation_id": self.state_mutation_id,
        }


@dataclass(frozen=True, slots=True)
class TerminalProjection:
    binding: TerminalBinding
    status: TerminalStatus
    permission: TerminalPermission
    socket_path: str
    correlation_id: str
    causation_id: str
    event_id: str = ""
    ticket: str = ""
    ticket_expires_at: str = ""
    intervention_counted: bool = False
    human_intervention_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "protocol": "zyra.terminal.v1",
            "binding": self.binding.to_json(),
            "status": self.status.to_json(),
            "permission": self.permission.to_json(),
            "socket_path": self.socket_path,
            "ticket": self.ticket or None,
            "ticket_expires_at": self.ticket_expires_at or None,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "event_id": self.event_id or None,
            "intervention_counted": self.intervention_counted,
            "human_intervention_count": self.human_intervention_count,
        }


@dataclass(frozen=True, slots=True)
class TerminalCreateRequest:
    task_id: str
    run_id: str
    session_id: str
    worker_id: str
    command_id: str
    tool_call_id: str
    span_id: str
    actor_id: str
    command: str
    title: str
    cwd: str
    shell: str
    rows: int
    cols: int
    sealed: bool
    competition_mode: str
    permission_permit_id: str = ""
    environment: Mapping[str, str] = field(default_factory=dict)
    correlation_id: str = ""
    causation_id: str = ""


@dataclass(frozen=True, slots=True)
class TerminalControlRequest:
    task_id: str
    run_id: str
    terminal_id: str
    session_id: str
    worker_id: str
    tool_call_id: str
    span_id: str
    actor_id: str
    action: str
    sequence: int
    sealed: bool
    competition_mode: str
    data: str = ""
    rows: int = 0
    cols: int = 0
    reason: str = ""
    permission_permit_id: str = ""
