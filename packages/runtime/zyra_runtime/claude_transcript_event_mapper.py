from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


class TranscriptMappingStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    EMPTY = "empty"


class TranscriptMappingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class TranscriptMappingSurface(StrEnum):
    SESSION_METADATA = "session_metadata"
    MESSAGE_CHAIN = "message_chain"
    TURN_CHAIN = "turn_chain"
    STREAM_EVENT = "stream_event"
    TOOL_CALL = "tool_call"
    ARTIFACT = "artifact"
    RESUME = "resume"


class TranscriptEventPhase(StrEnum):
    SESSION_STARTED = "session_started"
    QUERY_SESSION_SEED_ATTACHED = "query_session_seed_attached"
    CONTEXT_SNAPSHOT_ATTACHED = "context_snapshot_attached"
    TURN_STARTED = "turn_started"
    TURN_START = "turn_start"
    STREAM_REQUEST_START = "stream_request_start"
    MESSAGE_CREATED = "message_created"
    MESSAGE_DELTA = "message_delta"
    MESSAGE_COMPLETED = "message_completed"
    TOOL_LOOP_PLAN = "tool_loop_plan"
    TOOL_BATCH_STARTED = "tool_batch_started"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_USE_SUMMARY = "tool_use_summary"
    TOOL_BATCH_COMPLETED = "tool_batch_completed"
    CONTEXT_COMPACTED = "context_compacted"
    CONTINUE = "continue"
    ERROR = "error"
    TURN_COMPLETED = "turn_completed"
    TURN_END = "turn_end"
    QUERY_SESSION_SNAPSHOT = "query_session_snapshot"
    SESSION_FOUNDATION_AUDIT = "session_foundation_audit"
    SESSION_COMPLETED = "session_completed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TranscriptMappingFinding:
    code: str
    severity: TranscriptMappingSeverity
    surface: TranscriptMappingSurface
    message: str
    sequence: int = 0
    uuid: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == TranscriptMappingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "sequence": self.sequence,
            "uuid": self.uuid,
            "blocking": self.blocking,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TranscriptEntryProjection:
    sequence: int
    entry_type: str
    uuid: str
    parent_uuid: str
    turn_id: str
    role: str
    content_chars: int
    event_type: str
    created_at: str
    phase: TranscriptEventPhase
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def is_message(self) -> bool:
        return self.entry_type in {"user", "assistant", "tool", "system"}

    @property
    def is_stream_event(self) -> bool:
        return self.entry_type == "stream_event"

    def to_event_record(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": session_id,
                    "worker_request_id": worker_request_id,
                    "phase": str(self.phase),
                    "sequence": self.sequence,
                    "uuid": self.uuid,
                    "parent_uuid": self.parent_uuid,
                    "turn_id": self.turn_id,
                    "role": self.role,
                    "entry_type": self.entry_type,
                    "content_chars": self.content_chars,
                    **self.payload,
                }
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "entry_type": self.entry_type,
            "uuid": self.uuid,
            "parent_uuid": self.parent_uuid,
            "turn_id": self.turn_id,
            "role": self.role,
            "content_chars": self.content_chars,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "phase": str(self.phase),
            "is_message": self.is_message,
            "is_stream_event": self.is_stream_event,
            "payload": to_jsonable(self.payload),
        }


