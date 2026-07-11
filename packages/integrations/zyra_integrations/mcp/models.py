from __future__ import annotations

"""Zyra-owned domain models shared by the MCP client runtime.

The module intentionally has no dependency on an MCP SDK.  It models the
parts of the protocol that Zyra must own across transports: configuration and
provenance, connection/auth state, capability projection, typed content,
pagination, long-running tasks, sampling, elicitation, and server instruction
deltas.  Every model validates untrusted wire data before it can enter a
session store or tool registry.
"""

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, ClassVar, TypeAlias
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from uuid import uuid4


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_mcp_id(prefix: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", prefix.casefold()).strip("-") or "mcp"
    return f"{normalized}_{uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(to_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_digest(value: Any, *, prefix: str = "sha256") -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def to_json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("MCP JSON values cannot contain NaN or infinity")
        return value
    if isinstance(value, StrEnum):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return to_json_value(value.to_dict())
    if isinstance(value, Mapping):
        output: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("MCP JSON object keys must be strings")
            output[key] = to_json_value(item)
        return output
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray | memoryview):
        return [to_json_value(item) for item in value]
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


_SECRET_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "api-key",
    "api_key",
    "private-key",
    "private_key",
)


def is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("_", "-")
    return any(fragment.replace("_", "-") in normalized for fragment in _SECRET_KEY_FRAGMENTS)


def redact_value(value: Any, *, replacement: str = "<redacted>") -> JsonValue:
    if isinstance(value, Mapping):
        return {
            str(key): replacement if is_sensitive_key(str(key)) else redact_value(item, replacement=replacement)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray | memoryview):
        return [redact_value(item, replacement=replacement) for item in value]
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<binary:{len(value)} bytes>"
    return to_json_value(value)


def require_non_empty(value: Any, name: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    selected = value.strip()
    if not selected:
        raise ValueError(f"{name} is required")
    if len(selected) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    return selected


def optional_string(value: Any, name: str, *, max_length: int = 65536) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    return value


def require_mapping(value: Any, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    converted = to_json_value(value)
    assert isinstance(converted, dict)
    return converted


class McpModelError(ValueError):
    """Raised when untrusted MCP data fails domain validation."""


class McpTransportKind(StrEnum):
    IN_PROCESS = "in_process"
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class McpConfigScope(StrEnum):
    CLAUDE_AI = "claudeai"
    PLUGIN = "plugin"
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"
    ENTERPRISE = "enterprise"
    DYNAMIC = "dynamic"
    SDK = "sdk"


class McpApprovalState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class McpConnectionState(StrEnum):
    PENDING = "pending"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    NEEDS_AUTH = "needs_auth"
    FAILED = "failed"
    DISABLED = "disabled"
    RECONNECTING = "reconnecting"
    CLOSING = "closing"
    CLOSED = "closed"


class McpAuthState(StrEnum):
    UNKNOWN = "unknown"
    NOT_REQUIRED = "not_required"
    NEEDS_AUTH = "needs_auth"
    AUTHENTICATING = "authenticating"
    AUTHENTICATED = "authenticated"
    REFRESHING = "refreshing"
    REVOKED = "revoked"
    FAILED = "failed"


class McpCapabilityKind(StrEnum):
    TOOLS = "tools"
    RESOURCES = "resources"
    PROMPTS = "prompts"
    LOGGING = "logging"
    SAMPLING = "sampling"
    ELICITATION = "elicitation"
    TASKS = "tasks"
    INSTRUCTIONS = "instructions"


class McpContentKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    BINARY = "binary"
    RESOURCE = "resource"
    RESOURCE_LINK = "resource_link"
    STRUCTURED = "structured"


class McpToolTaskSupport(StrEnum):
    FORBIDDEN = "forbidden"
    OPTIONAL = "optional"
    REQUIRED = "required"


class McpTaskStatus(StrEnum):
    PENDING = "pending"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INPUT_REQUIRED = "input_required"
    OUTCOME_UNKNOWN = "outcome_unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            McpTaskStatus.COMPLETED,
            McpTaskStatus.FAILED,
            McpTaskStatus.CANCELLED,
            McpTaskStatus.INPUT_REQUIRED,
            McpTaskStatus.OUTCOME_UNKNOWN,
        }


class McpSamplingDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class McpElicitationMode(StrEnum):
    FORM = "form"
    URL = "url"


class McpElicitationAction(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


class McpElicitationStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class McpInstructionsDeltaAction(StrEnum):
    REPLACE = "replace"
    APPEND = "append"
    CLEAR = "clear"


class McpRiskClass(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    DESTRUCTIVE = "destructive"
    OPEN_WORLD = "open_world"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """Validated server configuration with source and policy provenance."""

    server_id: str
    name: str
    transport: McpTransportKind
    scope: McpConfigScope
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    disabled: bool = False
    approval: McpApprovalState = McpApprovalState.NOT_REQUIRED
    source_path: str = ""
    source_revision: str = ""
    policy_provenance: tuple[str, ...] = ()
    filter_provenance: tuple[str, ...] = ()
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 30.0
    max_response_bytes: int = 8 * 1024 * 1024
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        object.__setattr__(self, "name", require_non_empty(self.name, "name", max_length=256))
        if not isinstance(self.transport, McpTransportKind):
            object.__setattr__(self, "transport", McpTransportKind(str(self.transport)))
        if not isinstance(self.scope, McpConfigScope):
            object.__setattr__(self, "scope", McpConfigScope(str(self.scope)))
        if not isinstance(self.approval, McpApprovalState):
            object.__setattr__(self, "approval", McpApprovalState(str(self.approval)))
        command = optional_string(self.command, "command", max_length=32768).strip()
        url = optional_string(self.url, "url", max_length=32768).strip()
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "args", tuple(require_non_empty(item, "args[]", max_length=32768) for item in self.args))
        object.__setattr__(self, "env", _string_mapping(self.env, "env"))
        object.__setattr__(self, "headers", _string_mapping(self.headers, "headers"))
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))
        object.__setattr__(self, "policy_provenance", _string_tuple(self.policy_provenance, "policy_provenance"))
        object.__setattr__(self, "filter_provenance", _string_tuple(self.filter_provenance, "filter_provenance"))
        if self.transport is McpTransportKind.STDIO:
            if not command:
                raise McpModelError("stdio MCP configuration requires command")
            if url:
                raise McpModelError("stdio MCP configuration cannot include url")
        elif self.transport is McpTransportKind.STREAMABLE_HTTP:
            if not url:
                raise McpModelError("streamable HTTP MCP configuration requires url")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise McpModelError("streamable HTTP MCP url must be absolute http(s)")
            if parsed.username or parsed.password:
                raise McpModelError("credentials must not be embedded in MCP url")
            if command:
                raise McpModelError("streamable HTTP MCP configuration cannot include command")
        elif self.transport is McpTransportKind.IN_PROCESS and not command and not url:
            # In-process transports are resolved by registered server_id.
            pass
        if self.connect_timeout_seconds <= 0 or not math.isfinite(self.connect_timeout_seconds):
            raise McpModelError("connect_timeout_seconds must be positive and finite")
        if self.request_timeout_seconds <= 0 or not math.isfinite(self.request_timeout_seconds):
            raise McpModelError("request_timeout_seconds must be positive and finite")
        if not 1024 <= self.max_response_bytes <= 256 * 1024 * 1024:
            raise McpModelError("max_response_bytes must be between 1 KiB and 256 MiB")

    @property
    def connectable(self) -> bool:
        return not self.disabled and self.approval not in {McpApprovalState.PENDING, McpApprovalState.REJECTED}

    @property
    def signature(self) -> str:
        if self.transport is McpTransportKind.STDIO:
            return "stdio:" + canonical_json([self.command, *self.args])
        if self.transport is McpTransportKind.STREAMABLE_HTTP:
            return f"url:{self.url}"
        return f"in-process:{self.server_id}"

    @property
    def fingerprint(self) -> str:
        return stable_digest(
            {
                "server_id": self.server_id,
                "name": self.name,
                "transport": self.transport,
                "scope": self.scope,
                "signature": self.signature,
                "disabled": self.disabled,
                "approval": self.approval,
                "source_revision": self.source_revision,
                "policy_provenance": self.policy_provenance,
                "filter_provenance": self.filter_provenance,
                "env_keys": sorted(self.env),
                "header_keys": sorted(self.headers),
            }
        )

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "name": self.name,
            "transport": str(self.transport),
            "scope": str(self.scope),
            "command": self.command,
            "args": list(_redact_command_args(self.args)),
            "url": _redact_url(self.url),
            "env": {key: "<redacted>" if is_sensitive_key(key) else value for key, value in self.env.items()},
            "headers": {key: "<redacted>" for key in self.headers},
            "disabled": self.disabled,
            "approval": str(self.approval),
            "source_path": self.source_path,
            "source_revision": self.source_revision,
            "policy_provenance": list(self.policy_provenance),
            "filter_provenance": list(self.filter_provenance),
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "metadata": dict(self.metadata),
            "signature": _safe_server_signature(self),
            "signature_digest": stable_digest(self.signature),
            "fingerprint": self.fingerprint,
            "connectable": self.connectable,
        }

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, JsonValue]:
        payload = self.safe_dict()
        if include_secrets:
            payload["env"] = dict(self.env)
            payload["headers"] = dict(self.headers)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "McpServerConfig":
        return cls(
            server_id=value.get("server_id", ""),
            name=value.get("name", ""),
            transport=McpTransportKind(str(value.get("transport", ""))),
            scope=McpConfigScope(str(value.get("scope", McpConfigScope.DYNAMIC))),
            command=value.get("command", ""),
            args=tuple(value.get("args") or ()),
            url=value.get("url", ""),
            env=value.get("env") or {},
            headers=value.get("headers") or {},
            disabled=bool(value.get("disabled", False)),
            approval=McpApprovalState(str(value.get("approval", McpApprovalState.NOT_REQUIRED))),
            source_path=value.get("source_path", ""),
            source_revision=value.get("source_revision", ""),
            policy_provenance=tuple(value.get("policy_provenance") or ()),
            filter_provenance=tuple(value.get("filter_provenance") or ()),
            connect_timeout_seconds=float(value.get("connect_timeout_seconds", 10.0)),
            request_timeout_seconds=float(value.get("request_timeout_seconds", 30.0)),
            max_response_bytes=int(value.get("max_response_bytes", 8 * 1024 * 1024)),
            metadata=value.get("metadata") or {},
        )


