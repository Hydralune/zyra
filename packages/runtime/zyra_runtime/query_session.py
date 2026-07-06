from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping

from zyra_core import new_id, now_iso, to_jsonable


class QueryMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class MessageLifecyclePhase(StrEnum):
    CREATED = "created"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPACTED = "compacted"
    SUPERSEDED = "superseded"


class TurnLifecyclePhase(StrEnum):
    CREATED = "created"
    STARTED = "started"
    STREAMING = "streaming"
    TOOL_EXECUTING = "tool_executing"
    CONTINUING = "continuing"
    COMPLETED = "completed"
    FAILED = "failed"


class QueryStreamEventType(StrEnum):
    SESSION_STARTED = "session_started"
    STREAM_REQUEST_START = "stream_request_start"
    TURN_STARTED = "turn_started"
    TURN_START = "turn_start"
    MESSAGE_CREATED = "message_created"
    MESSAGE_DELTA = "message_delta"
    MESSAGE_COMPLETED = "message_completed"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_BATCH_STARTED = "tool_batch_started"
    TOOL_BATCH_COMPLETED = "tool_batch_completed"
    TOOL_USE_SUMMARY = "tool_use_summary"
    TOOL_LOOP_PLAN = "tool_loop_plan"
    TOOL_RESULT_BUDGET_EXCEEDED = "tool_result_budget_exceeded"
    TOOL_FAILURE_SIGNAL = "tool_failure_signal"
    WATCHDOG_SIGNAL = "watchdog_signal"
    CONTEXT_COMPACTED = "context_compacted"
    ERROR = "error"
    CONTINUE = "continue"
    TURN_COMPLETED = "turn_completed"
    TURN_END = "turn_end"
    QUERY_SESSION_SNAPSHOT = "query_session_snapshot"
    SESSION_COMPLETED = "session_completed"


class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_TURNS_EXCEEDED = "max_turns_exceeded"
    TOOL_ERROR = "tool_error"
    STREAM_ERROR = "stream_error"
    CONTEXT_COMPACTED = "context_compacted"
    CONTINUE_REQUESTED = "continue_requested"
    USER_CANCELLED = "user_cancelled"
    SESSION_COMPLETED = "session_completed"
    UNKNOWN = "unknown"


class TranscriptEntryType(StrEnum):
    SESSION_METADATA = "session_metadata"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"
    STREAM_EVENT = "stream_event"
    SUMMARY = "summary"
    SNAPSHOT = "snapshot"


@dataclass(slots=True)
class MessageLifecycle:
    message_id: str
    role: QueryMessageRole
    turn_id: str | None = None
    parent_uuid: str | None = None
    phase: MessageLifecyclePhase = MessageLifecyclePhase.CREATED
    content: str = ""
    deltas: list[str] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    completed_at: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def append_delta(self, delta: str) -> None:
        self.phase = MessageLifecyclePhase.STREAMING
        self.deltas.append(delta)
        self.content += delta
        self.updated_at = now_iso()

    def complete(self, *, metadata: Mapping[str, Any] | None = None) -> None:
        self.phase = MessageLifecyclePhase.COMPLETED
        self.completed_at = now_iso()
        self.updated_at = self.completed_at
        if metadata:
            self.metadata.update(dict(metadata))

    def fail(self, error: str, *, metadata: Mapping[str, Any] | None = None) -> None:
        self.phase = MessageLifecyclePhase.FAILED
        self.error = error
        self.completed_at = now_iso()
        self.updated_at = self.completed_at
        if metadata:
            self.metadata.update(dict(metadata))

    def compact(self, *, artifact_id: str, preview: str = "") -> None:
        self.phase = MessageLifecyclePhase.COMPACTED
        self.metadata["compact_artifact_id"] = artifact_id
        if preview:
            self.content = preview
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": str(self.role),
            "turn_id": self.turn_id,
            "parent_uuid": self.parent_uuid,
            "phase": str(self.phase),
            "content": self.content,
            "deltas": list(self.deltas),
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "metadata": to_jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MessageLifecycle":
        return cls(
            message_id=str(data.get("message_id") or new_id("qmsg")),
            role=_enum_or_default(QueryMessageRole, data.get("role"), QueryMessageRole.USER),
            turn_id=_optional_str(data.get("turn_id")),
            parent_uuid=_optional_str(data.get("parent_uuid")),
            phase=_enum_or_default(MessageLifecyclePhase, data.get("phase"), MessageLifecyclePhase.CREATED),
            content=str(data.get("content") or ""),
            deltas=[str(item) for item in _as_list(data.get("deltas"))],
            tool_call_id=_optional_str(data.get("tool_call_id")),
            tool_name=_optional_str(data.get("tool_name")),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            completed_at=_optional_str(data.get("completed_at")),
            error=_optional_str(data.get("error")),
            metadata=dict(_as_mapping(data.get("metadata"))),
        )