@dataclass(frozen=True, slots=True)
class TranscriptMessageProjection:
    message_id: str
    role: str
    turn_id: str
    parent_uuid: str
    phase: str
    content_chars: int
    tool_call_id: str = ""
    tool_name: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.phase in {"completed", "compacted", "superseded"}

    @property
    def failed(self) -> bool:
        return self.phase == "failed" or bool(self.error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "turn_id": self.turn_id,
            "parent_uuid": self.parent_uuid,
            "phase": self.phase,
            "content_chars": self.content_chars,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "error": self.error,
            "completed": self.completed,
            "failed": self.failed,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TranscriptTurnProjection:
    turn_id: str
    turn_index: int
    phase: str
    user_message_id: str
    assistant_message_ids: tuple[str, ...]
    tool_message_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    stop_reason: str = ""
    error: str = ""
    continue_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.phase == "completed"

    @property
    def failed(self) -> bool:
        return self.phase == "failed" or bool(self.error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "turn_index": self.turn_index,
            "phase": self.phase,
            "user_message_id": self.user_message_id,
            "assistant_message_ids": list(self.assistant_message_ids),
            "tool_message_ids": list(self.tool_message_ids),
            "tool_call_ids": list(self.tool_call_ids),
            "stop_reason": self.stop_reason,
            "error": self.error,
            "continue_count": self.continue_count,
            "completed": self.completed,
            "failed": self.failed,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TranscriptToolProjection:
    tool_call_id: str
    tool_name: str
    started_sequence: int
    completed_sequence: int = 0
    ok: bool = False
    error: str = ""
    turn_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.completed_sequence > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "started_sequence": self.started_sequence,
            "completed_sequence": self.completed_sequence,
            "completed": self.completed,
            "ok": self.ok,
            "error": self.error,
            "turn_id": self.turn_id,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TranscriptChainReport:
    ok: bool
    leaf_uuid: str
    chain_length: int
    message_count: int
    missing_parent_count: int
    duplicate_uuid_count: int
    dangling_turn_message_count: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "leaf_uuid": self.leaf_uuid,
            "chain_length": self.chain_length,
            "message_count": self.message_count,
            "missing_parent_count": self.missing_parent_count,
            "duplicate_uuid_count": self.duplicate_uuid_count,
            "dangling_turn_message_count": self.dangling_turn_message_count,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TranscriptEventMappingReport:
    mapping_id: str
    status: TranscriptMappingStatus
    session_id: str
    worker_request_id: str
    run_id: str
    task_id: str
    node_id: str
    entries: tuple[TranscriptEntryProjection, ...]
    messages: tuple[TranscriptMessageProjection, ...]
    turns: tuple[TranscriptTurnProjection, ...]
    tools: tuple[TranscriptToolProjection, ...]
    chain_report: TranscriptChainReport
    findings: tuple[TranscriptMappingFinding, ...] = ()
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {TranscriptMappingStatus.READY, TranscriptMappingStatus.DEGRADED} and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == TranscriptMappingSeverity.WARNING)

    @property
    def phase_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[str(entry.phase)] = counts.get(str(entry.phase), 0) + 1
        return counts

    def projected_events(self) -> list[EventRecord]:
        return [
            entry.to_event_record(
                run_id=self.run_id,
                task_id=self.task_id,
                node_id=self.node_id or None,
                session_id=self.session_id,
                worker_request_id=self.worker_request_id,
            )
            for entry in self.entries
        ]

    def metadata_values(self) -> dict[str, str]:
        return {
            "transcript_mapping_id": self.mapping_id,
            "transcript_mapping_ok": str(self.ok).lower(),
            "transcript_mapping_status": str(self.status),
            "transcript_mapping_entries": str(len(self.entries)),
            "transcript_mapping_messages": str(len(self.messages)),
            "transcript_mapping_turns": str(len(self.turns)),
            "transcript_mapping_tools": str(len(self.tools)),
            "transcript_mapping_blockers": str(self.blocker_count),
            "transcript_mapping_warnings": str(self.warning_count),
            "transcript_mapping_chain_ok": str(self.chain_report.ok).lower(),
            "transcript_mapping_phase_count": str(len(self.phase_counts)),
        }

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        data = {
            "mapping_id": self.mapping_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "entries": [entry.to_dict() for entry in self.entries],
            "messages": [message.to_dict() for message in self.messages],
            "turns": [turn.to_dict() for turn in self.turns],
            "tools": [tool.to_dict() for tool in self.tools],
            "chain_report": self.chain_report.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
            "phase_counts": self.phase_counts,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }
        if include_events:
            data["projected_events"] = [to_jsonable(event) for event in self.projected_events()]
        return data


class TranscriptEventMapper:
    """Projects QuerySession snapshots into auditable lifecycle event state."""

    def map_snapshot(self, snapshot: Mapping[str, Any]) -> TranscriptEventMappingReport:
        entries = tuple(project_transcript_entry(entry) for entry in _as_list(snapshot.get("transcript")))
        messages = tuple(project_message(message) for message in _as_list(snapshot.get("messages")))
        turns = tuple(project_turn(turn) for turn in _as_list(snapshot.get("turns")))
        tools = tuple(project_tools(entries))
        chain_report = build_chain_report(snapshot=snapshot, messages=messages, turns=turns)
        findings = list(validate_entry_sequences(entries))
        findings.extend(validate_message_chain(messages))
        findings.extend(validate_turn_links(turns=turns, messages=messages))
        findings.extend(validate_tool_pairs(tools))
        findings.extend(validate_required_phases(entries))
        status = mapping_status(entries=entries, chain_report=chain_report, findings=findings)
        return TranscriptEventMappingReport(
            mapping_id=new_id("tmap"),
            status=status,
            session_id=str(snapshot.get("session_id") or ""),
            worker_request_id=str(snapshot.get("worker_request_id") or ""),
            run_id=str(snapshot.get("run_id") or ""),
            task_id=str(snapshot.get("task_id") or ""),
            node_id=str(snapshot.get("node_id") or ""),
            entries=entries,
            messages=messages,
            turns=turns,
            tools=tools,
            chain_report=chain_report,
            findings=tuple(findings),
            metadata={
                "source_path": "src/QueryEngine.ts",
                "target_path": "packages/runtime/zyra_runtime/claude_transcript_event_mapper.py",
                "owner_unit": "M1-02B",
            },
        )

    def event_for_report(self, report: TranscriptEventMappingReport) -> EventRecord:
        return EventRecord(
            run_id=report.run_id,
            task_id=report.task_id,
            node_id=report.node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": "transcript_event_mapping",
                    "mapping_id": report.mapping_id,
                    "ok": report.ok,
                    "status": str(report.status),
                    "entry_count": len(report.entries),
                    "message_count": len(report.messages),
                    "turn_count": len(report.turns),
                    "tool_count": len(report.tools),
                    "chain_ok": report.chain_report.ok,
                    "blocker_count": report.blocker_count,
                    "warning_count": report.warning_count,
                }
            },
        )


