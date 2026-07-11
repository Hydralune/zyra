from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .errors import SkillNameError


SKILL_SCHEMA = "zyra.skill/v1"
SKILL_STATE_SCHEMA = "zyra.skill-state/v1"
SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
TOOL_COMPONENT_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def validate_skill_name(value: str) -> str:
    name = str(value or "").strip()
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise SkillNameError(
            "skill name must be lowercase hyphen-separated ASCII",
            detail={"name": name},
        )
    if len(name) > 96:
        raise SkillNameError("skill name exceeds 96 characters", detail={"name": name})
    return name


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        return tuple(str(item) for item in value)
    raise TypeError("expected a string sequence")


def _mapping(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("expected a mapping")
    return {str(key): item for key, item in value.items()}


class SkillSourceKind(StrEnum):
    MANAGED = "managed"
    BUILTIN = "builtin"
    USER = "user"
    PROJECT = "project"
    ADD_DIR = "add_dir"
    PLUGIN = "plugin"
    MCP = "mcp"


class SkillTrustTier(StrEnum):
    MANAGED = "managed"
    PRODUCT = "product"
    USER = "user"
    PROJECT = "project"
    THIRD_PARTY = "third_party"
    REMOTE = "remote"


class SkillInvocationMode(StrEnum):
    INLINE = "inline"
    FORK = "fork"


class SkillLifecycle(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    REVOKED = "revoked"
    INVALID = "invalid"
    SUPERSEDED = "superseded"


class SkillRevisionLifecycle(StrEnum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    AVAILABLE = "available"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class SkillInvocationStatus(StrEnum):
    REQUESTED = "requested"
    REF_RESOLVED = "ref_resolved"
    BODY_LOADED = "body_loaded"
    POLICY_BOUND = "policy_bound"
    HOOKS_REGISTERED = "hooks_registered"
    INLINE_ACTIVE = "inline_active"
    FORK_PENDING = "fork_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REVOKED = "revoked"
    BLOCKED = "blocked"

    @property
    def terminal(self) -> bool:
        return self in {
            self.COMPLETED,
            self.FAILED,
            self.CANCELLED,
            self.REVOKED,
            self.BLOCKED,
        }


class SkillHookEvent(StrEnum):
    PRE_INVOKE = "pre_invoke"
    POST_INVOKE = "post_invoke"
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    ON_ERROR = "on_error"
    ON_CANCEL = "on_cancel"
    ON_COMPACT = "on_compact"
    SESSION_END = "session_end"


class SkillHookLifetime(StrEnum):
    ONCE = "once"
    INVOCATION = "invocation"
    SESSION = "session"


class SkillResourceKind(StrEnum):
    TEXT = "text"
    JSON = "json"
    SCHEMA = "schema"
    TEMPLATE = "template"
    REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class ToolSelector:
    namespace: str
    name: str
    server_id: str = ""
    operations: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    read_only: bool = False

    def __post_init__(self) -> None:
        namespace = self.namespace.strip()
        name = self.name.strip()
        if not namespace or not name:
            raise ValueError("tool selector namespace and name are required")
        if namespace != "*" and not TOOL_COMPONENT_PATTERN.fullmatch(namespace):
            raise ValueError(f"invalid tool namespace: {namespace}")
        if name != "*" and not TOOL_COMPONENT_PATTERN.fullmatch(name):
            raise ValueError(f"invalid tool name: {name}")
        if self.server_id and not TOOL_COMPONENT_PATTERN.fullmatch(self.server_id):
            raise ValueError(f"invalid tool server id: {self.server_id}")
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "server_id", self.server_id.strip())
        object.__setattr__(self, "operations", tuple(sorted(set(self.operations))))
        object.__setattr__(self, "path_prefixes", tuple(sorted(set(self.path_prefixes))))
        object.__setattr__(self, "domains", tuple(sorted({item.lower() for item in self.domains})))

    @property
    def canonical_name(self) -> str:
        server = f"/{self.server_id}" if self.server_id else ""
        return f"{self.namespace}{server}/{self.name}"

    def matches(
        self,
        *,
        namespace: str,
        name: str,
        server_id: str = "",
        operation: str = "execute",
        path: str = "",
        domain: str = "",
    ) -> bool:
        if self.namespace not in {"*", namespace}:
            return False
        if self.name not in {"*", name}:
            return False
        # A selector without server scope is local-only. Hosted namespaces
        # must name a server (or explicit wildcard) so ``mcp/foo`` cannot be
        # admitted by an underspecified ``mcp/foo`` selector.
        if server_id and not self.server_id and namespace not in {"builtin", "local"}:
            return False
        if self.server_id not in {"", "*"} and self.server_id != server_id:
            return False
        if self.operations and operation not in self.operations:
            return False
        if self.path_prefixes:
            if not path:
                return False
            candidate = Path(path).resolve()
            if not any(_is_relative_to(candidate, Path(prefix).resolve()) for prefix in self.path_prefixes):
                return False
        if self.domains:
            normalized = domain.lower().rstrip(".")
            if not normalized:
                return False
            if not any(normalized == allowed or normalized.endswith(f".{allowed}") for allowed in self.domains):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "server_id": self.server_id,
            "operations": list(self.operations),
            "path_prefixes": list(self.path_prefixes),
            "domains": list(self.domains),
            "read_only": self.read_only,
        }

    @classmethod
    def parse(cls, value: str | Mapping[str, Any]) -> "ToolSelector":
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                raise ValueError("empty tool selector")
            namespace = "builtin"
            server_id = ""
            name = raw
            if "/" in raw:
                head, name = raw.rsplit("/", 1)
                if "/" in head:
                    namespace, server_id = head.split("/", 1)
                else:
                    namespace = head
            return cls(namespace=namespace, name=name, server_id=server_id)
        item = _mapping(value)
        return cls(
            namespace=str(item.get("namespace") or "builtin"),
            name=str(item.get("name") or ""),
            server_id=str(item.get("server_id") or ""),
            operations=_strings(item.get("operations")),
            path_prefixes=_strings(item.get("path_prefixes")),
            domains=_strings(item.get("domains")),
            read_only=bool(item.get("read_only", False)),
        )


@dataclass(frozen=True, slots=True)
class SkillContextBudget:
    listing_tokens: int = 96
    body_tokens: int = 6_000
    resource_read_tokens: int = 4_000
    invocation_total_tokens: int = 12_000
    restore_tokens: int = 5_000
    max_resource_bytes: int = 512_000
    max_total_resource_bytes: int = 2_000_000
    max_resources: int = 64

    def __post_init__(self) -> None:
        limits = {
            "listing_tokens": (8, 1024),
            "body_tokens": (128, 20_000),
            "resource_read_tokens": (64, 20_000),
            "invocation_total_tokens": (256, 40_000),
            "restore_tokens": (64, 10_000),
            "max_resource_bytes": (128, 5_000_000),
            "max_total_resource_bytes": (128, 20_000_000),
            "max_resources": (0, 512),
        }
        for name, (minimum, maximum) in limits.items():
            value = int(getattr(self, name))
            if value < minimum or value > maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if self.invocation_total_tokens < min(self.body_tokens, 256):
            raise ValueError("invocation total token budget is too small for the body budget")

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SkillContextBudget":
        item = _mapping(data)
        aliases = {
            "listing-tokens": "listing_tokens",
            "body-tokens": "body_tokens",
            "resource-read-tokens": "resource_read_tokens",
            "invocation-total-tokens": "invocation_total_tokens",
            "restore-tokens": "restore_tokens",
            "max-resource-bytes": "max_resource_bytes",
            "max-total-resource-bytes": "max_total_resource_bytes",
            "max-resources": "max_resources",
        }
        normalized = {aliases.get(key, key.replace("-", "_")): value for key, value in item.items()}
        allowed = set(cls.__dataclass_fields__)
        unknown = set(normalized) - allowed
        if unknown:
            raise ValueError(f"unknown context budget fields: {sorted(unknown)}")
        return cls(**{key: int(value) for key, value in normalized.items()})


@dataclass(frozen=True, slots=True)
class SkillHookSpec:
    hook_id: str
    event: SkillHookEvent
    lifetime: SkillHookLifetime = SkillHookLifetime.INVOCATION
    action: str = "emit_event"
    parameters: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.hook_id.strip():
            raise ValueError("hook id is required")
        if self.action not in {"emit_event", "attach_reference", "require_verification"}:
            raise ValueError(f"unsupported declarative skill hook action: {self.action}")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise ValueError("hook timeout must be between 0 and 60 seconds")

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "event": str(self.event),
            "lifetime": str(self.lifetime),
            "action": self.action,
            "parameters": dict(self.parameters),
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, index: int = 0) -> "SkillHookSpec":
        item = _mapping(data)
        return cls(
            hook_id=str(item.get("id") or item.get("hook_id") or f"hook-{index}"),
            event=SkillHookEvent(str(item.get("event") or "pre_invoke")),
            lifetime=SkillHookLifetime(str(item.get("lifetime") or ("once" if item.get("once") else "invocation"))),
            action=str(item.get("action") or "emit_event"),
            parameters=_mapping(item.get("parameters")),
            timeout_seconds=float(item.get("timeout_seconds") or item.get("timeout") or 5),
        )


