from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, new_id, now_iso, to_jsonable

from .artifacts import LocalArtifactStore
from .claude_context_assembly_foundation import ContextAssemblySnapshot, context_snapshot_to_messages
from .claude_input_processor import (
    QueryInputKind,
    QueryInputProcessingReport,
    QueryInputRecord,
    QuerySourceKind,
    QuerySourceMetadata,
)
from .claude_session_acceptance_runtime import SessionAcceptanceReport
from .claude_session_foundation_audit import FoundationAuditReport
from .claude_session_lifecycle_state import SessionLifecycleStateReport
from .claude_session_replay_runtime import SessionReplayPlan
from .claude_session_store import CodeWorkerSessionSeed, CodeWorkerSessionStore
from .claude_turn_lifecycle_runtime import TurnLifecycleProjection, TurnLifecycleSeed, TurnSeedRoute
from .query_session import StopReason, snapshot_checkpoint_metadata


class QuerySessionIntegrationStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class QuerySessionIntegrationSeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class QuerySessionIntegrationSurface(StrEnum):
    SESSION_STORE = "session_store"
    INPUT_PROCESSOR = "input_processor"
    CONTEXT_ASSEMBLY = "context_assembly"
    QUERY_ENTRY = "query_entry"
    CONTROL_STATE = "control_state"
    RESUME = "resume"
    CHECKPOINT = "checkpoint"
    EVENT_STREAM = "event_stream"
    DOWNSTREAM_HANDOFF = "downstream_handoff"


class QueryEntryRoute(StrEnum):
    MODEL_QUERY = "model_query"
    TOOL_LOOP = "tool_loop"
    CONTROL_COMMAND = "control_command"
    SHELL_TOOL = "shell_tool"
    REPLAY_RESTORE = "replay_restore"
    BLOCKED = "blocked"


class QueryControlActionKind(StrEnum):
    NONE = "none"
    RESUME = "resume"
    INTERRUPT = "interrupt"
    CANCEL = "cancel"
    CHECKPOINT = "checkpoint"
    REWIND = "rewind"


class QueryControlTransitionStatus(StrEnum):
    APPLIED = "applied"
    PENDING = "pending"
    BLOCKED = "blocked"
    IGNORED = "ignored"


class QueryCheckpointStatus(StrEnum):
    READY = "ready"
    WRITTEN = "written"
    BLOCKED = "blocked"
    NOT_REQUESTED = "not_requested"


class QueryEntryBlockReason(StrEnum):
    NONE = "none"
    DISABLED = "disabled"
    FOUNDATION_BLOCKED = "foundation_blocked"
    CONTEXT_BLOCKED = "context_blocked"
    INPUT_BLOCKED = "input_blocked"
    CONTROL_INTERRUPTED = "control_interrupted"
    CONTROL_CANCELLED = "control_cancelled"
    STALE_CONTEXT = "stale_context"
    REPLAY_BLOCKED = "replay_blocked"
    EMPTY_MESSAGES = "empty_messages"


