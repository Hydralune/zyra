from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .claude_input_processor import QueryInputKind
from .claude_session_store import (
    CodeWorkerSessionStore,
    CodeWorkerSessionStoreRecord,
    CodeWorkerSessionStoreRecordType,
)


class SessionReplayStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    EMPTY = "empty"


class SessionReplayFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class SessionReplaySourceKind(StrEnum):
    STORE_RECORD = "store_record"
    INPUT_RECORD = "input_record"
    CONTEXT_SNAPSHOT = "context_snapshot"
    QUERY_ENGINE_ATTACH = "query_engine_attach"
    SYNTHETIC_CURSOR = "synthetic_cursor"


class SessionReplayAction(StrEnum):
    RESTORE_CONTEXT = "restore_context"
    RESTORE_INPUT = "restore_input"
    REATTACH_QUERY_ENGINE = "reattach_query_engine"
    REPLAY_EVENTS = "replay_events"
    BLOCK = "block"


class SessionReplayGapKind(StrEnum):
    NONE = "none"
    MISSING_SEED = "missing_seed"
    MISSING_INPUT = "missing_input"
    MISSING_CONTEXT = "missing_context"
    SEQUENCE_GAP = "sequence_gap"
    SESSION_MISMATCH = "session_mismatch"
    REQUEST_MISMATCH = "request_mismatch"
    STORE_READ_FAILED = "store_read_failed"