_CONNECTION_TRANSITIONS: Mapping[McpConnectionState, frozenset[McpConnectionState]] = {
    McpConnectionState.PENDING: frozenset({McpConnectionState.CONNECTING, McpConnectionState.DISABLED, McpConnectionState.CLOSED}),
    McpConnectionState.CONNECTING: frozenset({McpConnectionState.CONNECTED, McpConnectionState.NEEDS_AUTH, McpConnectionState.FAILED, McpConnectionState.CLOSING}),
    McpConnectionState.CONNECTED: frozenset({McpConnectionState.RECONNECTING, McpConnectionState.NEEDS_AUTH, McpConnectionState.FAILED, McpConnectionState.CLOSING, McpConnectionState.DISABLED}),
    McpConnectionState.NEEDS_AUTH: frozenset({McpConnectionState.CONNECTING, McpConnectionState.DISABLED, McpConnectionState.CLOSING}),
    McpConnectionState.FAILED: frozenset({McpConnectionState.CONNECTING, McpConnectionState.RECONNECTING, McpConnectionState.DISABLED, McpConnectionState.CLOSING}),
    McpConnectionState.DISABLED: frozenset({McpConnectionState.PENDING, McpConnectionState.CLOSED}),
    McpConnectionState.RECONNECTING: frozenset({McpConnectionState.CONNECTED, McpConnectionState.NEEDS_AUTH, McpConnectionState.FAILED, McpConnectionState.CLOSING}),
    McpConnectionState.CLOSING: frozenset({McpConnectionState.CLOSED, McpConnectionState.FAILED}),
    McpConnectionState.CLOSED: frozenset({McpConnectionState.CONNECTING, McpConnectionState.DISABLED}),
}