def project_transcript_entry(entry: Mapping[str, Any]) -> TranscriptEntryProjection:
    metadata = entry.get("metadata") if isinstance(entry.get("metadata"), Mapping) else {}
    event_type = str(entry.get("event_type") or "")
    phase = phase_from_entry(entry, metadata)
    content = str(entry.get("content") or "")
    return TranscriptEntryProjection(
        sequence=_safe_int(entry.get("sequence"), default=0),
        entry_type=str(entry.get("type") or ""),
        uuid=str(entry.get("uuid") or ""),
        parent_uuid=str(entry.get("parent_uuid") or ""),
        turn_id=str(entry.get("turn_id") or ""),
        role=str(entry.get("role") or ""),
        content_chars=len(content),
        event_type=event_type,
        created_at=str(entry.get("created_at") or ""),
        phase=phase,
        payload=dict(metadata),
    )


def phase_from_entry(entry: Mapping[str, Any], metadata: Mapping[str, Any]) -> TranscriptEventPhase:
    event_type = str(entry.get("event_type") or metadata.get("phase") or "")
    if event_type:
        try:
            return TranscriptEventPhase(event_type)
        except ValueError:
            return TranscriptEventPhase.UNKNOWN
    entry_type = str(entry.get("type") or "")
    if entry_type == "session_metadata":
        return TranscriptEventPhase.SESSION_STARTED
    if entry_type in {"user", "assistant", "tool", "system"}:
        return TranscriptEventPhase.MESSAGE_DELTA
    return TranscriptEventPhase.UNKNOWN


def project_message(message: Mapping[str, Any]) -> TranscriptMessageProjection:
    return TranscriptMessageProjection(
        message_id=str(message.get("message_id") or ""),
        role=str(message.get("role") or ""),
        turn_id=str(message.get("turn_id") or ""),
        parent_uuid=str(message.get("parent_uuid") or ""),
        phase=str(message.get("phase") or ""),
        content_chars=len(str(message.get("content") or "")),
        tool_call_id=str(message.get("tool_call_id") or ""),
        tool_name=str(message.get("tool_name") or ""),
        error=str(message.get("error") or ""),
        metadata=dict(message.get("metadata") or {}),
    )


def project_turn(turn: Mapping[str, Any]) -> TranscriptTurnProjection:
    return TranscriptTurnProjection(
        turn_id=str(turn.get("turn_id") or ""),
        turn_index=_safe_int(turn.get("turn_index"), default=0),
        phase=str(turn.get("phase") or ""),
        user_message_id=str(turn.get("user_message_id") or ""),
        assistant_message_ids=tuple(str(item) for item in _as_list(turn.get("assistant_message_ids"))),
        tool_message_ids=tuple(str(item) for item in _as_list(turn.get("tool_message_ids"))),
        tool_call_ids=tuple(str(item) for item in _as_list(turn.get("tool_call_ids"))),
        stop_reason=str(turn.get("stop_reason") or ""),
        error=str(turn.get("error") or ""),
        continue_count=_safe_int(turn.get("continue_count"), default=0),
        metadata=dict(turn.get("metadata") or {}),
    )