@dataclass(frozen=True, slots=True)
class SkillInvocationSpec:
    mode: SkillInvocationMode = SkillInvocationMode.INLINE
    agent: str = ""
    max_skill_depth: int = 0

    def __post_init__(self) -> None:
        if self.max_skill_depth < 0 or self.max_skill_depth > 8:
            raise ValueError("max skill depth must be between zero and eight")
        if self.mode is SkillInvocationMode.FORK and not self.agent:
            raise ValueError("forked skill requires an agent type")

    def to_dict(self) -> dict[str, Any]:
        return {"mode": str(self.mode), "agent": self.agent, "max_skill_depth": self.max_skill_depth}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, *, context: str = "") -> "SkillInvocationSpec":
        item = _mapping(data)
        mode = str(item.get("mode") or context or "inline")
        if mode == "forked":
            mode = "fork"
        return cls(
            mode=SkillInvocationMode(mode),
            agent=str(item.get("agent") or ""),
            max_skill_depth=int(item.get("max-skill-depth") or item.get("max_skill_depth") or 0),
        )


@dataclass(frozen=True, slots=True)
class SkillDeclaredMetadata:
    name: str
    description: str
    declared_version: str = "0"
    display_name: str = ""
    when_to_use: str = ""
    argument_hint: str = ""
    user_invocable: bool = True
    model_invocable: bool = True
    path_conditions: tuple[str, ...] = ()
    invocation: SkillInvocationSpec = field(default_factory=SkillInvocationSpec)
    allowed_tools: tuple[ToolSelector, ...] | None = None
    context_budget: SkillContextBudget = field(default_factory=SkillContextBudget)
    resources: tuple[str, ...] = ()
    hooks: tuple[SkillHookSpec, ...] = ()
    model: str = ""
    effort: str = ""
    extensions: dict[str, Any] = field(default_factory=dict)
    schema: str = SKILL_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_skill_name(self.name))
        description = self.description.strip()
        if not description or len(description) > 1_000:
            raise ValueError("skill description is required and limited to 1000 characters")
        object.__setattr__(self, "description", description)
        if self.schema != SKILL_SCHEMA:
            raise ValueError(f"unsupported skill schema: {self.schema}")
        if self.allowed_tools is not None:
            object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        object.__setattr__(self, "resources", tuple(dict.fromkeys(self.resources)))
        object.__setattr__(self, "path_conditions", tuple(dict.fromkeys(self.path_conditions)))

    @property
    def purpose(self) -> str:
        return self.description

    @property
    def preferred_runtime(self) -> str:
        return self.invocation.agent or "CodeWorkerRuntime"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "purpose": self.description,
            "when_to_use": self.when_to_use,
            "declared_version": self.declared_version,
            "argument_hint": self.argument_hint,
            "user_invocable": self.user_invocable,
            "model_invocable": self.model_invocable,
            "path_conditions": list(self.path_conditions),
            "invocation": self.invocation.to_dict(),
            "preferred_runtime": self.preferred_runtime,
            "allowed_tools": None if self.allowed_tools is None else [item.to_dict() for item in self.allowed_tools],
            "context_budget": self.context_budget.to_dict(),
            "resources": list(self.resources),
            "hooks": [item.to_dict() for item in self.hooks],
            "model": self.model,
            "effort": self.effort,
            "extensions": dict(self.extensions),
        }