@dataclass(frozen=True, slots=True)
class McpConnectionSnapshot:
    server_id: str
    state: McpConnectionState = McpConnectionState.PENDING
    revision: int = 0
    generation: int = 0
    config_fingerprint: str = ""
    protocol_version: str = ""
    session_id: str = ""
    error_code: str = ""
    error_message: str = ""
    last_transition_at: str = field(default_factory=utc_now_iso)
    connected_at: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        if not isinstance(self.state, McpConnectionState):
            object.__setattr__(self, "state", McpConnectionState(str(self.state)))
        if self.revision < 0 or self.generation < 0:
            raise McpModelError("connection revision and generation cannot be negative")
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))

    @property
    def healthy(self) -> bool:
        return self.state is McpConnectionState.CONNECTED and not self.error_code

    def transition(
        self,
        state: McpConnectionState,
        *,
        error_code: str = "",
        error_message: str = "",
        protocol_version: str | None = None,
        session_id: str | None = None,
        config_fingerprint: str | None = None,
        metadata: Mapping[str, str] | None = None,
        now: str | None = None,
    ) -> "McpConnectionSnapshot":
        selected = McpConnectionState(str(state))
        if selected is not self.state and selected not in _CONNECTION_TRANSITIONS[self.state]:
            raise McpModelError(f"invalid MCP connection transition: {self.state} -> {selected}")
        if selected is McpConnectionState.FAILED and not error_code:
            raise McpModelError("failed connection transition requires error_code")
        if selected is McpConnectionState.NEEDS_AUTH and error_code and error_code != "needs_auth":
            raise McpModelError("needs-auth transition has incompatible error_code")
        timestamp = now or utc_now_iso()
        generation = self.generation + int(selected in {McpConnectionState.CONNECTING, McpConnectionState.RECONNECTING})
        connected_at = timestamp if selected is McpConnectionState.CONNECTED else self.connected_at
        return replace(
            self,
            state=selected,
            revision=self.revision + 1,
            generation=generation,
            config_fingerprint=self.config_fingerprint if config_fingerprint is None else config_fingerprint,
            protocol_version=self.protocol_version if protocol_version is None else protocol_version,
            session_id=self.session_id if session_id is None else session_id,
            error_code=error_code,
            error_message=error_message[:4096],
            last_transition_at=timestamp,
            connected_at=connected_at,
            metadata={**self.metadata, **dict(metadata or {})},
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "state": str(self.state),
            "healthy": self.healthy,
            "revision": self.revision,
            "generation": self.generation,
            "config_fingerprint": self.config_fingerprint,
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "last_transition_at": self.last_transition_at,
            "connected_at": self.connected_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class McpServerCapabilities:
    kinds: frozenset[McpCapabilityKind] = frozenset()
    tools_list_changed: bool = False
    resources_subscribe: bool = False
    resources_list_changed: bool = False
    prompts_list_changed: bool = False
    task_requests: bool = False
    experimental: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kinds", frozenset(McpCapabilityKind(str(item)) for item in self.kinds))
        object.__setattr__(self, "experimental", require_mapping(self.experimental, "experimental"))

    def supports(self, kind: McpCapabilityKind | str) -> bool:
        return McpCapabilityKind(str(kind)) in self.kinds

    @classmethod
    def from_initialize_result(cls, value: Mapping[str, Any]) -> "McpServerCapabilities":
        capabilities = value.get("capabilities") or {}
        if not isinstance(capabilities, Mapping):
            raise McpModelError("initialize result capabilities must be an object")
        kinds: set[McpCapabilityKind] = set()
        for key, kind in {
            "tools": McpCapabilityKind.TOOLS,
            "resources": McpCapabilityKind.RESOURCES,
            "prompts": McpCapabilityKind.PROMPTS,
            "logging": McpCapabilityKind.LOGGING,
            "sampling": McpCapabilityKind.SAMPLING,
            "elicitation": McpCapabilityKind.ELICITATION,
            "tasks": McpCapabilityKind.TASKS,
        }.items():
            if key in capabilities:
                kinds.add(kind)
        if value.get("instructions"):
            kinds.add(McpCapabilityKind.INSTRUCTIONS)
        tools = capabilities.get("tools") if isinstance(capabilities.get("tools"), Mapping) else {}
        resources = capabilities.get("resources") if isinstance(capabilities.get("resources"), Mapping) else {}
        prompts = capabilities.get("prompts") if isinstance(capabilities.get("prompts"), Mapping) else {}
        tasks = capabilities.get("tasks") if isinstance(capabilities.get("tasks"), Mapping) else {}
        experimental = capabilities.get("experimental") if isinstance(capabilities.get("experimental"), Mapping) else {}
        return cls(
            kinds=frozenset(kinds),
            tools_list_changed=bool(tools.get("listChanged", False)),
            resources_subscribe=bool(resources.get("subscribe", False)),
            resources_list_changed=bool(resources.get("listChanged", False)),
            prompts_list_changed=bool(prompts.get("listChanged", False)),
            task_requests=bool(tasks.get("requests", False)),
            experimental=require_mapping(experimental, "capabilities.experimental"),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kinds": sorted(str(item) for item in self.kinds),
            "tools_list_changed": self.tools_list_changed,
            "resources_subscribe": self.resources_subscribe,
            "resources_list_changed": self.resources_list_changed,
            "prompts_list_changed": self.prompts_list_changed,
            "task_requests": self.task_requests,
            "experimental": dict(self.experimental),
        }


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    server_id: str
    remote_name: str
    local_name: str
    description: str
    input_schema: Mapping[str, JsonValue]
    output_schema: Mapping[str, JsonValue] = field(default_factory=dict)
    risk: McpRiskClass = McpRiskClass.UNKNOWN
    task_support: McpToolTaskSupport = McpToolTaskSupport.FORBIDDEN
    projection_revision: int = 0
    annotations: Mapping[str, JsonValue] = field(default_factory=dict)
    trusted_meta: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        object.__setattr__(self, "remote_name", require_non_empty(self.remote_name, "remote_name", max_length=256))
        object.__setattr__(self, "local_name", require_non_empty(self.local_name, "local_name", max_length=256))
        optional_string(self.description, "description")
        schema = require_mapping(self.input_schema, "input_schema")
        if schema.get("type") == "object" and "properties" not in schema:
            schema["properties"] = {}
        object.__setattr__(self, "input_schema", schema)
        object.__setattr__(self, "output_schema", require_mapping(self.output_schema, "output_schema"))
        object.__setattr__(self, "annotations", require_mapping(self.annotations, "annotations"))
        object.__setattr__(self, "trusted_meta", require_mapping(self.trusted_meta, "trusted_meta"))
        if not isinstance(self.risk, McpRiskClass):
            object.__setattr__(self, "risk", McpRiskClass(str(self.risk)))
        if not isinstance(self.task_support, McpToolTaskSupport):
            object.__setattr__(self, "task_support", McpToolTaskSupport(str(self.task_support)))
        if self.projection_revision < 0:
            raise McpModelError("projection_revision cannot be negative")

    @property
    def identity(self) -> str:
        return f"mcp:{self.server_id}:{self.remote_name}"

    @property
    def schema_fingerprint(self) -> str:
        return stable_digest({"input": self.input_schema, "output": self.output_schema})

    @classmethod
    def from_wire(
        cls,
        server_id: str,
        value: Mapping[str, Any],
        *,
        local_name: str | None = None,
        projection_revision: int = 0,
    ) -> "McpToolDescriptor":
        remote_name = require_non_empty(value.get("name"), "tool.name", max_length=256)
        execution = value.get("execution") if isinstance(value.get("execution"), Mapping) else {}
        support = execution.get("taskSupport", McpToolTaskSupport.FORBIDDEN)
        annotations = value.get("annotations") if isinstance(value.get("annotations"), Mapping) else {}
        risk = _risk_from_annotations(annotations)
        return cls(
            server_id=server_id,
            remote_name=remote_name,
            local_name=local_name or _normalize_local_name(server_id, remote_name),
            description=optional_string(value.get("description"), "tool.description"),
            input_schema=value.get("inputSchema") or {"type": "object", "properties": {}},
            output_schema=value.get("outputSchema") or {},
            risk=risk,
            task_support=McpToolTaskSupport(str(support)),
            projection_revision=projection_revision,
            annotations=annotations,
            trusted_meta=value.get("_meta") if isinstance(value.get("_meta"), Mapping) else {},
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "remote_name": self.remote_name,
            "local_name": self.local_name,
            "identity": self.identity,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "schema_fingerprint": self.schema_fingerprint,
            "risk": str(self.risk),
            "task_support": str(self.task_support),
            "projection_revision": self.projection_revision,
            "annotations": dict(self.annotations),
            "trusted_meta": redact_value(self.trusted_meta),
        }


@dataclass(frozen=True, slots=True)
class McpResourceDescriptor:
    server_id: str
    uri: str
    name: str
    description: str = ""
    mime_type: str = "application/octet-stream"
    size: int | None = None
    annotations: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        object.__setattr__(self, "uri", _validate_resource_uri(self.uri))
        object.__setattr__(self, "name", require_non_empty(self.name, "resource.name", max_length=1024))
        optional_string(self.description, "resource.description")
        object.__setattr__(self, "mime_type", optional_string(self.mime_type, "resource.mimeType", max_length=256) or "application/octet-stream")
        if self.size is not None and self.size < 0:
            raise McpModelError("resource size cannot be negative")
        object.__setattr__(self, "annotations", require_mapping(self.annotations, "resource.annotations"))

    @classmethod
    def from_wire(cls, server_id: str, value: Mapping[str, Any]) -> "McpResourceDescriptor":
        return cls(
            server_id=server_id,
            uri=value.get("uri", ""),
            name=value.get("name") or value.get("uri", ""),
            description=value.get("description", ""),
            mime_type=value.get("mimeType", "application/octet-stream"),
            size=int(value["size"]) if isinstance(value.get("size"), int) else None,
            annotations=value.get("annotations") if isinstance(value.get("annotations"), Mapping) else {},
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mime_type": self.mime_type,
            "size": self.size,
            "annotations": dict(self.annotations),
        }


@dataclass(frozen=True, slots=True)
class McpPromptArgument:
    name: str
    description: str = ""
    required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_non_empty(self.name, "prompt.argument.name", max_length=256))
        optional_string(self.description, "prompt.argument.description")

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> "McpPromptArgument":
        return cls(value.get("name", ""), value.get("description", ""), bool(value.get("required", False)))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"name": self.name, "description": self.description, "required": self.required}


