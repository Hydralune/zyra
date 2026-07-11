from __future__ import annotations

"""MCP event projection and secret-safe serialization.

The MCP runtime handles values from configuration files, remote servers,
OAuth providers, tools, and user elicitation.  Those values share an event
stream, but they do not share a trust boundary.  This module is the single
projection boundary used before MCP data enters either Zyra's event log or the
MCP state journal.

The implementation deliberately does not import MCP model classes.  Event
projection accepts mappings, dataclasses and objects exposing ``to_dict`` or
``safe_dict``.  This keeps the projection reusable by the transport, auth and
configuration owners without introducing import cycles.
"""

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

from zyra_core import EventRecord, EventType, now_iso


MCP_EVENT_SCHEMA = "zyra.mcp.event"
MCP_EVENT_SCHEMA_VERSION = 1
MCP_JOURNAL_SCHEMA = "zyra.mcp.journal-entry"
MCP_JOURNAL_SCHEMA_VERSION = 1
REDACTED = "[REDACTED]"
OMITTED = "[OMITTED]"


class McpRuntimeEventKind(StrEnum):
    CONFIG_SOURCE_LOADED = "mcp_config_source_loaded"
    CONFIG_SOURCE_REMOVED = "mcp_config_source_removed"
    CONFIG_CHANGED = "mcp_config_changed"
    CONFIG_REJECTED = "mcp_config_rejected"
    CONFIG_APPROVAL_PENDING = "mcp_config_approval_pending"
    CONFIG_APPROVED = "mcp_config_approved"
    CONFIG_REJECTED_BY_USER = "mcp_config_rejected_by_user"
    CONFIG_DEDUPLICATED = "mcp_config_deduplicated"
    CONFIG_STALE_CLEANUP = "mcp_config_stale_cleanup"
    POLICY_EVALUATED = "mcp_policy_evaluated"
    CONNECTION_PENDING = "mcp_connection_pending"
    CONNECTION_CONNECTED = "mcp_connection_connected"
    CONNECTION_FAILED = "mcp_connection_failed"
    CONNECTION_NEEDS_AUTH = "mcp_connection_needs_auth"
    CONNECTION_DISABLED = "mcp_connection_disabled"
    CONNECTION_RECONNECTING = "mcp_connection_reconnecting"
    CONNECTION_DISCONNECTED = "mcp_connection_disconnected"
    CAPABILITIES_CHANGED = "mcp_capabilities_changed"
    TOOLS_CHANGED = "mcp_tools_changed"
    RESOURCES_CHANGED = "mcp_resources_changed"
    PROMPTS_CHANGED = "mcp_prompts_changed"
    AUTH_STARTED = "mcp_auth_started"
    AUTH_REQUIRED = "mcp_auth_required"
    AUTH_REFRESHED = "mcp_auth_refreshed"
    AUTH_REVOKED = "mcp_auth_revoked"
    AUTH_FAILED = "mcp_auth_failed"
    ELICITATION_CREATED = "mcp_elicitation_created"
    ELICITATION_RESOLVED = "mcp_elicitation_resolved"
    ELICITATION_CANCELLED = "mcp_elicitation_cancelled"
    ELICITATION_EXPIRED = "mcp_elicitation_expired"
    SAMPLING_DENIED = "mcp_sampling_denied"
    SAMPLING_COMPLETED = "mcp_sampling_completed"
    TASK_CREATED = "mcp_task_created"
    TASK_PROGRESS = "mcp_task_progress"
    TASK_COMPLETED = "mcp_task_completed"
    TASK_FAILED = "mcp_task_failed"
    TASK_CANCELLED = "mcp_task_cancelled"
    INSTRUCTIONS_CHANGED = "mcp_instructions_changed"
    TOOL_CALL_STARTED = "mcp_tool_call_started"
    TOOL_CALL_PROGRESS = "mcp_tool_call_progress"
    TOOL_CALL_COMPLETED = "mcp_tool_call_completed"
    TOOL_CALL_FAILED = "mcp_tool_call_failed"
    OUTPUT_EXTERNALIZED = "mcp_output_externalized"
    STATE_RESTORED = "mcp_state_restored"
    STATE_CONFLICT = "mcp_state_conflict"
    STATE_COMPACTED = "mcp_state_compacted"