@dataclass(frozen=True, slots=True)
class SkillProvenance:
    source_kind: SkillSourceKind
    source_id: str
    source_namespace: str
    origin_uri: str
    canonical_root: str
    discovery_root: str
    trust_tier: SkillTrustTier
    loader_revision: str = "skill-loader-v1"
    plugin_id: str = ""
    plugin_version: str = ""
    server_id: str = ""
    source_rank: int = 0
    project_distance: int = 0
    configured_order: int = 0
    discovered_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id or not self.source_namespace or not self.canonical_root:
            raise ValueError("skill provenance requires source id, namespace, and canonical root")
        if self.source_kind in {SkillSourceKind.PLUGIN, SkillSourceKind.MCP} and not self.source_namespace:
            raise ValueError("plugin and MCP skills require a namespace")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": str(self.source_kind),
            "source_id": self.source_id,
            "source_namespace": self.source_namespace,
            "origin_uri": self.origin_uri,
            "canonical_root": self.canonical_root,
            "discovery_root": self.discovery_root,
            "trust_tier": str(self.trust_tier),
            "loader_revision": self.loader_revision,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "server_id": self.server_id,
            "source_rank": self.source_rank,
            "project_distance": self.project_distance,
            "configured_order": self.configured_order,
            "discovered_at": self.discovered_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SkillResourceDescriptor:
    relative_path: str
    kind: SkillResourceKind
    media_type: str
    size_bytes: int
    digest: str
    immutable_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "kind": str(self.kind),
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "digest": self.digest,
            "immutable_ref": self.immutable_ref,
        }