@dataclass(frozen=True, slots=True)
class SessionReplayCursor:
    session_id: str
    worker_request_id: str
    sequence: int
    record_id: str = ""
    resume_token: str = ""
    created_at: str = field(default_factory=now_iso)
    source_kind: SessionReplaySourceKind = SessionReplaySourceKind.SYNTHETIC_CURSOR
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.session_id and not self.worker_request_id and self.sequence <= 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "sequence": self.sequence,
            "record_id": self.record_id,
            "resume_token": self.resume_token,
            "created_at": self.created_at,
            "source_kind": str(self.source_kind),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionReplayFinding:
    code: str
    severity: SessionReplayFindingSeverity
    gap_kind: SessionReplayGapKind
    message: str
    sequence: int = 0
    record_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == SessionReplayFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "gap_kind": str(self.gap_kind),
            "message": self.message,
            "sequence": self.sequence,
            "record_id": self.record_id,
            "blocking": self.blocking,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionReplayRecordProjection:
    sequence: int
    record_id: str
    record_type: CodeWorkerSessionStoreRecordType
    source_kind: SessionReplaySourceKind
    session_id: str
    worker_request_id: str
    created_at: str
    payload_summary: dict[str, Any]
    payload_chars: int
    accepted: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_cursor(self) -> SessionReplayCursor:
        resume_token = str(self.payload_summary.get("resume_token") or "")
        return SessionReplayCursor(
            session_id=self.session_id,
            worker_request_id=self.worker_request_id,
            sequence=self.sequence,
            record_id=self.record_id,
            resume_token=resume_token,
            created_at=self.created_at,
            source_kind=self.source_kind,
            metadata={"record_type": str(self.record_type), **self.metadata},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "record_id": self.record_id,
            "record_type": str(self.record_type),
            "source_kind": str(self.source_kind),
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "created_at": self.created_at,
            "payload_summary": to_jsonable(self.payload_summary),
            "payload_chars": self.payload_chars,
            "accepted": self.accepted,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionReplayInputState:
    input_id: str
    kind: str
    disposition: str
    text: str
    accepted: bool
    command_name: str = ""
    risk: str = ""
    sequence: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)

    def to_message(self) -> dict[str, Any]:
        return {
            "role": "user",
            "content": self.text,
            "metadata": {
                "input_id": self.input_id,
                "kind": self.kind,
                "disposition": self.disposition,
                "command_name": self.command_name,
                "risk": self.risk,
                "replayed": "true",
                **{str(k): str(v) for k, v in self.metadata.items() if isinstance(v, (str, int, float, bool))},
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_id": self.input_id,
            "kind": self.kind,
            "disposition": self.disposition,
            "text": self.text,
            "accepted": self.accepted,
            "command_name": self.command_name,
            "risk": self.risk,
            "sequence": self.sequence,
            "chars": self.chars,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionReplayContextState:
    snapshot_id: str
    fingerprint: str
    status: str
    selected_block_count: int
    dropped_block_count: int
    active_chars: int
    messages: tuple[dict[str, Any], ...] = ()
    raw_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.snapshot_id) and self.status not in {"blocked", ""}

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        data = {
            "snapshot_id": self.snapshot_id,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "selected_block_count": self.selected_block_count,
            "dropped_block_count": self.dropped_block_count,
            "active_chars": self.active_chars,
            "ok": self.ok,
            "messages": [to_jsonable(message) for message in self.messages],
        }
        if include_raw:
            data["raw_snapshot"] = to_jsonable(self.raw_snapshot)
        return data


@dataclass(frozen=True, slots=True)
class SessionReplayAttachState:
    query_session_id: str
    resume_token: str
    attached_at: str
    sequence: int
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.query_session_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_session_id": self.query_session_id,
            "resume_token": self.resume_token,
            "attached_at": self.attached_at,
            "sequence": self.sequence,
            "ok": self.ok,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionReplayWindow:
    start_sequence: int
    end_sequence: int
    requested_after_sequence: int = 0
    limit: int | None = None
    projections: tuple[SessionReplayRecordProjection, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.projections

    @property
    def count(self) -> int:
        return len(self.projections)

    def cursors(self) -> list[SessionReplayCursor]:
        return [projection.to_cursor() for projection in self.projections]

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "requested_after_sequence": self.requested_after_sequence,
            "limit": self.limit,
            "count": self.count,
            "empty": self.empty,
            "projections": [projection.to_dict() for projection in self.projections],
            "cursors": [cursor.to_dict() for cursor in self.cursors()],
        }


@dataclass(frozen=True, slots=True)
class SessionReplayPlan:
    plan_id: str
    status: SessionReplayStatus
    session_id: str
    worker_request_id: str
    actions: tuple[SessionReplayAction, ...]
    input_states: tuple[SessionReplayInputState, ...]
    context_state: SessionReplayContextState | None
    attach_state: SessionReplayAttachState | None
    window: SessionReplayWindow
    findings: tuple[SessionReplayFinding, ...] = ()
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {SessionReplayStatus.READY, SessionReplayStatus.DEGRADED} and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == SessionReplayFindingSeverity.WARNING)

    @property
    def replay_messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if self.context_state is not None:
            messages.extend(self.context_state.messages)
        existing_input_ids = {
            str(message.get("metadata", {}).get("input_id"))
            for message in messages
            if isinstance(message.get("metadata"), Mapping)
        }
        for state in self.input_states:
            if state.accepted and state.input_id not in existing_input_ids:
                messages.append(state.to_message())
        return messages

    def metadata_values(self) -> dict[str, str]:
        return {
            "session_replay_plan_id": self.plan_id,
            "session_replay_ok": str(self.ok).lower(),
            "session_replay_status": str(self.status),
            "session_replay_actions": ",".join(str(action) for action in self.actions),
            "session_replay_input_count": str(len(self.input_states)),
            "session_replay_message_count": str(len(self.replay_messages)),
            "session_replay_context_ready": str(self.context_state.ok if self.context_state else False).lower(),
            "session_replay_attach_ready": str(self.attach_state.ok if self.attach_state else False).lower(),
            "session_replay_window_count": str(self.window.count),
            "session_replay_blockers": str(self.blocker_count),
            "session_replay_warnings": str(self.warning_count),
        }

    def to_dict(self, *, include_raw_context: bool = False) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "actions": [str(action) for action in self.actions],
            "input_states": [state.to_dict() for state in self.input_states],
            "context_state": self.context_state.to_dict(include_raw=include_raw_context) if self.context_state else None,
            "attach_state": self.attach_state.to_dict() if self.attach_state else None,
            "window": self.window.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
            "replay_messages": [to_jsonable(message) for message in self.replay_messages],
        }