@dataclass(frozen=True, slots=True)
class QuerySessionIntegrationFinding:
    code: str
    severity: QuerySessionIntegrationSeverity
    surface: QuerySessionIntegrationSurface
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == QuerySessionIntegrationSeverity.BLOCKER

    @property
    def passed(self) -> bool:
        return self.severity == QuerySessionIntegrationSeverity.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "passed": self.passed,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryControlTransition:
    transition_id: str
    action: QueryControlActionKind
    status: QueryControlTransitionStatus
    session_id: str
    worker_request_id: str
    from_state: str
    to_state: str
    created_at: str = field(default_factory=now_iso)
    reason: str = ""
    blocks_query: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {QueryControlTransitionStatus.APPLIED, QueryControlTransitionStatus.IGNORED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "action": str(self.action),
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "created_at": self.created_at,
            "reason": self.reason,
            "blocks_query": self.blocks_query,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryControlState:
    state_id: str
    status: QuerySessionIntegrationStatus
    session_id: str
    worker_request_id: str
    active_actions: tuple[QueryControlActionKind, ...]
    transitions: tuple[QueryControlTransition, ...]
    pending_interrupt: bool = False
    pending_cancel: bool = False
    checkpoint_requested: bool = False
    resume_requested: bool = False
    stale_context: bool = False
    restored_parent_uuid: str = ""
    restored_resume_token: str = ""
    restored_context_fingerprint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {QuerySessionIntegrationStatus.READY, QuerySessionIntegrationStatus.DEGRADED}

    @property
    def blocks_query(self) -> bool:
        return self.pending_cancel or self.pending_interrupt or self.stale_context or any(
            transition.blocks_query for transition in self.transitions
        )

    @property
    def blocker_count(self) -> int:
        return sum(1 for transition in self.transitions if transition.blocks_query)

    @property
    def action_names(self) -> tuple[str, ...]:
        return tuple(str(action) for action in self.active_actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_id": self.state_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "active_actions": list(self.action_names),
            "transitions": [transition.to_dict() for transition in self.transitions],
            "pending_interrupt": self.pending_interrupt,
            "pending_cancel": self.pending_cancel,
            "checkpoint_requested": self.checkpoint_requested,
            "resume_requested": self.resume_requested,
            "stale_context": self.stale_context,
            "blocks_query": self.blocks_query,
            "blocker_count": self.blocker_count,
            "restored_parent_uuid": self.restored_parent_uuid,
            "restored_resume_token": self.restored_resume_token,
            "restored_context_fingerprint": self.restored_context_fingerprint,
            "metadata": to_jsonable(self.metadata),
        }

    def metadata_values(self) -> dict[str, str]:
        return {
            "query_session_control_state_id": self.state_id,
            "query_session_control_ok": str(self.ok).lower(),
            "query_session_control_status": str(self.status),
            "query_session_control_blocks_query": str(self.blocks_query).lower(),
            "query_session_control_actions": ",".join(self.action_names),
            "query_session_pending_interrupt": str(self.pending_interrupt).lower(),
            "query_session_pending_cancel": str(self.pending_cancel).lower(),
            "query_session_checkpoint_requested": str(self.checkpoint_requested).lower(),
            "query_session_resume_requested": str(self.resume_requested).lower(),
            "query_session_stale_context": str(self.stale_context).lower(),
            "query_session_restored_parent_uuid": self.restored_parent_uuid,
            "query_session_restored_resume_token": self.restored_resume_token,
            "query_session_restored_context_fingerprint": self.restored_context_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class QueryCheckpointRecord:
    checkpoint_id: str
    status: QueryCheckpointStatus
    session_id: str
    worker_request_id: str
    context_fingerprint: str
    parent_uuid: str
    resume_token: str
    sequence: int
    artifact: ArtifactRef | None = None
    created_at: str = field(default_factory=now_iso)
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {QueryCheckpointStatus.READY, QueryCheckpointStatus.WRITTEN, QueryCheckpointStatus.NOT_REQUESTED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "context_fingerprint": self.context_fingerprint,
            "parent_uuid": self.parent_uuid,
            "resume_token": self.resume_token,
            "sequence": self.sequence,
            "artifact": to_jsonable(self.artifact) if self.artifact else None,
            "created_at": self.created_at,
            "error": self.error,
            "metadata": to_jsonable(self.metadata),
        }

    def metadata_values(self) -> dict[str, str]:
        return {
            "query_session_checkpoint_id": self.checkpoint_id,
            "query_session_checkpoint_status": str(self.status),
            "query_session_checkpoint_ok": str(self.ok).lower(),
            "query_session_checkpoint_artifact_id": self.artifact.artifact_id if self.artifact else "",
            "query_session_checkpoint_context_fingerprint": self.context_fingerprint,
            "query_session_checkpoint_parent_uuid": self.parent_uuid,
            "query_session_checkpoint_resume_token": self.resume_token,
            "query_session_checkpoint_sequence": str(self.sequence),
            "query_session_checkpoint_error": self.error,
        }


@dataclass(frozen=True, slots=True)
class QueryEntryMessage:
    message_id: str
    role: str
    content: str
    source: str
    sequence: int
    parent_uuid: str = ""
    input_id: str = ""
    context_block_id: str = ""
    replayed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.content)

    def to_model_message(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "metadata": {
                "query_entry_message_id": self.message_id,
                "query_entry_source": self.source,
                "query_entry_sequence": str(self.sequence),
                "parent_uuid": self.parent_uuid,
                "input_id": self.input_id,
                "context_block_id": self.context_block_id,
                "replayed": str(self.replayed).lower(),
                **{str(key): str(value) for key, value in self.metadata.items() if isinstance(value, (str, int, float, bool))},
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "source": self.source,
            "sequence": self.sequence,
            "parent_uuid": self.parent_uuid,
            "input_id": self.input_id,
            "context_block_id": self.context_block_id,
            "replayed": self.replayed,
            "chars": self.chars,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryEntryContextEnvelope:
    snapshot_id: str
    fingerprint: str
    status: str
    selected_block_count: int
    active_chars: int
    messages: tuple[QueryEntryMessage, ...]
    source: QuerySourceMetadata
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.snapshot_id) and self.status != "blocked" and bool(self.messages)

    @property
    def message_count(self) -> int:
        return len(self.messages)

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "ok": self.ok,
            "selected_block_count": self.selected_block_count,
            "active_chars": self.active_chars,
            "message_count": self.message_count,
            "messages": [message.to_dict() if include_text else {**message.to_dict(), "content": ""} for message in self.messages],
            "source": self.source.to_dict(),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryEntryToolEnvelope:
    tool_names: tuple[str, ...]
    planned_tool_names: tuple[str, ...]
    read_only_tool_names: tuple[str, ...] = ()
    mutating_tool_names: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_count(self) -> int:
        return len(self.tool_names)

    @property
    def planned_tool_count(self) -> int:
        return len(self.planned_tool_names)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_names": list(self.tool_names),
            "planned_tool_names": list(self.planned_tool_names),
            "read_only_tool_names": list(self.read_only_tool_names),
            "mutating_tool_names": list(self.mutating_tool_names),
            "allowed_tools": list(self.allowed_tools),
            "denied_tools": list(self.denied_tools),
            "tool_count": self.tool_count,
            "planned_tool_count": self.planned_tool_count,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryEntryPermissionEnvelope:
    permission_mode: str
    command_scoped_allowed_tools: tuple[str, ...]
    requested_permission_mode: str = ""
    policy_owner: str = "ToolPermissionPolicy"
    denial_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.permission_mode)

    def to_dict(self) -> dict[str, Any]:
        return {
            "permission_mode": self.permission_mode,
            "requested_permission_mode": self.requested_permission_mode,
            "policy_owner": self.policy_owner,
            "command_scoped_allowed_tools": list(self.command_scoped_allowed_tools),
            "denial_count": self.denial_count,
            "ok": self.ok,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryEntryResumeEnvelope:
    requested: bool
    ok: bool
    session_id: str = ""
    worker_request_id: str = ""
    parent_uuid: str = ""
    resume_token: str = ""
    context_fingerprint: str = ""
    replay_message_count: int = 0
    replay_actions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "parent_uuid": self.parent_uuid,
            "resume_token": self.resume_token,
            "context_fingerprint": self.context_fingerprint,
            "replay_message_count": self.replay_message_count,
            "replay_actions": list(self.replay_actions),
            "warnings": list(self.warnings),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryEntryDownstreamHandoff:
    handoff_id: str
    owner_unit: str
    target_unit: str
    packet_schema: str
    runtime_ports: tuple[str, ...]
    event_phases: tuple[str, ...]
    messages_ready: bool
    context_ready: bool
    control_ready: bool
    permission_ready: bool
    tool_loop_ready: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            self.messages_ready
            and self.context_ready
            and self.control_ready
            and self.permission_ready
            and self.tool_loop_ready
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "owner_unit": self.owner_unit,
            "target_unit": self.target_unit,
            "packet_schema": self.packet_schema,
            "runtime_ports": list(self.runtime_ports),
            "event_phases": list(self.event_phases),
            "messages_ready": self.messages_ready,
            "context_ready": self.context_ready,
            "control_ready": self.control_ready,
            "permission_ready": self.permission_ready,
            "tool_loop_ready": self.tool_loop_ready,
            "ok": self.ok,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryEntryPacket:
    packet_id: str
    status: QuerySessionIntegrationStatus
    route: QueryEntryRoute
    session_id: str
    worker_request_id: str
    parent_uuid: str
    context_fingerprint: str
    source: QuerySourceMetadata
    context: QueryEntryContextEnvelope
    tools: QueryEntryToolEnvelope
    permission: QueryEntryPermissionEnvelope
    control: QueryControlState
    resume: QueryEntryResumeEnvelope
    checkpoint: QueryCheckpointRecord
    handoff: QueryEntryDownstreamHandoff
    messages: tuple[QueryEntryMessage, ...]
    findings: tuple[QuerySessionIntegrationFinding, ...] = ()
    created_at: str = field(default_factory=now_iso)
    block_reason: QueryEntryBlockReason = QueryEntryBlockReason.NONE
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            self.status in {QuerySessionIntegrationStatus.READY, QuerySessionIntegrationStatus.DEGRADED}
            and self.route != QueryEntryRoute.BLOCKED
            and self.block_reason == QueryEntryBlockReason.NONE
            and not any(finding.blocking for finding in self.findings)
        )

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == QuerySessionIntegrationSeverity.WARNING)

    def request_messages(self) -> list[dict[str, Any]]:
        return [message.to_model_message() for message in self.messages]

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        messages = [message.to_dict() for message in self.messages]
        if not include_text:
            messages = [{**message, "content": ""} for message in messages]
        return {
            "packet_id": self.packet_id,
            "status": str(self.status),
            "ok": self.ok,
            "route": str(self.route),
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "parent_uuid": self.parent_uuid,
            "context_fingerprint": self.context_fingerprint,
            "source": self.source.to_dict(),
            "context": self.context.to_dict(include_text=include_text),
            "tools": self.tools.to_dict(),
            "permission": self.permission.to_dict(),
            "control": self.control.to_dict(),
            "resume": self.resume.to_dict(),
            "checkpoint": self.checkpoint.to_dict(),
            "handoff": self.handoff.to_dict(),
            "messages": messages,
            "message_count": self.message_count,
            "findings": [finding.to_dict() for finding in self.findings],
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "created_at": self.created_at,
            "block_reason": str(self.block_reason),
            "metadata": to_jsonable(self.metadata),
        }

    def metadata_values(self) -> dict[str, str]:
        values = {
            "query_entry_packet_id": self.packet_id,
            "query_entry_ok": str(self.ok).lower(),
            "query_entry_status": str(self.status),
            "query_entry_route": str(self.route),
            "query_entry_block_reason": str(self.block_reason),
            "query_entry_message_count": str(self.message_count),
            "query_entry_blockers": str(self.blocker_count),
            "query_entry_warnings": str(self.warning_count),
            "query_entry_context_fingerprint": self.context_fingerprint,
            "query_entry_parent_uuid": self.parent_uuid,
            "query_entry_handoff_ok": str(self.handoff.ok).lower(),
            "query_entry_tool_count": str(self.tools.tool_count),
            "query_entry_planned_tool_count": str(self.tools.planned_tool_count),
            **self.control.metadata_values(),
            **self.checkpoint.metadata_values(),
        }
        values.update(self.source.metadata("query_entry_source"))
        return values


@dataclass(frozen=True, slots=True)
class QuerySessionIntegrationReport:
    report_id: str
    status: QuerySessionIntegrationStatus
    session_id: str
    worker_request_id: str
    packet: QueryEntryPacket
    control: QueryControlState
    checkpoint: QueryCheckpointRecord
    findings: tuple[QuerySessionIntegrationFinding, ...]
    event_phases: tuple[str, ...]
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {QuerySessionIntegrationStatus.READY, QuerySessionIntegrationStatus.DEGRADED} and self.packet.ok

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == QuerySessionIntegrationSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        for finding in self.findings:
            if finding.blocking:
                return finding.code
        return ""

    def metadata_values(self) -> dict[str, str]:
        return {
            "query_session_integration_report_id": self.report_id,
            "query_session_integration_ok": str(self.ok).lower(),
            "query_session_integration_status": str(self.status),
            "query_session_integration_blockers": str(self.blocker_count),
            "query_session_integration_warnings": str(self.warning_count),
            "query_session_integration_first_blocker": self.first_blocker_code,
            "query_session_integration_event_phases": ",".join(self.event_phases),
            **self.packet.metadata_values(),
        }

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "packet": self.packet.to_dict(include_text=include_text),
            "control": self.control.to_dict(),
            "checkpoint": self.checkpoint.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "first_blocker_code": self.first_blocker_code,
            "event_phases": list(self.event_phases),
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