_SENSITIVE_EXACT = frozenset(
    {
        "access_token",
        "token",
        "oauth_token",
        "bearer_token",
        "session_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "private_key",
        "private_key_data",
        "password",
        "passwd",
        "secret",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "apikey",
        "x_api_key",
        "bearer",
        "raw_credential",
        "credential_value",
        "session_cookie",
        "csrf_token",
    }
)
_SENSITIVE_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "access_token",
    "refresh_token",
    "id_token",
    "private_key",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential_value",
)
_REFERENCE_SUFFIXES = (
    "_ref",
    "_reference",
    "_digest",
    "_hash",
    "_id",
    "_present",
    "_expires_at",
    "_issued_at",
    "_status",
    "_state",
    "_scope",
    "_type",
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "key",
        "api_key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "signature",
        "sig",
        "code",
        "credential",
    }
)
_URL_KEY_FRAGMENTS = ("url", "uri", "endpoint", "callback", "redirect")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{6,}")
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?-----END [A-Z0-9 ]+-----",
    re.DOTALL,
)
_JWT_LIKE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b")


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """Limits and redaction rules for persistence and event projection."""

    max_depth: int = 16
    max_items: int = 2_000
    max_collection_items: int = 500
    max_string_chars: int = 32_000
    redact_environment_values: bool = True
    redact_sensitive_query_values: bool = True
    redact_url_userinfo: bool = True
    detect_auth_schemes_in_strings: bool = True
    detect_pem_blocks: bool = True
    detect_jwt_like_strings: bool = False
    preserve_redacted_digest: bool = False

    def __post_init__(self) -> None:
        if self.max_depth < 1:
            raise ValueError("max_depth must be positive")
        if self.max_items < 1:
            raise ValueError("max_items must be positive")
        if self.max_collection_items < 1:
            raise ValueError("max_collection_items must be positive")
        if self.max_string_chars < 32:
            raise ValueError("max_string_chars must be at least 32")


DEFAULT_REDACTION_POLICY = RedactionPolicy()


@dataclass(frozen=True, slots=True)
class SanitizationReport:
    value: Any
    redacted_paths: tuple[str, ...] = ()
    omitted_paths: tuple[str, ...] = ()
    truncated_paths: tuple[str, ...] = ()
    converted_paths: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(
            self.redacted_paths
            or self.omitted_paths
            or self.truncated_paths
            or self.converted_paths
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "redacted_field_count": len(self.redacted_paths),
            "omitted_field_count": len(self.omitted_paths),
            "truncated_field_count": len(self.truncated_paths),
            "converted_field_count": len(self.converted_paths),
        }