@dataclass(frozen=True, slots=True)
class McpPromptDescriptor:
    server_id: str
    remote_name: str
    description: str = ""
    arguments: tuple[McpPromptArgument, ...] = ()
    projection_revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        object.__setattr__(self, "remote_name", require_non_empty(self.remote_name, "prompt.name", max_length=256))
        optional_string(self.description, "prompt.description")
        names = [argument.name for argument in self.arguments]
        if len(names) != len(set(names)):
            raise McpModelError("prompt argument names must be unique")
        if self.projection_revision < 0:
            raise McpModelError("projection_revision cannot be negative")

    @classmethod
    def from_wire(cls, server_id: str, value: Mapping[str, Any], *, projection_revision: int = 0) -> "McpPromptDescriptor":
        raw_arguments = value.get("arguments") or []
        if not isinstance(raw_arguments, list):
            raise McpModelError("prompt.arguments must be an array")
        return cls(
            server_id=server_id,
            remote_name=value.get("name", ""),
            description=value.get("description", ""),
            arguments=tuple(McpPromptArgument.from_wire(require_mapping(item, "prompt.argument")) for item in raw_arguments),
            projection_revision=projection_revision,
        )

    def validate_arguments(self, arguments: Mapping[str, Any]) -> dict[str, JsonValue]:
        selected = require_mapping(arguments, "prompt arguments")
        known = {argument.name: argument for argument in self.arguments}
        missing = [argument.name for argument in self.arguments if argument.required and argument.name not in selected]
        unknown = sorted(set(selected) - set(known))
        if missing:
            raise McpModelError(f"missing required prompt arguments: {', '.join(missing)}")
        if unknown:
            raise McpModelError(f"unknown prompt arguments: {', '.join(unknown)}")
        return selected

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "remote_name": self.remote_name,
            "description": self.description,
            "arguments": [item.to_dict() for item in self.arguments],
            "projection_revision": self.projection_revision,
        }