def project_tools(entries: Sequence[TranscriptEntryProjection]) -> Iterable[TranscriptToolProjection]:
    started: dict[str, TranscriptToolProjection] = {}
    completed: dict[str, TranscriptToolProjection] = {}
    for entry in entries:
        tool_call_id = str(entry.payload.get("tool_call_id") or "")
        if not tool_call_id:
            continue
        tool_name = str(entry.payload.get("tool_name") or "")
        if entry.phase == TranscriptEventPhase.TOOL_CALL_STARTED:
            started[tool_call_id] = TranscriptToolProjection(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                started_sequence=entry.sequence,
                turn_id=entry.turn_id,
                metadata=entry.payload,
            )
        elif entry.phase == TranscriptEventPhase.TOOL_CALL_COMPLETED:
            base = started.get(tool_call_id)
            completed[tool_call_id] = TranscriptToolProjection(
                tool_call_id=tool_call_id,
                tool_name=tool_name or (base.tool_name if base else ""),
                started_sequence=base.started_sequence if base else 0,
                completed_sequence=entry.sequence,
                ok=entry.payload.get("ok") is True,
                error=str(entry.payload.get("error") or ""),
                turn_id=entry.turn_id or (base.turn_id if base else ""),
                metadata={**(base.metadata if base else {}), **entry.payload},
            )
    yielded = set()
    for tool_call_id, tool in completed.items():
        yielded.add(tool_call_id)
        yield tool
    for tool_call_id, tool in started.items():
        if tool_call_id not in yielded:
            yield tool


def build_chain_report(
    *,
    snapshot: Mapping[str, Any],
    messages: Sequence[TranscriptMessageProjection],
    turns: Sequence[TranscriptTurnProjection],
) -> TranscriptChainReport:
    ids = [message.message_id for message in messages if message.message_id]
    id_set = set(ids)
    duplicate_count = len(ids) - len(id_set)
    missing_parents = [
        message.parent_uuid
        for message in messages
        if message.parent_uuid and message.parent_uuid not in id_set
    ]
    turn_message_ids = {
        message_id
        for turn in turns
        for message_id in [turn.user_message_id, *turn.assistant_message_ids, *turn.tool_message_ids]
        if message_id
    }
    dangling = turn_message_ids - id_set
    leaf_uuid = str(snapshot.get("leaf_uuid") or "")
    chain = chain_from_messages(messages, leaf_uuid=leaf_uuid)
    ok = not duplicate_count and not missing_parents and not dangling and (not messages or bool(chain))
    return TranscriptChainReport(
        ok=ok,
        leaf_uuid=leaf_uuid,
        chain_length=len(chain),
        message_count=len(messages),
        missing_parent_count=len(missing_parents),
        duplicate_uuid_count=duplicate_count,
        dangling_turn_message_count=len(dangling),
        metadata={
            "missing_parents": missing_parents,
            "dangling_turn_messages": sorted(dangling),
        },
    )


def chain_from_messages(messages: Sequence[TranscriptMessageProjection], *, leaf_uuid: str) -> list[str]:
    by_id = {message.message_id: message for message in messages}
    if not leaf_uuid:
        return []
    chain: list[str] = []
    seen: set[str] = set()
    current = leaf_uuid
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        current = by_id[current].parent_uuid if current in by_id else ""
    chain.reverse()
    return chain