class _Sanitizer:
    def __init__(self, policy: RedactionPolicy) -> None:
        self.policy = policy
        self.redacted: list[str] = []
        self.omitted: list[str] = []
        self.truncated: list[str] = []
        self.converted: list[str] = []
        self._active: set[int] = set()
        self._seen_items = 0

    def sanitize(self, value: Any, *, path: tuple[str, ...] = (), key: str = "") -> Any:
        dotted = _display_path(path)
        if len(path) > self.policy.max_depth:
            self.omitted.append(dotted)
            return OMITTED
        self._seen_items += 1
        if self._seen_items > self.policy.max_items:
            self.omitted.append(dotted)
            return OMITTED

        if _is_sensitive_key(key):
            self.redacted.append(dotted)
            return _redacted_marker(value, self.policy)

        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            self.converted.append(dotted)
            return str(value)
        if isinstance(value, str):
            return self._string(value, path=path, key=key)
        if isinstance(value, bytes | bytearray | memoryview):
            payload = bytes(value)
            self.converted.append(dotted)
            return {
                "binary": True,
                "byte_length": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        if isinstance(value, Enum):
            self.converted.append(dotted)
            return self.sanitize(value.value, path=path, key=key)
        if isinstance(value, Path):
            self.converted.append(dotted)
            return str(value)
        if isinstance(value, UUID):
            self.converted.append(dotted)
            return str(value)
        if isinstance(value, datetime):
            self.converted.append(dotted)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(value, date):
            self.converted.append(dotted)
            return value.isoformat()

        object_id = id(value)
        if object_id in self._active:
            self.omitted.append(dotted)
            return "[CIRCULAR]"
        self._active.add(object_id)
        try:
            projected = _object_mapping(value)
            if projected is not None:
                return self._mapping(projected, path=path)
            if isinstance(value, Mapping):
                return self._mapping(value, path=path)
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                return self._sequence(value, path=path)
            if isinstance(value, set | frozenset):
                ordered = sorted(value, key=lambda item: repr(item))
                return self._sequence(ordered, path=path)
            if isinstance(value, Iterable):
                return self._sequence(list(value), path=path)
            self.converted.append(dotted)
            return self._string(str(value), path=path, key=key)
        finally:
            self._active.discard(object_id)

    def _mapping(self, value: Mapping[Any, Any], *, path: tuple[str, ...]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            key = str(raw_key)
            child_path = (*path, key)
            if index >= self.policy.max_collection_items:
                self.omitted.append(_display_path(child_path))
                result["__omitted_items__"] = len(value) - index if hasattr(value, "__len__") else 1
                break
            if _is_environment_container(path, key) and isinstance(item, Mapping):
                result[key] = self._environment(item, path=child_path)
                continue
            result[key] = self.sanitize(item, path=child_path, key=key)
        return result

    def _environment(self, value: Mapping[Any, Any], *, path: tuple[str, ...]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            name = str(raw_key)
            child_path = (*path, name)
            if index >= self.policy.max_collection_items:
                self.omitted.append(_display_path(child_path))
                result["__omitted_items__"] = len(value) - index if hasattr(value, "__len__") else 1
                break
            if self.policy.redact_environment_values:
                self.redacted.append(_display_path(child_path))
                result[name] = _redacted_marker(item, self.policy)
            else:
                result[name] = self.sanitize(item, path=child_path, key=name)
        return result

    def _sequence(self, value: Sequence[Any], *, path: tuple[str, ...]) -> list[Any]:
        result: list[Any] = []
        for index, item in enumerate(value):
            child_path = (*path, str(index))
            if index >= self.policy.max_collection_items:
                self.omitted.append(_display_path(child_path))
                result.append({"omitted_items": max(1, len(value) - index)})
                break
            result.append(self.sanitize(item, path=child_path))
        return result

    def _string(self, value: str, *, path: tuple[str, ...], key: str) -> str | dict[str, Any]:
        dotted = _display_path(path)
        result = value
        changed = False
        if _looks_like_url_key(key) or _looks_like_url(value):
            sanitized_url = sanitize_url(value, policy=self.policy)
            changed = sanitized_url != value
            result = sanitized_url
        if self.policy.detect_auth_schemes_in_strings and _AUTH_SCHEME.search(result):
            result = _AUTH_SCHEME.sub(lambda match: f"{match.group(1)} {REDACTED}", result)
            changed = True
        if self.policy.detect_pem_blocks and _PEM_BLOCK.search(result):
            result = _PEM_BLOCK.sub(REDACTED, result)
            changed = True
        if self.policy.detect_jwt_like_strings and _JWT_LIKE.search(result):
            result = _JWT_LIKE.sub(REDACTED, result)
            changed = True
        if changed:
            self.redacted.append(dotted)
        if len(result) > self.policy.max_string_chars:
            digest = hashlib.sha256(result.encode("utf-8", errors="replace")).hexdigest()
            result = result[: self.policy.max_string_chars]
            self.truncated.append(dotted)
            return {
                "text": result,
                "truncated": True,
                "original_char_count": len(value),
                "sha256": digest,
            }
        return result


def sanitize_mcp_value(
    value: Any,
    *,
    policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
) -> Any:
    """Return a bounded, JSON-compatible and secret-safe projection."""

    return sanitize_mcp_value_with_report(value, policy=policy).value


def sanitize_mcp_value_with_report(
    value: Any,
    *,
    policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
) -> SanitizationReport:
    sanitizer = _Sanitizer(policy)
    projected = sanitizer.sanitize(value)
    return SanitizationReport(
        value=projected,
        redacted_paths=tuple(sanitizer.redacted),
        omitted_paths=tuple(sanitizer.omitted),
        truncated_paths=tuple(sanitizer.truncated),
        converted_paths=tuple(sanitizer.converted),
    )


def sanitize_mcp_payload(
    value: Mapping[str, Any] | Any,
    *,
    policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
) -> dict[str, Any]:
    projected = sanitize_mcp_value(value, policy=policy)
    if isinstance(projected, Mapping):
        return {str(key): item for key, item in projected.items()}
    return {"value": projected}


def redact_payload(
    value: Mapping[str, Any] | Any,
    *,
    policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
) -> dict[str, Any]:
    return sanitize_mcp_payload(value, policy=policy)


def sanitize_url(url: str, *, policy: RedactionPolicy = DEFAULT_REDACTION_POLICY) -> str:
    """Remove URL userinfo and security-sensitive query values.

    Malformed strings are returned after generic auth-pattern redaction.  The
    function never raises for server-controlled URL-like data.
    """

    try:
        parsed = urlsplit(url)
    except ValueError:
        return _AUTH_SCHEME.sub(lambda match: f"{match.group(1)} {REDACTED}", url)
    if not parsed.scheme or not parsed.netloc:
        return url

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if not policy.redact_url_userinfo and parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"

    query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if policy.redact_sensitive_query_values and _is_sensitive_query_key(key):
            query.append((key, REDACTED))
        else:
            query.append((key, value))
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query, doseq=True), parsed.fragment))


