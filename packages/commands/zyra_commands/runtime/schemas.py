from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, fields, is_dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import AgentRole, ArtifactRef, new_id, now_iso


class CommandKind(StrEnum):
    PROMPT = "prompt"
    CONTROL = "control"
    UI_PROJECTION = "ui_projection"


class CommandSourceKind(StrEnum):
    ZYRA_BUILTIN = "zyra_builtin"
    CLAUDE_MIGRATED = "claude_migrated"
    OPENCODE_ADAPTED = "opencode_adapted"
    SKILL = "skill"
    PLUGIN = "plugin"
    MCP = "mcp"
    PROJECT = "project"


class CommandMutationScope(StrEnum):
    READ_ONLY = "read_only"
    SESSION = "session"
    CONTEXT = "context"
    PERMISSION = "permission"
    MCP = "mcp"
    SKILL_PLUGIN = "skill_plugin"
    SUBAGENT = "subagent"
    TASK_GRAPH = "task_graph"
    PROVIDER = "provider"
    ARTIFACT = "artifact"


class CommandConcurrency(StrEnum):
    READ_ONLY_PARALLEL = "read_only_parallel"
    SESSION_SERIAL = "session_serial"
    INTERRUPTING = "interrupting"


class CommandOrigin(StrEnum):
    API = "api"
    CLI = "cli"
    WEB = "web"
    SDK = "sdk"
    REMOTE = "remote"
    AGENT = "agent"
    SYSTEM = "system"


class CommandStatus(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.DENIED,
            self.CANCELLED,
            self.CONFLICT,
        }


class QueuePriority(StrEnum):
    NOW = "now"
    NEXT = "next"
    LATER = "later"


class QueueEntryKind(StrEnum):
    PROMPT = "prompt"
    CONTROL = "control"
    PERMISSION_REPLY = "permission_reply"
    TASK_NOTIFICATION = "task_notification"


class QueueEntryStatus(StrEnum):
    QUEUED = "queued"
    RESERVED = "reserved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class RuntimeGuardState(StrEnum):
    IDLE = "idle"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    INTERRUPTING = "interrupting"


class ReplyMode(StrEnum):
    INLINE = "inline"
    ASYNC = "async"
    STREAM = "stream"


class ControlErrorCode(StrEnum):
    INVALID_SCHEMA = "invalid_schema"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNKNOWN_COMMAND = "unknown_command"
    REGISTRY_STALE = "registry_stale"
    COMMAND_DISABLED = "command_disabled"
    COMMAND_UNAVAILABLE = "command_unavailable"
    UNSAFE_ORIGIN = "unsafe_origin"
    ARGUMENT_INVALID = "argument_invalid"
    REVISION_CONFLICT = "revision_conflict"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    QUEUE_FULL = "queue_full"
    ALREADY_DEQUEUED = "already_dequeued"
    PERMISSION_DENIED = "permission_denied"
    STATE_OWNER_UNAVAILABLE = "state_owner_unavailable"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    HANDLER_FAILED = "handler_failed"
    INVARIANT_VIOLATION = "invariant_violation"
    SIDE_QUESTION_TOOL_VIOLATION = "side_question_tool_violation"


@dataclass(frozen=True, slots=True)
class CommandSource:
    kind: CommandSourceKind
    source_id: str
    version: str = "1"
    trust: str = "product"
    source_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommandSource":
        return cls(
            kind=_enum(CommandSourceKind, raw.get("kind"), CommandSourceKind.PROJECT),
            source_id=str(raw.get("source_id") or raw.get("id") or ""),
            version=str(raw.get("version") or "1"),
            trust=str(raw.get("trust") or "project"),
            source_paths=tuple(str(item) for item in raw.get("source_paths") or ()),
        )