def validate_entry_sequences(entries: Sequence[TranscriptEntryProjection]) -> list[TranscriptMappingFinding]:
    findings: list[TranscriptMappingFinding] = []
    if not entries:
        findings.append(
            TranscriptMappingFinding(
                code="transcript_empty",
                severity=TranscriptMappingSeverity.BLOCKER,
                surface=TranscriptMappingSurface.STREAM_EVENT,
                message="Transcript does not contain entries.",
            )
        )
        return findings
    sequences = [entry.sequence for entry in entries]
    expected = list(range(min(sequences), max(sequences) + 1))
    if sequences != expected:
        findings.append(
            TranscriptMappingFinding(
                code="transcript_sequence_gap",
                severity=TranscriptMappingSeverity.BLOCKER,
                surface=TranscriptMappingSurface.STREAM_EVENT,
                message="Transcript entry sequence is not contiguous.",
                metadata={"sequences": sequences, "expected": expected},
            )
        )
    if entries[0].phase != TranscriptEventPhase.SESSION_STARTED:
        findings.append(
            TranscriptMappingFinding(
                code="transcript_missing_session_start",
                severity=TranscriptMappingSeverity.WARNING,
                surface=TranscriptMappingSurface.SESSION_METADATA,
                message="First transcript projection is not a session start.",
                sequence=entries[0].sequence,
                uuid=entries[0].uuid,
            )
        )
    return findings


def validate_message_chain(messages: Sequence[TranscriptMessageProjection]) -> list[TranscriptMappingFinding]:
    findings: list[TranscriptMappingFinding] = []
    ids = {message.message_id for message in messages if message.message_id}
    for message in messages:
        if message.parent_uuid and message.parent_uuid not in ids:
            findings.append(
                TranscriptMappingFinding(
                    code="message_missing_parent",
                    severity=TranscriptMappingSeverity.BLOCKER,
                    surface=TranscriptMappingSurface.MESSAGE_CHAIN,
                    message="Message parent uuid is not present in message set.",
                    uuid=message.message_id,
                    metadata={"parent_uuid": message.parent_uuid},
                )
            )
        if message.role == "assistant" and not message.completed and not message.failed:
            findings.append(
                TranscriptMappingFinding(
                    code="assistant_message_incomplete",
                    severity=TranscriptMappingSeverity.WARNING,
                    surface=TranscriptMappingSurface.MESSAGE_CHAIN,
                    message="Assistant message is neither completed nor failed.",
                    uuid=message.message_id,
                    metadata={"phase": message.phase},
                )
            )
    return findings


def validate_turn_links(
    *,
    turns: Sequence[TranscriptTurnProjection],
    messages: Sequence[TranscriptMessageProjection],
) -> list[TranscriptMappingFinding]:
    findings: list[TranscriptMappingFinding] = []
    message_ids = {message.message_id for message in messages}
    for turn in turns:
        linked = [turn.user_message_id, *turn.assistant_message_ids, *turn.tool_message_ids]
        missing = [message_id for message_id in linked if message_id and message_id not in message_ids]
        if missing:
            findings.append(
                TranscriptMappingFinding(
                    code="turn_links_missing_messages",
                    severity=TranscriptMappingSeverity.BLOCKER,
                    surface=TranscriptMappingSurface.TURN_CHAIN,
                    message="Turn references messages not present in snapshot.",
                    metadata={"turn_id": turn.turn_id, "missing": missing},
                )
            )
        if not turn.completed and not turn.failed:
            findings.append(
                TranscriptMappingFinding(
                    code="turn_incomplete",
                    severity=TranscriptMappingSeverity.WARNING,
                    surface=TranscriptMappingSurface.TURN_CHAIN,
                    message="Turn is neither completed nor failed.",
                    metadata={"turn_id": turn.turn_id, "phase": turn.phase},
                )
            )
    return findings


def validate_tool_pairs(tools: Sequence[TranscriptToolProjection]) -> list[TranscriptMappingFinding]:
    findings: list[TranscriptMappingFinding] = []
    for tool in tools:
        if not tool.completed:
            findings.append(
                TranscriptMappingFinding(
                    code="tool_call_missing_completion",
                    severity=TranscriptMappingSeverity.WARNING,
                    surface=TranscriptMappingSurface.TOOL_CALL,
                    message="Tool call was started but no completion event was projected.",
                    metadata={"tool_call_id": tool.tool_call_id, "tool_name": tool.tool_name},
                )
            )
    return findings


def validate_required_phases(entries: Sequence[TranscriptEntryProjection]) -> list[TranscriptMappingFinding]:
    phases = {entry.phase for entry in entries}
    findings: list[TranscriptMappingFinding] = []
    for required in (
        TranscriptEventPhase.SESSION_STARTED,
        TranscriptEventPhase.TURN_STARTED,
        TranscriptEventPhase.STREAM_REQUEST_START,
        TranscriptEventPhase.SESSION_COMPLETED,
    ):
        if required not in phases:
            findings.append(
                TranscriptMappingFinding(
                    code=f"missing_phase_{required}",
                    severity=TranscriptMappingSeverity.WARNING,
                    surface=TranscriptMappingSurface.STREAM_EVENT,
                    message=f"Transcript projection does not contain required phase {required}.",
                )
            )
    return findings