def canonical_event_digest(value: Any) -> str:
    safe = sanitize_mcp_value(value)
    encoded = json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class McpEventEnvelope:
    kind: McpRuntimeEventKind | str
    server_id: str = ""
    session_id: str = ""
    worker_request_id: str = ""
    tool_call_id: str = ""
    request_id: str = ""
    task_handle: str = ""
    config_revision: int = 0
    connection_generation: int = 0
    capability_generation: int = 0
    catalog_revision: int = 0
    cause_event_id: str = ""
    parent_event_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    artifact_ids: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.config_revision < 0:
            raise ValueError("config_revision cannot be negative")
        if self.connection_generation < 0:
            raise ValueError("connection_generation cannot be negative")
        if self.capability_generation < 0:
            raise ValueError("capability_generation cannot be negative")
        if self.catalog_revision < 0:
            raise ValueError("catalog_revision cannot be negative")

    @property
    def phase(self) -> str:
        return _enum_text(self.kind)

    def to_dict(self, *, policy: RedactionPolicy = DEFAULT_REDACTION_POLICY) -> dict[str, Any]:
        report = sanitize_mcp_value_with_report(self.payload, policy=policy)
        safe_payload = report.value if isinstance(report.value, Mapping) else {"value": report.value}
        return {
            "schema": MCP_EVENT_SCHEMA,
            "schema_version": MCP_EVENT_SCHEMA_VERSION,
            "kind": self.phase,
            "phase": self.phase,
            "server_id": self.server_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "tool_call_id": self.tool_call_id,
            "request_id": self.request_id,
            "task_handle": self.task_handle,
            "config_revision": self.config_revision,
            "connection_generation": self.connection_generation,
            "capability_generation": self.capability_generation,
            "catalog_revision": self.catalog_revision,
            "cause_event_id": self.cause_event_id,
            "parent_event_id": self.parent_event_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "artifact_ids": list(self.artifact_ids),
            "payload": dict(safe_payload),
            "sanitization": report.metadata(),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class McpJournalEntry:
    kind: McpRuntimeEventKind | str
    revision: int
    actor: str = ""
    reason: str = ""
    server_id: str = ""
    source_id: str = ""
    cause_event_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    journal_id: str = field(default_factory=lambda: f"mcpjournal_{uuid4().hex}")
    created_at: str = field(default_factory=now_iso)

    def to_dict(self, *, policy: RedactionPolicy = DEFAULT_REDACTION_POLICY) -> dict[str, Any]:
        report = sanitize_mcp_value_with_report(self.payload, policy=policy)
        value = report.value if isinstance(report.value, Mapping) else {"value": report.value}
        return {
            "schema": MCP_JOURNAL_SCHEMA,
            "schema_version": MCP_JOURNAL_SCHEMA_VERSION,
            "journal_id": self.journal_id,
            "kind": _enum_text(self.kind),
            "revision": self.revision,
            "actor": self.actor,
            "reason": self.reason,
            "server_id": self.server_id,
            "source_id": self.source_id,
            "cause_event_id": self.cause_event_id,
            "payload": dict(value),
            "sanitization": report.metadata(),
            "created_at": self.created_at,
        }


_EVENT_TYPE_NAMES: dict[str, str] = {
    "config": "MCP_CONFIG_CHANGED",
    "connection": "MCP_CONNECTION_CHANGED",
    "capabilities": "MCP_CAPABILITIES_CHANGED",
    "auth": "MCP_AUTH_CHANGED",
    "elicitation": "MCP_ELICITATION",
    "task": "MCP_TASK_UPDATED",
    "instructions": "MCP_INSTRUCTIONS_CHANGED",
    "tool": "MCP_TOOL_RESULT",
}


class McpEventProjector:
    """Project MCP envelopes to canonical Zyra ``EventRecord`` values."""

    def __init__(
        self,
        *,
        owner_unit: str = "M1-S03B-01",
        runtime_id: str = "zyra-mcp-client-runtime",
        redaction_policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.redaction_policy = redaction_policy

    def event(
        self,
        envelope: McpEventEnvelope,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
        category: str | None = None,
    ) -> EventRecord:
        body = envelope.to_dict(policy=self.redaction_policy)
        body["owner_unit"] = self.owner_unit
        body["runtime_id"] = self.runtime_id
        event_type = _event_type(category or _category_for_kind(envelope.phase))
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=event_type,
            payload={
                "query_session": {
                    "session_id": envelope.session_id,
                    "worker_request_id": envelope.worker_request_id,
                    "phase": envelope.phase,
                    "cause_event_id": envelope.cause_event_id,
                    "mcp_runtime": body,
                }
            },
        )

    def config_event(
        self,
        *,
        kind: McpRuntimeEventKind | str,
        run_id: str,
        task_id: str,
        revision: int,
        payload: Mapping[str, Any],
        node_id: str | None = None,
        session_id: str = "",
        worker_request_id: str = "",
        server_id: str = "",
        cause_event_id: str = "",
    ) -> EventRecord:
        return self.event(
            McpEventEnvelope(
                kind=kind,
                server_id=server_id,
                session_id=session_id,
                worker_request_id=worker_request_id,
                config_revision=revision,
                cause_event_id=cause_event_id,
                payload=payload,
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            category="config",
        )

    def connection_event(
        self,
        snapshot: Any,
        *,
        kind: McpRuntimeEventKind | str,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
        session_id: str = "",
        worker_request_id: str = "",
        cause_event_id: str = "",
    ) -> EventRecord:
        payload = sanitize_mcp_payload(snapshot, policy=self.redaction_policy)
        return self.event(
            McpEventEnvelope(
                kind=kind,
                server_id=str(_get(snapshot, "server_id", _get(snapshot, "name", ""))),
                session_id=session_id,
                worker_request_id=worker_request_id,
                config_revision=_nonnegative_int(_get(snapshot, "config_revision", 0)),
                connection_generation=_nonnegative_int(_get(snapshot, "generation", 0)),
                capability_generation=_nonnegative_int(_get(snapshot, "capability_generation", 0)),
                cause_event_id=cause_event_id,
                payload=payload,
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            category="connection",
        )

    def auth_event(
        self,
        record: Any,
        *,
        kind: McpRuntimeEventKind | str,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
        session_id: str = "",
        worker_request_id: str = "",
        cause_event_id: str = "",
    ) -> EventRecord:
        payload = sanitize_mcp_payload(record, policy=self.redaction_policy)
        return self.event(
            McpEventEnvelope(
                kind=kind,
                server_id=str(_get(record, "server_id", "")),
                session_id=session_id,
                worker_request_id=worker_request_id,
                request_id=str(_get(record, "request_id", "")),
                cause_event_id=cause_event_id,
                payload=payload,
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            category="auth",
        )

    def tool_event(
        self,
        result: Any,
        *,
        kind: McpRuntimeEventKind | str,
        run_id: str,
        task_id: str,
        tool_call_id: str,
        node_id: str | None = None,
        session_id: str = "",
        worker_request_id: str = "",
        server_id: str = "",
        cause_event_id: str = "",
        artifact_ids: Sequence[str] = (),
    ) -> EventRecord:
        return self.event(
            McpEventEnvelope(
                kind=kind,
                server_id=server_id or str(_get(result, "server_id", "")),
                session_id=session_id,
                worker_request_id=worker_request_id,
                tool_call_id=tool_call_id,
                cause_event_id=cause_event_id,
                artifact_ids=tuple(str(value) for value in artifact_ids),
                payload=sanitize_mcp_payload(result, policy=self.redaction_policy),
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            category="tool",
        )

    def task_event(
        self,
        snapshot: Any,
        *,
        kind: McpRuntimeEventKind | str,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
        session_id: str = "",
        worker_request_id: str = "",
        cause_event_id: str = "",
    ) -> EventRecord:
        return self.event(
            McpEventEnvelope(
                kind=kind,
                server_id=str(_get(snapshot, "server_id", "")),
                session_id=session_id,
                worker_request_id=worker_request_id,
                request_id=str(_get(snapshot, "request_id", "")),
                task_handle=str(_get(snapshot, "task_handle", _get(snapshot, "task_id", ""))),
                cause_event_id=cause_event_id,
                payload=sanitize_mcp_payload(snapshot, policy=self.redaction_policy),
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            category="task",
        )

    def instruction_event(
        self,
        delta: Any,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
        session_id: str = "",
        worker_request_id: str = "",
        server_id: str = "",
        cause_event_id: str = "",
        artifact_ids: Sequence[str] = (),
    ) -> EventRecord:
        return self.event(
            McpEventEnvelope(
                kind=McpRuntimeEventKind.INSTRUCTIONS_CHANGED,
                server_id=server_id or str(_get(delta, "server_id", "")),
                session_id=session_id,
                worker_request_id=worker_request_id,
                catalog_revision=_nonnegative_int(_get(delta, "revision", 0)),
                cause_event_id=cause_event_id,
                artifact_ids=tuple(str(value) for value in artifact_ids),
                payload=sanitize_mcp_payload(delta, policy=self.redaction_policy),
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            category="instructions",
        )


McpEventRecordProjector = McpEventProjector


def make_journal_entry(
    *,
    kind: McpRuntimeEventKind | str,
    revision: int,
    actor: str = "",
    reason: str = "",
    server_id: str = "",
    source_id: str = "",
    cause_event_id: str = "",
    payload: Mapping[str, Any] | None = None,
    policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
) -> dict[str, Any]:
    return McpJournalEntry(
        kind=kind,
        revision=revision,
        actor=actor,
        reason=reason,
        server_id=server_id,
        source_id=source_id,
        cause_event_id=cause_event_id,
        payload=payload or {},
    ).to_dict(policy=policy)


def _event_type(category: str) -> EventType:
    name = _EVENT_TYPE_NAMES.get(category, "")
    if name and hasattr(EventType, name):
        return getattr(EventType, name)
    if category in {"config", "connection", "capabilities", "auth"}:
        return EventType.SYSTEM_NOTICE
    return EventType.AGENT_MESSAGE


def _category_for_kind(kind: str) -> str:
    if "config" in kind or "policy" in kind:
        return "config"
    if "connection" in kind:
        return "connection"
    if any(value in kind for value in ("capabilities", "tools_changed", "resources_changed", "prompts_changed")):
        return "capabilities"
    if "auth" in kind:
        return "auth"
    if "elicitation" in kind:
        return "elicitation"
    if "task" in kind:
        return "task"
    if "instructions" in kind:
        return "instructions"
    if "tool_call" in kind or "output_externalized" in kind:
        return "tool"
    return "runtime"


def _object_mapping(value: Any) -> Mapping[str, Any] | None:
    safe_dict = getattr(value, "safe_dict", None)
    if callable(safe_dict):
        candidate = safe_dict()
        if isinstance(candidate, Mapping):
            return candidate
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        candidate = to_dict()
        if isinstance(candidate, Mapping):
            return candidate
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    return None


def _normalize_key(key: str) -> str:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized)
    return normalized.strip("_").casefold()


def _is_sensitive_key(key: str) -> bool:
    if not key:
        return False
    normalized = _normalize_key(key)
    if normalized.endswith(_REFERENCE_SUFFIXES):
        return False
    if normalized.endswith(("_count", "_limit", "_budget", "_usage")):
        return False
    if normalized in _SENSITIVE_EXACT:
        return True
    return any(fragment in normalized for fragment in _SENSITIVE_FRAGMENTS)


def is_sensitive_field_name(key: str) -> bool:
    return _is_sensitive_key(key)


def _is_sensitive_query_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return normalized in _SENSITIVE_QUERY_KEYS or _is_sensitive_key(normalized)


def _looks_like_url_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return any(fragment in normalized for fragment in _URL_KEY_FRAGMENTS)


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "ws://", "wss://"))


def _is_environment_container(path: tuple[str, ...], key: str) -> bool:
    normalized = _normalize_key(key)
    if normalized in {"env", "environment", "environment_variables"}:
        return True
    return bool(path and _normalize_key(path[-1]) in {"env", "environment"})


def _redacted_marker(value: Any, policy: RedactionPolicy) -> str | dict[str, Any]:
    if not policy.preserve_redacted_digest:
        return REDACTED
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(value).encode("utf-8", errors="replace")
    return {"redacted": True, "sha256": hashlib.sha256(encoded).hexdigest()}


def _display_path(path: tuple[str, ...]) -> str:
    return ".".join(path) if path else "$"


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "DEFAULT_REDACTION_POLICY",
    "MCP_EVENT_SCHEMA",
    "MCP_EVENT_SCHEMA_VERSION",
    "MCP_JOURNAL_SCHEMA",
    "MCP_JOURNAL_SCHEMA_VERSION",
    "McpEventEnvelope",
    "McpEventProjector",
    "McpEventRecordProjector",
    "McpJournalEntry",
    "McpRuntimeEventKind",
    "OMITTED",
    "REDACTED",
    "RedactionPolicy",
    "SanitizationReport",
    "canonical_event_digest",
    "is_sensitive_field_name",
    "make_journal_entry",
    "redact_payload",
    "sanitize_mcp_payload",
    "sanitize_mcp_value",
    "sanitize_mcp_value_with_report",
    "sanitize_url",
]
