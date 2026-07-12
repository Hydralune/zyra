from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields, is_dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, new_id, now_iso

from .digests import digest_object, stable_id
from .errors import AgentDefinitionError


class AgentDefinitionSource(StrEnum):
    BUILTIN = "builtin"
    MANAGED = "managed"
    PROJECT = "project"
    USER = "user"
    PLUGIN = "plugin"
    REQUEST = "request"


class AgentExecutionMode(StrEnum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"


class AgentContextMode(StrEnum):
    ISOLATED = "isolated"
    FORK = "fork"
    RESUME = "resume"


class SubagentIsolationKind(StrEnum):
    NONE = "none"
    WORKSPACE = "workspace"
    WORKTREE = "worktree"
    SANDBOX = "sandbox"
    REMOTE = "remote"


class SubagentTaskStatus(StrEnum):
    CREATED = "created"
    VALIDATING = "validating"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING = "waiting"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    KILLED = "killed"
    CLEANUP_FAILED = "cleanup_failed"

    @property
    def terminal(self) -> bool:
        return self in {
            self.COMPLETED,
            self.FAILED,
            self.CANCELLED,
            self.KILLED,
            self.CLEANUP_FAILED,
        }


class SubagentFailureKind(StrEnum):
    VALIDATION = "validation"
    PERMISSION = "permission"
    BUDGET = "budget"
    DISPATCH = "dispatch"
    EXECUTION = "execution"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ISOLATION = "isolation"
    CLEANUP = "cleanup"
    TRANSCRIPT = "transcript"
    UNKNOWN = "unknown"


class RecoveryDisposition(StrEnum):
    NONE = "none"
    RETRY = "retry"
    RESUME = "resume"
    REPLAN = "replan"
    REROUTE = "reroute"
    ESCALATE = "escalate"
    TERMINATE = "terminate"


class PermissionMode(StrEnum):
    DENY = "deny"
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    PLAN = "plan"
    AUTO = "auto"
    BYPASS = "bypass"


class IsolationCleanupStatus(StrEnum):
    RELEASED = "released"
    RETAINED = "retained"
    FAILED = "failed"
    NOT_REQUIRED = "not_required"


class TranscriptEntryKind(StrEnum):
    TASK_CREATED = "task_created"
    CONTEXT = "context"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    PROGRESS = "progress"
    CONTINUATION = "continuation"
    HANDOFF = "handoff"
    FAILURE = "failure"
    CANCEL = "cancel"
    METADATA = "metadata"


class ControlSignalKind(StrEnum):
    CANCEL = "cancel"
    STOP = "stop"
    CONTINUE = "continue"
    MESSAGE = "message"
    BACKGROUND = "background"


@dataclass(frozen=True, slots=True)
class UsageBudget:
    max_turns: int = 12
    max_tool_calls: int = 48
    max_input_tokens: int = 64_000
    max_output_tokens: int = 16_000
    max_result_chars: int = 120_000
    max_wall_time_ms: int = 900_000
    max_children: int = 4
    max_depth: int = 3

    def __post_init__(self) -> None:
        for item in fields(self):
            value = int(getattr(self, item.name))
            if value < 0:
                raise AgentDefinitionError("usage budget values cannot be negative", field=item.name, value=value)

    def narrowed_by(self, child: "UsageBudget") -> "UsageBudget":
        return UsageBudget(**{item.name: min(int(getattr(self, item.name)), int(getattr(child, item.name))) for item in fields(self)})

    def to_dict(self) -> dict[str, int]:
        return {item.name: int(getattr(self, item.name)) for item in fields(self)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "UsageBudget":
        data = dict(raw or {})
        aliases = {
            "maxTurns": "max_turns",
            "maxToolCalls": "max_tool_calls",
            "maxInputTokens": "max_input_tokens",
            "maxOutputTokens": "max_output_tokens",
            "maxResultChars": "max_result_chars",
            "maxWallTimeMs": "max_wall_time_ms",
            "maxChildren": "max_children",
            "maxDepth": "max_depth",
        }
        normalized = {aliases.get(str(key), str(key)): value for key, value in data.items()}
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: int(value) for key, value in normalized.items() if key in allowed})