@dataclass(slots=True)
class TurnState:
    turn_id: str
    turn_index: int
    phase: TurnLifecyclePhase = TurnLifecyclePhase.CREATED
    user_message_id: str | None = None
    assistant_message_ids: list[str] = field(default_factory=list)
    tool_message_ids: list[str] = field(default_factory=list)
    tool_call_ids: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    completed_at: str | None = None
    stop_reason: StopReason | None = None
    error: str | None = None
    continue_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def start(self) -> None:
        self.phase = TurnLifecyclePhase.STARTED
        self.updated_at = now_iso()

    def stream(self) -> None:
        self.phase = TurnLifecyclePhase.STREAMING
        self.updated_at = now_iso()

    def execute_tool(self, tool_call_id: str) -> None:
        self.phase = TurnLifecyclePhase.TOOL_EXECUTING
        if tool_call_id not in self.tool_call_ids:
            self.tool_call_ids.append(tool_call_id)
        self.updated_at = now_iso()

    def request_continue(self, *, reason: StopReason = StopReason.CONTINUE_REQUESTED, error: str | None = None) -> None:
        self.phase = TurnLifecyclePhase.CONTINUING
        self.stop_reason = reason
        self.error = error
        self.continue_count += 1
        self.updated_at = now_iso()

    def complete(self, *, stop_reason: StopReason = StopReason.END_TURN) -> None:
        self.phase = TurnLifecyclePhase.COMPLETED
        self.stop_reason = stop_reason
        self.completed_at = now_iso()
        self.updated_at = self.completed_at

    def fail(self, error: str, *, stop_reason: StopReason = StopReason.TOOL_ERROR) -> None:
        self.phase = TurnLifecyclePhase.FAILED
        self.error = error
        self.stop_reason = stop_reason
        self.completed_at = now_iso()
        self.updated_at = self.completed_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "turn_index": self.turn_index,
            "phase": str(self.phase),
            "user_message_id": self.user_message_id,
            "assistant_message_ids": list(self.assistant_message_ids),
            "tool_message_ids": list(self.tool_message_ids),
            "tool_call_ids": list(self.tool_call_ids),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "stop_reason": str(self.stop_reason) if self.stop_reason else None,
            "error": self.error,
            "continue_count": self.continue_count,
            "metadata": to_jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TurnState":
        return cls(
            turn_id=str(data.get("turn_id") or new_id("turn")),
            turn_index=int(data.get("turn_index") or 0),
            phase=_enum_or_default(TurnLifecyclePhase, data.get("phase"), TurnLifecyclePhase.CREATED),
            user_message_id=_optional_str(data.get("user_message_id")),
            assistant_message_ids=[str(item) for item in _as_list(data.get("assistant_message_ids"))],
            tool_message_ids=[str(item) for item in _as_list(data.get("tool_message_ids"))],
            tool_call_ids=[str(item) for item in _as_list(data.get("tool_call_ids"))],
            started_at=str(data.get("started_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            completed_at=_optional_str(data.get("completed_at")),
            stop_reason=_optional_enum(StopReason, data.get("stop_reason")),
            error=_optional_str(data.get("error")),
            continue_count=int(data.get("continue_count") or 0),
            metadata=dict(_as_mapping(data.get("metadata"))),
        )


@dataclass(slots=True)
class SessionTranscriptEntry:
    sequence: int
    entry_type: TranscriptEntryType
    uuid: str
    parent_uuid: str | None = None
    turn_id: str | None = None
    role: QueryMessageRole | None = None
    content: str = ""
    event_type: QueryStreamEventType | None = None
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "type": str(self.entry_type),
            "uuid": self.uuid,
            "parent_uuid": self.parent_uuid,
            "turn_id": self.turn_id,
            "role": str(self.role) if self.role else None,
            "content": self.content,
            "event_type": str(self.event_type) if self.event_type else None,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SessionTranscriptEntry":
        return cls(
            sequence=int(data.get("sequence") or 0),
            entry_type=_enum_or_default(TranscriptEntryType, data.get("type"), TranscriptEntryType.STREAM_EVENT),
            uuid=str(data.get("uuid") or new_id("entry")),
            parent_uuid=_optional_str(data.get("parent_uuid")),
            turn_id=_optional_str(data.get("turn_id")),
            role=_optional_enum(QueryMessageRole, data.get("role")),
            content=str(data.get("content") or ""),
            event_type=_optional_enum(QueryStreamEventType, data.get("event_type")),
            created_at=str(data.get("created_at") or now_iso()),
            metadata=dict(_as_mapping(data.get("metadata"))),
        )


@dataclass(slots=True)
class QuerySessionSnapshot:
    session_id: str
    worker_request_id: str
    run_id: str
    task_id: str
    node_id: str | None
    status: str
    created_at: str
    updated_at: str
    leaf_uuid: str | None
    sequence: int
    resume_token: str
    messages: list[dict[str, Any]]
    turns: list[dict[str, Any]]
    transcript: list[dict[str, Any]]
    stats: dict[str, Any]
    consistency: dict[str, Any]
    source_contract: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(
            {
                "session_id": self.session_id,
                "worker_request_id": self.worker_request_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "node_id": self.node_id,
                "status": self.status,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "leaf_uuid": self.leaf_uuid,
                "sequence": self.sequence,
                "resume_token": self.resume_token,
                "messages": self.messages,
                "turns": self.turns,
                "transcript": self.transcript,
                "stats": self.stats,
                "consistency": self.consistency,
                "source_contract": self.source_contract,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QuerySessionSnapshot":
        return cls(
            session_id=str(data.get("session_id") or new_id("codesession")),
            worker_request_id=str(data.get("worker_request_id") or ""),
            run_id=str(data.get("run_id") or ""),
            task_id=str(data.get("task_id") or ""),
            node_id=_optional_str(data.get("node_id")),
            status=str(data.get("status") or "active"),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            leaf_uuid=_optional_str(data.get("leaf_uuid")),
            sequence=int(data.get("sequence") or 0),
            resume_token=str(data.get("resume_token") or ""),
            messages=[dict(_as_mapping(item)) for item in _as_list(data.get("messages"))],
            turns=[dict(_as_mapping(item)) for item in _as_list(data.get("turns"))],
            transcript=[dict(_as_mapping(item)) for item in _as_list(data.get("transcript"))],
            stats=dict(_as_mapping(data.get("stats"))),
            consistency=dict(_as_mapping(data.get("consistency"))),
            source_contract=dict(_as_mapping(data.get("source_contract"))),
            metadata=dict(_as_mapping(data.get("metadata"))),
        )


class QuerySession:
    """Append-only query/session state aligned with Claude Code session semantics.

    The class deliberately keeps an in-memory mutable working set plus an
    append-only transcript. Snapshots are immutable dictionaries suitable for
    checkpoint metadata and artifact storage.
    """

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        worker_request_id: str,
        node_id: str | None = None,
        session_id: str | None = None,
        source_contract: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.node_id = node_id
        self.worker_request_id = worker_request_id
        self.session_id = session_id or new_id("codesession")
        self.created_at = now_iso()
        self.updated_at = self.created_at
        self.status = "active"
        self.source_contract = dict(source_contract or {})
        self.metadata = dict(metadata or {})
        self.messages: list[MessageLifecycle] = []
        self.turns: list[TurnState] = []
        self.transcript: list[SessionTranscriptEntry] = []
        self.leaf_uuid: str | None = None
        self._sequence = 0
        self._active_turn: TurnState | None = None
        self._active_assistant_message: MessageLifecycle | None = None
        self._append_transcript(
            TranscriptEntryType.SESSION_METADATA,
            uuid=self.session_id,
            parent_uuid=None,
            metadata={
                "session_id": self.session_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "node_id": self.node_id,
                "worker_request_id": self.worker_request_id,
                "source": "claude-code-best",
            },
        )
        self._append_stream_event(
            QueryStreamEventType.SESSION_STARTED,
            metadata={
                "worker_request_id": worker_request_id,
                "node_id": node_id,
                "source": "claude-code-best",
            },
        )

    @classmethod
    def restore(cls, snapshot: Mapping[str, Any]) -> "QuerySession":
        restored = QuerySessionSnapshot.from_dict(snapshot)
        session = cls(
            run_id=restored.run_id,
            task_id=restored.task_id,
            node_id=restored.node_id,
            worker_request_id=restored.worker_request_id,
            session_id=restored.session_id,
            source_contract=restored.source_contract,
            metadata=restored.metadata,
        )
        session.created_at = restored.created_at
        session.updated_at = restored.updated_at
        session.status = restored.status
        session.leaf_uuid = restored.leaf_uuid
        session.messages = [MessageLifecycle.from_dict(item) for item in restored.messages]
        session.turns = [TurnState.from_dict(item) for item in restored.turns]
        session.transcript = [SessionTranscriptEntry.from_dict(item) for item in restored.transcript]
        session._sequence = max([entry.sequence for entry in session.transcript], default=restored.sequence)
        session._active_turn = next((turn for turn in reversed(session.turns) if turn.completed_at is None), None)
        session._active_assistant_message = next(
            (
                message
                for message in reversed(session.messages)
                if message.role == QueryMessageRole.ASSISTANT and message.phase == MessageLifecyclePhase.STREAMING
            ),
            None,
        )
        return session

    @classmethod
    def from_jsonl(cls, lines: Iterable[str], *, source_contract: Mapping[str, Any] | None = None) -> "QuerySession":
        entries = []
        for line in lines:
            if not line.strip():
                continue
            entries.append(SessionTranscriptEntry.from_dict(json.loads(line)))
        metadata = next((entry for entry in entries if entry.entry_type == TranscriptEntryType.SESSION_METADATA), None)
        if metadata is None:
            raise ValueError("session transcript does not contain session metadata")
        session = cls(
            run_id=str(metadata.metadata.get("run_id") or ""),
            task_id=str(metadata.metadata.get("task_id") or ""),
            node_id=_optional_str(metadata.metadata.get("node_id")),
            worker_request_id=str(metadata.metadata.get("worker_request_id") or ""),
            session_id=str(metadata.metadata.get("session_id") or metadata.uuid),
            source_contract=source_contract,
        )
        session.transcript = entries
        session._sequence = max((entry.sequence for entry in entries), default=0)
        session.leaf_uuid = entries[-1].uuid if entries else None
        return session

    @property
    def active_turn(self) -> TurnState | None:
        return self._active_turn

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def resume_token(self) -> str:
        leaf = self.leaf_uuid or "none"
        return f"{self.session_id}:{leaf}:{self._sequence}"

    def start_turn(self, turn_index: int, *, user_content: str, metadata: Mapping[str, Any] | None = None) -> TurnState:
        turn = TurnState(turn_id=new_id("turn"), turn_index=turn_index, metadata=dict(metadata or {}))
        turn.start()
        self.turns.append(turn)
        self._active_turn = turn
        self._append_stream_event(QueryStreamEventType.TURN_STARTED, turn_id=turn.turn_id, metadata={"turn_index": turn_index})
        self._append_stream_event(QueryStreamEventType.TURN_START, turn_id=turn.turn_id, metadata={"turn_index": turn_index})
        user_message = self._append_message(
            QueryMessageRole.USER,
            content=user_content,
            turn_id=turn.turn_id,
            metadata={"turn_index": turn_index, **dict(metadata or {})},
        )
        user_message.complete()
        turn.user_message_id = user_message.message_id
        self._append_stream_event(
            QueryStreamEventType.MESSAGE_DELTA,
            turn_id=turn.turn_id,
            message_id=user_message.message_id,
            metadata={"role": str(user_message.role), "delta": user_content, "synthetic": True},
        )
        return turn

    def start_stream_request(self, *, turn_id: str | None = None, metadata: Mapping[str, Any] | None = None) -> None:
        self._append_stream_event(QueryStreamEventType.STREAM_REQUEST_START, turn_id=turn_id, metadata=dict(metadata or {}))
        if self._active_turn is not None:
            self._active_turn.stream()

    def start_assistant_message(self, *, turn_id: str | None = None, metadata: Mapping[str, Any] | None = None) -> MessageLifecycle:
        message = self._append_message(
            QueryMessageRole.ASSISTANT,
            content="",
            turn_id=turn_id or (self._active_turn.turn_id if self._active_turn else None),
            metadata=dict(metadata or {}),
        )
        message.phase = MessageLifecyclePhase.STREAMING
        self._active_assistant_message = message
        if self._active_turn is not None:
            self._active_turn.assistant_message_ids.append(message.message_id)
        self._append_stream_event(
            QueryStreamEventType.MESSAGE_CREATED,
            turn_id=message.turn_id,
            message_id=message.message_id,
            metadata={"role": str(message.role), **dict(metadata or {})},
        )
        return message

    def append_assistant_delta(self, delta: str, *, metadata: Mapping[str, Any] | None = None) -> MessageLifecycle:
        message = self._active_assistant_message or self.start_assistant_message(metadata=metadata)
        message.append_delta(delta)
        self._append_stream_event(
            QueryStreamEventType.MESSAGE_DELTA,
            turn_id=message.turn_id,
            message_id=message.message_id,
            metadata={"role": str(message.role), "delta": delta, **dict(metadata or {})},
        )
        return message

    def complete_assistant_message(self, *, metadata: Mapping[str, Any] | None = None) -> None:
        if self._active_assistant_message is None:
            return
        self._active_assistant_message.complete(metadata=metadata)
        self._append_stream_event(
            QueryStreamEventType.MESSAGE_COMPLETED,
            turn_id=self._active_assistant_message.turn_id,
            message_id=self._active_assistant_message.message_id,
            metadata={"role": str(self._active_assistant_message.role), **dict(metadata or {})},
        )
        self._active_assistant_message = None

    def record_tool_call(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        turn_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self._active_turn is not None:
            self._active_turn.execute_tool(tool_call_id)
        self._append_stream_event(
            QueryStreamEventType.TOOL_CALL_STARTED,
            turn_id=turn_id or (self._active_turn.turn_id if self._active_turn else None),
            metadata={"tool_call_id": tool_call_id, "tool_name": tool_name, **dict(metadata or {})},
        )

    def record_tool_result(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        summary: str,
        ok: bool,
        error: str | None = None,
        artifacts: Iterable[str] = (),
        turn_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MessageLifecycle:
        message = self._append_message(
            QueryMessageRole.TOOL,
            content=summary,
            turn_id=turn_id or (self._active_turn.turn_id if self._active_turn else None),
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            metadata={
                "ok": ok,
                "error": error,
                "artifact_ids": list(artifacts),
                **dict(metadata or {}),
            },
        )
        if ok:
            message.complete()
        else:
            message.fail(error or "tool_error")
        if self._active_turn is not None:
            self._active_turn.tool_message_ids.append(message.message_id)
            if tool_call_id not in self._active_turn.tool_call_ids:
                self._active_turn.tool_call_ids.append(tool_call_id)
        self._append_stream_event(
            QueryStreamEventType.MESSAGE_DELTA,
            turn_id=message.turn_id,
            message_id=message.message_id,
            metadata={
                "role": str(message.role),
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "delta": summary,
                "ok": ok,
                "error": error,
            },
        )
        self._append_stream_event(
            QueryStreamEventType.TOOL_CALL_COMPLETED,
            turn_id=message.turn_id,
            metadata={
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "ok": ok,
                "error": error,
                "artifact_ids": list(artifacts),
                **dict(metadata or {}),
            },
        )
        return message

    def record_batch_event(
        self,
        event_type: QueryStreamEventType,
        *,
        turn_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._append_stream_event(event_type, turn_id=turn_id, metadata=dict(metadata or {}))

    def record_context_compaction(self, *, artifact_id: str, metadata: Mapping[str, Any] | None = None) -> None:
        self._append_stream_event(
            QueryStreamEventType.CONTEXT_COMPACTED,
            turn_id=self._active_turn.turn_id if self._active_turn else None,
            metadata={"artifact_id": artifact_id, **dict(metadata or {})},
        )

    def record_error(
        self,
        *,
        error: str,
        stop_reason: StopReason = StopReason.TOOL_ERROR,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self._active_turn is not None:
            self._active_turn.fail(error, stop_reason=stop_reason)
        if self._active_assistant_message is not None:
            self._active_assistant_message.fail(error)
        self._append_stream_event(
            QueryStreamEventType.ERROR,
            turn_id=self._active_turn.turn_id if self._active_turn else None,
            metadata={"error": error, "stop_reason": str(stop_reason), **dict(metadata or {})},
        )

    def record_continue(
        self,
        *,
        reason: StopReason = StopReason.CONTINUE_REQUESTED,
        error: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self._active_turn is not None:
            self._active_turn.request_continue(reason=reason, error=error)
        self._append_stream_event(
            QueryStreamEventType.CONTINUE,
            turn_id=self._active_turn.turn_id if self._active_turn else None,
            metadata={"reason": str(reason), "error": error, **dict(metadata or {})},
        )

    def end_turn(
        self,
        *,
        ok: bool,
        stop_reason: StopReason = StopReason.END_TURN,
        error: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self._active_turn is None:
            return
        if ok:
            self._active_turn.complete(stop_reason=stop_reason)
        else:
            self._active_turn.fail(error or str(stop_reason), stop_reason=stop_reason)
        self.complete_assistant_message(metadata={"final": True})
        payload = {
            "ok": ok,
            "stop_reason": str(stop_reason),
            "error": error,
            **dict(metadata or {}),
        }
        self._append_stream_event(QueryStreamEventType.TURN_COMPLETED, turn_id=self._active_turn.turn_id, metadata=payload)
        self._append_stream_event(QueryStreamEventType.TURN_END, turn_id=self._active_turn.turn_id, metadata=payload)
        self._active_turn = None

    def complete_session(
        self,
        *,
        ok: bool,
        stop_reason: StopReason = StopReason.SESSION_COMPLETED,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.status = "completed" if ok else "failed"
        self.complete_assistant_message(metadata={"session_final": True})
        self._append_stream_event(
            QueryStreamEventType.SESSION_COMPLETED,
            metadata={"ok": ok, "stop_reason": str(stop_reason), **dict(metadata or {})},
        )

    def snapshot(self, *, include_transcript: bool = True, metadata: Mapping[str, Any] | None = None) -> QuerySessionSnapshot:
        consistency = self.consistency_report()
        snapshot = QuerySessionSnapshot(
            session_id=self.session_id,
            worker_request_id=self.worker_request_id,
            run_id=self.run_id,
            task_id=self.task_id,
            node_id=self.node_id,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            leaf_uuid=self.leaf_uuid,
            sequence=self._sequence,
            resume_token=self.resume_token,
            messages=[message.to_dict() for message in self.messages],
            turns=[turn.to_dict() for turn in self.turns],
            transcript=[entry.to_dict() for entry in self.transcript] if include_transcript else [],
            stats=self.stats(),
            consistency=consistency,
            source_contract=to_jsonable(self.source_contract),
            metadata={**self.metadata, **dict(metadata or {})},
        )
        return snapshot

    def snapshot_payload(self, *, include_transcript: bool = True, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.snapshot(include_transcript=include_transcript, metadata=metadata).to_dict()

    def replay(self, *, after_sequence: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        selected = [entry.to_dict() for entry in self.transcript if entry.sequence > after_sequence]
        if limit is not None:
            selected = selected[: max(limit, 0)]
        return selected

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True) for entry in self.transcript) + "\n"

    def stats(self) -> dict[str, Any]:
        return {
            "message_count": len(self.messages),
            "turn_count": len(self.turns),
            "transcript_entry_count": len(self.transcript),
            "tool_message_count": sum(1 for item in self.messages if item.role == QueryMessageRole.TOOL),
            "assistant_message_count": sum(1 for item in self.messages if item.role == QueryMessageRole.ASSISTANT),
            "user_message_count": sum(1 for item in self.messages if item.role == QueryMessageRole.USER),
            "error_turn_count": sum(1 for item in self.turns if item.phase == TurnLifecyclePhase.FAILED),
            "continue_count": sum(item.continue_count for item in self.turns),
            "leaf_uuid": self.leaf_uuid,
            "sequence": self._sequence,
        }

    def chain_from_leaf(self, leaf_uuid: str | None = None) -> list[str]:
        leaf = leaf_uuid or self.leaf_uuid
        if leaf is None:
            return []
        by_id = {message.message_id: message for message in self.messages}
        chain: list[str] = []
        seen: set[str] = set()
        current = leaf
        while current and current not in seen:
            seen.add(current)
            chain.append(current)
            parent = by_id.get(current).parent_uuid if current in by_id else None
            current = parent
        chain.reverse()
        return chain

    def consistency_report(self) -> dict[str, Any]:
        message_ids = {message.message_id for message in self.messages}
        missing_parents = [
            message.parent_uuid
            for message in self.messages
            if message.parent_uuid is not None and message.parent_uuid not in message_ids
        ]
        chain = self.chain_from_leaf()
        turn_message_ids = {
            item
            for turn in self.turns
            for item in [turn.user_message_id, *turn.assistant_message_ids, *turn.tool_message_ids]
            if item
        }
        dangling_turn_messages = sorted(turn_message_ids - message_ids)
        return {
            "ok": not missing_parents and not dangling_turn_messages and (not self.messages or bool(chain)),
            "message_count": len(self.messages),
            "chain_length": len(chain),
            "missing_parent_count": len(missing_parents),
            "missing_parents": missing_parents,
            "dangling_turn_message_count": len(dangling_turn_messages),
            "dangling_turn_messages": dangling_turn_messages,
            "leaf_uuid": self.leaf_uuid,
        }

    def event_payload(
        self,
        phase: str,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = {
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "phase": phase,
            "turn_id": turn_id,
            "message_id": message_id,
            "sequence": self._sequence,
            "resume_token": self.resume_token,
            **dict(payload or {}),
        }
        return {"query_session": {key: value for key, value in data.items() if value is not None}}

    def _append_message(
        self,
        role: QueryMessageRole,
        *,
        content: str,
        turn_id: str | None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MessageLifecycle:
        message = MessageLifecycle(
            message_id=new_id("qmsg"),
            role=role,
            turn_id=turn_id,
            parent_uuid=self.leaf_uuid,
            content=content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            metadata=dict(metadata or {}),
        )
        self.messages.append(message)
        self.leaf_uuid = message.message_id
        self.updated_at = now_iso()
        self._append_transcript(
            TranscriptEntryType(role.value),
            uuid=message.message_id,
            parent_uuid=message.parent_uuid,
            turn_id=turn_id,
            role=role,
            content=content,
            metadata={**message.metadata, "message_phase": str(message.phase)},
        )
        return message

    def _append_stream_event(
        self,
        event_type: QueryStreamEventType,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionTranscriptEntry:
        payload = dict(metadata or {})
        if message_id is not None:
            payload["message_id"] = message_id
        return self._append_transcript(
            TranscriptEntryType.STREAM_EVENT,
            uuid=new_id("qevent"),
            parent_uuid=self.leaf_uuid,
            turn_id=turn_id,
            event_type=event_type,
            metadata=payload,
        )

    def _append_transcript(
        self,
        entry_type: TranscriptEntryType,
        *,
        uuid: str,
        parent_uuid: str | None,
        turn_id: str | None = None,
        role: QueryMessageRole | None = None,
        content: str = "",
        event_type: QueryStreamEventType | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionTranscriptEntry:
        self._sequence += 1
        entry = SessionTranscriptEntry(
            sequence=self._sequence,
            entry_type=entry_type,
            uuid=uuid,
            parent_uuid=parent_uuid,
            turn_id=turn_id,
            role=role,
            content=content,
            event_type=event_type,
            metadata=dict(metadata or {}),
        )
        self.transcript.append(entry)
        self.updated_at = entry.created_at
        return entry


def snapshot_checkpoint_metadata(snapshot: QuerySessionSnapshot | Mapping[str, Any]) -> dict[str, str]:
    payload = snapshot.to_dict() if isinstance(snapshot, QuerySessionSnapshot) else dict(snapshot)
    stats = _as_mapping(payload.get("stats"))
    consistency = _as_mapping(payload.get("consistency"))
    return {
        "query_session_id": str(payload.get("session_id") or ""),
        "query_session_resume_token": str(payload.get("resume_token") or ""),
        "query_session_status": str(payload.get("status") or ""),
        "query_session_turns": str(stats.get("turn_count") or 0),
        "query_session_messages": str(stats.get("message_count") or 0),
        "query_session_transcript_entries": str(stats.get("transcript_entry_count") or 0),
        "query_session_consistent": str(consistency.get("ok") is True).lower(),
        "query_session_leaf_uuid": str(payload.get("leaf_uuid") or ""),
    }


def transcript_from_snapshot(snapshot: Mapping[str, Any]) -> list[SessionTranscriptEntry]:
    return [SessionTranscriptEntry.from_dict(_as_mapping(item)) for item in _as_list(snapshot.get("transcript"))]


def replay_from_snapshot(snapshot: Mapping[str, Any], *, after_sequence: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    entries = [entry.to_dict() for entry in transcript_from_snapshot(snapshot) if entry.sequence > after_sequence]
    if limit is not None:
        entries = entries[: max(limit, 0)]
    return entries


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default


def _optional_enum(enum_type: type[StrEnum], value: Any) -> Any:
    if value is None:
        return None
    try:
        return enum_type(str(value))
    except ValueError:
        return None


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