class QuerySessionControlRuntime:
    """Resolves resume, interrupt, cancel and checkpoint state before query entry.

    The upstream QueryEngine uses an abort controller and session transcript
    chain together. Zyra models that as deterministic control transitions so
    API/CLI/worker callers can see whether a turn was allowed to enter query.
    """

    def resolve(
        self,
        *,
        request: Any,
        seed: CodeWorkerSessionSeed,
        context_snapshot: ContextAssemblySnapshot | None,
        replay_plan: SessionReplayPlan | None = None,
    ) -> QueryControlState:
        constraints = _as_mapping(getattr(request, "constraints", {}))
        actions = tuple(_control_actions_from_constraints(constraints, replay_plan=replay_plan))
        transitions: list[QueryControlTransition] = []
        restored_parent_uuid = ""
        restored_resume_token = ""
        restored_context_fingerprint = ""
        if replay_plan is not None:
            restored_parent_uuid = _parent_uuid_from_replay(replay_plan)
            restored_resume_token = _resume_token_from_replay(replay_plan)
            restored_context_fingerprint = _context_fingerprint_from_replay(replay_plan)
            status = QueryControlTransitionStatus.APPLIED if replay_plan.ok else QueryControlTransitionStatus.BLOCKED
            transitions.append(
                QueryControlTransition(
                    transition_id=new_id("qctl"),
                    action=QueryControlActionKind.RESUME,
                    status=status,
                    session_id=seed.session_id,
                    worker_request_id=seed.worker_request_id,
                    from_state="session_store_replay",
                    to_state="query_entry_parent_restored" if replay_plan.ok else "query_entry_resume_blocked",
                    reason="resume selector restored pre-query session store records",
                    blocks_query=not replay_plan.ok,
                    metadata=replay_plan.to_dict(include_raw_context=False),
                )
            )
        pending_interrupt = _truthy(constraints.get("interrupt")) or _truthy(constraints.get("interrupt_session"))
        pending_cancel = _truthy(constraints.get("cancel")) or _truthy(constraints.get("cancel_session"))
        checkpoint_requested = _truthy(constraints.get("checkpoint")) or _truthy(constraints.get("checkpoint_session"))
        if pending_interrupt:
            transitions.append(
                QueryControlTransition(
                    transition_id=new_id("qctl"),
                    action=QueryControlActionKind.INTERRUPT,
                    status=QueryControlTransitionStatus.PENDING,
                    session_id=seed.session_id,
                    worker_request_id=seed.worker_request_id,
                    from_state="query_entry_preflight",
                    to_state="query_entry_interrupted",
                    reason=str(constraints.get("interrupt_reason") or "interrupt requested before query entry"),
                    blocks_query=True,
                    metadata={"stop_reason": str(StopReason.USER_CANCELLED)},
                )
            )
        if pending_cancel:
            transitions.append(
                QueryControlTransition(
                    transition_id=new_id("qctl"),
                    action=QueryControlActionKind.CANCEL,
                    status=QueryControlTransitionStatus.PENDING,
                    session_id=seed.session_id,
                    worker_request_id=seed.worker_request_id,
                    from_state="query_entry_preflight",
                    to_state="query_entry_cancelled",
                    reason=str(constraints.get("cancel_reason") or "cancel requested before query entry"),
                    blocks_query=True,
                    metadata={"stop_reason": str(StopReason.USER_CANCELLED)},
                )
            )
        if checkpoint_requested:
            transitions.append(
                QueryControlTransition(
                    transition_id=new_id("qctl"),
                    action=QueryControlActionKind.CHECKPOINT,
                    status=QueryControlTransitionStatus.APPLIED,
                    session_id=seed.session_id,
                    worker_request_id=seed.worker_request_id,
                    from_state="query_entry_preflight",
                    to_state="query_entry_checkpoint_ready",
                    reason="checkpoint requested for pre-query handoff",
                    blocks_query=False,
                    metadata={"context_snapshot_id": getattr(context_snapshot, "snapshot_id", "") if context_snapshot else ""},
                )
            )
        requested_context_fingerprint = str(constraints.get("expected_context_fingerprint") or "")
        actual_context_fingerprint = context_snapshot.fingerprint if context_snapshot is not None else ""
        stale_context = bool(
            requested_context_fingerprint
            and actual_context_fingerprint
            and requested_context_fingerprint != actual_context_fingerprint
        )
        if stale_context:
            transitions.append(
                QueryControlTransition(
                    transition_id=new_id("qctl"),
                    action=QueryControlActionKind.CHECKPOINT,
                    status=QueryControlTransitionStatus.BLOCKED,
                    session_id=seed.session_id,
                    worker_request_id=seed.worker_request_id,
                    from_state="query_entry_context_bound",
                    to_state="query_entry_stale_context_blocked",
                    reason="expected_context_fingerprint does not match current ContextAssemblyRuntime snapshot",
                    blocks_query=True,
                    metadata={
                        "expected_context_fingerprint": requested_context_fingerprint,
                        "actual_context_fingerprint": actual_context_fingerprint,
                    },
                )
            )
        status = QuerySessionIntegrationStatus.READY
        if any(transition.blocks_query for transition in transitions):
            status = QuerySessionIntegrationStatus.BLOCKED
        elif transitions:
            status = QuerySessionIntegrationStatus.DEGRADED
        return QueryControlState(
            state_id=new_id("qctlstate"),
            status=status,
            session_id=seed.session_id,
            worker_request_id=seed.worker_request_id,
            active_actions=actions,
            transitions=tuple(transitions),
            pending_interrupt=pending_interrupt,
            pending_cancel=pending_cancel,
            checkpoint_requested=checkpoint_requested,
            resume_requested=replay_plan is not None,
            stale_context=stale_context,
            restored_parent_uuid=restored_parent_uuid,
            restored_resume_token=restored_resume_token,
            restored_context_fingerprint=restored_context_fingerprint,
            metadata={
                "source_path": "src/QueryEngine.ts",
                "target_path": "packages/runtime/zyra_runtime/claude_query_session_integration.py",
                "owner_unit": "M1-02B",
            },
        )

    def event_for_state(self, state: QueryControlState, *, run_id: str, task_id: str, node_id: str | None = None) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": state.session_id,
                    "worker_request_id": state.worker_request_id,
                    "phase": "query_control_state",
                    "ok": state.ok,
                    "status": str(state.status),
                    "actions": list(state.action_names),
                    "blocks_query": state.blocks_query,
                    "pending_interrupt": state.pending_interrupt,
                    "pending_cancel": state.pending_cancel,
                    "checkpoint_requested": state.checkpoint_requested,
                    "resume_requested": state.resume_requested,
                    "stale_context": state.stale_context,
                    "transition_count": len(state.transitions),
                    "restored_parent_uuid": state.restored_parent_uuid,
                    "restored_resume_token": state.restored_resume_token,
                }
            },
        )