@dataclass(frozen=True, slots=True)
class CommandAvailability:
    enabled: bool = True
    feature: str = ""
    session_modes: tuple[str, ...] = ()
    origins: tuple[CommandOrigin, ...] = (
        CommandOrigin.API,
        CommandOrigin.CLI,
        CommandOrigin.WEB,
        CommandOrigin.SDK,
    )
    reason: str = ""

    def allows(self, *, origin: CommandOrigin, session_mode: str) -> bool:
        if not self.enabled or origin not in self.origins:
            return False
        return not self.session_modes or session_mode in self.session_modes

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class CommandExposure:
    local: bool = True
    api: bool = True
    remote: bool = False
    agent: bool = False

    def allows(self, origin: CommandOrigin) -> bool:
        if origin in {CommandOrigin.CLI, CommandOrigin.WEB}:
            return self.local
        if origin in {CommandOrigin.API, CommandOrigin.SDK}:
            return self.api
        if origin == CommandOrigin.REMOTE:
            return self.remote
        if origin == CommandOrigin.AGENT:
            return self.agent
        return True

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ControlCommandDescriptor:
    canonical_name: str
    description: str
    source: CommandSource
    handler_id: str
    kind: CommandKind = CommandKind.CONTROL
    aliases: tuple[str, ...] = ()
    descriptor_version: str = "1"
    argument_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    argument_hint: str = ""
    availability: CommandAvailability = field(default_factory=CommandAvailability)
    exposure: CommandExposure = field(default_factory=CommandExposure)
    user_invocable: bool = True
    model_invocable: bool = False
    mutation_scope: CommandMutationScope = CommandMutationScope.READ_ONLY
    concurrency: CommandConcurrency = CommandConcurrency.READ_ONLY_PARALLEL
    immediate: bool = False
    permission_action: str = "inspect"
    result_modes: tuple[str, ...] = ("display_text", "data")
    category: str = "runtime"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = _normalize_name(self.canonical_name)
        if not name or name == "/":
            raise ValueError("command canonical name is required")
        aliases = tuple(dict.fromkeys(_normalize_name(item) for item in self.aliases))
        if name in aliases:
            aliases = tuple(item for item in aliases if item != name)
        object.__setattr__(self, "canonical_name", name)
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "argument_schema", copy.deepcopy(dict(self.argument_schema)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        if self.mutation_scope != CommandMutationScope.READ_ONLY and self.concurrency == CommandConcurrency.READ_ONLY_PARALLEL:
            raise ValueError("mutating commands cannot use read_only_parallel concurrency")
        if self.immediate and self.concurrency == CommandConcurrency.SESSION_SERIAL and self.mutation_scope != CommandMutationScope.READ_ONLY:
            # Immediate means "admit while a turn is active", not bypass
            # serialization. Mutating commands must never claim this mode.
            raise ValueError("mutating session-serial command cannot be immediate")

    @property
    def name(self) -> str:
        return self.canonical_name

    @property
    def purpose(self) -> str:
        return self.description

    @property
    def event_hint(self) -> str:
        return str(self.metadata.get("event_hint") or "control_command")

    @property
    def requires_task(self) -> bool:
        return bool(self.metadata.get("requires_task", True))

    def to_dict(self) -> dict[str, Any]:
        payload = _to_dict(self)
        payload.update({
            "name": self.canonical_name,
            "purpose": self.description,
            "event_hint": self.event_hint,
            "requires_task": self.requires_task,
        })
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ControlCommandDescriptor":
        availability_raw = raw.get("availability") if isinstance(raw.get("availability"), Mapping) else {}
        exposure_raw = raw.get("exposure") if isinstance(raw.get("exposure"), Mapping) else {}
        return cls(
            canonical_name=str(raw.get("canonical_name") or raw.get("name") or ""),
            description=str(raw.get("description") or raw.get("purpose") or ""),
            source=CommandSource.from_dict(raw.get("source") if isinstance(raw.get("source"), Mapping) else {"id": raw.get("source")}),
            handler_id=str(raw.get("handler_id") or ""),
            kind=_enum(CommandKind, raw.get("kind"), CommandKind.CONTROL),
            aliases=tuple(raw.get("aliases") or ()),
            descriptor_version=str(raw.get("descriptor_version") or "1"),
            argument_schema=dict(raw.get("argument_schema") or {"type": "object"}),
            argument_hint=str(raw.get("argument_hint") or ""),
            availability=CommandAvailability(
                enabled=bool(availability_raw.get("enabled", True)),
                feature=str(availability_raw.get("feature") or ""),
                session_modes=tuple(availability_raw.get("session_modes") or ()),
                origins=tuple(_enum(CommandOrigin, item, CommandOrigin.API) for item in availability_raw.get("origins") or (CommandOrigin.API.value,)),
                reason=str(availability_raw.get("reason") or ""),
            ),
            exposure=CommandExposure(**{key: bool(exposure_raw.get(key, default)) for key, default in {"local": True, "api": True, "remote": False, "agent": False}.items()}),
            user_invocable=bool(raw.get("user_invocable", True)),
            model_invocable=bool(raw.get("model_invocable", False)),
            mutation_scope=_enum(CommandMutationScope, raw.get("mutation_scope"), CommandMutationScope.READ_ONLY),
            concurrency=_enum(CommandConcurrency, raw.get("concurrency"), CommandConcurrency.READ_ONLY_PARALLEL),
            immediate=bool(raw.get("immediate", False)),
            permission_action=str(raw.get("permission_action") or "inspect"),
            result_modes=tuple(raw.get("result_modes") or ("display_text", "data")),
            category=str(raw.get("category") or "runtime"),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ControlCommandRequest:
    run_id: str
    task_id: str
    session_id: str
    canonical_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: new_id("controlreq"))
    command_id: str = field(default_factory=lambda: new_id("cmd"))
    protocol_version: str = "zyra.control/v1"
    idempotency_key: str = ""
    target_subagent_task_id: str = ""
    issued_by: AgentRole = AgentRole.USER
    origin: CommandOrigin = CommandOrigin.API
    registry_generation: int = 0
    expected_session_revision: int | None = None
    priority: QueuePriority = QueuePriority.NEXT
    deadline_at: str = ""
    correlation_id: str = ""
    causation_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    reply_mode: ReplyMode = ReplyMode.INLINE
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_name", _normalize_name(self.canonical_name))
        object.__setattr__(self, "arguments", copy.deepcopy(dict(self.arguments)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", f"control:{self.request_id}")
        if not self.correlation_id:
            object.__setattr__(self, "correlation_id", self.request_id)

    @property
    def body_digest(self) -> str:
        from hashlib import sha256

        payload = json.dumps({
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "canonical_name": self.canonical_name,
            "arguments": self.arguments,
            "target_subagent_task_id": self.target_subagent_task_id,
            "origin": self.origin.value,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"sha256:{sha256(payload.encode('utf-8')).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ControlCommandRequest":
        return cls(
            run_id=str(raw.get("run_id") or ""),
            task_id=str(raw.get("task_id") or ""),
            session_id=str(raw.get("session_id") or ""),
            canonical_name=str(raw.get("canonical_name") or raw.get("name") or raw.get("command") or ""),
            arguments=dict(raw.get("arguments") or raw.get("args") or {}),
            request_id=str(raw.get("request_id") or new_id("controlreq")),
            command_id=str(raw.get("command_id") or new_id("cmd")),
            protocol_version=str(raw.get("protocol_version") or "zyra.control/v1"),
            idempotency_key=str(raw.get("idempotency_key") or ""),
            target_subagent_task_id=str(raw.get("target_subagent_task_id") or ""),
            issued_by=_enum(AgentRole, raw.get("issued_by"), AgentRole.USER),
            origin=_enum(CommandOrigin, raw.get("origin"), CommandOrigin.API),
            registry_generation=int(raw.get("registry_generation") or 0),
            expected_session_revision=(int(raw["expected_session_revision"]) if raw.get("expected_session_revision") is not None else None),
            priority=_enum(QueuePriority, raw.get("priority"), QueuePriority.NEXT),
            deadline_at=str(raw.get("deadline_at") or ""),
            correlation_id=str(raw.get("correlation_id") or ""),
            causation_id=str(raw.get("causation_id") or ""),
            trace_id=str(raw.get("trace_id") or ""),
            span_id=str(raw.get("span_id") or ""),
            reply_mode=_enum(ReplyMode, raw.get("reply_mode"), ReplyMode.INLINE),
            created_at=str(raw.get("created_at") or now_iso()),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ControlError:
    code: ControlErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ControlResult:
    display_text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    checkpoint_ref: str = ""
    ui_surface: dict[str, Any] = field(default_factory=dict)
    followup_queue_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class ControlCommandResponse:
    request_id: str
    command_id: str
    status: CommandStatus
    registry_generation: int
    result: ControlResult = field(default_factory=ControlResult)
    error: ControlError | None = None
    revision_before: int | None = None
    revision_after: int | None = None
    event_ids: tuple[str, ...] = ()
    started_at: str = ""
    finished_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == CommandStatus.SUCCEEDED

    @property
    def summary(self) -> str:
        return self.result.display_text or (self.error.message if self.error else "")

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, **_to_dict(self)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ControlCommandResponse":
        result_raw = raw.get("result") if isinstance(raw.get("result"), Mapping) else {}
        error_raw = raw.get("error") if isinstance(raw.get("error"), Mapping) else None
        return cls(
            request_id=str(raw.get("request_id") or ""),
            command_id=str(raw.get("command_id") or ""),
            status=_enum(CommandStatus, raw.get("status"), CommandStatus.FAILED),
            registry_generation=int(raw.get("registry_generation") or 0),
            result=ControlResult(
                display_text=str(result_raw.get("display_text") or ""),
                data=dict(result_raw.get("data") or {}),
                artifact_refs=(),
                checkpoint_ref=str(result_raw.get("checkpoint_ref") or ""),
                ui_surface=dict(result_raw.get("ui_surface") or {}),
                followup_queue_id=str(result_raw.get("followup_queue_id") or ""),
                usage=dict(result_raw.get("usage") or {}),
                metadata=dict(result_raw.get("metadata") or {}),
            ),
            error=(
                ControlError(
                    code=_enum(ControlErrorCode, error_raw.get("code"), ControlErrorCode.HANDLER_FAILED),
                    message=str(error_raw.get("message") or ""),
                    retryable=bool(error_raw.get("retryable", False)),
                    details=dict(error_raw.get("details") or {}),
                )
                if error_raw
                else None
            ),
            revision_before=(int(raw["revision_before"]) if raw.get("revision_before") is not None else None),
            revision_after=(int(raw["revision_after"]) if raw.get("revision_after") is not None else None),
            event_ids=tuple(raw.get("event_ids") or ()),
            started_at=str(raw.get("started_at") or ""),
            finished_at=str(raw.get("finished_at") or now_iso()),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class CommandRegistryCollision:
    name: str
    active_source: str
    shadowed_source: str
    active_handler_id: str
    shadowed_handler_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class CommandRegistrySnapshot:
    generation: int
    descriptors: tuple[ControlCommandDescriptor, ...]
    collisions: tuple[CommandRegistryCollision, ...]
    digest: str
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class PromptQueueEntry:
    session_id: str
    kind: QueueEntryKind
    payload: dict[str, Any]
    priority: QueuePriority
    origin: CommandOrigin
    target_subagent_task_id: str = ""
    parse_slash: bool = False
    is_meta: bool = False
    mode: str = "default"
    idempotency_key: str = ""
    queue_id: str = field(default_factory=lambda: new_id("promptqueue"))
    status: QueueEntryStatus = QueueEntryStatus.QUEUED
    sequence: int = 0
    claim_token: str = field(default="", repr=False)
    claim_expires_at: str = ""
    expected_revision: int | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def target_key(self) -> str:
        return self.target_subagent_task_id or "main"

    def safe_dict(self) -> dict[str, Any]:
        payload = _to_dict(self)
        payload.pop("claim_token", None)
        payload["claim_token_present"] = bool(self.claim_token)
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PromptQueueEntry":
        return cls(
            session_id=str(raw.get("session_id") or ""),
            kind=_enum(QueueEntryKind, raw.get("kind"), QueueEntryKind.PROMPT),
            payload=dict(raw.get("payload") or {}),
            priority=_enum(QueuePriority, raw.get("priority"), QueuePriority.NEXT),
            origin=_enum(CommandOrigin, raw.get("origin"), CommandOrigin.API),
            target_subagent_task_id=str(raw.get("target_subagent_task_id") or ""),
            parse_slash=bool(raw.get("parse_slash", False)),
            is_meta=bool(raw.get("is_meta", False)),
            mode=str(raw.get("mode") or "default"),
            idempotency_key=str(raw.get("idempotency_key") or ""),
            queue_id=str(raw.get("queue_id") or new_id("promptqueue")),
            status=_enum(QueueEntryStatus, raw.get("status"), QueueEntryStatus.QUEUED),
            sequence=int(raw.get("sequence") or 0),
            claim_token=str(raw.get("claim_token") or ""),
            claim_expires_at=str(raw.get("claim_expires_at") or ""),
            expected_revision=(int(raw["expected_revision"]) if raw.get("expected_revision") is not None else None),
            created_at=str(raw.get("created_at") or now_iso()),
            updated_at=str(raw.get("updated_at") or now_iso()),
            metadata=dict(raw.get("metadata") or {}),
        )


def _normalize_name(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized.startswith("/") else f"/{normalized}"


def _enum(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except (TypeError, ValueError):
        return default


def _to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _to_dict(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_dict(item) for item in value]
    return copy.deepcopy(value)