@dataclass(frozen=True, slots=True)
class McpContent:
    kind: McpContentKind
    text: str = ""
    data: bytes = b""
    mime_type: str = ""
    uri: str = ""
    name: str = ""
    structured: JsonValue = None
    annotations: Mapping[str, JsonValue] = field(default_factory=dict)

    MAX_INLINE_BINARY: ClassVar[int] = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.kind, McpContentKind):
            object.__setattr__(self, "kind", McpContentKind(str(self.kind)))
        optional_string(self.text, "content.text", max_length=16 * 1024 * 1024)
        optional_string(self.mime_type, "content.mimeType", max_length=256)
        optional_string(self.name, "content.name", max_length=1024)
        if not isinstance(self.data, bytes):
            object.__setattr__(self, "data", bytes(self.data))
        if len(self.data) > self.MAX_INLINE_BINARY:
            raise McpModelError("binary MCP content exceeds 16 MiB decode limit")
        if self.kind is McpContentKind.TEXT and not self.text:
            raise McpModelError("text MCP content requires text")
        if self.kind in {McpContentKind.IMAGE, McpContentKind.AUDIO, McpContentKind.BINARY} and not self.data:
            raise McpModelError(f"{self.kind} MCP content requires data")
        if self.kind in {McpContentKind.RESOURCE, McpContentKind.RESOURCE_LINK}:
            object.__setattr__(self, "uri", _validate_resource_uri(self.uri))
        elif self.uri:
            optional_string(self.uri, "content.uri", max_length=32768)
        if self.kind is McpContentKind.STRUCTURED:
            object.__setattr__(self, "structured", to_json_value(self.structured))
        object.__setattr__(self, "annotations", require_mapping(self.annotations, "content.annotations"))

    @property
    def size_bytes(self) -> int:
        if self.data:
            return len(self.data)
        if self.text:
            return len(self.text.encode("utf-8"))
        if self.structured is not None:
            return len(canonical_json(self.structured).encode("utf-8"))
        return 0

    @property
    def digest(self) -> str:
        if self.data:
            payload = self.data
        elif self.text:
            payload = self.text.encode("utf-8")
        else:
            payload = canonical_json(self.structured).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> "McpContent":
        content_type = str(value.get("type") or "")
        annotations = value.get("annotations") if isinstance(value.get("annotations"), Mapping) else {}
        if content_type == "text":
            return cls(McpContentKind.TEXT, text=optional_string(value.get("text"), "content.text"), annotations=annotations)
        if content_type in {"image", "audio", "binary"}:
            raw = value.get("data")
            if not isinstance(raw, str):
                raise McpModelError(f"{content_type} content data must be base64 string")
            try:
                decoded = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as error:
                raise McpModelError(f"invalid base64 {content_type} content") from error
            return cls(
                McpContentKind(content_type),
                data=decoded,
                mime_type=optional_string(value.get("mimeType"), "content.mimeType", max_length=256),
                annotations=annotations,
            )
        if content_type == "resource_link":
            return cls(
                McpContentKind.RESOURCE_LINK,
                uri=value.get("uri", ""),
                name=value.get("name", ""),
                mime_type=value.get("mimeType", ""),
                annotations=annotations,
            )
        if content_type == "resource":
            resource = value.get("resource")
            if not isinstance(resource, Mapping):
                raise McpModelError("embedded resource content requires resource object")
            if isinstance(resource.get("blob"), str):
                try:
                    decoded = base64.b64decode(resource["blob"], validate=True)
                except (binascii.Error, ValueError) as error:
                    raise McpModelError("invalid base64 embedded resource") from error
                return cls(
                    McpContentKind.RESOURCE,
                    data=decoded,
                    uri=resource.get("uri", ""),
                    mime_type=resource.get("mimeType", ""),
                    annotations=annotations,
                )
            return cls(
                McpContentKind.RESOURCE,
                text=optional_string(resource.get("text"), "resource.text", max_length=16 * 1024 * 1024),
                uri=resource.get("uri", ""),
                mime_type=resource.get("mimeType", ""),
                annotations=annotations,
            )
        if content_type == "structured" or "structuredContent" in value:
            return cls(McpContentKind.STRUCTURED, structured=value.get("structuredContent"), annotations=annotations)
        raise McpModelError(f"unsupported MCP content type: {content_type or '<missing>'}")

    def to_wire(self, *, include_binary: bool = True) -> dict[str, JsonValue]:
        if self.kind is McpContentKind.TEXT:
            payload: dict[str, JsonValue] = {"type": "text", "text": self.text}
        elif self.kind in {McpContentKind.IMAGE, McpContentKind.AUDIO, McpContentKind.BINARY}:
            payload = {
                "type": str(self.kind),
                "data": base64.b64encode(self.data).decode("ascii") if include_binary else f"<{len(self.data)} bytes>",
                "mimeType": self.mime_type,
            }
        elif self.kind is McpContentKind.RESOURCE_LINK:
            payload = {"type": "resource_link", "uri": self.uri, "name": self.name, "mimeType": self.mime_type}
        elif self.kind is McpContentKind.RESOURCE:
            resource: dict[str, JsonValue] = {"uri": self.uri, "mimeType": self.mime_type}
            if self.data:
                resource["blob"] = base64.b64encode(self.data).decode("ascii") if include_binary else f"<{len(self.data)} bytes>"
            else:
                resource["text"] = self.text
            payload = {"type": "resource", "resource": resource}
        else:
            payload = {"type": "structured", "structuredContent": self.structured}
        if self.annotations:
            payload["annotations"] = dict(self.annotations)
        return payload

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": str(self.kind),
            "text": self.text,
            "binary": bool(self.data),
            "size_bytes": self.size_bytes,
            "digest": self.digest,
            "mime_type": self.mime_type,
            "uri": self.uri,
            "name": self.name,
            "structured": self.structured,
            "annotations": dict(self.annotations),
        }


@dataclass(frozen=True, slots=True)
class McpPage:
    items: tuple[Mapping[str, JsonValue], ...]
    next_cursor: str = ""
    request_cursor: str = ""
    page_index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(require_mapping(item, "page.items[]") for item in self.items))
        optional_string(self.next_cursor, "nextCursor", max_length=4096)
        optional_string(self.request_cursor, "requestCursor", max_length=4096)
        if self.page_index < 0:
            raise McpModelError("page_index cannot be negative")

    @property
    def terminal(self) -> bool:
        return not self.next_cursor

    @classmethod
    def from_result(
        cls,
        result: Mapping[str, Any],
        *,
        item_key: str,
        request_cursor: str = "",
        page_index: int = 0,
    ) -> "McpPage":
        raw_items = result.get(item_key)
        if not isinstance(raw_items, list):
            raise McpModelError(f"paginated result {item_key} must be an array")
        next_cursor = result.get("nextCursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise McpModelError("nextCursor must be a string or null")
        return cls(tuple(require_mapping(item, f"{item_key}[]") for item in raw_items), next_cursor or "", request_cursor, page_index)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "items": [dict(item) for item in self.items],
            "next_cursor": self.next_cursor,
            "request_cursor": self.request_cursor,
            "page_index": self.page_index,
            "terminal": self.terminal,
        }