@dataclass(frozen=True, slots=True)
class SkillBodyDescriptor:
    size_bytes: int
    token_estimate: int
    digest: str
    immutable_ref: str
    line_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SkillVersionRef:
    skill_id: str
    qualified_name: str
    source_id: str
    declared_version: str
    content_digest: str
    body_digest: str
    resource_manifest_digest: str
    policy_digest: str
    provenance_digest: str
    registry_generation: int
    revocation_epoch: int = 0
    schema: str = SKILL_SCHEMA

    def __post_init__(self) -> None:
        required = (
            self.skill_id,
            self.qualified_name,
            self.source_id,
            self.content_digest,
            self.body_digest,
            self.policy_digest,
            self.provenance_digest,
        )
        if not all(required):
            raise ValueError("skill version reference is incomplete")

    @property
    def immutable_ref(self) -> str:
        return f"skill://{self.qualified_name}@sha256:{self.content_digest}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "immutable_ref": self.immutable_ref}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SkillVersionRef":
        item = _mapping(data)
        return cls(**{name: item.get(name) for name in cls.__dataclass_fields__ if name in item})


@dataclass(frozen=True, slots=True)
class SkillRevision:
    metadata: SkillDeclaredMetadata
    provenance: SkillProvenance
    version_ref: SkillVersionRef
    body: SkillBodyDescriptor
    resources: tuple[SkillResourceDescriptor, ...]
    skill_root: str
    skill_file: str
    lifecycle: SkillRevisionLifecycle = SkillRevisionLifecycle.AVAILABLE
    validated_at: str = field(default_factory=utc_now)

    @property
    def qualified_name(self) -> str:
        return self.version_ref.qualified_name

    def to_dict(self, *, include_paths: bool = False) -> dict[str, Any]:
        result = {
            "metadata": self.metadata.to_dict(),
            "provenance": self.provenance.to_dict(),
            "version_ref": self.version_ref.to_dict(),
            "body": self.body.to_dict(),
            "resources": [item.to_dict() for item in self.resources],
            "lifecycle": str(self.lifecycle),
            "validated_at": self.validated_at,
        }
        if include_paths:
            result["skill_root"] = self.skill_root
            result["skill_file"] = self.skill_file
        return result