class CodeWorkerSessionReplayRuntime:
    """Builds resume/replay plans from Zyra's CodeWorkerSessionStore."""

    def __init__(self, store: CodeWorkerSessionStore) -> None:
        self.store = store

    def build_plan(
        self,
        *,
        session_id: str = "",
        worker_request_id: str = "",
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> SessionReplayPlan:
        records, path, read_error = self._load_records(session_id=session_id, worker_request_id=worker_request_id)
        findings = list(self._initial_findings(records, read_error=read_error))
        projections = [project_store_record(record) for record in records]
        window = select_replay_window(projections, after_sequence=after_sequence, limit=limit)
        seed_session_id = session_id or _first_non_empty(record.session_id for record in records)
        seed_worker_request_id = worker_request_id or _first_non_empty(record.worker_request_id for record in records)
        findings.extend(validate_replay_identity(records, session_id=seed_session_id, worker_request_id=seed_worker_request_id))
        findings.extend(validate_replay_sequences(records))
        input_states = tuple(input_state_from_record(record) for record in records if _is_input_record(record))
        context_state = context_state_from_records(records)
        attach_state = attach_state_from_records(records)
        findings.extend(validate_replay_components(input_states=input_states, context_state=context_state, attach_state=attach_state))
        actions = replay_actions(input_states=input_states, context_state=context_state, attach_state=attach_state, findings=findings)
        status = replay_status(records=records, findings=findings, context_state=context_state)
        return SessionReplayPlan(
            plan_id=new_id("replayplan"),
            status=status,
            session_id=seed_session_id,
            worker_request_id=seed_worker_request_id,
            actions=tuple(actions),
            input_states=input_states,
            context_state=context_state,
            attach_state=attach_state,
            window=window,
            findings=tuple(findings),
            metadata={
                "store_root": str(self.store.root),
                "store_path": path,
                "after_sequence": after_sequence,
                "limit": limit,
            },
        )

    def build_plan_from_constraints(self, constraints: Mapping[str, Any]) -> SessionReplayPlan | None:
        session_id = str(
            constraints.get("resume_session_id")
            or constraints.get("resume_code_worker_session_id")
            or constraints.get("session_id")
            or ""
        )
        worker_request_id = str(
            constraints.get("resume_worker_request_id")
            or constraints.get("worker_request_id")
            or ""
        )
        if not session_id and not worker_request_id:
            return None
        return self.build_plan(
            session_id=session_id,
            worker_request_id=worker_request_id,
            after_sequence=_safe_int(constraints.get("resume_after_sequence"), default=0),
            limit=_optional_positive_int(constraints.get("resume_limit")),
        )

    def event_for_plan(self, plan: SessionReplayPlan, *, run_id: str, task_id: str, node_id: str | None = None) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": plan.session_id,
                    "worker_request_id": plan.worker_request_id,
                    "phase": "session_replay_plan",
                    "ok": plan.ok,
                    "status": str(plan.status),
                    "plan_id": plan.plan_id,
                    "actions": [str(action) for action in plan.actions],
                    "input_count": len(plan.input_states),
                    "message_count": len(plan.replay_messages),
                    "window_count": plan.window.count,
                    "blocker_count": plan.blocker_count,
                    "warning_count": plan.warning_count,
                }
            },
        )

    def _load_records(
        self,
        *,
        session_id: str,
        worker_request_id: str,
    ) -> tuple[list[CodeWorkerSessionStoreRecord], str, str]:
        if session_id:
            replay = self.store.replay_session(session_id)
        elif worker_request_id:
            replay = self.store.replay_request(worker_request_id)
        else:
            return [], "", "missing_replay_selector"
        return list(replay.records), replay.path, "" if replay.ok else replay.error

    def _initial_findings(
        self,
        records: Sequence[CodeWorkerSessionStoreRecord],
        *,
        read_error: str,
    ) -> Iterable[SessionReplayFinding]:
        if read_error:
            yield SessionReplayFinding(
                code="store_read_failed",
                severity=SessionReplayFindingSeverity.BLOCKER,
                gap_kind=SessionReplayGapKind.STORE_READ_FAILED,
                message=f"CodeWorkerSessionStore replay failed: {read_error}",
                metadata={"error": read_error},
            )
        if not records:
            yield SessionReplayFinding(
                code="store_replay_empty",
                severity=SessionReplayFindingSeverity.BLOCKER,
                gap_kind=SessionReplayGapKind.MISSING_SEED,
                message="No session store records were available for replay.",
            )