class QueryCheckpointRuntime:
    """Writes pre-query checkpoints that make the handoff reproducible."""

    def __init__(self, artifact_store: LocalArtifactStore | None = None) -> None:
        self.artifact_store = artifact_store

    def build(
        self,
        *,
        request: Any,
        seed: CodeWorkerSessionSeed,
        context_snapshot: ContextAssemblySnapshot | None,
        control_state: QueryControlState,
        replay_plan: SessionReplayPlan | None = None,
        disabled: bool = False,
    ) -> QueryCheckpointRecord:
        constraints = _as_mapping(getattr(request, "constraints", {}))
        requested = control_state.checkpoint_requested or _truthy(constraints.get("checkpoint_query_entry"))
        if disabled:
            return QueryCheckpointRecord(
                checkpoint_id=new_id("qchk"),
                status=QueryCheckpointStatus.BLOCKED,
                session_id=seed.session_id,
                worker_request_id=seed.worker_request_id,
                context_fingerprint=context_snapshot.fingerprint if context_snapshot else "",
                parent_uuid=control_state.restored_parent_uuid,
                resume_token=control_state.restored_resume_token,
                sequence=_last_sequence_from_replay(replay_plan),
                error="query_session_checkpoint_runtime_disabled",
            )
        if not requested:
            return QueryCheckpointRecord(
                checkpoint_id=new_id("qchk"),
                status=QueryCheckpointStatus.NOT_REQUESTED,
                session_id=seed.session_id,
                worker_request_id=seed.worker_request_id,
                context_fingerprint=context_snapshot.fingerprint if context_snapshot else "",
                parent_uuid=control_state.restored_parent_uuid,
                resume_token=control_state.restored_resume_token,
                sequence=_last_sequence_from_replay(replay_plan),
                metadata={"requested": False},
            )
        payload = {
            "schema": "zyra.claude.query_entry_checkpoint.v1",
            "session_id": seed.session_id,
            "worker_request_id": seed.worker_request_id,
            "run_id": seed.run_id,
            "task_id": seed.task_id,
            "context_snapshot": context_snapshot.to_dict(include_text=False) if context_snapshot else None,
            "control_state": control_state.to_dict(),
            "replay_plan": replay_plan.to_dict(include_raw_context=False) if replay_plan else None,
            "seed": seed.to_dict(include_text=False),
            "created_at": now_iso(),
        }
        artifact = None
        status = QueryCheckpointStatus.READY
        error = ""
        if self.artifact_store is not None:
            try:
                artifact = self.artifact_store.write_text(
                    run_id=seed.run_id,
                    task_id=seed.task_id,
                    content=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                    title=f"CodeWorker query entry checkpoint {seed.session_id}",
                    kind=ArtifactKind.STRUCTURED_DATA,
                    extension=".json",
                    producer_node_id=seed.node_id or None,
                )
                status = QueryCheckpointStatus.WRITTEN
            except OSError as exc:
                status = QueryCheckpointStatus.BLOCKED
                error = type(exc).__name__
        return QueryCheckpointRecord(
            checkpoint_id=new_id("qchk"),
            status=status,
            session_id=seed.session_id,
            worker_request_id=seed.worker_request_id,
            context_fingerprint=context_snapshot.fingerprint if context_snapshot else "",
            parent_uuid=control_state.restored_parent_uuid,
            resume_token=control_state.restored_resume_token,
            sequence=_last_sequence_from_replay(replay_plan),
            artifact=artifact,
            error=error,
            metadata={"payload_sha256": _hash_payload(payload), "requested": True},
        )

    def event_for_checkpoint(
        self,
        checkpoint: QueryCheckpointRecord,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": checkpoint.session_id,
                    "worker_request_id": checkpoint.worker_request_id,
                    "phase": "query_session_checkpoint",
                    "ok": checkpoint.ok,
                    "status": str(checkpoint.status),
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "artifact_id": checkpoint.artifact.artifact_id if checkpoint.artifact else "",
                    "context_fingerprint": checkpoint.context_fingerprint,
                    "parent_uuid": checkpoint.parent_uuid,
                    "resume_token": checkpoint.resume_token,
                    "sequence": checkpoint.sequence,
                    "error": checkpoint.error,
                }
            },
        )


class QueryEntryPacketBuilder:
    """Builds the query entry packet consumed by the worker and later 02C."""

    def __init__(self, *, source: QuerySourceMetadata | None = None) -> None:
        self.source = source or default_query_entry_source()

    def build(
        self,
        *,
        request: Any,
        seed: CodeWorkerSessionSeed,
        input_report: QueryInputProcessingReport,
        context_snapshot: ContextAssemblySnapshot | None,
        tool_specs: Sequence[Any],
        query_turns: Sequence[Sequence[Mapping[str, Any]]],
        turn_lifecycle: TurnLifecycleProjection,
        control_state: QueryControlState,
        checkpoint: QueryCheckpointRecord,
        replay_plan: SessionReplayPlan | None = None,
        disabled: bool = False,
    ) -> QueryEntryPacket:
        findings = list(
            self._base_findings(
                seed=seed,
                input_report=input_report,
                context_snapshot=context_snapshot,
                turn_lifecycle=turn_lifecycle,
                replay_plan=replay_plan,
                control_state=control_state,
                checkpoint=checkpoint,
                disabled=disabled,
            )
        )
        context_envelope = build_context_envelope(context_snapshot, source=self.source)
        tool_envelope = build_tool_envelope(tool_specs, query_turns=query_turns, input_report=input_report)
        permission_envelope = build_permission_envelope(request, input_report=input_report)
        resume_envelope = build_resume_envelope(replay_plan, control_state=control_state)
        parent_uuid = control_state.restored_parent_uuid or _parent_uuid_from_context(context_snapshot) or seed.session_id
        messages = tuple(
            _merge_entry_messages(
                context_envelope.messages,
                replay_plan=replay_plan,
                input_report=input_report,
                parent_uuid=parent_uuid,
            )
        )
        if not messages:
            findings.append(
                QuerySessionIntegrationFinding(
                    code="query_entry_messages_empty",
                    severity=QuerySessionIntegrationSeverity.BLOCKER,
                    surface=QuerySessionIntegrationSurface.QUERY_ENTRY,
                    message="Query entry packet has no messages to pass to QueryEngine.",
                )
            )
        route = route_from_turn_lifecycle(turn_lifecycle, control_state=control_state)
        block_reason = block_reason_for(
            disabled=disabled,
            seed=seed,
            input_report=input_report,
            context_snapshot=context_snapshot,
            replay_plan=replay_plan,
            control_state=control_state,
            messages=messages,
        )
        if block_reason != QueryEntryBlockReason.NONE:
            route = QueryEntryRoute.BLOCKED
        status = status_from_findings(findings)
        if route == QueryEntryRoute.BLOCKED:
            status = QuerySessionIntegrationStatus.BLOCKED
        handoff = build_downstream_handoff(
            messages=messages,
            context=context_envelope,
            control=control_state,
            permission=permission_envelope,
            tools=tool_envelope,
            status=status,
        )
        return QueryEntryPacket(
            packet_id=new_id("qentry"),
            status=status,
            route=route,
            session_id=seed.session_id,
            worker_request_id=seed.worker_request_id,
            parent_uuid=parent_uuid,
            context_fingerprint=context_envelope.fingerprint,
            source=self.source,
            context=context_envelope,
            tools=tool_envelope,
            permission=permission_envelope,
            control=control_state,
            resume=resume_envelope,
            checkpoint=checkpoint,
            handoff=handoff,
            messages=messages,
            findings=tuple(findings),
            block_reason=block_reason,
            metadata={
                "source_path": "src/QueryEngine.ts",
                "query_boundary": "submitMessage -> query",
                "tool_loop_target_unit": "M1-02C",
            },
        )

    def _base_findings(
        self,
        *,
        seed: CodeWorkerSessionSeed,
        input_report: QueryInputProcessingReport,
        context_snapshot: ContextAssemblySnapshot | None,
        turn_lifecycle: TurnLifecycleProjection,
        replay_plan: SessionReplayPlan | None,
        control_state: QueryControlState,
        checkpoint: QueryCheckpointRecord,
        disabled: bool,
    ) -> Iterable[QuerySessionIntegrationFinding]:
        if disabled:
            yield _finding(
                "query_entry_packet_disabled",
                QuerySessionIntegrationSeverity.BLOCKER,
                QuerySessionIntegrationSurface.QUERY_ENTRY,
                "QueryEntryPacketBuilder was disabled by request constraints.",
            )
        yield _pass_or_block(
            code="session_seed_ready",
            ok=seed.ok,
            surface=QuerySessionIntegrationSurface.SESSION_STORE,
            message="CodeWorkerSessionStore owns the query lifecycle seed.",
            metadata=seed.to_dict(include_text=False),
        )
        yield _pass_or_block(
            code="input_processor_ready",
            ok=input_report.ok,
            surface=QuerySessionIntegrationSurface.INPUT_PROCESSOR,
            message="QueryInputProcessor produced accepted input records for query entry.",
            metadata=input_report.to_dict(),
        )
        context_ok = context_snapshot is not None and context_snapshot.ok
        yield _pass_or_block(
            code="context_snapshot_ready",
            ok=context_ok,
            surface=QuerySessionIntegrationSurface.CONTEXT_ASSEMBLY,
            message="ContextAssemblyRuntime snapshot is bound to query entry.",
            metadata=context_snapshot.to_dict(include_text=False) if context_snapshot else {},
        )
        yield _pass_or_block(
            code="turn_lifecycle_ready",
            ok=turn_lifecycle.ok,
            surface=QuerySessionIntegrationSurface.QUERY_ENTRY,
            message="TurnLifecycleRuntime bound input/context/tool-plan to query entry.",
            metadata=turn_lifecycle.to_dict(),
        )
        if replay_plan is not None:
            yield _pass_or_block(
                code="resume_replay_ready",
                ok=replay_plan.ok,
                surface=QuerySessionIntegrationSurface.RESUME,
                message="Replay plan restored parent/context state for resume.",
                metadata=replay_plan.to_dict(include_raw_context=False),
            )
        if control_state.blocks_query:
            yield _finding(
                "control_state_blocks_query",
                QuerySessionIntegrationSeverity.BLOCKER,
                QuerySessionIntegrationSurface.CONTROL_STATE,
                "Interrupt, cancel or stale context state prevents query entry.",
                metadata=control_state.to_dict(),
            )
        else:
            yield _finding(
                "control_state_allows_query",
                QuerySessionIntegrationSeverity.PASS,
                QuerySessionIntegrationSurface.CONTROL_STATE,
                "Control state permits query entry.",
                metadata=control_state.to_dict(),
            )
        if not checkpoint.ok:
            yield _finding(
                "checkpoint_blocked",
                QuerySessionIntegrationSeverity.BLOCKER,
                QuerySessionIntegrationSurface.CHECKPOINT,
                "Pre-query checkpoint could not be prepared.",
                metadata=checkpoint.to_dict(),
            )