@dataclass(frozen=True, slots=True)
class McpAuthRecord:
    server_id: str
    state: McpAuthState = McpAuthState.UNKNOWN
    revision: int = 0
    credential_reference: str = ""
    access_token_digest: str = ""
    refresh_token_digest: str = ""
    expires_at: str = ""
    scopes: tuple[str, ...] = ()
    needs_auth_cached_at: str = ""
    revoked_at: str = ""
    error_code: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        if not isinstance(self.state, McpAuthState):
            object.__setattr__(self, "state", McpAuthState(str(self.state)))
        if self.revision < 0:
            raise McpModelError("auth revision cannot be negative")
        for name, digest in (("access_token_digest", self.access_token_digest), ("refresh_token_digest", self.refresh_token_digest)):
            if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise McpModelError(f"{name} must contain a SHA-256 digest, never a raw token")
        object.__setattr__(self, "scopes", _string_tuple(self.scopes, "scopes"))
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))

    @classmethod
    def from_tokens(
        cls,
        server_id: str,
        *,
        credential_reference: str,
        access_token: str,
        refresh_token: str = "",
        expires_at: str = "",
        scopes: Iterable[str] = (),
    ) -> "McpAuthRecord":
        require_non_empty(credential_reference, "credential_reference", max_length=2048)
        require_non_empty(access_token, "access_token", max_length=1024 * 1024)
        return cls(
            server_id=server_id,
            state=McpAuthState.AUTHENTICATED,
            revision=1,
            credential_reference=credential_reference,
            access_token_digest=_secret_digest(access_token),
            refresh_token_digest=_secret_digest(refresh_token) if refresh_token else "",
            expires_at=expires_at,
            scopes=tuple(scopes),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "state": str(self.state),
            "revision": self.revision,
            "credential_reference": self.credential_reference,
            "access_token_digest": self.access_token_digest,
            "refresh_token_digest": self.refresh_token_digest,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
            "needs_auth_cached_at": self.needs_auth_cached_at,
            "revoked_at": self.revoked_at,
            "error_code": self.error_code,
            "metadata": dict(self.metadata),
            "raw_token_included": False,
        }


@dataclass(frozen=True, slots=True)
class McpTaskOptions:
    default_ttl_ms: int | None = None
    max_wait_seconds: float | None = None
    min_poll_interval_seconds: float = 0.5
    max_poll_interval_seconds: float = 5.0
    cancel_remote_on_local_cancel: bool = True

    def __post_init__(self) -> None:
        if self.default_ttl_ms is not None and self.default_ttl_ms <= 0:
            raise McpModelError("default_ttl_ms must be positive")
        if self.max_wait_seconds is not None and (self.max_wait_seconds <= 0 or not math.isfinite(self.max_wait_seconds)):
            raise McpModelError("max_wait_seconds must be positive and finite")
        if self.min_poll_interval_seconds <= 0 or self.max_poll_interval_seconds < self.min_poll_interval_seconds:
            raise McpModelError("invalid task polling interval bounds")

    def clamp_poll_interval(self, server_interval_ms: int | None) -> float:
        if server_interval_ms is None or server_interval_ms <= 0:
            return self.min_poll_interval_seconds
        return min(self.max_poll_interval_seconds, max(self.min_poll_interval_seconds, server_interval_ms / 1000.0))

    def to_task_metadata(self) -> dict[str, JsonValue]:
        return {"ttl": self.default_ttl_ms} if self.default_ttl_ms is not None else {}


@dataclass(frozen=True, slots=True)
class McpTaskSnapshot:
    server_id: str
    task_id: str
    tool_name: str
    status: McpTaskStatus
    revision: int = 0
    poll_interval_ms: int | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    ttl_ms: int | None = None
    error_message: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        object.__setattr__(self, "task_id", require_non_empty(self.task_id, "task_id", max_length=1024))
        object.__setattr__(self, "tool_name", require_non_empty(self.tool_name, "tool_name", max_length=256))
        if not isinstance(self.status, McpTaskStatus):
            object.__setattr__(self, "status", McpTaskStatus(str(self.status)))
        if self.revision < 0:
            raise McpModelError("task revision cannot be negative")
        if self.poll_interval_ms is not None and self.poll_interval_ms < 0:
            raise McpModelError("poll_interval_ms cannot be negative")
        if self.ttl_ms is not None and self.ttl_ms < 0:
            raise McpModelError("ttl_ms cannot be negative")
        object.__setattr__(self, "metadata", require_mapping(self.metadata, "task.metadata"))

    @classmethod
    def from_result(cls, server_id: str, tool_name: str, value: Mapping[str, Any], *, revision: int = 0) -> "McpTaskSnapshot":
        raw_task = value.get("task") if isinstance(value.get("task"), Mapping) else value
        return cls(
            server_id=server_id,
            task_id=raw_task.get("taskId") or raw_task.get("task_id") or "",
            tool_name=tool_name,
            status=McpTaskStatus(str(raw_task.get("status", McpTaskStatus.PENDING))),
            revision=revision,
            poll_interval_ms=int(raw_task["pollInterval"]) if isinstance(raw_task.get("pollInterval"), int) else None,
            ttl_ms=int(raw_task["ttl"]) if isinstance(raw_task.get("ttl"), int) else None,
            error_message=optional_string(raw_task.get("message"), "task.message", max_length=4096),
            metadata=raw_task.get("_meta") if isinstance(raw_task.get("_meta"), Mapping) else {},
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "task_id": self.task_id,
            "tool_name": self.tool_name,
            "status": str(self.status),
            "terminal": self.status.terminal,
            "revision": self.revision,
            "poll_interval_ms": self.poll_interval_ms,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ttl_ms": self.ttl_ms,
            "error_message": self.error_message,
            "metadata": redact_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class McpSamplingRequest:
    server_id: str
    request_id: str
    messages: tuple[Mapping[str, JsonValue], ...]
    max_tokens: int
    system_prompt: str = ""
    model_preferences: Mapping[str, JsonValue] = field(default_factory=dict)
    tools: tuple[Mapping[str, JsonValue], ...] = ()
    temperature: float | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        object.__setattr__(self, "request_id", require_non_empty(self.request_id, "request_id", max_length=1024))
        object.__setattr__(self, "messages", tuple(require_mapping(item, "sampling.messages[]") for item in self.messages))
        if not self.messages:
            raise McpModelError("sampling request requires at least one message")
        if self.max_tokens <= 0:
            raise McpModelError("sampling max_tokens must be positive")
        optional_string(self.system_prompt, "sampling.systemPrompt", max_length=1024 * 1024)
        if self.temperature is not None and (not math.isfinite(self.temperature) or self.temperature < 0):
            raise McpModelError("sampling temperature must be finite and non-negative")
        object.__setattr__(self, "model_preferences", require_mapping(self.model_preferences, "sampling.modelPreferences"))
        object.__setattr__(self, "tools", tuple(require_mapping(item, "sampling.tools[]") for item in self.tools))
        object.__setattr__(self, "metadata", require_mapping(self.metadata, "sampling.metadata"))

    @classmethod
    def from_params(cls, server_id: str, request_id: str, params: Mapping[str, Any]) -> "McpSamplingRequest":
        messages = params.get("messages")
        if not isinstance(messages, list):
            raise McpModelError("sampling messages must be an array")
        return cls(
            server_id=server_id,
            request_id=request_id,
            messages=tuple(require_mapping(item, "sampling.messages[]") for item in messages),
            max_tokens=int(params.get("maxTokens", 0)),
            system_prompt=optional_string(params.get("systemPrompt"), "sampling.systemPrompt", max_length=1024 * 1024),
            model_preferences=params.get("modelPreferences") if isinstance(params.get("modelPreferences"), Mapping) else {},
            tools=tuple(params.get("tools") or ()),
            temperature=float(params["temperature"]) if isinstance(params.get("temperature"), int | float) else None,
            metadata=params.get("_meta") if isinstance(params.get("_meta"), Mapping) else {},
        )

    def capped(self, maximum: int | None) -> "McpSamplingRequest":
        if maximum is None:
            return self
        if maximum <= 0:
            raise McpModelError("sampling token cap must be positive")
        return replace(self, max_tokens=min(self.max_tokens, maximum))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "request_id": self.request_id,
            "message_count": len(self.messages),
            "max_tokens": self.max_tokens,
            "has_system_prompt": bool(self.system_prompt),
            "tool_count": len(self.tools),
            "temperature": self.temperature,
            "metadata": redact_value(self.metadata),
            "raw_messages_included": False,
        }