def project_store_record(record: CodeWorkerSessionStoreRecord) -> SessionReplayRecordProjection:
    payload = dict(record.payload)
    payload_summary = summarize_store_payload(record)
    source_kind = source_kind_for_record(record.record_type)
    accepted = True
    if record.record_type == CodeWorkerSessionStoreRecordType.INPUT_ACCEPTED:
        accepted = payload.get("accepted") is not False
    payload_chars = len(json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True))
    return SessionReplayRecordProjection(
        sequence=record.sequence,
        record_id=record.record_id,
        record_type=record.record_type,
        source_kind=source_kind,
        session_id=record.session_id,
        worker_request_id=record.worker_request_id,
        created_at=record.created_at,
        payload_summary=payload_summary,
        payload_chars=payload_chars,
        accepted=accepted,
        metadata=dict(record.metadata),
    )


def source_kind_for_record(record_type: CodeWorkerSessionStoreRecordType) -> SessionReplaySourceKind:
    if record_type == CodeWorkerSessionStoreRecordType.INPUT_ACCEPTED:
        return SessionReplaySourceKind.INPUT_RECORD
    if record_type == CodeWorkerSessionStoreRecordType.CONTEXT_SNAPSHOT:
        return SessionReplaySourceKind.CONTEXT_SNAPSHOT
    if record_type == CodeWorkerSessionStoreRecordType.QUERY_ENGINE_ATTACHED:
        return SessionReplaySourceKind.QUERY_ENGINE_ATTACH
    return SessionReplaySourceKind.STORE_RECORD


def summarize_store_payload(record: CodeWorkerSessionStoreRecord) -> dict[str, Any]:
    payload = dict(record.payload)
    if record.record_type == CodeWorkerSessionStoreRecordType.SESSION_SEED:
        return {
            "node_id": payload.get("node_id", ""),
            "status": payload.get("status", ""),
            "source_path": _nested(payload, "source", "source_path", default=""),
            "target_path": _nested(payload, "source", "target_path", default=""),
        }
    if record.record_type == CodeWorkerSessionStoreRecordType.INPUT_ACCEPTED:
        return {
            "input_id": payload.get("input_id", ""),
            "kind": payload.get("kind", ""),
            "disposition": payload.get("disposition", ""),
            "accepted": payload.get("accepted", False),
            "chars": payload.get("chars", len(str(payload.get("normalized_text") or ""))),
            "command_name": payload.get("command_name", ""),
        }
    if record.record_type == CodeWorkerSessionStoreRecordType.CONTEXT_SNAPSHOT:
        return {
            "snapshot_id": payload.get("snapshot_id", ""),
            "status": payload.get("status", ""),
            "fingerprint": payload.get("fingerprint", ""),
            "selected_block_count": len(payload.get("selected_blocks") or []),
            "active_chars": payload.get("active_chars", 0),
        }
    if record.record_type == CodeWorkerSessionStoreRecordType.QUERY_ENGINE_ATTACHED:
        return {
            "query_session_id": payload.get("query_session_id", ""),
            "resume_token": payload.get("resume_token", ""),
            "attached_at": payload.get("attached_at", ""),
        }
    return {"keys": sorted(payload.keys())}


def select_replay_window(
    projections: Sequence[SessionReplayRecordProjection],
    *,
    after_sequence: int = 0,
    limit: int | None = None,
) -> SessionReplayWindow:
    selected = [projection for projection in projections if projection.sequence > after_sequence]
    if limit is not None:
        selected = selected[: max(0, limit)]
    return SessionReplayWindow(
        start_sequence=min((projection.sequence for projection in selected), default=0),
        end_sequence=max((projection.sequence for projection in selected), default=0),
        requested_after_sequence=after_sequence,
        limit=limit,
        projections=tuple(selected),
    )