class QuerySessionIntegrationRuntime:
    """Main integration runtime for M1-02B-02."""

    def __init__(
        self,
        *,
        store: CodeWorkerSessionStore,
        artifact_store: LocalArtifactStore | None = None,
        source: QuerySourceMetadata | None = None,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store
        self.source = source or default_query_session_integration_source()
        self.control_runtime = QuerySessionControlRuntime()
        self.checkpoint_runtime = QueryCheckpointRuntime(artifact_store)
        self.packet_builder = QueryEntryPacketBuilder()

    def prepare(
        self,
        *,
        request: Any,
        seed: CodeWorkerSessionSeed,
        input_report: QueryInputProcessingReport,
        context_snapshot: ContextAssemblySnapshot | None,
        tool_specs: Sequence[Any],
        query_turns: Sequence[Sequence[Mapping[str, Any]]],
        turn_lifecycle: TurnLifecycleProjection,
        foundation_audit: FoundationAuditReport | None = None,
        acceptance_report: SessionAcceptanceReport | None = None,
        lifecycle_report: SessionLifecycleStateReport | None = None,
        replay_plan: SessionReplayPlan | None = None,
    ) -> QuerySessionIntegrationReport:
        constraints = _as_mapping(getattr(request, "constraints", {}))
        control = self.control_runtime.resolve(
            request=request,
            seed=seed,
            context_snapshot=context_snapshot,
            replay_plan=replay_plan,
        )
        checkpoint = self.checkpoint_runtime.build(
            request=request,
            seed=seed,
            context_snapshot=context_snapshot,
            control_state=control,
            replay_plan=replay_plan,
            disabled=_truthy(constraints.get("disable_query_session_checkpoint")),
        )
        packet = self.packet_builder.build(
            request=request,
            seed=seed,
            input_report=input_report,
            context_snapshot=context_snapshot,
            tool_specs=tool_specs,
            query_turns=query_turns,
            turn_lifecycle=turn_lifecycle,
            control_state=control,
            checkpoint=checkpoint,
            replay_plan=replay_plan,
            disabled=_truthy(constraints.get("disable_query_entry_packet")),
        )
        findings = [
            *packet.findings,
            *integration_cross_component_findings(
                packet=packet,
                foundation_audit=foundation_audit,
                acceptance_report=acceptance_report,
                lifecycle_report=lifecycle_report,
            ),
        ]
        store_receipt = self.store.append_query_entry_report(
            session_id=seed.session_id,
            worker_request_id=seed.worker_request_id,
            run_id=seed.run_id,
            task_id=seed.task_id,
            control=control.to_dict(),
            checkpoint=checkpoint.to_dict(),
            packet=packet.to_dict(include_text=False),
            report={
                "report_id": "",
                "status": str(status_from_findings(findings)),
                "ok": packet.ok and not any(finding.blocking for finding in findings),
                "session_id": seed.session_id,
                "worker_request_id": seed.worker_request_id,
                "first_blocker_code": next((finding.code for finding in findings if finding.blocking), ""),
                "blocker_count": sum(1 for finding in findings if finding.blocking),
            },
            disabled=_truthy(constraints.get("disable_query_entry_store")),
        )
        findings.append(
            _pass_or_block(
                code="query_entry_store_append_ready",
                ok=store_receipt.ok,
                surface=QuerySessionIntegrationSurface.SESSION_STORE,
                message="CodeWorkerSessionStore persisted query control, checkpoint and entry packet before QueryEngine.",
                metadata=store_receipt.to_dict(),
            )
        )
        status = status_from_findings(findings)
        if not packet.ok:
            status = QuerySessionIntegrationStatus.BLOCKED
        if not store_receipt.ok:
            status = QuerySessionIntegrationStatus.BLOCKED
            event_phases = ("query_session_integration", "query_control_state", "query_entry_store_blocked")
        else:
            event_phases = tuple(event_phase_plan(packet, control_state=control, checkpoint=checkpoint))
        return QuerySessionIntegrationReport(
            report_id=new_id("qint"),
            status=status,
            session_id=seed.session_id,
            worker_request_id=seed.worker_request_id,
            packet=packet,
            control=control,
            checkpoint=checkpoint,
            findings=tuple(findings),
            event_phases=event_phases,
            metadata={
                "source": self.source.to_dict(),
                "store_root": str(self.store.root),
                "store_receipt": store_receipt.to_dict(),
                "artifact_store": str(self.artifact_store.root) if self.artifact_store else "",
            },
        )

    def events_for_report(
        self,
        report: QuerySessionIntegrationReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
    ) -> list[EventRecord]:
        events: list[EventRecord] = []
        for phase in report.event_phases:
            payload = self._payload_for_phase(report, phase)
            events.append(
                EventRecord(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    event_type=EventType.AGENT_MESSAGE,
                    payload={"query_session": payload},
                )
            )
        return events

    def _payload_for_phase(self, report: QuerySessionIntegrationReport, phase: str) -> dict[str, Any]:
        packet = report.packet
        base = {
            "session_id": report.session_id,
            "worker_request_id": report.worker_request_id,
            "phase": phase,
            "report_id": report.report_id,
            "packet_id": packet.packet_id,
            "ok": report.ok,
            "status": str(report.status),
            "route": str(packet.route),
            "block_reason": str(packet.block_reason),
        }
        if phase == "query_session_integration":
            return {
                **base,
                "blocker_count": report.blocker_count,
                "warning_count": report.warning_count,
                "handoff_ok": packet.handoff.ok,
                "event_phases": list(report.event_phases),
            }
        if phase == "query_entry_packet_ready":
            return {
                **base,
                "message_count": packet.message_count,
                "context_fingerprint": packet.context_fingerprint,
                "parent_uuid": packet.parent_uuid,
                "permission_mode": packet.permission.permission_mode,
                "planned_tool_names": list(packet.tools.planned_tool_names),
            }
        if phase == "query_started":
            return {
                **base,
                "message_count": packet.message_count,
                "context_fingerprint": packet.context_fingerprint,
                "parent_uuid": packet.parent_uuid,
                "handoff_target": packet.handoff.target_unit,
            }
        if phase in {"query_interrupted", "query_cancelled", "query_stale_context_blocked"}:
            return {
                **base,
                "control": report.control.to_dict(),
                "stop_reason": str(StopReason.USER_CANCELLED),
            }
        if phase == "query_entry_store_blocked":
            return {
                **base,
                "store_receipt": to_jsonable(report.metadata.get("store_receipt", {})),
                "first_blocker_code": report.first_blocker_code,
            }
        if phase == "query_resume_restored":
            return {
                **base,
                "resume": packet.resume.to_dict(),
                "parent_uuid": packet.resume.parent_uuid,
                "resume_token": packet.resume.resume_token,
            }
        if phase == "query_session_checkpoint":
            return {
                **base,
                "checkpoint": report.checkpoint.to_dict(),
                "artifact_id": report.checkpoint.artifact.artifact_id if report.checkpoint.artifact else "",
            }
        if phase == "query_downstream_handoff_ready":
            return {
                **base,
                "handoff": packet.handoff.to_dict(),
            }
        return base


def build_context_envelope(
    snapshot: ContextAssemblySnapshot | None,
    *,
    source: QuerySourceMetadata,
) -> QueryEntryContextEnvelope:
    if snapshot is None:
        return QueryEntryContextEnvelope(
            snapshot_id="",
            fingerprint="",
            status="missing",
            selected_block_count=0,
            active_chars=0,
            messages=(),
            source=source,
            metadata={"error": "context_snapshot_missing"},
        )
    messages = []
    for index, raw in enumerate(snapshot.to_messages(), start=1):
        metadata = _as_mapping(raw.get("metadata"))
        messages.append(
            QueryEntryMessage(
                message_id=new_id("qemsg"),
                role=str(raw.get("role") or "system"),
                content=str(raw.get("content") or ""),
                source="context_snapshot",
                sequence=index,
                context_block_id=str(metadata.get("context_block_id") or ""),
                input_id=str(metadata.get("input_id") or ""),
                metadata=dict(metadata),
            )
        )
    return QueryEntryContextEnvelope(
        snapshot_id=snapshot.snapshot_id,
        fingerprint=snapshot.fingerprint,
        status=str(snapshot.status),
        selected_block_count=len(snapshot.selected_blocks),
        active_chars=snapshot.active_chars,
        messages=tuple(messages),
        source=snapshot.source,
        metadata={
            "source_path": snapshot.source.source_path,
            "target_path": snapshot.source.target_path,
            "block_kinds": sorted({str(block.kind) for block in snapshot.selected_blocks}),
        },
    )


def build_tool_envelope(
    tool_specs: Sequence[Any],
    *,
    query_turns: Sequence[Sequence[Mapping[str, Any]]],
    input_report: QueryInputProcessingReport,
) -> QueryEntryToolEnvelope:
    tool_names = tuple(str(getattr(tool, "name", "")) for tool in tool_specs if getattr(tool, "name", ""))
    read_only = tuple(
        str(getattr(tool, "name", ""))
        for tool in tool_specs
        if getattr(tool, "name", "") and _as_mapping(getattr(tool, "metadata", {})).get("read_only") == "true"
    )
    mutating = tuple(name for name in tool_names if name not in set(read_only))
    planned = tuple(
        dict.fromkeys(
            str(step.get("tool_name") or step.get("tool") or "")
            for turn in query_turns
            for step in turn
            if isinstance(step, Mapping) and (step.get("tool_name") or step.get("tool"))
        )
    )
    command_allowed = []
    for record in input_report.accepted_records:
        allowed = _as_mapping(record.metadata).get("allowed_tools")
        if isinstance(allowed, Sequence) and not isinstance(allowed, (str, bytes)):
            command_allowed.extend(str(item) for item in allowed)
    return QueryEntryToolEnvelope(
        tool_names=tool_names,
        planned_tool_names=planned,
        read_only_tool_names=read_only,
        mutating_tool_names=mutating,
        allowed_tools=tuple(dict.fromkeys(command_allowed)),
        denied_tools=(),
        metadata={
            "source_path": "src/tools.ts",
            "target_path": "packages/runtime/zyra_runtime/tools.py",
            "visible_tool_pool_stable": True,
        },
    )


def build_permission_envelope(request: Any, *, input_report: QueryInputProcessingReport) -> QueryEntryPermissionEnvelope:
    constraints = _as_mapping(getattr(request, "constraints", {}))
    mode = str(constraints.get("permission_mode") or "workspace")
    command_allowed = []
    for record in input_report.accepted_records:
        if record.kind == QueryInputKind.SLASH_COMMAND and record.command_name:
            command_allowed.append(record.command_name)
    return QueryEntryPermissionEnvelope(
        permission_mode=mode,
        requested_permission_mode=str(constraints.get("requested_permission_mode") or mode),
        command_scoped_allowed_tools=tuple(dict.fromkeys(command_allowed)),
        metadata={
            "source_path": "src/Tool.ts",
            "permission_state_source": "ContextAssemblyRuntime.permission_state_block",
        },
    )


def build_resume_envelope(
    replay_plan: SessionReplayPlan | None,
    *,
    control_state: QueryControlState,
) -> QueryEntryResumeEnvelope:
    if replay_plan is None:
        return QueryEntryResumeEnvelope(requested=False, ok=True)
    return QueryEntryResumeEnvelope(
        requested=True,
        ok=replay_plan.ok,
        session_id=replay_plan.session_id,
        worker_request_id=replay_plan.worker_request_id,
        parent_uuid=control_state.restored_parent_uuid,
        resume_token=control_state.restored_resume_token,
        context_fingerprint=control_state.restored_context_fingerprint,
        replay_message_count=len(replay_plan.replay_messages),
        replay_actions=tuple(str(action) for action in replay_plan.actions),
        warnings=tuple(finding.code for finding in replay_plan.findings if not finding.blocking),
        metadata=replay_plan.metadata,
    )


def build_downstream_handoff(
    *,
    messages: Sequence[QueryEntryMessage],
    context: QueryEntryContextEnvelope,
    control: QueryControlState,
    permission: QueryEntryPermissionEnvelope,
    tools: QueryEntryToolEnvelope,
    status: QuerySessionIntegrationStatus,
) -> QueryEntryDownstreamHandoff:
    return QueryEntryDownstreamHandoff(
        handoff_id=new_id("qhand"),
        owner_unit="M1-02B",
        target_unit="M1-02C",
        packet_schema="zyra.claude.query_entry_packet.v1",
        runtime_ports=(
            "QueryEntryPacket.messages",
            "QueryEntryPacket.context",
            "QueryEntryPacket.permission",
            "QueryEntryPacket.tools",
            "QueryEntryPacket.control",
        ),
        event_phases=(
            "query_entry_packet_ready",
            "query_started",
            "query_downstream_handoff_ready",
        ),
        messages_ready=bool(messages),
        context_ready=context.ok,
        control_ready=control.ok and not control.blocks_query,
        permission_ready=permission.ok,
        tool_loop_ready=tools.tool_count > 0 and status != QuerySessionIntegrationStatus.BLOCKED,
        metadata={
            "source_graph_batches": [
                "batch-01-query-session-context",
                "batch-02-query-tool-loop",
                "batch-08-api-streaming-retry-client",
            ],
            "handoff_claim": "02C can consume this packet without rebuilding session/context.",
        },
    )


def route_from_turn_lifecycle(
    projection: TurnLifecycleProjection,
    *,
    control_state: QueryControlState,
) -> QueryEntryRoute:
    if control_state.blocks_query:
        return QueryEntryRoute.BLOCKED
    routes = {str(seed.route) for seed in projection.seeds}
    if str(TurnSeedRoute.CONTROL_COMMAND) in routes:
        return QueryEntryRoute.CONTROL_COMMAND
    if str(TurnSeedRoute.SHELL_TOOL) in routes:
        return QueryEntryRoute.SHELL_TOOL
    if str(TurnSeedRoute.REPLAY_RESTORE) in routes:
        return QueryEntryRoute.REPLAY_RESTORE
    if str(TurnSeedRoute.STRUCTURED_TOOL_LOOP) in routes:
        return QueryEntryRoute.TOOL_LOOP
    if str(TurnSeedRoute.QUERY_ENGINE) in routes:
        return QueryEntryRoute.MODEL_QUERY
    return QueryEntryRoute.MODEL_QUERY if projection.ok else QueryEntryRoute.BLOCKED


def block_reason_for(
    *,
    disabled: bool,
    seed: CodeWorkerSessionSeed,
    input_report: QueryInputProcessingReport,
    context_snapshot: ContextAssemblySnapshot | None,
    replay_plan: SessionReplayPlan | None,
    control_state: QueryControlState,
    messages: Sequence[QueryEntryMessage],
) -> QueryEntryBlockReason:
    if disabled:
        return QueryEntryBlockReason.DISABLED
    if not seed.ok:
        return QueryEntryBlockReason.FOUNDATION_BLOCKED
    if not input_report.ok:
        return QueryEntryBlockReason.INPUT_BLOCKED
    if context_snapshot is None or not context_snapshot.ok:
        return QueryEntryBlockReason.CONTEXT_BLOCKED
    if replay_plan is not None and not replay_plan.ok:
        return QueryEntryBlockReason.REPLAY_BLOCKED
    if control_state.pending_cancel:
        return QueryEntryBlockReason.CONTROL_CANCELLED
    if control_state.pending_interrupt:
        return QueryEntryBlockReason.CONTROL_INTERRUPTED
    if control_state.stale_context:
        return QueryEntryBlockReason.STALE_CONTEXT
    if not messages:
        return QueryEntryBlockReason.EMPTY_MESSAGES
    return QueryEntryBlockReason.NONE


def integration_cross_component_findings(
    *,
    packet: QueryEntryPacket,
    foundation_audit: FoundationAuditReport | None,
    acceptance_report: SessionAcceptanceReport | None,
    lifecycle_report: SessionLifecycleStateReport | None,
) -> Iterable[QuerySessionIntegrationFinding]:
    if foundation_audit is not None:
        yield _pass_or_block(
            code="foundation_audit_ready",
            ok=foundation_audit.ok,
            surface=QuerySessionIntegrationSurface.SESSION_STORE,
            message="Session foundation audit passed before query entry integration.",
            metadata=foundation_audit.to_dict(),
        )
    if acceptance_report is not None:
        yield _pass_or_block(
            code="pre_query_acceptance_ready",
            ok=acceptance_report.ok,
            surface=QuerySessionIntegrationSurface.QUERY_ENTRY,
            message="Pre-query acceptance gate passed before query entry integration.",
            metadata=acceptance_report.to_dict(),
        )
    if lifecycle_report is not None:
        if lifecycle_report.ok:
            yield _finding(
                "pre_query_lifecycle_ready",
                QuerySessionIntegrationSeverity.PASS,
                QuerySessionIntegrationSurface.EVENT_STREAM,
                "Pre-query event lifecycle is observable before query entry integration.",
                metadata=lifecycle_report.to_dict(),
            )
        else:
            yield _finding(
                "pre_query_lifecycle_degraded",
                QuerySessionIntegrationSeverity.WARNING,
                QuerySessionIntegrationSurface.EVENT_STREAM,
                "Pre-query lifecycle report is degraded; query entry event-flow and state graph gates remain authoritative.",
                metadata=lifecycle_report.to_dict(),
            )
    if packet.handoff.ok:
        yield _finding(
            "downstream_handoff_ready",
            QuerySessionIntegrationSeverity.PASS,
            QuerySessionIntegrationSurface.DOWNSTREAM_HANDOFF,
            "Query entry packet is ready for M1-02C tool-loop handoff.",
            metadata=packet.handoff.to_dict(),
        )
    else:
        yield _finding(
            "downstream_handoff_blocked",
            QuerySessionIntegrationSeverity.BLOCKER,
            QuerySessionIntegrationSurface.DOWNSTREAM_HANDOFF,
            "Query entry packet is not safe for M1-02C tool-loop handoff.",
            metadata=packet.handoff.to_dict(),
        )


def event_phase_plan(
    packet: QueryEntryPacket,
    *,
    control_state: QueryControlState,
    checkpoint: QueryCheckpointRecord,
) -> Iterable[str]:
    yield "query_session_integration"
    yield "query_control_state"
    if packet.resume.requested:
        yield "query_resume_restored"
    if checkpoint.status != QueryCheckpointStatus.NOT_REQUESTED:
        yield "query_session_checkpoint"
    yield "query_entry_packet_ready"
    if control_state.pending_interrupt:
        yield "query_interrupted"
        return
    if control_state.pending_cancel:
        yield "query_cancelled"
        return
    if control_state.stale_context:
        yield "query_stale_context_blocked"
        return
    if packet.ok:
        yield "query_started"
        yield "query_downstream_handoff_ready"


def _merge_entry_messages(
    context_messages: Sequence[QueryEntryMessage],
    *,
    replay_plan: SessionReplayPlan | None,
    input_report: QueryInputProcessingReport,
    parent_uuid: str,
) -> Iterable[QueryEntryMessage]:
    sequence = 0
    seen_input_ids: set[str] = set()
    for message in context_messages:
        sequence += 1
        if message.input_id:
            seen_input_ids.add(message.input_id)
        yield QueryEntryMessage(
            message_id=message.message_id,
            role=message.role,
            content=message.content,
            source=message.source,
            sequence=sequence,
            parent_uuid=parent_uuid,
            input_id=message.input_id,
            context_block_id=message.context_block_id,
            replayed=message.replayed,
            metadata=message.metadata,
        )
    if replay_plan is not None:
        for raw in replay_plan.replay_messages:
            metadata = _as_mapping(raw.get("metadata"))
            input_id = str(metadata.get("input_id") or "")
            if input_id and input_id in seen_input_ids:
                continue
            sequence += 1
            if input_id:
                seen_input_ids.add(input_id)
            yield QueryEntryMessage(
                message_id=new_id("qemsg"),
                role=str(raw.get("role") or "user"),
                content=str(raw.get("content") or ""),
                source="session_replay",
                sequence=sequence,
                parent_uuid=parent_uuid,
                input_id=input_id,
                context_block_id=str(metadata.get("context_block_id") or ""),
                replayed=True,
                metadata=dict(metadata),
            )
    for record in input_report.accepted_records:
        if record.input_id in seen_input_ids:
            continue
        sequence += 1
        seen_input_ids.add(record.input_id)
        yield QueryEntryMessage(
            message_id=new_id("qemsg"),
            role=record.role,
            content=record.normalized_text,
            source="input_processor",
            sequence=sequence,
            parent_uuid=parent_uuid,
            input_id=record.input_id,
            metadata={
                "kind": str(record.kind),
                "disposition": str(record.disposition),
                "risk": str(record.risk),
                "command_name": record.command_name,
            },
        )


def _control_actions_from_constraints(
    constraints: Mapping[str, Any],
    *,
    replay_plan: SessionReplayPlan | None,
) -> Iterable[QueryControlActionKind]:
    if replay_plan is not None:
        yield QueryControlActionKind.RESUME
    if _truthy(constraints.get("interrupt")) or _truthy(constraints.get("interrupt_session")):
        yield QueryControlActionKind.INTERRUPT
    if _truthy(constraints.get("cancel")) or _truthy(constraints.get("cancel_session")):
        yield QueryControlActionKind.CANCEL
    if _truthy(constraints.get("checkpoint")) or _truthy(constraints.get("checkpoint_session")):
        yield QueryControlActionKind.CHECKPOINT
    if _truthy(constraints.get("rewind")) or _truthy(constraints.get("rewind_session")):
        yield QueryControlActionKind.REWIND


def _parent_uuid_from_replay(plan: SessionReplayPlan | None) -> str:
    if plan is None:
        return ""
    if plan.attach_state is not None and plan.attach_state.resume_token:
        parts = plan.attach_state.resume_token.split(":")
        if len(parts) >= 2:
            return parts[1]
    if plan.window.projections:
        return plan.window.projections[-1].record_id
    return ""


def _resume_token_from_replay(plan: SessionReplayPlan | None) -> str:
    if plan is None:
        return ""
    if plan.attach_state is not None:
        return plan.attach_state.resume_token
    cursors = plan.window.cursors()
    return cursors[-1].resume_token if cursors else ""


def _context_fingerprint_from_replay(plan: SessionReplayPlan | None) -> str:
    if plan is None or plan.context_state is None:
        return ""
    return plan.context_state.fingerprint


def _last_sequence_from_replay(plan: SessionReplayPlan | None) -> int:
    if plan is None:
        return 0
    return plan.window.end_sequence


def _parent_uuid_from_context(snapshot: ContextAssemblySnapshot | None) -> str:
    if snapshot is None:
        return ""
    return f"{snapshot.session_id}:{snapshot.snapshot_id}"


def _pass_or_block(
    *,
    code: str,
    ok: bool,
    surface: QuerySessionIntegrationSurface,
    message: str,
    metadata: Mapping[str, Any] | None = None,
) -> QuerySessionIntegrationFinding:
    return _finding(
        code,
        QuerySessionIntegrationSeverity.PASS if ok else QuerySessionIntegrationSeverity.BLOCKER,
        surface,
        message,
        metadata=metadata,
    )


def _finding(
    code: str,
    severity: QuerySessionIntegrationSeverity,
    surface: QuerySessionIntegrationSurface,
    message: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> QuerySessionIntegrationFinding:
    return QuerySessionIntegrationFinding(
        code=code,
        severity=severity,
        surface=surface,
        message=message,
        metadata=dict(metadata or {}),
    )


def status_from_findings(findings: Sequence[QuerySessionIntegrationFinding]) -> QuerySessionIntegrationStatus:
    if any(finding.blocking for finding in findings):
        return QuerySessionIntegrationStatus.BLOCKED
    if any(finding.severity == QuerySessionIntegrationSeverity.WARNING for finding in findings):
        return QuerySessionIntegrationStatus.DEGRADED
    return QuerySessionIntegrationStatus.READY


def default_query_entry_source() -> QuerySourceMetadata:
    return QuerySourceMetadata(
        source_repo="claude-code-best",
        source_path="src/QueryEngine.ts",
        target_path="packages/runtime/zyra_runtime/claude_query_session_integration.py",
        source_kind=QuerySourceKind.CLAUDE_QUERY_ENGINE,
        source_graph_batch="batch-01-query-session-context",
        source_graph_document="source-graphs/claude-code-best/batch-01-query-session-context.md",
        upstream_signals=(
            "submitMessage",
            "processUserInput",
            "ProcessUserInputContext",
            "query({ messages, systemPrompt, toolUseContext })",
        ),
        notes=(
            "QueryEntryPacket is the explicit boundary from pre-query session lifecycle to QueryEngine/tool loop.",
            "M1-02C consumes this packet instead of rebuilding context/session state.",
        ),
    )


def default_query_session_integration_source() -> QuerySourceMetadata:
    return QuerySourceMetadata(
        source_repo="claude-code-best",
        source_path="src/QueryEngine.ts",
        target_path="packages/runtime/zyra_runtime/claude_query_session_integration.py",
        source_kind=QuerySourceKind.CLAUDE_QUERY_ENGINE,
        source_graph_batch="batch-01-query-session-context",
        source_graph_document="source-graphs/claude-code-best/batch-01-query-session-context.md",
        upstream_signals=(
            "mutableMessages owner",
            "abortController interrupt",
            "transcript prewrite",
            "query entry handoff",
            "session resume",
        ),
        notes=(
            "Integration runtime owns the pre-query control state and event handoff.",
            "Disabling query entry packet blocks the worker before stream_request_start.",
        ),
    )


def query_session_integration_metadata(report: QuerySessionIntegrationReport | None) -> dict[str, str]:
    if report is None:
        return {
            "query_session_integration_ok": "",
            "query_entry_ok": "",
        }
    return report.metadata_values()


def render_query_session_integration_markdown(report: QuerySessionIntegrationReport) -> str:
    lines = [
        "# Query Session Integration",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- report_id: `{report.report_id}`",
        f"- session_id: `{report.session_id}`",
        f"- worker_request_id: `{report.worker_request_id}`",
        f"- packet_id: `{report.packet.packet_id}`",
        f"- route: `{report.packet.route}`",
        f"- block_reason: `{report.packet.block_reason}`",
        f"- message_count: `{report.packet.message_count}`",
        f"- parent_uuid: `{report.packet.parent_uuid}`",
        f"- context_fingerprint: `{report.packet.context_fingerprint}`",
        f"- handoff_ok: `{str(report.packet.handoff.ok).lower()}`",
        "",
        "## Event Phases",
        "",
        *(f"- `{phase}`" for phase in report.event_phases),
        "",
        "## Control",
        "",
        f"- status: `{report.control.status}`",
        f"- actions: `{', '.join(report.control.action_names)}`",
        f"- blocks_query: `{str(report.control.blocks_query).lower()}`",
        f"- pending_interrupt: `{str(report.control.pending_interrupt).lower()}`",
        f"- pending_cancel: `{str(report.control.pending_cancel).lower()}`",
        f"- checkpoint_requested: `{str(report.control.checkpoint_requested).lower()}`",
        f"- resume_requested: `{str(report.control.resume_requested).lower()}`",
        "",
        "## Findings",
        "",
    ]
    if report.findings:
        lines.extend(
            f"- `{finding.severity}` `{finding.surface}` `{finding.code}` {finding.message}"
            for finding in report.findings
        )
    else:
        lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def query_entry_packet_from_payload(payload: Mapping[str, Any]) -> QueryEntryPacket:
    # Lightweight loader for API/tests. It preserves the shape needed by M1-02C
    # without reconstructing full upstream runtime objects.
    source_payload = _as_mapping(payload.get("source"))
    source = QuerySourceMetadata(
        source_repo=str(source_payload.get("source_repo") or "claude-code-best"),
        source_path=str(source_payload.get("source_path") or "src/QueryEngine.ts"),
        target_path=str(source_payload.get("target_path") or "packages/runtime/zyra_runtime/claude_query_session_integration.py"),
        source_kind=QuerySourceKind(str(source_payload.get("source_kind") or QuerySourceKind.CLAUDE_QUERY_ENGINE)),
    )
    context_payload = _as_mapping(payload.get("context"))
    messages = tuple(
        QueryEntryMessage(
            message_id=str(item.get("message_id") or new_id("qemsg")),
            role=str(item.get("role") or "user"),
            content=str(item.get("content") or ""),
            source=str(item.get("source") or "payload"),
            sequence=_safe_int(item.get("sequence"), default=index),
            parent_uuid=str(item.get("parent_uuid") or ""),
            input_id=str(item.get("input_id") or ""),
            context_block_id=str(item.get("context_block_id") or ""),
            replayed=item.get("replayed") is True,
            metadata=dict(item.get("metadata") or {}),
        )
        for index, item in enumerate(payload.get("messages", []), start=1)
        if isinstance(item, Mapping)
    )
    context = QueryEntryContextEnvelope(
        snapshot_id=str(context_payload.get("snapshot_id") or ""),
        fingerprint=str(context_payload.get("fingerprint") or ""),
        status=str(context_payload.get("status") or ""),
        selected_block_count=_safe_int(context_payload.get("selected_block_count"), default=0),
        active_chars=_safe_int(context_payload.get("active_chars"), default=0),
        messages=messages,
        source=source,
        metadata=dict(context_payload.get("metadata") or {}),
    )
    tool_payload = _as_mapping(payload.get("tools"))
    tools = QueryEntryToolEnvelope(
        tool_names=tuple(str(item) for item in tool_payload.get("tool_names", []) if item),
        planned_tool_names=tuple(str(item) for item in tool_payload.get("planned_tool_names", []) if item),
        read_only_tool_names=tuple(str(item) for item in tool_payload.get("read_only_tool_names", []) if item),
        mutating_tool_names=tuple(str(item) for item in tool_payload.get("mutating_tool_names", []) if item),
        allowed_tools=tuple(str(item) for item in tool_payload.get("allowed_tools", []) if item),
        denied_tools=tuple(str(item) for item in tool_payload.get("denied_tools", []) if item),
        metadata=dict(tool_payload.get("metadata") or {}),
    )
    permission_payload = _as_mapping(payload.get("permission"))
    permission = QueryEntryPermissionEnvelope(
        permission_mode=str(permission_payload.get("permission_mode") or "workspace"),
        requested_permission_mode=str(permission_payload.get("requested_permission_mode") or ""),
        command_scoped_allowed_tools=tuple(
            str(item) for item in permission_payload.get("command_scoped_allowed_tools", []) if item
        ),
        policy_owner=str(permission_payload.get("policy_owner") or "ToolPermissionPolicy"),
        denial_count=_safe_int(permission_payload.get("denial_count"), default=0),
        metadata=dict(permission_payload.get("metadata") or {}),
    )
    control = QueryControlState(
        state_id=str(_as_mapping(payload.get("control")).get("state_id") or new_id("qctlstate")),
        status=QuerySessionIntegrationStatus(str(_as_mapping(payload.get("control")).get("status") or QuerySessionIntegrationStatus.BLOCKED)),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        active_actions=(),
        transitions=(),
    )
    checkpoint = QueryCheckpointRecord(
        checkpoint_id=str(_as_mapping(payload.get("checkpoint")).get("checkpoint_id") or new_id("qchk")),
        status=QueryCheckpointStatus(str(_as_mapping(payload.get("checkpoint")).get("status") or QueryCheckpointStatus.NOT_REQUESTED)),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        context_fingerprint=str(payload.get("context_fingerprint") or ""),
        parent_uuid=str(payload.get("parent_uuid") or ""),
        resume_token="",
        sequence=0,
    )
    resume = QueryEntryResumeEnvelope(requested=False, ok=True)
    handoff = QueryEntryDownstreamHandoff(
        handoff_id=str(_as_mapping(payload.get("handoff")).get("handoff_id") or new_id("qhand")),
        owner_unit="M1-02B",
        target_unit="M1-02C",
        packet_schema="zyra.claude.query_entry_packet.v1",
        runtime_ports=(),
        event_phases=(),
        messages_ready=bool(messages),
        context_ready=context.ok,
        control_ready=control.ok,
        permission_ready=permission.ok,
        tool_loop_ready=bool(tools.tool_names),
    )
    return QueryEntryPacket(
        packet_id=str(payload.get("packet_id") or new_id("qentry")),
        status=QuerySessionIntegrationStatus(str(payload.get("status") or QuerySessionIntegrationStatus.BLOCKED)),
        route=QueryEntryRoute(str(payload.get("route") or QueryEntryRoute.BLOCKED)),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        parent_uuid=str(payload.get("parent_uuid") or ""),
        context_fingerprint=str(payload.get("context_fingerprint") or ""),
        source=source,
        context=context,
        tools=tools,
        permission=permission,
        control=control,
        resume=resume,
        checkpoint=checkpoint,
        handoff=handoff,
        messages=messages,
        block_reason=QueryEntryBlockReason(str(payload.get("block_reason") or QueryEntryBlockReason.NONE)),
        metadata=dict(payload.get("metadata") or {}),
    )


def _hash_payload(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