@dataclass(frozen=True, slots=True)
class McpSamplingResolution:
    request_id: str
    decision: McpSamplingDecision
    reason: str
    effective_max_tokens: int = 0
    response: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", require_non_empty(self.request_id, "request_id", max_length=1024))
        if not isinstance(self.decision, McpSamplingDecision):
            object.__setattr__(self, "decision", McpSamplingDecision(str(self.decision)))
        object.__setattr__(self, "reason", require_non_empty(self.reason, "reason", max_length=4096))
        if self.decision is McpSamplingDecision.ALLOW and self.effective_max_tokens <= 0:
            raise McpModelError("allowed sampling resolution requires positive effective_max_tokens")
        object.__setattr__(self, "response", require_mapping(self.response, "sampling.response"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "request_id": self.request_id,
            "decision": str(self.decision),
            "reason": self.reason,
            "effective_max_tokens": self.effective_max_tokens,
            "response": dict(self.response),
        }


@dataclass(frozen=True, slots=True)
class McpElicitationField:
    name: str
    schema: Mapping[str, JsonValue]
    required: bool = False
    sensitive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_non_empty(self.name, "elicitation.field.name", max_length=256))
        object.__setattr__(self, "schema", require_mapping(self.schema, "elicitation.field.schema"))

    def validate(self, value: Any) -> JsonValue:
        selected = to_json_value(value)
        expected = self.schema.get("type")
        if expected == "string" and not isinstance(selected, str):
            raise McpModelError(f"elicitation field {self.name} must be string")
        if expected == "integer" and (not isinstance(selected, int) or isinstance(selected, bool)):
            raise McpModelError(f"elicitation field {self.name} must be integer")
        if expected == "number" and (not isinstance(selected, int | float) or isinstance(selected, bool)):
            raise McpModelError(f"elicitation field {self.name} must be number")
        if expected == "boolean" and not isinstance(selected, bool):
            raise McpModelError(f"elicitation field {self.name} must be boolean")
        if expected == "array" and not isinstance(selected, list):
            raise McpModelError(f"elicitation field {self.name} must be array")
        if expected == "object" and not isinstance(selected, dict):
            raise McpModelError(f"elicitation field {self.name} must be object")
        return selected

    def to_dict(self) -> dict[str, JsonValue]:
        return {"name": self.name, "schema": dict(self.schema), "required": self.required, "sensitive": self.sensitive}