def input_state_from_record(record: CodeWorkerSessionStoreRecord) -> SessionReplayInputState:
    payload = dict(record.payload)
    text = str(payload.get("normalized_text") or payload.get("raw_text") or "")
    return SessionReplayInputState(
        input_id=str(payload.get("input_id") or record.record_id),
        kind=str(payload.get("kind") or QueryInputKind.TEXT),
        disposition=str(payload.get("disposition") or ""),
        text=text,
        accepted=payload.get("accepted") is not False and not payload.get("blockers"),
        command_name=str(payload.get("command_name") or ""),
        risk=str(payload.get("risk") or ""),
        sequence=record.sequence,
        metadata={
            "store_record_id": record.record_id,
            "store_sequence": record.sequence,
            "source_path": _nested(payload, "source", "source_path", default=""),
            "target_path": _nested(payload, "source", "target_path", default=""),
        },
    )


def context_state_from_records(records: Sequence[CodeWorkerSessionStoreRecord]) -> SessionReplayContextState | None:
    context_records = [record for record in records if record.record_type == CodeWorkerSessionStoreRecordType.CONTEXT_SNAPSHOT]
    if not context_records:
        return None
    record = context_records[-1]
    payload = dict(record.payload)
    selected_blocks = payload.get("selected_blocks") if isinstance(payload.get("selected_blocks"), list) else []
    messages = tuple(context_block_to_message(block) for block in selected_blocks if isinstance(block, Mapping))
    return SessionReplayContextState(
        snapshot_id=str(payload.get("snapshot_id") or ""),
        fingerprint=str(payload.get("fingerprint") or ""),
        status=str(payload.get("status") or ""),
        selected_block_count=len(selected_blocks),
        dropped_block_count=len(payload.get("dropped_blocks") or []),
        active_chars=_safe_int(payload.get("active_chars"), default=0),
        messages=messages,
        raw_snapshot=payload,
    )


def context_block_to_message(block: Mapping[str, Any]) -> dict[str, Any]:
    role = str(block.get("role") or "meta")
    if role in {"meta", "system"}:
        message_role = "system"
    elif role in {"assistant", "tool", "user"}:
        message_role = role
    else:
        message_role = "system"
    return {
        "role": message_role,
        "content": str(block.get("text") or ""),
        "metadata": {
            "replayed_context_block": "true",
            "context_block_id": str(block.get("block_id") or ""),
            "context_block_kind": str(block.get("kind") or ""),
            "source_path": _nested(block, "source", "source_path", default=""),
            "target_path": _nested(block, "source", "target_path", default=""),
            "fingerprint_source": "session_replay",
        },
    }


def attach_state_from_records(records: Sequence[CodeWorkerSessionStoreRecord]) -> SessionReplayAttachState | None:
    attach_records = [record for record in records if record.record_type == CodeWorkerSessionStoreRecordType.QUERY_ENGINE_ATTACHED]
    if not attach_records:
        return None
    record = attach_records[-1]
    payload = dict(record.payload)
    return SessionReplayAttachState(
        query_session_id=str(payload.get("query_session_id") or ""),
        resume_token=str(payload.get("resume_token") or ""),
        attached_at=str(payload.get("attached_at") or record.created_at),
        sequence=record.sequence,
        metadata={"store_record_id": record.record_id},
    )


def validate_replay_identity(
    records: Sequence[CodeWorkerSessionStoreRecord],
    *,
    session_id: str,
    worker_request_id: str,
) -> list[SessionReplayFinding]:
    findings: list[SessionReplayFinding] = []
    session_ids = {record.session_id for record in records if record.session_id}
    request_ids = {record.worker_request_id for record in records if record.worker_request_id}
    if session_id and session_ids and session_ids != {session_id}:
        findings.append(
            SessionReplayFinding(
                code="session_id_mismatch",
                severity=SessionReplayFindingSeverity.BLOCKER,
                gap_kind=SessionReplayGapKind.SESSION_MISMATCH,
                message="Replay records contain multiple or unexpected session ids.",
                metadata={"expected": session_id, "actual": sorted(session_ids)},
            )
        )
    if worker_request_id and request_ids and request_ids != {worker_request_id}:
        findings.append(
            SessionReplayFinding(
                code="worker_request_id_mismatch",
                severity=SessionReplayFindingSeverity.BLOCKER,
                gap_kind=SessionReplayGapKind.REQUEST_MISMATCH,
                message="Replay records contain multiple or unexpected worker request ids.",
                metadata={"expected": worker_request_id, "actual": sorted(request_ids)},
            )
        )
    return findings