@dataclass(frozen=True, slots=True)
class UsageLedger:
    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    result_chars: int = 0
    wall_time_ms: int = 0
    child_count: int = 0

    def add(self, **delta: int) -> "UsageLedger":
        values = {item.name: int(getattr(self, item.name)) + int(delta.get(item.name, 0)) for item in fields(self)}
        return UsageLedger(**values)

    def to_dict(self) -> dict[str, int]:
        return {item.name: int(getattr(self, item.name)) for item in fields(self)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "UsageLedger":
        data = dict(raw or {})
        return cls(**{item.name: int(data.get(item.name, 0)) for item in fields(cls)})


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_type: str
    description: str
    source: AgentDefinitionSource = AgentDefinitionSource.PROJECT
    version: str = "1"
    tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    mcp_servers: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    model: str = "inherit"
    effort: str = "inherit"
    background: bool = False
    isolation: SubagentIsolationKind = SubagentIsolationKind.WORKSPACE
    memory_scope: str = "task"
    system_prompt: str = ""
    budget: UsageBudget = field(default_factory=UsageBudget)
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = str(self.agent_type).strip()
        if not name or any(char.isspace() for char in name):
            raise AgentDefinitionError("agent_type must be a non-empty token", agent_type=self.agent_type)
        if not str(self.description).strip():
            raise AgentDefinitionError("agent definition requires a description", agent_type=name)
        overlap = set(self.tools) & set(self.disallowed_tools)
        if overlap:
            raise AgentDefinitionError("agent tool allow and deny sets overlap", agent_type=name, tools=sorted(overlap))
        object.__setattr__(self, "agent_type", name)
        object.__setattr__(self, "tools", _tokens(self.tools))
        object.__setattr__(self, "disallowed_tools", _tokens(self.disallowed_tools))
        object.__setattr__(self, "mcp_servers", _tokens(self.mcp_servers))
        object.__setattr__(self, "skills", _tokens(self.skills))
        object.__setattr__(self, "hooks", _tokens(self.hooks))
        object.__setattr__(self, "capabilities", _tokens(self.capabilities))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    @property
    def definition_id(self) -> str:
        return stable_id("agentdef", self.agent_type, self.source, self.version, self.digest)

    @property
    def digest(self) -> str:
        return digest_object(self.to_dict(include_prompt=True))

    def to_dict(self, *, include_prompt: bool = False) -> dict[str, Any]:
        payload = {
            "agent_type": self.agent_type,
            "description": self.description,
            "source": self.source.value,
            "version": self.version,
            "tools": list(self.tools),
            "disallowed_tools": list(self.disallowed_tools),
            "permission_mode": self.permission_mode.value,
            "mcp_servers": list(self.mcp_servers),
            "skills": list(self.skills),
            "hooks": list(self.hooks),
            "capabilities": list(self.capabilities),
            "model": self.model,
            "effort": self.effort,
            "background": self.background,
            "isolation": self.isolation.value,
            "memory_scope": self.memory_scope,
            "budget": self.budget.to_dict(),
            "enabled": self.enabled,
            "metadata": copy.deepcopy(self.metadata),
        }
        if include_prompt:
            payload["system_prompt"] = self.system_prompt
        else:
            payload["system_prompt_digest"] = digest_object(self.system_prompt)
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AgentDefinition":
        return cls(
            agent_type=str(raw.get("agent_type") or raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            source=_enum(AgentDefinitionSource, raw.get("source"), AgentDefinitionSource.PROJECT),
            version=str(raw.get("version") or "1"),
            tools=tuple(raw.get("tools") or ()),
            disallowed_tools=tuple(raw.get("disallowed_tools") or raw.get("disallowedTools") or ()),
            permission_mode=_enum(PermissionMode, raw.get("permission_mode") or raw.get("permissionMode"), PermissionMode.DEFAULT),
            mcp_servers=tuple(raw.get("mcp_servers") or raw.get("mcpServers") or ()),
            skills=tuple(raw.get("skills") or ()),
            hooks=tuple(raw.get("hooks") or ()),
            capabilities=tuple(raw.get("capabilities") or ()),
            model=str(raw.get("model") or "inherit"),
            effort=str(raw.get("effort") or "inherit"),
            background=bool(raw.get("background", False)),
            isolation=_enum(SubagentIsolationKind, raw.get("isolation"), SubagentIsolationKind.WORKSPACE),
            memory_scope=str(raw.get("memory_scope") or raw.get("memory") or "task"),
            system_prompt=str(raw.get("system_prompt") or raw.get("prompt") or ""),
            budget=UsageBudget.from_dict(raw.get("budget") if isinstance(raw.get("budget"), Mapping) else raw),
            enabled=bool(raw.get("enabled", True)),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ToolScope:
    parent_tools: tuple[str, ...]
    child_tools: tuple[str, ...]
    denied_tools: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    dynamic_tool_identities: dict[str, str] = field(default_factory=dict)
    digest: str = ""

    def __post_init__(self) -> None:
        parent = _tokens(self.parent_tools)
        child = _tokens(self.child_tools)
        denied = _tokens(self.denied_tools)
        required = _tokens(self.required_tools)
        object.__setattr__(self, "parent_tools", parent)
        object.__setattr__(self, "child_tools", child)
        object.__setattr__(self, "denied_tools", denied)
        object.__setattr__(self, "required_tools", required)
        object.__setattr__(self, "dynamic_tool_identities", dict(self.dynamic_tool_identities))
        if not self.digest:
            object.__setattr__(self, "digest", digest_object({
                "parent": parent,
                "child": child,
                "denied": denied,
                "required": required,
                "dynamic": self.dynamic_tool_identities,
            }))

    @property
    def narrowed(self) -> bool:
        return set(self.child_tools) < set(self.parent_tools)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_tools": list(self.parent_tools),
            "child_tools": list(self.child_tools),
            "denied_tools": list(self.denied_tools),
            "required_tools": list(self.required_tools),
            "dynamic_tool_identities": dict(self.dynamic_tool_identities),
            "narrowed": self.narrowed,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ToolScope":
        return cls(
            parent_tools=tuple(raw.get("parent_tools") or ()),
            child_tools=tuple(raw.get("child_tools") or ()),
            denied_tools=tuple(raw.get("denied_tools") or ()),
            required_tools=tuple(raw.get("required_tools") or ()),
            dynamic_tool_identities=dict(raw.get("dynamic_tool_identities") or {}),
            digest=str(raw.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class PermissionDerivation:
    parent_mode: PermissionMode
    child_mode: PermissionMode
    inherited_rule_ids: tuple[str, ...] = ()
    inherited_denials: tuple[str, ...] = ()
    exact_grants_inherited: bool = False
    mcp_servers: tuple[str, ...] = ()
    monotonic: bool = True
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "inherited_rule_ids", _tokens(self.inherited_rule_ids))
        object.__setattr__(self, "inherited_denials", _tokens(self.inherited_denials))
        object.__setattr__(self, "mcp_servers", _tokens(self.mcp_servers))
        if not self.digest:
            object.__setattr__(self, "digest", digest_object({
                "parent_mode": self.parent_mode,
                "child_mode": self.child_mode,
                "rules": self.inherited_rule_ids,
                "denials": self.inherited_denials,
                "exact_grants_inherited": self.exact_grants_inherited,
                "mcp_servers": self.mcp_servers,
                "monotonic": self.monotonic,
            }))

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_mode": self.parent_mode.value,
            "child_mode": self.child_mode.value,
            "inherited_rule_ids": list(self.inherited_rule_ids),
            "inherited_denials": list(self.inherited_denials),
            "exact_grants_inherited": self.exact_grants_inherited,
            "mcp_servers": list(self.mcp_servers),
            "monotonic": self.monotonic,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PermissionDerivation":
        return cls(
            parent_mode=_enum(PermissionMode, raw.get("parent_mode"), PermissionMode.DEFAULT),
            child_mode=_enum(PermissionMode, raw.get("child_mode"), PermissionMode.DEFAULT),
            inherited_rule_ids=tuple(raw.get("inherited_rule_ids") or ()),
            inherited_denials=tuple(raw.get("inherited_denials") or ()),
            exact_grants_inherited=bool(raw.get("exact_grants_inherited", False)),
            mcp_servers=tuple(raw.get("mcp_servers") or ()),
            monotonic=bool(raw.get("monotonic", True)),
            digest=str(raw.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class SubagentContextSnapshot:
    snapshot_id: str
    parent_session_id: str
    parent_task_id: str
    parent_worker_request_id: str
    mode: AgentContextMode
    context_epoch: int = 0
    compact_boundary_id: str = ""
    message_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    invoked_skill_refs: tuple[dict[str, Any], ...] = ()
    content_replacement_refs: tuple[str, ...] = ()
    system_prompt_digest: str = ""
    cache_prefix_digest: str = ""
    ancestry: tuple[str, ...] = ()
    depth: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_refs", _tokens(self.message_refs))
        object.__setattr__(self, "artifact_refs", _tokens(self.artifact_refs))
        object.__setattr__(self, "evidence_refs", _tokens(self.evidence_refs))
        object.__setattr__(self, "content_replacement_refs", _tokens(self.content_replacement_refs))
        object.__setattr__(self, "ancestry", tuple(str(item) for item in self.ancestry))
        object.__setattr__(self, "invoked_skill_refs", tuple(copy.deepcopy(dict(item)) for item in self.invoked_skill_refs))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "parent_session_id": self.parent_session_id,
            "parent_task_id": self.parent_task_id,
            "parent_worker_request_id": self.parent_worker_request_id,
            "mode": self.mode.value,
            "context_epoch": self.context_epoch,
            "compact_boundary_id": self.compact_boundary_id,
            "message_refs": list(self.message_refs),
            "artifact_refs": list(self.artifact_refs),
            "evidence_refs": list(self.evidence_refs),
            "invoked_skill_refs": [copy.deepcopy(item) for item in self.invoked_skill_refs],
            "content_replacement_refs": list(self.content_replacement_refs),
            "system_prompt_digest": self.system_prompt_digest,
            "cache_prefix_digest": self.cache_prefix_digest,
            "ancestry": list(self.ancestry),
            "depth": self.depth,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SubagentContextSnapshot":
        return cls(
            snapshot_id=str(raw.get("snapshot_id") or new_id("subctx")),
            parent_session_id=str(raw.get("parent_session_id") or ""),
            parent_task_id=str(raw.get("parent_task_id") or ""),
            parent_worker_request_id=str(raw.get("parent_worker_request_id") or ""),
            mode=_enum(AgentContextMode, raw.get("mode"), AgentContextMode.ISOLATED),
            context_epoch=int(raw.get("context_epoch") or 0),
            compact_boundary_id=str(raw.get("compact_boundary_id") or ""),
            message_refs=tuple(raw.get("message_refs") or ()),
            artifact_refs=tuple(raw.get("artifact_refs") or ()),
            evidence_refs=tuple(raw.get("evidence_refs") or ()),
            invoked_skill_refs=tuple(raw.get("invoked_skill_refs") or ()),
            content_replacement_refs=tuple(raw.get("content_replacement_refs") or ()),
            system_prompt_digest=str(raw.get("system_prompt_digest") or ""),
            cache_prefix_digest=str(raw.get("cache_prefix_digest") or ""),
            ancestry=tuple(raw.get("ancestry") or ()),
            depth=int(raw.get("depth") or 0),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class SubagentIsolationRequest:
    run_id: str
    task_id: str
    parent_task_id: str
    kind: SubagentIsolationKind
    workspace_root: str
    requested_cwd: str = ""
    writable_paths: tuple[str, ...] = ()
    read_only_paths: tuple[str, ...] = ()
    network_allowed: bool = False
    cleanup_required: bool = True
    request_id: str = field(default_factory=lambda: new_id("isoreq"))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "writable_paths", _tokens(self.writable_paths))
        object.__setattr__(self, "read_only_paths", _tokens(self.read_only_paths))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SubagentIsolationRequest":
        return cls(
            run_id=str(raw.get("run_id") or ""),
            task_id=str(raw.get("task_id") or ""),
            parent_task_id=str(raw.get("parent_task_id") or ""),
            kind=_enum(SubagentIsolationKind, raw.get("kind"), SubagentIsolationKind.WORKSPACE),
            workspace_root=str(raw.get("workspace_root") or ""),
            requested_cwd=str(raw.get("requested_cwd") or ""),
            writable_paths=tuple(raw.get("writable_paths") or ()),
            read_only_paths=tuple(raw.get("read_only_paths") or ()),
            network_allowed=bool(raw.get("network_allowed", False)),
            cleanup_required=bool(raw.get("cleanup_required", True)),
            request_id=str(raw.get("request_id") or new_id("isoreq")),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class SubagentIsolationManifest:
    isolation_id: str
    request_id: str
    kind: SubagentIsolationKind
    workspace_root: str
    effective_cwd: str
    logical_only: bool
    state_owner: str
    cleanup_token: str = field(repr=False)
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "isolation_id": self.isolation_id,
            "request_id": self.request_id,
            "kind": self.kind.value,
            "workspace_root": self.workspace_root,
            "effective_cwd": self.effective_cwd,
            "logical_only": self.logical_only,
            "state_owner": self.state_owner,
            "cleanup_token_present": bool(self.cleanup_token),
            "created_at": self.created_at,
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class IsolationCleanupReceipt:
    isolation_id: str
    status: IsolationCleanupStatus
    safe_to_forget: bool
    reason: str
    retained_paths: tuple[str, ...] = ()
    cleanup_id: str = field(default_factory=lambda: new_id("isocleanup"))
    completed_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class SubagentDispatchRequest:
    task_id: str
    run_id: str
    parent_task_id: str
    worker_name: str
    agent_definition_id: str
    prompt: str
    tool_scope: ToolScope
    permission: PermissionDerivation
    context: SubagentContextSnapshot
    isolation: SubagentIsolationManifest
    budget: UsageBudget
    execution_mode: AgentExecutionMode
    attempt: int = 1
    dispatch_id: str = field(default_factory=lambda: new_id("subdispatch"))
    idempotency_key: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", stable_id("subdispatch", self.task_id, self.attempt, self.context.snapshot_id))
        object.__setattr__(self, "constraints", copy.deepcopy(dict(self.constraints)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "parent_task_id": self.parent_task_id,
            "worker_name": self.worker_name,
            "agent_definition_id": self.agent_definition_id,
            "prompt_digest": digest_object(self.prompt),
            "tool_scope": self.tool_scope.to_dict(),
            "permission": self.permission.to_dict(),
            "context": self.context.to_dict(),
            "isolation": self.isolation.safe_dict(),
            "budget": self.budget.to_dict(),
            "execution_mode": self.execution_mode.value,
            "attempt": self.attempt,
            "dispatch_id": self.dispatch_id,
            "idempotency_key": self.idempotency_key,
            "constraints": copy.deepcopy(self.constraints),
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SubagentDispatchReceipt:
    task_id: str
    dispatch_id: str
    accepted: bool
    execution_ref: str
    status: SubagentTaskStatus
    accepted_at: str = field(default_factory=now_iso)
    worker_projection: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class RecoverySignal:
    failure_kind: SubagentFailureKind
    disposition: RecoveryDisposition
    reason: str
    retryable: bool
    task_id: str
    attempt: int
    error_code: str = ""
    signal_id: str = field(default_factory=lambda: new_id("subrecovery"))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class StructuredSubagentMessage:
    sender_task_id: str
    target_task_id: str
    intent: str
    summary: str
    state_delta: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    uncertainty: float | None = None
    message_id: str = field(default_factory=lambda: new_id("submsg"))
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_delta", copy.deepcopy(dict(self.state_delta)))
        object.__setattr__(self, "evidence_refs", _tokens(self.evidence_refs))
        object.__setattr__(self, "artifact_refs", _tokens(self.artifact_refs))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class StructuredHandoff:
    task_id: str
    parent_task_id: str
    summary: str
    message: StructuredSubagentMessage
    artifacts: tuple[ArtifactRef, ...] = ()
    usage: UsageLedger = field(default_factory=UsageLedger)
    recovery_signal: RecoverySignal | None = None
    transcript_ref: str = ""
    handoff_id: str = field(default_factory=lambda: new_id("handoff"))
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "summary": self.summary,
            "message": self.message.to_dict(),
            "artifacts": [_to_dict(item) for item in self.artifacts],
            "usage": self.usage.to_dict(),
            "recovery_signal": self.recovery_signal.to_dict() if self.recovery_signal else None,
            "transcript_ref": self.transcript_ref,
            "handoff_id": self.handoff_id,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SubagentExecutionResult:
    task_id: str
    execution_ref: str
    ok: bool
    summary: str
    artifacts: tuple[ArtifactRef, ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    usage: UsageLedger = field(default_factory=UsageLedger)
    error_code: str = ""
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "execution_ref": self.execution_ref,
            "ok": self.ok,
            "summary": self.summary,
            "artifacts": [_to_dict(item) for item in self.artifacts],
            "events": [copy.deepcopy(item) for item in self.events],
            "usage": self.usage.to_dict(),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SubagentProgress:
    task_id: str
    status: SubagentTaskStatus
    summary: str
    usage: UsageLedger
    execution_ref: str = ""
    tool_name: str = ""
    progress_id: str = field(default_factory=lambda: new_id("subprogress"))
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    task_id: str
    kind: TranscriptEntryKind
    payload: dict[str, Any]
    sequence: int
    entry_id: str = field(default_factory=lambda: new_id("subtranscript"))
    parent_entry_id: str = ""
    created_at: str = field(default_factory=now_iso)
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))
        if not self.digest:
            object.__setattr__(self, "digest", digest_object({
                "task_id": self.task_id,
                "kind": self.kind,
                "payload": self.payload,
                "sequence": self.sequence,
                "entry_id": self.entry_id,
                "parent_entry_id": self.parent_entry_id,
            }))

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TranscriptEntry":
        return cls(
            task_id=str(raw.get("task_id") or ""),
            kind=_enum(TranscriptEntryKind, raw.get("kind"), TranscriptEntryKind.METADATA),
            payload=dict(raw.get("payload") or {}),
            sequence=int(raw.get("sequence") or 0),
            entry_id=str(raw.get("entry_id") or new_id("subtranscript")),
            parent_entry_id=str(raw.get("parent_entry_id") or ""),
            created_at=str(raw.get("created_at") or now_iso()),
            digest=str(raw.get("digest") or ""),
        )


@dataclass(slots=True)
class SubagentTaskRecord:
    run_id: str
    task_id: str
    parent_task_id: str
    parent_session_id: str
    agent_type: str
    definition_id: str
    status: SubagentTaskStatus
    context_snapshot: SubagentContextSnapshot
    tool_scope: ToolScope
    permission: PermissionDerivation
    budget: UsageBudget
    isolation_request: SubagentIsolationRequest
    execution_mode: AgentExecutionMode
    prompt_digest: str
    revision: int = 0
    dispatch_request: dict[str, Any] = field(default_factory=dict)
    execution_ref: str = ""
    attempt: int = 0
    usage: UsageLedger = field(default_factory=UsageLedger)
    pending_messages: list[StructuredSubagentMessage] = field(default_factory=list)
    handoff: StructuredHandoff | None = None
    recovery_signals: list[RecoverySignal] = field(default_factory=list)
    child_task_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    completed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "parent_session_id": self.parent_session_id,
            "agent_type": self.agent_type,
            "definition_id": self.definition_id,
            "status": self.status.value,
            "context_snapshot": self.context_snapshot.to_dict(),
            "tool_scope": self.tool_scope.to_dict(),
            "permission": self.permission.to_dict(),
            "budget": self.budget.to_dict(),
            "isolation_request": self.isolation_request.to_dict(),
            "execution_mode": self.execution_mode.value,
            "prompt_digest": self.prompt_digest,
            "revision": self.revision,
            "dispatch_request": copy.deepcopy(self.dispatch_request),
            "execution_ref": self.execution_ref,
            "attempt": self.attempt,
            "usage": self.usage.to_dict(),
            "pending_messages": [item.to_dict() for item in self.pending_messages],
            "handoff": self.handoff.safe_dict() if self.handoff else None,
            "recovery_signals": [item.to_dict() for item in self.recovery_signals],
            "child_task_ids": list(self.child_task_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SubagentTaskRecord":
        handoff_raw = raw.get("handoff") if isinstance(raw.get("handoff"), Mapping) else None
        return cls(
            run_id=str(raw.get("run_id") or ""),
            task_id=str(raw.get("task_id") or ""),
            parent_task_id=str(raw.get("parent_task_id") or ""),
            parent_session_id=str(raw.get("parent_session_id") or ""),
            agent_type=str(raw.get("agent_type") or ""),
            definition_id=str(raw.get("definition_id") or ""),
            status=_enum(SubagentTaskStatus, raw.get("status"), SubagentTaskStatus.CREATED),
            context_snapshot=SubagentContextSnapshot.from_dict(_mapping(raw.get("context_snapshot"))),
            tool_scope=ToolScope.from_dict(_mapping(raw.get("tool_scope"))),
            permission=PermissionDerivation.from_dict(_mapping(raw.get("permission"))),
            budget=UsageBudget.from_dict(_mapping(raw.get("budget"))),
            isolation_request=SubagentIsolationRequest.from_dict(_mapping(raw.get("isolation_request"))),
            execution_mode=_enum(AgentExecutionMode, raw.get("execution_mode"), AgentExecutionMode.FOREGROUND),
            prompt_digest=str(raw.get("prompt_digest") or ""),
            revision=int(raw.get("revision") or 0),
            dispatch_request=dict(raw.get("dispatch_request") or {}),
            execution_ref=str(raw.get("execution_ref") or ""),
            attempt=int(raw.get("attempt") or 0),
            usage=UsageLedger.from_dict(_mapping(raw.get("usage"))),
            pending_messages=[_structured_message(item) for item in raw.get("pending_messages") or () if isinstance(item, Mapping)],
            handoff=_structured_handoff(handoff_raw) if handoff_raw else None,
            recovery_signals=[_recovery_signal(item) for item in raw.get("recovery_signals") or () if isinstance(item, Mapping)],
            child_task_ids=[str(item) for item in raw.get("child_task_ids") or ()],
            created_at=str(raw.get("created_at") or now_iso()),
            updated_at=str(raw.get("updated_at") or now_iso()),
            completed_at=str(raw.get("completed_at") or ""),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class SubagentSpawnRequest:
    run_id: str
    parent_task_id: str
    parent_session_id: str
    parent_worker_request_id: str
    agent_type: str
    prompt: str
    parent_tools: tuple[str, ...]
    parent_permission_mode: PermissionMode = PermissionMode.DEFAULT
    requested_tools: tuple[str, ...] = ()
    requested_permission_mode: PermissionMode | None = None
    available_mcp_servers: tuple[str, ...] = ()
    requested_mcp_servers: tuple[str, ...] = ()
    context_mode: AgentContextMode = AgentContextMode.ISOLATED
    execution_mode: AgentExecutionMode = AgentExecutionMode.FOREGROUND
    workspace_root: str = ""
    requested_cwd: str = ""
    context_payload: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    task_id: str = field(default_factory=lambda: new_id("subagenttask"))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent_tools", _tokens(self.parent_tools))
        object.__setattr__(self, "requested_tools", _tokens(self.requested_tools))
        object.__setattr__(self, "available_mcp_servers", _tokens(self.available_mcp_servers))
        object.__setattr__(self, "requested_mcp_servers", _tokens(self.requested_mcp_servers))
        object.__setattr__(self, "context_payload", copy.deepcopy(dict(self.context_payload)))
        object.__setattr__(self, "constraints", copy.deepcopy(dict(self.constraints)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", stable_id(
                "subspawn",
                self.run_id,
                self.parent_task_id,
                self.parent_session_id,
                self.agent_type,
                digest_object(self.prompt),
            ))


@dataclass(frozen=True, slots=True)
class SubagentRuntimeSnapshot:
    tasks: tuple[dict[str, Any], ...]
    active_task_ids: tuple[str, ...]
    terminal_task_ids: tuple[str, ...]
    definition_generation: int
    state_digest: str
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return _to_dict(self)


def artifact_from_dict(raw: Mapping[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=str(raw.get("artifact_id") or new_id("artifact")),
        kind=_enum(ArtifactKind, raw.get("kind"), ArtifactKind.FILE),
        uri=str(raw.get("uri") or ""),
        title=str(raw.get("title") or ""),
        producer_node_id=str(raw.get("producer_node_id") or "") or None,
        created_at=str(raw.get("created_at") or now_iso()),
        metadata=dict(raw.get("metadata") or {}),
    )


def _structured_message(raw: Mapping[str, Any]) -> StructuredSubagentMessage:
    return StructuredSubagentMessage(
        sender_task_id=str(raw.get("sender_task_id") or ""),
        target_task_id=str(raw.get("target_task_id") or ""),
        intent=str(raw.get("intent") or "message"),
        summary=str(raw.get("summary") or ""),
        state_delta=dict(raw.get("state_delta") or {}),
        evidence_refs=tuple(raw.get("evidence_refs") or ()),
        artifact_refs=tuple(raw.get("artifact_refs") or ()),
        uncertainty=float(raw["uncertainty"]) if raw.get("uncertainty") is not None else None,
        message_id=str(raw.get("message_id") or new_id("submsg")),
        created_at=str(raw.get("created_at") or now_iso()),
        metadata=dict(raw.get("metadata") or {}),
    )


def _recovery_signal(raw: Mapping[str, Any]) -> RecoverySignal:
    return RecoverySignal(
        failure_kind=_enum(SubagentFailureKind, raw.get("failure_kind"), SubagentFailureKind.UNKNOWN),
        disposition=_enum(RecoveryDisposition, raw.get("disposition"), RecoveryDisposition.REPLAN),
        reason=str(raw.get("reason") or ""),
        retryable=bool(raw.get("retryable", False)),
        task_id=str(raw.get("task_id") or ""),
        attempt=int(raw.get("attempt") or 0),
        error_code=str(raw.get("error_code") or ""),
        signal_id=str(raw.get("signal_id") or new_id("subrecovery")),
        metadata=dict(raw.get("metadata") or {}),
    )


def _structured_handoff(raw: Mapping[str, Any]) -> StructuredHandoff:
    return StructuredHandoff(
        task_id=str(raw.get("task_id") or ""),
        parent_task_id=str(raw.get("parent_task_id") or ""),
        summary=str(raw.get("summary") or ""),
        message=_structured_message(_mapping(raw.get("message"))),
        artifacts=tuple(artifact_from_dict(item) for item in raw.get("artifacts") or () if isinstance(item, Mapping)),
        usage=UsageLedger.from_dict(_mapping(raw.get("usage"))),
        recovery_signal=_recovery_signal(_mapping(raw.get("recovery_signal"))) if raw.get("recovery_signal") else None,
        transcript_ref=str(raw.get("transcript_ref") or ""),
        handoff_id=str(raw.get("handoff_id") or new_id("handoff")),
        created_at=str(raw.get("created_at") or now_iso()),
        metadata=dict(raw.get("metadata") or {}),
    )


def _tokens(values: Sequence[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _enum(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except (TypeError, ValueError):
        return default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: _to_dict(getattr(value, item.name)) for item in fields(value) if item.name != "cleanup_token"}
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_dict(item) for item in value]
    return copy.deepcopy(value)