@dataclass(frozen=True, slots=True)
class SkillListingEntry:
    name: str
    qualified_name: str
    description: str
    when_to_use: str
    source_kind: SkillSourceKind
    source_namespace: str
    invocation_mode: SkillInvocationMode
    preferred_runtime: str
    declared_version: str
    content_digest: str
    token_estimate: int
    user_invocable: bool
    model_invocable: bool
    lifecycle: SkillLifecycle
    shadowed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "source_kind": str(self.source_kind),
            "invocation_mode": str(self.invocation_mode),
            "lifecycle": str(self.lifecycle),
        }


@dataclass(frozen=True, slots=True)
class SkillTombstone:
    name: str
    source_kind: SkillSourceKind
    source_id: str
    reason: str
    revocation_epoch: int
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class SkillRegistrySnapshot:
    generation: int
    revisions_by_ref: dict[str, SkillRevision]
    active_by_qualified_name: dict[str, str]
    aliases: dict[str, str]
    shadowed: dict[str, tuple[str, ...]]
    tombstones: dict[str, SkillTombstone]
    source_status: dict[str, dict[str, Any]]
    created_at: str = field(default_factory=utc_now)
    snapshot_id: str = field(default_factory=lambda: new_id("skillsnap"))

    @classmethod
    def empty(cls) -> "SkillRegistrySnapshot":
        return cls(
            generation=0,
            revisions_by_ref={},
            active_by_qualified_name={},
            aliases={},
            shadowed={},
            tombstones={},
            source_status={},
        )

    def get_revision(self, ref: str) -> SkillRevision | None:
        return self.revisions_by_ref.get(ref)

    def active_revision(self, qualified_name: str) -> SkillRevision | None:
        ref = self.active_by_qualified_name.get(qualified_name)
        return self.revisions_by_ref.get(ref or "")

    def to_dict(self, *, include_revisions: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "snapshot_id": self.snapshot_id,
            "generation": self.generation,
            "active_by_qualified_name": dict(self.active_by_qualified_name),
            "aliases": dict(self.aliases),
            "shadowed": {key: list(value) for key, value in self.shadowed.items()},
            "tombstones": {key: asdict(value) for key, value in self.tombstones.items()},
            "source_status": {key: dict(value) for key, value in self.source_status.items()},
            "created_at": self.created_at,
        }
        if include_revisions:
            result["revisions"] = {
                key: revision.to_dict() for key, revision in self.revisions_by_ref.items()
            }
        return result


@dataclass(frozen=True, slots=True)
class SkillBody:
    version_ref: SkillVersionRef
    text: str
    token_estimate: int
    truncated: bool = False
    loaded_at: str = field(default_factory=utc_now)

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        result = {
            "version_ref": self.version_ref.to_dict(),
            "token_estimate": self.token_estimate,
            "truncated": self.truncated,
            "loaded_at": self.loaded_at,
        }
        if include_text:
            result["text"] = self.text
        return result


@dataclass(frozen=True, slots=True)
class LoadedSkillResource:
    version_ref: SkillVersionRef
    descriptor: SkillResourceDescriptor
    content: str | bytes
    token_estimate: int
    truncated: bool = False

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        result = {
            "version_ref": self.version_ref.to_dict(),
            "descriptor": self.descriptor.to_dict(),
            "token_estimate": self.token_estimate,
            "truncated": self.truncated,
        }
        if include_content:
            result["content"] = self.content
        return result