def mapping_status(
    *,
    entries: Sequence[TranscriptEntryProjection],
    chain_report: TranscriptChainReport,
    findings: Sequence[TranscriptMappingFinding],
) -> TranscriptMappingStatus:
    if not entries:
        return TranscriptMappingStatus.EMPTY
    if any(finding.blocking for finding in findings) or not chain_report.ok:
        return TranscriptMappingStatus.BLOCKED
    if any(finding.severity == TranscriptMappingSeverity.WARNING for finding in findings):
        return TranscriptMappingStatus.DEGRADED
    return TranscriptMappingStatus.READY


def transcript_mapping_metadata(report: TranscriptEventMappingReport | None) -> dict[str, str]:
    if report is None:
        return {
            "transcript_mapping_id": "",
            "transcript_mapping_ok": "",
            "transcript_mapping_status": "",
        }
    return report.metadata_values()


def render_transcript_mapping_markdown(report: TranscriptEventMappingReport) -> str:
    lines = [
        "# Transcript Event Mapping",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- mapping_id: `{report.mapping_id}`",
        f"- session_id: `{report.session_id}`",
        f"- worker_request_id: `{report.worker_request_id}`",
        f"- entries: `{len(report.entries)}`",
        f"- messages: `{len(report.messages)}`",
        f"- turns: `{len(report.turns)}`",
        f"- tools: `{len(report.tools)}`",
        f"- chain_ok: `{str(report.chain_report.ok).lower()}`",
        "",
        "## Findings",
        "",
    ]
    if report.findings:
        for finding in report.findings:
            lines.append(f"- `{finding.severity}` `{finding.surface}` `{finding.code}` {finding.message}")
    else:
        lines.append("- none")
    lines.extend(["", "## Phase Counts", ""])
    for phase, count in sorted(report.phase_counts.items()):
        lines.append(f"- `{phase}`: `{count}`")
    return "\n".join(lines) + "\n"


def mapping_report_from_payload(payload: Mapping[str, Any]) -> TranscriptEventMappingReport:
    entries = tuple(project_transcript_entry(item) for item in _as_list(payload.get("entries")) if isinstance(item, Mapping))
    messages = tuple(project_message(item) for item in _as_list(payload.get("messages")) if isinstance(item, Mapping))
    turns = tuple(project_turn(item) for item in _as_list(payload.get("turns")) if isinstance(item, Mapping))
    chain_report = TranscriptChainReport(
        ok=bool(_nested(payload, "chain_report", "ok", default=False)),
        leaf_uuid=str(_nested(payload, "chain_report", "leaf_uuid", default="")),
        chain_length=_safe_int(_nested(payload, "chain_report", "chain_length", default=0), default=0),
        message_count=_safe_int(_nested(payload, "chain_report", "message_count", default=0), default=0),
        missing_parent_count=_safe_int(_nested(payload, "chain_report", "missing_parent_count", default=0), default=0),
        duplicate_uuid_count=_safe_int(_nested(payload, "chain_report", "duplicate_uuid_count", default=0), default=0),
        dangling_turn_message_count=_safe_int(_nested(payload, "chain_report", "dangling_turn_message_count", default=0), default=0),
    )
    return TranscriptEventMappingReport(
        mapping_id=str(payload.get("mapping_id") or new_id("tmap")),
        status=_enum_or_default(TranscriptMappingStatus, payload.get("status"), TranscriptMappingStatus.BLOCKED),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        run_id=str(payload.get("run_id") or ""),
        task_id=str(payload.get("task_id") or ""),
        node_id=str(payload.get("node_id") or ""),
        entries=entries,
        messages=messages,
        turns=turns,
        tools=tuple(project_tools(entries)),
        chain_report=chain_report,
        findings=(),
        created_at=str(payload.get("created_at") or now_iso()),
        metadata=dict(payload.get("metadata") or {}),
    )


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _nested(payload: Mapping[str, Any], *keys: str, default: Any) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return default
        current = current.get(key)
    return default if current is None else current


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default