@dataclass(frozen=True, slots=True)
class McpElicitationRequest:
    server_id: str
    request_id: str
    session_id: str
    mode: McpElicitationMode
    message: str
    fields: tuple[McpElicitationField, ...] = ()
    url: str = ""
    expires_at: str = ""
    revision: int = 0
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        object.__setattr__(self, "request_id", require_non_empty(self.request_id, "request_id", max_length=1024))
        object.__setattr__(self, "session_id", require_non_empty(self.session_id, "session_id", max_length=1024))
        if not isinstance(self.mode, McpElicitationMode):
            object.__setattr__(self, "mode", McpElicitationMode(str(self.mode)))
        object.__setattr__(self, "message", require_non_empty(self.message, "elicitation.message", max_length=65536))
        names = [item.name for item in self.fields]
        if len(names) != len(set(names)):
            raise McpModelError("elicitation field names must be unique")
        if self.mode is McpElicitationMode.URL:
            parsed = urlparse(self.url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise McpModelError("URL elicitation requires credential-free absolute https url")
        elif self.url:
            raise McpModelError("form elicitation cannot include url")
        if self.revision < 0:
            raise McpModelError("elicitation revision cannot be negative")
        object.__setattr__(self, "metadata", require_mapping(self.metadata, "elicitation.metadata"))

    def validate_content(self, content: Mapping[str, Any]) -> dict[str, JsonValue]:
        if self.mode is not McpElicitationMode.FORM:
            if content:
                raise McpModelError("URL elicitation resolution cannot contain form content")
            return {}
        selected = require_mapping(content, "elicitation.content")
        fields = {field.name: field for field in self.fields}
        missing = [field.name for field in self.fields if field.required and field.name not in selected]
        unknown = sorted(set(selected) - set(fields))
        if missing:
            raise McpModelError(f"missing required elicitation fields: {', '.join(missing)}")
        if unknown:
            raise McpModelError(f"unknown elicitation fields: {', '.join(unknown)}")
        return {name: fields[name].validate(value) for name, value in selected.items()}

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "mode": str(self.mode),
            "message": self.message,
            "fields": [item.to_dict() for item in self.fields],
            "url": self.url,
            "expires_at": self.expires_at,
            "revision": self.revision,
            "metadata": redact_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class McpElicitationResolution:
    request_id: str
    session_id: str
    server_id: str
    expected_revision: int
    action: McpElicitationAction
    content: Mapping[str, JsonValue] = field(default_factory=dict)
    actor_id: str = ""
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", require_non_empty(self.request_id, "request_id", max_length=1024))
        object.__setattr__(self, "session_id", require_non_empty(self.session_id, "session_id", max_length=1024))
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        if self.expected_revision < 0:
            raise McpModelError("expected_revision cannot be negative")
        if not isinstance(self.action, McpElicitationAction):
            object.__setattr__(self, "action", McpElicitationAction(str(self.action)))
        object.__setattr__(self, "content", require_mapping(self.content, "elicitation.content"))
        object.__setattr__(self, "actor_id", require_non_empty(self.actor_id, "actor_id", max_length=1024))
        object.__setattr__(self, "idempotency_key", require_non_empty(self.idempotency_key, "idempotency_key", max_length=1024))
        if self.action is not McpElicitationAction.ACCEPT and self.content:
            raise McpModelError("decline/cancel elicitation resolution cannot contain content")

    def to_wire(self, request: McpElicitationRequest) -> dict[str, JsonValue]:
        if (request.request_id, request.session_id, request.server_id, request.revision) != (
            self.request_id,
            self.session_id,
            self.server_id,
            self.expected_revision,
        ):
            raise McpModelError("elicitation resolution identity or revision mismatch")
        content = request.validate_content(self.content) if self.action is McpElicitationAction.ACCEPT else {}
        payload: dict[str, JsonValue] = {"action": str(self.action)}
        if content:
            payload["content"] = content
        return payload

    def safe_dict(self, *, sensitive_fields: Iterable[str] = ()) -> dict[str, JsonValue]:
        hidden = set(sensitive_fields)
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "server_id": self.server_id,
            "expected_revision": self.expected_revision,
            "action": str(self.action),
            "content": {key: "<redacted>" if key in hidden else value for key, value in self.content.items()},
            "actor_id": self.actor_id,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class McpInstructionsDelta:
    server_id: str
    connection_generation: int
    revision: int
    action: McpInstructionsDeltaAction
    instructions: str = ""
    source_event_id: str = ""
    received_at: str = field(default_factory=utc_now_iso)
    untrusted: bool = True
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", require_non_empty(self.server_id, "server_id", max_length=256))
        if self.connection_generation < 0 or self.revision < 0:
            raise McpModelError("instruction generation and revision cannot be negative")
        if not isinstance(self.action, McpInstructionsDeltaAction):
            object.__setattr__(self, "action", McpInstructionsDeltaAction(str(self.action)))
        optional_string(self.instructions, "instructions", max_length=4 * 1024 * 1024)
        if self.action is McpInstructionsDeltaAction.CLEAR and self.instructions:
            raise McpModelError("clear instructions delta cannot contain instructions")
        if self.action is not McpInstructionsDeltaAction.CLEAR and not self.instructions:
            raise McpModelError("replace/append instructions delta requires instructions")
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))

    @property
    def identity(self) -> tuple[str, int, int]:
        return self.server_id, self.connection_generation, self.revision

    def apply(self, current: str, *, current_generation: int, current_revision: int) -> str:
        if self.connection_generation < current_generation:
            raise McpModelError("stale instructions connection generation")
        if self.connection_generation == current_generation and self.revision <= current_revision:
            raise McpModelError("stale or duplicate instructions revision")
        if self.action is McpInstructionsDeltaAction.CLEAR:
            return ""
        if self.action is McpInstructionsDeltaAction.REPLACE:
            return self.instructions
        separator = "\n" if current and not current.endswith("\n") else ""
        return f"{current}{separator}{self.instructions}"

    def to_dict(self, *, include_instructions: bool = True) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "connection_generation": self.connection_generation,
            "revision": self.revision,
            "action": str(self.action),
            "instructions": self.instructions if include_instructions else "<omitted>",
            "instructions_digest": stable_digest(self.instructions),
            "source_event_id": self.source_event_id,
            "received_at": self.received_at,
            "untrusted": self.untrusted,
            "metadata": dict(self.metadata),
        }


def _string_mapping(value: Mapping[str, Any], name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    output: dict[str, str] = {}
    for key, item in value.items():
        selected_key = require_non_empty(key, f"{name} key", max_length=1024)
        if not isinstance(item, str):
            raise TypeError(f"{name}.{selected_key} must be a string")
        if len(item) > 1024 * 1024:
            raise McpModelError(f"{name}.{selected_key} is too large")
        output[selected_key] = item
    return output


def _string_tuple(value: Iterable[Any], name: str) -> tuple[str, ...]:
    return tuple(require_non_empty(item, f"{name}[]", max_length=4096) for item in value)


def _secret_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _redact_command_args(arguments: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    redact_next = False
    for argument in arguments:
        if redact_next:
            output.append("<redacted>")
            redact_next = False
            continue
        name, separator, _ = argument.partition("=")
        if is_sensitive_key(name.lstrip("-")):
            if separator:
                output.append(f"{name}=<redacted>")
            else:
                output.append(argument)
                redact_next = True
            continue
        output.append(argument)
    return tuple(output)


def _redact_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    query = urlencode(
        [
            (key, "<redacted>" if is_sensitive_key(key) else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _safe_server_signature(config: McpServerConfig) -> str:
    if config.transport is McpTransportKind.STDIO:
        return "stdio:" + canonical_json([config.command, *_redact_command_args(config.args)])
    if config.transport is McpTransportKind.STREAMABLE_HTTP:
        return f"url:{_redact_url(config.url)}"
    return f"in-process:{config.server_id}"


def _normalize_local_name(server_id: str, remote_name: str) -> str:
    server = re.sub(r"[^a-zA-Z0-9_]+", "_", server_id).strip("_") or "server"
    remote = re.sub(r"[^a-zA-Z0-9_]+", "_", remote_name).strip("_") or "tool"
    return f"mcp__{server}__{remote}"[:256]


def _risk_from_annotations(value: Mapping[str, Any]) -> McpRiskClass:
    if value.get("destructiveHint") is True:
        return McpRiskClass.DESTRUCTIVE
    if value.get("openWorldHint") is True:
        return McpRiskClass.OPEN_WORLD
    if value.get("readOnlyHint") is True:
        return McpRiskClass.READ_ONLY
    if value.get("idempotentHint") is False:
        return McpRiskClass.LOCAL_WRITE
    return McpRiskClass.UNKNOWN


def _validate_resource_uri(value: Any) -> str:
    uri = require_non_empty(value, "resource.uri", max_length=32768)
    parsed = urlparse(uri)
    if not parsed.scheme:
        raise McpModelError("resource URI must be absolute")
    if parsed.username or parsed.password:
        raise McpModelError("resource URI must not contain credentials")
    if parsed.scheme == "file":
        normalized = PurePosixPath(parsed.path.replace("\\", "/"))
        if ".." in normalized.parts:
            raise McpModelError("resource file URI cannot contain parent traversal")
    return uri