def validate_replay_sequences(records: Sequence[CodeWorkerSessionStoreRecord]) -> list[SessionReplayFinding]:
    findings: list[SessionReplayFinding] = []
    if not records:
        return findings
    sequences = [record.sequence for record in records]
    expected = list(range(min(sequences), max(sequences) + 1))
    if sequences != expected:
        findings.append(
            SessionReplayFinding(
                code="store_sequence_gap",
                severity=SessionReplayFindingSeverity.BLOCKER,
                gap_kind=SessionReplayGapKind.SEQUENCE_GAP,
                message="Session store record sequences are not contiguous.",
                metadata={"sequences": sequences, "expected": expected},
            )
        )
    return findings


def validate_replay_components(
    *,
    input_states: Sequence[SessionReplayInputState],
    context_state: SessionReplayContextState | None,
    attach_state: SessionReplayAttachState | None,
) -> list[SessionReplayFinding]:
    findings: list[SessionReplayFinding] = []
    if not input_states:
        findings.append(
            SessionReplayFinding(
                code="missing_input_records",
                severity=SessionReplayFindingSeverity.BLOCKER,
                gap_kind=SessionReplayGapKind.MISSING_INPUT,
                message="Replay plan cannot restore a prompt without input records.",
            )
        )
    if context_state is None:
        findings.append(
            SessionReplayFinding(
                code="missing_context_snapshot",
                severity=SessionReplayFindingSeverity.BLOCKER,
                gap_kind=SessionReplayGapKind.MISSING_CONTEXT,
                message="Replay plan cannot restore context without a context snapshot record.",
            )
        )
    elif not context_state.ok:
        findings.append(
            SessionReplayFinding(
                code="context_snapshot_blocked",
                severity=SessionReplayFindingSeverity.BLOCKER,
                gap_kind=SessionReplayGapKind.MISSING_CONTEXT,
                message="Context snapshot exists but is blocked or incomplete.",
                metadata=context_state.to_dict(include_raw=False),
            )
        )
    if attach_state is None:
        findings.append(
            SessionReplayFinding(
                code="missing_query_engine_attach",
                severity=SessionReplayFindingSeverity.WARNING,
                gap_kind=SessionReplayGapKind.NONE,
                message="Replay can restore the pre-query seed, but no QueryEngine attachment record exists yet.",
            )
        )
    return findings


def replay_actions(
    *,
    input_states: Sequence[SessionReplayInputState],
    context_state: SessionReplayContextState | None,
    attach_state: SessionReplayAttachState | None,
    findings: Sequence[SessionReplayFinding],
) -> list[SessionReplayAction]:
    if any(finding.blocking for finding in findings):
        return [SessionReplayAction.BLOCK]
    actions = []
    if context_state is not None:
        actions.append(SessionReplayAction.RESTORE_CONTEXT)
    if input_states:
        actions.append(SessionReplayAction.RESTORE_INPUT)
    if attach_state is not None and attach_state.ok:
        actions.append(SessionReplayAction.REATTACH_QUERY_ENGINE)
    actions.append(SessionReplayAction.REPLAY_EVENTS)
    return actions


def replay_status(
    *,
    records: Sequence[CodeWorkerSessionStoreRecord],
    findings: Sequence[SessionReplayFinding],
    context_state: SessionReplayContextState | None,
) -> SessionReplayStatus:
    if not records:
        return SessionReplayStatus.EMPTY
    if any(finding.blocking for finding in findings):
        return SessionReplayStatus.BLOCKED
    if any(finding.severity == SessionReplayFindingSeverity.WARNING for finding in findings) or (
        context_state is not None and context_state.status == "degraded"
    ):
        return SessionReplayStatus.DEGRADED
    return SessionReplayStatus.READY