@dataclass(frozen=True, slots=True)
class SkillPolicySnapshot:
    snapshot_id: str
    invocation_id: str
    session_id: str
    version_ref: SkillVersionRef
    effective_tools: tuple[ToolSelector, ...] | None
    parent_snapshot_ids: tuple[str, ...]
    policy_digest: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "invocation_id": self.invocation_id,
            "session_id": self.session_id,
            "version_ref": self.version_ref.to_dict(),
            "effective_tools": None if self.effective_tools is None else [item.to_dict() for item in self.effective_tools],
            "parent_snapshot_ids": list(self.parent_snapshot_ids),
            "policy_digest": self.policy_digest,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class SkillInvocationRequest:
    run_id: str
    task_id: str
    session_id: str
    agent_id: str
    skill_name: str
    arguments: dict[str, Any]
    node_id: str | None = None
    worker_request_id: str = ""
    parent_tool_use_id: str = ""
    requested_version_ref: SkillVersionRef | None = None
    parent_policy_snapshot_ids: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    attachment_refs: tuple[str, ...] = ()
    skill_depth: int = 0
    idempotency_key: str = ""
    interactive: bool = True
    headless: bool = False
    invocation_id: str = field(default_factory=lambda: new_id("skillinv"))
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not all((self.run_id, self.task_id, self.session_id, self.skill_name)):
            raise ValueError("skill invocation requires run/task/session/skill identity")
        if self.skill_depth < 0 or self.skill_depth > 8:
            raise ValueError("skill invocation depth is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "requested_version_ref": self.requested_version_ref.to_dict() if self.requested_version_ref else None,
            "parent_policy_snapshot_ids": list(self.parent_policy_snapshot_ids),
            "context_refs": list(self.context_refs),
            "attachment_refs": list(self.attachment_refs),
        }


@dataclass(frozen=True, slots=True)
class SkillAttachment:
    attachment_id: str
    kind: str
    immutable_ref: str
    label: str
    token_estimate: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SkillMessageDelta:
    role: str
    content: str
    parent_tool_use_id: str = ""
    meta: bool = True
    hidden_from_user: bool = True
    attachments: tuple[SkillAttachment, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "parent_tool_use_id": self.parent_tool_use_id,
            "meta": self.meta,
            "hidden_from_user": self.hidden_from_user,
            "attachments": [item.to_dict() for item in self.attachments],
        }


@dataclass(frozen=True, slots=True)
class SkillForkRequest:
    invocation_id: str
    run_id: str
    task_id: str
    parent_session_id: str
    parent_agent_id: str
    agent_type: str
    version_ref: SkillVersionRef
    arguments_digest: str
    policy_snapshot: SkillPolicySnapshot
    context_refs: tuple[str, ...]
    attachment_refs: tuple[str, ...]
    body_ref: str
    requested_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "version_ref": self.version_ref.to_dict(),
            "policy_snapshot": self.policy_snapshot.to_dict(),
            "context_refs": list(self.context_refs),
            "attachment_refs": list(self.attachment_refs),
        }


@dataclass(frozen=True, slots=True)
class SkillOutcomeProjection:
    invocation_id: str
    session_id: str
    agent_id: str
    version_ref: SkillVersionRef
    policy_snapshot_digest: str
    revocation_epoch: int
    status: SkillInvocationStatus
    outcome_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    stale: bool = False
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "version_ref": self.version_ref.to_dict(),
            "status": str(self.status),
            "outcome_refs": list(self.outcome_refs),
            "evidence_refs": list(self.evidence_refs),
            "artifact_refs": list(self.artifact_refs),
        }


@dataclass(frozen=True, slots=True)
class InvokedSkillState:
    invocation_id: str
    run_id: str
    task_id: str
    session_id: str
    agent_id: str
    version_ref: SkillVersionRef
    status: SkillInvocationStatus
    policy_snapshot: SkillPolicySnapshot | None = None
    hook_lease_ids: tuple[str, ...] = ()
    attachment_refs: tuple[str, ...] = ()
    message_delta_refs: tuple[str, ...] = ()
    fork_request_id: str = ""
    outcome_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    error_code: str = ""
    error_message: str = ""
    requested_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    completed_at: str = ""
    revision: int = 0

    def transition(self, status: SkillInvocationStatus, **changes: Any) -> "InvokedSkillState":
        if self.status.terminal:
            raise ValueError(f"terminal skill invocation cannot transition from {self.status}")
        allowed = {
            SkillInvocationStatus.REQUESTED: {SkillInvocationStatus.REF_RESOLVED, SkillInvocationStatus.FAILED, SkillInvocationStatus.BLOCKED},
            SkillInvocationStatus.REF_RESOLVED: {SkillInvocationStatus.BODY_LOADED, SkillInvocationStatus.FAILED, SkillInvocationStatus.REVOKED},
            SkillInvocationStatus.BODY_LOADED: {SkillInvocationStatus.POLICY_BOUND, SkillInvocationStatus.FAILED, SkillInvocationStatus.REVOKED},
            SkillInvocationStatus.POLICY_BOUND: {SkillInvocationStatus.HOOKS_REGISTERED, SkillInvocationStatus.INLINE_ACTIVE, SkillInvocationStatus.FORK_PENDING, SkillInvocationStatus.FAILED},
            SkillInvocationStatus.HOOKS_REGISTERED: {SkillInvocationStatus.INLINE_ACTIVE, SkillInvocationStatus.FORK_PENDING, SkillInvocationStatus.FAILED, SkillInvocationStatus.CANCELLED},
            SkillInvocationStatus.INLINE_ACTIVE: {SkillInvocationStatus.COMPLETED, SkillInvocationStatus.FAILED, SkillInvocationStatus.CANCELLED, SkillInvocationStatus.REVOKED},
            SkillInvocationStatus.FORK_PENDING: {SkillInvocationStatus.COMPLETED, SkillInvocationStatus.FAILED, SkillInvocationStatus.CANCELLED, SkillInvocationStatus.REVOKED},
        }
        if status not in allowed.get(self.status, set()):
            raise ValueError(f"invalid skill invocation transition: {self.status} -> {status}")
        terminal_at = utc_now() if status.terminal else self.completed_at
        return replace(
            self,
            status=status,
            updated_at=utc_now(),
            completed_at=terminal_at,
            revision=self.revision + 1,
            **changes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "version_ref": self.version_ref.to_dict(),
            "status": str(self.status),
            "policy_snapshot": self.policy_snapshot.to_dict() if self.policy_snapshot else None,
            "hook_lease_ids": list(self.hook_lease_ids),
            "attachment_refs": list(self.attachment_refs),
            "message_delta_refs": list(self.message_delta_refs),
            "outcome_refs": list(self.outcome_refs),
            "evidence_refs": list(self.evidence_refs),
            "artifact_refs": list(self.artifact_refs),
        }


@dataclass(frozen=True, slots=True)
class SkillInvocationPlan:
    request: SkillInvocationRequest
    revision: SkillRevision
    body: SkillBody
    resources: tuple[LoadedSkillResource, ...]
    policy_snapshot: SkillPolicySnapshot
    messages: tuple[SkillMessageDelta, ...]
    attachments: tuple[SkillAttachment, ...]
    state: InvokedSkillState
    permission_events: tuple[dict[str, Any], ...] = ()
    fork_request: SkillForkRequest | None = None

    def to_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "revision": self.revision.to_dict(),
            "body": self.body.to_dict(include_text=include_body),
            "resources": [item.to_dict(include_content=False) for item in self.resources],
            "policy_snapshot": self.policy_snapshot.to_dict(),
            "messages": [item.to_dict() for item in self.messages],
            "attachments": [item.to_dict() for item in self.attachments],
            "state": self.state.to_dict(),
            "permission_events": [dict(item) for item in self.permission_events],
            "fork_request": self.fork_request.to_dict() if self.fork_request else None,
        }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