def render_session_replay_plan_markdown(plan: SessionReplayPlan) -> str:
    lines = [
        "# CodeWorker Session Replay Plan",
        "",
        f"- ok: `{str(plan.ok).lower()}`",
        f"- status: `{plan.status}`",
        f"- plan_id: `{plan.plan_id}`",
        f"- session_id: `{plan.session_id}`",
        f"- worker_request_id: `{plan.worker_request_id}`",
        f"- actions: `{', '.join(str(action) for action in plan.actions)}`",
        f"- replay_messages: `{len(plan.replay_messages)}`",
        f"- window_count: `{plan.window.count}`",
        f"- blockers: `{plan.blocker_count}`",
        f"- warnings: `{plan.warning_count}`",
        "",
        "## Findings",
        "",
    ]
    if plan.findings:
        for finding in plan.findings:
            lines.append(f"- `{finding.severity}` `{finding.code}` {finding.message}")
    else:
        lines.append("- none")
    lines.extend(["", "## Inputs", ""])
    for state in plan.input_states:
        lines.append(f"- `{state.input_id}` `{state.kind}` accepted=`{str(state.accepted).lower()}` chars=`{state.chars}`")
    lines.extend(["", "## Context", ""])
    if plan.context_state is None:
        lines.append("- none")
    else:
        lines.append(f"- snapshot_id: `{plan.context_state.snapshot_id}`")
        lines.append(f"- fingerprint: `{plan.context_state.fingerprint}`")
        lines.append(f"- selected_blocks: `{plan.context_state.selected_block_count}`")
    return "\n".join(lines) + "\n"


def session_replay_metadata(plan: SessionReplayPlan | None) -> dict[str, str]:
    if plan is None:
        return {
            "session_replay_plan_id": "",
            "session_replay_ok": "",
            "session_replay_status": "",
        }
    return plan.metadata_values()


def replay_plan_from_payload(payload: Mapping[str, Any]) -> SessionReplayPlan:
    input_states = tuple(
        SessionReplayInputState(
            input_id=str(item.get("input_id") or ""),
            kind=str(item.get("kind") or ""),
            disposition=str(item.get("disposition") or ""),
            text=str(item.get("text") or ""),
            accepted=item.get("accepted") is True,
            command_name=str(item.get("command_name") or ""),
            risk=str(item.get("risk") or ""),
            sequence=_safe_int(item.get("sequence"), default=0),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("input_states", [])
        if isinstance(item, Mapping)
    )
    window_payload = payload.get("window") if isinstance(payload.get("window"), Mapping) else {}
    window = SessionReplayWindow(
        start_sequence=_safe_int(window_payload.get("start_sequence"), default=0),
        end_sequence=_safe_int(window_payload.get("end_sequence"), default=0),
        requested_after_sequence=_safe_int(window_payload.get("requested_after_sequence"), default=0),
        limit=_optional_positive_int(window_payload.get("limit")),
        projections=(),
    )
    findings = tuple(
        SessionReplayFinding(
            code=str(item.get("code") or ""),
            severity=_enum_or_default(SessionReplayFindingSeverity, item.get("severity"), SessionReplayFindingSeverity.INFO),
            gap_kind=_enum_or_default(SessionReplayGapKind, item.get("gap_kind"), SessionReplayGapKind.NONE),
            message=str(item.get("message") or ""),
            sequence=_safe_int(item.get("sequence"), default=0),
            record_id=str(item.get("record_id") or ""),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in payload.get("findings", [])
        if isinstance(item, Mapping)
    )
    return SessionReplayPlan(
        plan_id=str(payload.get("plan_id") or new_id("replayplan")),
        status=_enum_or_default(SessionReplayStatus, payload.get("status"), SessionReplayStatus.BLOCKED),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        actions=tuple(_enum_or_default(SessionReplayAction, item, SessionReplayAction.BLOCK) for item in payload.get("actions", [])),
        input_states=input_states,
        context_state=None,
        attach_state=None,
        window=window,
        findings=findings,
        created_at=str(payload.get("created_at") or now_iso()),
        metadata=dict(payload.get("metadata") or {}),
    )


def _is_input_record(record: CodeWorkerSessionStoreRecord) -> bool:
    return record.record_type == CodeWorkerSessionStoreRecordType.INPUT_ACCEPTED


def _first_non_empty(values: Iterable[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


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


def _optional_positive_int(value: Any) -> int | None:
    parsed = _safe_int(value, default=0)
    return parsed if parsed > 0 else None


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default
