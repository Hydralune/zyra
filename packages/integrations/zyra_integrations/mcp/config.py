from __future__ import annotations

"""Source-aware MCP configuration ownership and materialization.

The configuration store is a logical facade over ``McpRuntimeStateStore``.
Every source refresh, project approval, disable toggle and policy update shares
the state store revision, so independent API, CLI, plugin and connection
writers cannot overwrite one another with stale snapshots.

Configuration values are persisted as unresolved templates.  Environment
expansion happens only in ``materialize_server`` and the expanded values are
never written to state, provenance, journal or event payloads.
"""

import copy
import dataclasses
import fnmatch
import hashlib
import json
import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from .events import McpRuntimeEventKind, sanitize_url
from .models import (
    McpApprovalState,
    McpConfigScope,
    McpServerConfig,
    McpTransportKind,
)
from .store import McpRuntimeStateStore, McpStateConflict


class McpConfigError(ValueError):
    pass


class McpConfigValidationError(McpConfigError):
    pass


class McpConfigNotFound(McpConfigError):
    pass


class McpProjectApprovalError(McpConfigError):
    pass


class McpEnterpriseExclusiveError(McpConfigError):
    pass


class McpSourceGenerationConflict(McpStateConflict):
    pass


class McpEnvironmentExpansionError(McpConfigError):
    pass


class McpConfigSourceKind(StrEnum):
    CLAUDEAI = "claudeai"
    PLUGIN = "plugin"
    MANUAL = "manual"
    DYNAMIC = "dynamic"
    SDK = "sdk"
    ENTERPRISE = "enterprise"


class McpPolicyDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class McpSuppressionReason(StrEnum):
    NONE = ""
    ENTERPRISE_EXCLUSIVE = "enterprise_exclusive"
    NAME_SHADOWED = "name_shadowed"
    DUPLICATE_SIGNATURE = "duplicate_signature"
    PROJECT_PENDING = "project_pending"
    PROJECT_REJECTED = "project_rejected"
    DISABLED = "disabled"
    POLICY_DENIED = "policy_denied"
    SOURCE_DISABLED = "source_disabled"


_SOURCE_PRECEDENCE: Mapping[str, int] = {
    "claudeai": 10,
    "plugin": 20,
    "sdk": 25,
    "user": 30,
    "project": 40,
    "local": 50,
    "dynamic": 60,
    "enterprise": 100,
}
_MANUAL_SCOPES = frozenset({"user", "project", "local", "dynamic"})
_STALE_SOURCE_KINDS = frozenset({"plugin", "claudeai", "dynamic", "sdk"})
_CCR_PROXY_MARKERS = ("/v2/session_ingress/shttp/mcp/", "/v2/ccr-sessions/")
_PROXY_QUERY_KEYS = ("mcp_url", "mcpUrl", "target_url", "target", "upstream_url")
_SERVER_NAME_LIMIT = 256
_ENV_PATTERN = re.compile(r"(?<!\\)\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
_ESCAPED_ENV_PATTERN = re.compile(r"\\(\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\})")
_SENSITIVE_ENV_FRAGMENT = re.compile(
    r"(?i)(?:token|secret|password|passwd|credential|authorization|cookie|api_?key|private_?key)"
)
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
    }
)
_SECRET_ARGUMENT = re.compile(
    r"(?i)(?:--?(?:access[-_]?token|refresh[-_]?token|password|passwd|secret|api[-_]?key)|"
    r"(?:authorization|cookie))\s*(?:=|:)\s*([^\s]+)"
)
_UNSET = object()


@dataclass(frozen=True, slots=True)
class McpConfigIssue:
    code: str
    message: str
    source_id: str = ""
    server_name: str = ""
    path: str = ""
    severity: str = "error"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "source_id": self.source_id,
            "server_name": self.server_name,
            "path": self.path,
            "severity": self.severity,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class McpSourceProvenance:
    source_id: str
    source_kind: McpConfigSourceKind
    scope: McpConfigScope
    precedence: int
    generation: int
    source_revision: str = ""
    source_path: str = ""
    loaded_at: str = ""
    last_seen_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_kind": str(self.source_kind),
            "scope": str(self.scope),
            "precedence": self.precedence,
            "generation": self.generation,
            "source_revision": self.source_revision,
            "source_path": self.source_path,
            "loaded_at": self.loaded_at,
            "last_seen_at": self.last_seen_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class McpPolicyDecision:
    decision: McpPolicyDecisionKind
    reason: str
    matched_rule: Mapping[str, Any] = field(default_factory=dict)
    provenance: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is McpPolicyDecisionKind.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "allowed": self.allowed,
            "reason": self.reason,
            "matched_rule": copy.deepcopy(dict(self.matched_rule)),
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class McpEffectiveServer:
    name: str
    server_id: str
    config: McpServerConfig
    signature: str
    provenance: McpSourceProvenance
    approval: McpApprovalState
    policy: McpPolicyDecision
    enabled: bool
    suppression_reason: McpSuppressionReason = McpSuppressionReason.NONE
    duplicate_of: str = ""
    shadowed_provenance: tuple[McpSourceProvenance, ...] = ()
    duplicate_provenance: tuple[McpSourceProvenance, ...] = ()
    allowed_tools: tuple[str, ...] | None = None
    denied_tools: tuple[str, ...] = ()
    required: bool = False

    @property
    def connectable(self) -> bool:
        return (
            self.enabled
            and self.policy.allowed
            and self.approval not in {McpApprovalState.PENDING, McpApprovalState.REJECTED}
            and self.suppression_reason is McpSuppressionReason.NONE
            and not self.config.disabled
        )

    def to_dict(self, *, include_config: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "server_id": self.server_id,
            "signature": self.signature,
            "provenance": self.provenance.to_dict(),
            "approval": str(self.approval),
            "policy": self.policy.to_dict(),
            "enabled": self.enabled,
            "connectable": self.connectable,
            "suppression_reason": str(self.suppression_reason),
            "duplicate_of": self.duplicate_of,
            "shadowed_provenance": [item.to_dict() for item in self.shadowed_provenance],
            "duplicate_provenance": [item.to_dict() for item in self.duplicate_provenance],
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "denied_tools": list(self.denied_tools),
            "required": self.required,
        }
        if include_config:
            value["config"] = self.config.safe_dict()
        return value


@dataclass(frozen=True, slots=True)
class McpConfigResolution:
    revision: int
    servers: Mapping[str, McpEffectiveServer]
    all_servers: Mapping[str, McpEffectiveServer]
    pending: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    policy_blocked: tuple[str, ...] = ()
    suppressed: tuple[str, ...] = ()
    enterprise_exclusive: bool = False
    enterprise_source_ids: tuple[str, ...] = ()
    issues: tuple[McpConfigIssue, ...] = ()

    def configs(self) -> dict[str, McpServerConfig]:
        return {name: record.config for name, record in self.servers.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "servers": {name: item.to_dict() for name, item in self.servers.items()},
            "all_servers": {name: item.to_dict() for name, item in self.all_servers.items()},
            "pending": list(self.pending),
            "rejected": list(self.rejected),
            "disabled": list(self.disabled),
            "policy_blocked": list(self.policy_blocked),
            "suppressed": list(self.suppressed),
            "enterprise_exclusive": self.enterprise_exclusive,
            "enterprise_source_ids": list(self.enterprise_source_ids),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class McpEnvironmentExpansion:
    value: Any = field(repr=False)
    referenced_variables: tuple[str, ...] = ()
    missing_variables: tuple[str, ...] = ()
    defaulted_variables: tuple[str, ...] = ()
    sensitive_variables: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_variables

    def safe_dict(self) -> dict[str, Any]:
        return {
            "referenced_variables": list(self.referenced_variables),
            "missing_variables": list(self.missing_variables),
            "defaulted_variables": list(self.defaulted_variables),
            "sensitive_variables": list(self.sensitive_variables),
            "complete": self.complete,
            "expanded_value_included": False,
        }


@dataclass(frozen=True, slots=True)
class McpMaterializedServer:
    record: McpEffectiveServer
    config: McpServerConfig = field(repr=False)
    expansion: McpEnvironmentExpansion = field(repr=False)

    @property
    def ready(self) -> bool:
        return self.record.connectable and self.expansion.complete

    def safe_dict(self) -> dict[str, Any]:
        # Use the unresolved record for diagnostics.  The materialized config
        # can contain credentials in headers, URL parameters or arguments.
        return {
            "server": self.record.to_dict(),
            "expansion": self.expansion.safe_dict(),
            "ready": self.ready,
            "materialized_config_included": False,
        }


@dataclass(frozen=True, slots=True)
class McpStaleCleanupResult:
    revision: int
    removed_source_ids: tuple[str, ...]
    removed_server_ids: tuple[str, ...]
    retained_server_ids: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.removed_source_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "removed_source_ids": list(self.removed_source_ids),
            "removed_server_ids": list(self.removed_server_ids),
            "retained_server_ids": list(self.retained_server_ids),
            "changed": self.changed,
        }


def unwrap_remote_url(url: str) -> str:
    """Unwrap CCR/session-ingress URLs for content-based deduplication."""

    current = url
    for _ in range(4):
        if not any(marker in current for marker in _CCR_PROXY_MARKERS):
            return current
        try:
            parsed = urlsplit(current)
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        except ValueError:
            return current
        nested = next((query[key] for key in _PROXY_QUERY_KEYS if query.get(key)), "")
        if not nested:
            return current
        candidate = unquote(nested)
        if candidate == current:
            return current
        current = candidate
    return current


unwrap_ccr_proxy_url = unwrap_remote_url


def mcp_server_signature(config: Mapping[str, Any] | McpServerConfig) -> str | None:
    value = _config_mapping(config, include_templates=True)
    transport = _transport_text(value)
    command = str(value.get("command") or "")
    args = _string_list(value.get("args") or (), "args")
    if transport == "stdio" or (not transport and command):
        if not command:
            return None
        encoded = json.dumps([command, *args], ensure_ascii=False, separators=(",", ":"))
        return f"stdio:{encoded}"
    url = _extract_url(value)
    if url:
        return f"url:{unwrap_remote_url(url)}"
    server_id = str(value.get("server_id") or "")
    if transport == "in_process" and server_id:
        return f"in-process:{server_id}"
    return None


get_mcp_server_signature = mcp_server_signature


def url_matches_pattern(url: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(url, pattern)


def expand_env_string(
    value: str,
    environ: Mapping[str, str] | None = None,
) -> McpEnvironmentExpansion:
    selected_env = os.environ if environ is None else environ
    referenced: list[str] = []
    missing: list[str] = []
    defaulted: list[str] = []
    sensitive: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        referenced.append(name)
        if _SENSITIVE_ENV_FRAGMENT.search(name):
            sensitive.append(name)
        present = name in selected_env and str(selected_env[name]) != ""
        if present:
            return str(selected_env[name])
        if default is not None:
            defaulted.append(name)
            return default
        missing.append(name)
        return match.group(0)

    expanded = _ENV_PATTERN.sub(substitute, value)
    expanded = _ESCAPED_ENV_PATTERN.sub(lambda match: match.group(1), expanded)
    return McpEnvironmentExpansion(
        value=expanded,
        referenced_variables=tuple(dict.fromkeys(referenced)),
        missing_variables=tuple(dict.fromkeys(missing)),
        defaulted_variables=tuple(dict.fromkeys(defaulted)),
        sensitive_variables=tuple(dict.fromkeys(sensitive)),
    )


def expand_environment(
    value: Any,
    environ: Mapping[str, str] | None = None,
) -> McpEnvironmentExpansion:
    referenced: list[str] = []
    missing: list[str] = []
    defaulted: list[str] = []
    sensitive: list[str] = []

    def expand(item: Any) -> Any:
        if isinstance(item, str):
            result = expand_env_string(item, environ)
            referenced.extend(result.referenced_variables)
            missing.extend(result.missing_variables)
            defaulted.extend(result.defaulted_variables)
            sensitive.extend(result.sensitive_variables)
            return result.value
        if isinstance(item, Mapping):
            return {str(key): expand(child) for key, child in item.items()}
        if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            return [expand(child) for child in item]
        return copy.deepcopy(item)

    return McpEnvironmentExpansion(
        value=expand(value),
        referenced_variables=tuple(dict.fromkeys(referenced)),
        missing_variables=tuple(dict.fromkeys(missing)),
        defaulted_variables=tuple(dict.fromkeys(defaulted)),
        sensitive_variables=tuple(dict.fromkeys(sensitive)),
    )


class McpConfigStore:
    """Merges persisted MCP config sources into connectable server models."""

    def __init__(
        self,
        state_store: McpRuntimeStateStore | str | Path,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_store = (
            state_store
            if isinstance(state_store, McpRuntimeStateStore)
            else McpRuntimeStateStore(state_store)
        )
        self.environ = os.environ if environ is None else environ
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def revision(self) -> int:
        return self.state_store.revision

    def replace_source(
        self,
        source_id: str,
        servers: Mapping[str, Any] | Any,
        *,
        scope: McpConfigScope | str,
        source_kind: McpConfigSourceKind | str | None = None,
        source_revision: str = "",
        source_path: str = "",
        generation: int | None = None,
        precedence: int | None = None,
        enabled: bool = True,
        stale_after_seconds: float | None = None,
        metadata: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        source_id = _required_text(source_id, "source_id", limit=512)
        selected_scope = _coerce_scope(scope)
        selected_kind = _coerce_source_kind(source_kind, selected_scope)
        normalized_servers = _extract_server_mapping(servers)
        now = self._now_iso()
        normalized: dict[str, dict[str, Any]] = {}
        for name, config in normalized_servers.items():
            normalized[name] = self._normalize_config(
                name,
                config,
                source_id=source_id,
                scope=selected_scope,
                source_revision=source_revision,
                source_path=source_path,
            )

        def replace_source_state(state: dict[str, Any]) -> None:
            current = state["config_sources"].get(source_id)
            current_generation = int(current.get("generation") or 0) if isinstance(current, Mapping) else 0
            next_generation = current_generation + 1 if generation is None else int(generation)
            if next_generation <= current_generation:
                raise McpSourceGenerationConflict(
                    f"stale MCP source generation for {source_id}: {next_generation} <= {current_generation}"
                )
            loaded_at = (
                str(current.get("loaded_at") or now)
                if isinstance(current, Mapping)
                else now
            )
            state["config_sources"][source_id] = {
                "source_id": source_id,
                "source_kind": str(selected_kind),
                "scope": str(selected_scope),
                "precedence": int(precedence if precedence is not None else _default_precedence(selected_scope, selected_kind)),
                "generation": next_generation,
                "source_revision": source_revision,
                "source_path": source_path,
                "loaded_at": loaded_at,
                "last_seen_at": now,
                "stale_after_seconds": stale_after_seconds,
                "enabled": bool(enabled),
                "metadata": _safe_metadata(metadata or {}),
                "servers": copy.deepcopy(normalized),
            }
            prefix = f"{source_id}::"
            valid_approval_keys = {f"{source_id}::{name}" for name in normalized}
            for approval_key in list(state["approvals"]):
                if approval_key.startswith(prefix) and approval_key not in valid_approval_keys:
                    del state["approvals"][approval_key]
            if selected_scope is McpConfigScope.PROJECT:
                for name, config in normalized.items():
                    key = _approval_key(source_id, name)
                    signature = str(config["signature"])
                    existing = state["approvals"].get(key)
                    if not isinstance(existing, Mapping) or str(existing.get("signature") or "") != signature:
                        state["approvals"][key] = {
                            "source_id": source_id,
                            "server_name": name,
                            "signature": signature,
                            "status": str(McpApprovalState.PENDING),
                            "revision": int(existing.get("revision") or 0) + 1 if isinstance(existing, Mapping) else 0,
                            "decided_at": "",
                            "actor": "",
                            "reason": "configuration_changed" if existing else "project_configuration_discovered",
                        }

        state = self.state_store.mutate(
            replace_source_state,
            expected_revision=expected_revision,
            actor="mcp_config_store",
            reason="config_source_replaced",
            event_kind=McpRuntimeEventKind.CONFIG_SOURCE_LOADED,
            source_id=source_id,
            journal_payload={
                "source_kind": str(selected_kind),
                "scope": str(selected_scope),
                "server_names": sorted(normalized),
                "source_revision": source_revision,
                "generation": generation,
            },
        )
        return copy.deepcopy(state["config_sources"][source_id])

    set_source = replace_source

    def merge_source(
        self,
        source_id: str,
        servers: Mapping[str, Any],
        *,
        scope: McpConfigScope | str | None = None,
        expected_revision: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        state = self.state_store.read_state()
        existing = state["config_sources"].get(source_id)
        if not isinstance(existing, Mapping) and scope is None:
            raise McpConfigValidationError("scope is required when creating an MCP config source")
        current_servers = copy.deepcopy(existing.get("servers") or {}) if isinstance(existing, Mapping) else {}
        current_servers.update(_extract_server_mapping(servers))
        return self.replace_source(
            source_id,
            current_servers,
            scope=scope or str(existing["scope"]),
            source_kind=kwargs.pop("source_kind", existing.get("source_kind") if isinstance(existing, Mapping) else None),
            source_revision=kwargs.pop("source_revision", str(existing.get("source_revision") or "") if isinstance(existing, Mapping) else ""),
            source_path=kwargs.pop("source_path", str(existing.get("source_path") or "") if isinstance(existing, Mapping) else ""),
            precedence=kwargs.pop("precedence", int(existing.get("precedence") or 0) if isinstance(existing, Mapping) else None),
            enabled=kwargs.pop("enabled", bool(existing.get("enabled", True)) if isinstance(existing, Mapping) else True),
            stale_after_seconds=kwargs.pop("stale_after_seconds", existing.get("stale_after_seconds") if isinstance(existing, Mapping) else None),
            metadata=kwargs.pop("metadata", existing.get("metadata") if isinstance(existing, Mapping) else None),
            expected_revision=expected_revision,
            **kwargs,
        )

    def remove_source(
        self,
        source_id: str,
        *,
        expected_revision: int | None = None,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        removed: list[dict[str, Any]] = []

        def remove(state: dict[str, Any]) -> None:
            value = state["config_sources"].pop(source_id, None)
            if value is None and not missing_ok:
                raise McpConfigNotFound(f"unknown MCP config source: {source_id}")
            if isinstance(value, Mapping):
                removed.append(copy.deepcopy(dict(value)))
            prefix = f"{source_id}::"
            for key in list(state["approvals"]):
                if key.startswith(prefix):
                    del state["approvals"][key]
            _cleanup_orphan_runtime_records(state)

        self.state_store.mutate(
            remove,
            expected_revision=expected_revision,
            actor="mcp_config_store",
            reason="config_source_removed",
            event_kind=McpRuntimeEventKind.CONFIG_SOURCE_REMOVED,
            source_id=source_id,
            journal_payload={"source_id": source_id},
        )
        return removed[0] if removed else None

    def touch_source(
        self,
        source_id: str,
        *,
        source_revision: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        now = self._now_iso()

        def touch(state: dict[str, Any]) -> None:
            source = state["config_sources"].get(source_id)
            if not isinstance(source, MutableMapping):
                raise McpConfigNotFound(f"unknown MCP config source: {source_id}")
            source["last_seen_at"] = now
            if source_revision is not None:
                source["source_revision"] = source_revision

        state = self.state_store.mutate(
            touch,
            expected_revision=expected_revision,
            actor="mcp_config_store",
            reason="config_source_seen",
            event_kind=McpRuntimeEventKind.CONFIG_CHANGED,
            source_id=source_id,
            journal_payload={"source_revision": source_revision or ""},
        )
        return copy.deepcopy(state["config_sources"][source_id])

    def set_source_enabled(
        self,
        source_id: str,
        enabled: bool,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> None:
            source = state["config_sources"].get(source_id)
            if not isinstance(source, MutableMapping):
                raise McpConfigNotFound(f"unknown MCP config source: {source_id}")
            source["enabled"] = bool(enabled)

        state = self.state_store.mutate(
            update,
            expected_revision=expected_revision,
            actor="mcp_config_store",
            reason="config_source_enabled" if enabled else "config_source_disabled",
            event_kind=McpRuntimeEventKind.CONFIG_CHANGED,
            source_id=source_id,
            journal_payload={"enabled": bool(enabled)},
        )
        return copy.deepcopy(state["config_sources"][source_id])

    def upsert_server(
        self,
        name: str,
        config: Any,
        *,
        source_id: str = "manual:dynamic",
        scope: McpConfigScope | str = McpConfigScope.DYNAMIC,
        source_kind: McpConfigSourceKind | str = McpConfigSourceKind.MANUAL,
        expected_revision: int | None = None,
    ) -> McpServerConfig:
        state = self.state_store.read_state()
        if _enterprise_source_ids(state):
            raise McpEnterpriseExclusiveError(
                "cannot add MCP server while enterprise configuration has exclusive control"
            )
        source = state["config_sources"].get(source_id)
        servers = copy.deepcopy(source.get("servers") or {}) if isinstance(source, Mapping) else {}
        servers[name] = config
        self.replace_source(
            source_id,
            servers,
            scope=scope if not isinstance(source, Mapping) else str(source["scope"]),
            source_kind=source_kind if not isinstance(source, Mapping) else str(source["source_kind"]),
            expected_revision=expected_revision,
        )
        record = self.get_server(name, include_inactive=True)
        if record is None:
            raise McpConfigError(f"MCP server {name} was not materialized after update")
        return record.config

    add_server = upsert_server
    upsert = upsert_server

    def remove_server(
        self,
        name: str,
        *,
        source_id: str,
        expected_revision: int | None = None,
    ) -> McpServerConfig:
        state = self.state_store.read_state()
        source = state["config_sources"].get(source_id)
        if not isinstance(source, Mapping):
            raise McpConfigNotFound(f"unknown MCP config source: {source_id}")
        servers = copy.deepcopy(source.get("servers") or {})
        raw = servers.pop(name, None)
        if not isinstance(raw, Mapping):
            raise McpConfigNotFound(f"unknown MCP server {name!r} in source {source_id}")
        self.replace_source(
            source_id,
            servers,
            scope=str(source["scope"]),
            source_kind=str(source["source_kind"]),
            source_revision=str(source.get("source_revision") or ""),
            source_path=str(source.get("source_path") or ""),
            precedence=int(source.get("precedence") or 0),
            enabled=bool(source.get("enabled", True)),
            stale_after_seconds=source.get("stale_after_seconds"),
            metadata=source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {},
            expected_revision=expected_revision,
        )
        return _server_model(raw)

    def approve_project_server(
        self,
        name: str,
        *,
        source_id: str | None = None,
        actor: str = "user",
        reason: str = "approved",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self._set_project_approval(
            name,
            McpApprovalState.APPROVED,
            source_id=source_id,
            actor=actor,
            reason=reason,
            expected_revision=expected_revision,
        )

    approve = approve_project_server

    def reject_project_server(
        self,
        name: str,
        *,
        source_id: str | None = None,
        actor: str = "user",
        reason: str = "rejected",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self._set_project_approval(
            name,
            McpApprovalState.REJECTED,
            source_id=source_id,
            actor=actor,
            reason=reason,
            expected_revision=expected_revision,
        )

    reject = reject_project_server

    def reset_project_approval(
        self,
        name: str,
        *,
        source_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self._set_project_approval(
            name,
            McpApprovalState.PENDING,
            source_id=source_id,
            actor="runtime",
            reason="approval_reset",
            expected_revision=expected_revision,
        )

    def approval_status(self, name: str, *, source_id: str | None = None) -> McpApprovalState:
        state = self.state_store.read_state()
        selected_source = self._find_project_source(state, name, source_id)
        raw = state["approvals"].get(_approval_key(selected_source, name))
        if not isinstance(raw, Mapping):
            return McpApprovalState.PENDING
        return McpApprovalState(str(raw.get("status") or McpApprovalState.PENDING))

    def set_disabled(
        self,
        name: str,
        disabled: bool = True,
        *,
        source_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> None:
            if source_id is None:
                values = set(str(item) for item in state["policy"].get("disabled_servers") or [])
                if disabled:
                    values.add(name)
                else:
                    values.discard(name)
                state["policy"]["disabled_servers"] = sorted(values)
                return
            source = state["config_sources"].get(source_id)
            if not isinstance(source, MutableMapping):
                raise McpConfigNotFound(f"unknown MCP config source: {source_id}")
            server = source.get("servers", {}).get(name)
            if not isinstance(server, MutableMapping):
                raise McpConfigNotFound(f"unknown MCP server {name!r} in source {source_id}")
            server["disabled"] = bool(disabled)

        state = self.state_store.mutate(
            update,
            expected_revision=expected_revision,
            actor="mcp_config_store",
            reason="server_disabled" if disabled else "server_enabled",
            event_kind=McpRuntimeEventKind.CONFIG_CHANGED,
            server_id=name,
            source_id=source_id or "",
            journal_payload={"server_name": name, "disabled": bool(disabled)},
        )
        return state

    disable = set_disabled

    def enable(
        self,
        name: str,
        *,
        source_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self.set_disabled(
            name,
            False,
            source_id=source_id,
            expected_revision=expected_revision,
        )

    def set_policy(
        self,
        *,
        allowed_mcp_servers: Any = _UNSET,
        denied_mcp_servers: Any = _UNSET,
        disabled_servers: Any = _UNSET,
        allowed_tools: Any = _UNSET,
        denied_tools: Any = _UNSET,
        enterprise_exclusive: Any = _UNSET,
        expected_revision: int | None = None,
        provenance: Sequence[str] = (),
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if allowed_mcp_servers is not _UNSET:
            updates["allowed_mcp_servers"] = _validate_policy_entries(allowed_mcp_servers, allow_none=True)
        if denied_mcp_servers is not _UNSET:
            updates["denied_mcp_servers"] = _validate_policy_entries(denied_mcp_servers, allow_none=False)
        if disabled_servers is not _UNSET:
            updates["disabled_servers"] = _string_list(disabled_servers, "disabled_servers")
        if allowed_tools is not _UNSET:
            updates["allowed_tools"] = _tool_policy_mapping(allowed_tools, "allowed_tools")
        if denied_tools is not _UNSET:
            updates["denied_tools"] = _tool_policy_mapping(denied_tools, "denied_tools")
        if enterprise_exclusive is not _UNSET:
            updates["enterprise_exclusive"] = bool(enterprise_exclusive)
        if provenance:
            updates["provenance"] = _string_list(provenance, "provenance")

        def update(state: dict[str, Any]) -> None:
            state["policy"].update(copy.deepcopy(updates))

        state = self.state_store.mutate(
            update,
            expected_revision=expected_revision,
            actor="mcp_config_store",
            reason="policy_updated",
            event_kind=McpRuntimeEventKind.CONFIG_CHANGED,
            journal_payload={"updated_policy_fields": sorted(updates)},
        )
        return copy.deepcopy(state["policy"])

    configure_policy = set_policy

    def policy_decision(
        self,
        name: str,
        config: Mapping[str, Any] | McpServerConfig,
        *,
        policy: Mapping[str, Any] | None = None,
    ) -> McpPolicyDecision:
        selected = policy or self.state_store.read_state()["policy"]
        return _policy_decision(name, config, selected)

    def tool_allowed(self, server_name: str, tool_name: str) -> McpPolicyDecision:
        # Capability projection carries the canonical runtime ``server_id``;
        # policy configuration is keyed by the human-facing config name.  A
        # reference may match either identity, but it must resolve to exactly
        # one record.  Duplicate explicit IDs or name/ID collisions fail
        # closed rather than borrowing another server's allowlist.
        matches = self._servers_for_reference(server_name, include_inactive=True)
        if not matches:
            return McpPolicyDecision(McpPolicyDecisionKind.DENY, "unknown_server")
        if len(matches) != 1:
            return McpPolicyDecision(
                McpPolicyDecisionKind.DENY,
                "ambiguous_server_identity",
                {"server_reference": server_name, "match_count": len(matches)},
                ("canonical_identity_resolution",),
            )
        record = matches[0]
        policy_name = record.name
        state = self.state_store.read_state()
        global_denied = _patterns_for_server(state["policy"].get("denied_tools"), policy_name)
        denied = (*global_denied, *record.denied_tools)
        for pattern in denied:
            if fnmatch.fnmatchcase(tool_name, pattern):
                return McpPolicyDecision(
                    McpPolicyDecisionKind.DENY,
                    "tool_denylist_match",
                    {"pattern": pattern},
                    ("global_or_server_denylist",),
                )
        global_allowed = _patterns_for_server(
            state["policy"].get("allowed_tools"),
            policy_name,
            missing_none=True,
        )
        local_allowed = record.allowed_tools
        allowed_sets = [value for value in (global_allowed, local_allowed) if value is not None]
        if not allowed_sets:
            return McpPolicyDecision(McpPolicyDecisionKind.ALLOW, "no_tool_allowlist")
        for patterns in allowed_sets:
            if not any(fnmatch.fnmatchcase(tool_name, pattern) for pattern in patterns):
                return McpPolicyDecision(
                    McpPolicyDecisionKind.DENY,
                    "tool_not_in_allowlist",
                    provenance=("global_or_server_allowlist",),
                )
        return McpPolicyDecision(McpPolicyDecisionKind.ALLOW, "tool_allowlist_match")

    def resolve(self) -> McpConfigResolution:
        state = self.state_store.read_state()
        enterprise_ids = _enterprise_source_ids(state)
        exclusive = bool(enterprise_ids and state["policy"].get("enterprise_exclusive", True))
        candidates: list[_Candidate] = []
        excluded_by_enterprise: list[_Candidate] = []
        issues: list[McpConfigIssue] = []
        for source_id, source in state["config_sources"].items():
            if not isinstance(source, Mapping):
                issues.append(McpConfigIssue("invalid_source", "source record is not an object", source_id=source_id))
                continue
            try:
                provenance = _source_provenance(source_id, source)
            except (TypeError, ValueError) as error:
                issues.append(McpConfigIssue("invalid_source", str(error), source_id=source_id))
                continue
            for name, raw in (source.get("servers") or {}).items():
                if not isinstance(raw, Mapping):
                    issues.append(
                        McpConfigIssue(
                            "invalid_server",
                            "server configuration is not an object",
                            source_id=source_id,
                            server_name=str(name),
                        )
                    )
                    continue
                try:
                    candidate = _candidate_from_state(
                        str(name),
                        raw,
                        source,
                        provenance,
                        state["approvals"],
                        state["policy"],
                    )
                except (TypeError, ValueError) as error:
                    issues.append(
                        McpConfigIssue(
                            "invalid_server",
                            str(error),
                            source_id=source_id,
                            server_name=str(name),
                        )
                    )
                    continue
                if exclusive and source_id not in enterprise_ids:
                    excluded_by_enterprise.append(candidate)
                else:
                    candidates.append(candidate)

        by_name: dict[str, list[_Candidate]] = defaultdict(list)
        for candidate in candidates:
            by_name[candidate.name].append(candidate)
        winners: list[_Candidate] = []
        records: dict[str, McpEffectiveServer] = {}
        for name, values in by_name.items():
            ordered = sorted(values, key=lambda item: item.rank, reverse=True)
            winner = ordered[0]
            winners.append(winner)
            records[name] = winner.record(
                shadowed=tuple(item.provenance for item in ordered[1:]),
            )

        signature_owners: dict[str, str] = {}
        ordered_winners = sorted(winners, key=lambda item: item.rank, reverse=True)
        for candidate in ordered_winners:
            record = records[candidate.name]
            inactive_reason = _inactive_reason(record)
            if inactive_reason is not McpSuppressionReason.NONE:
                records[candidate.name] = replace(record, suppression_reason=inactive_reason)
                continue
            if candidate.signature and candidate.signature in signature_owners:
                owner = signature_owners[candidate.signature]
                owner_record = records[owner]
                records[candidate.name] = replace(
                    record,
                    suppression_reason=McpSuppressionReason.DUPLICATE_SIGNATURE,
                    duplicate_of=owner,
                )
                records[owner] = replace(
                    owner_record,
                    duplicate_provenance=(*owner_record.duplicate_provenance, candidate.provenance),
                )
                continue
            if candidate.signature:
                signature_owners[candidate.signature] = candidate.name

        for candidate in excluded_by_enterprise:
            if candidate.name in records:
                continue
            records[candidate.name] = replace(
                candidate.record(),
                suppression_reason=McpSuppressionReason.ENTERPRISE_EXCLUSIVE,
            )

        active = {
            name: record
            for name, record in sorted(records.items())
            if record.connectable
        }
        pending = tuple(sorted(name for name, item in records.items() if item.approval is McpApprovalState.PENDING))
        rejected = tuple(sorted(name for name, item in records.items() if item.approval is McpApprovalState.REJECTED))
        disabled = tuple(
            sorted(
                name
                for name, item in records.items()
                if item.config.disabled
                or item.suppression_reason in {McpSuppressionReason.DISABLED, McpSuppressionReason.SOURCE_DISABLED}
            )
        )
        policy_blocked = tuple(sorted(name for name, item in records.items() if not item.policy.allowed))
        suppressed = tuple(
            sorted(name for name, item in records.items() if item.suppression_reason is not McpSuppressionReason.NONE)
        )
        return McpConfigResolution(
            revision=int(state["revision"]),
            servers=active,
            all_servers=dict(sorted(records.items())),
            pending=pending,
            rejected=rejected,
            disabled=disabled,
            policy_blocked=policy_blocked,
            suppressed=suppressed,
            enterprise_exclusive=exclusive,
            enterprise_source_ids=enterprise_ids,
            issues=tuple(issues),
        )

    def effective_servers(self) -> dict[str, McpServerConfig]:
        return self.resolve().configs()

    list_effective = effective_servers

    def list_servers(self, *, include_inactive: bool = False) -> dict[str, McpEffectiveServer]:
        resolution = self.resolve()
        return dict(resolution.all_servers if include_inactive else resolution.servers)

    list = list_servers

    def get_server(self, name: str, *, include_inactive: bool = False) -> McpEffectiveServer | None:
        matches = self._servers_for_reference(name, include_inactive=include_inactive)
        return matches[0] if len(matches) == 1 else None

    def _servers_for_reference(
        self,
        reference: str,
        *,
        include_inactive: bool,
    ) -> tuple[McpEffectiveServer, ...]:
        selected = str(reference).strip()
        if not selected:
            return ()
        records = self.list_servers(include_inactive=include_inactive)
        matches: dict[tuple[str, str], McpEffectiveServer] = {}
        for name, record in records.items():
            if name == selected or record.server_id == selected or record.config.server_id == selected:
                matches[(record.name, record.server_id)] = record
        return tuple(matches[key] for key in sorted(matches))

    get = get_server

    def materialize_server(
        self,
        name: str,
        *,
        include_inactive: bool = False,
        require_complete: bool = True,
    ) -> McpMaterializedServer:
        record = self.get_server(name, include_inactive=include_inactive)
        if record is None:
            raise McpConfigNotFound(f"unknown or inactive MCP server: {name}")
        state = self.state_store.read_state()
        source = state["config_sources"].get(record.provenance.source_id)
        raw = source.get("servers", {}).get(record.name) if isinstance(source, Mapping) else None
        if not isinstance(raw, Mapping):
            raise McpConfigNotFound(f"MCP config provenance disappeared for server: {name}")
        expansion = expand_environment(raw, self.environ)
        if require_complete and expansion.missing_variables:
            raise McpEnvironmentExpansionError(
                f"missing environment variables for MCP server {name}: {', '.join(expansion.missing_variables)}"
            )
        expanded_mapping = copy.deepcopy(dict(expansion.value))
        # Derived fields were computed over the safe unresolved representation;
        # rebuild the model from runtime fields only.
        expanded_mapping.pop("signature", None)
        expanded_mapping.pop("fingerprint", None)
        expanded_mapping.pop("connectable", None)
        config = _server_model(expanded_mapping)
        return McpMaterializedServer(record=record, config=config, expansion=expansion)

    materialize = materialize_server

    def public_snapshot(self) -> dict[str, Any]:
        state = self.state_store.read_state()
        resolution = self.resolve()
        sources = {}
        for source_id, source in state["config_sources"].items():
            sources[source_id] = {
                key: copy.deepcopy(value)
                for key, value in source.items()
                if key != "servers"
            }
            sources[source_id]["server_names"] = sorted((source.get("servers") or {}).keys())
            sources[source_id]["server_count"] = len(source.get("servers") or {})
        return {
            "revision": int(state["revision"]),
            "sources": sources,
            "policy": copy.deepcopy(state["policy"]),
            "approvals": copy.deepcopy(state["approvals"]),
            "resolution": resolution.to_dict(),
            "secret_values_included": False,
        }

    snapshot = public_snapshot

    def cleanup_stale_sources(
        self,
        *,
        now: datetime | None = None,
        source_kinds: Iterable[McpConfigSourceKind | str] = _STALE_SOURCE_KINDS,
        source_ids: Iterable[str] | None = None,
        expected_revision: int | None = None,
    ) -> McpStaleCleanupResult:
        selected_now = now or self.clock()
        if selected_now.tzinfo is None or selected_now.utcoffset() is None:
            raise ValueError("stale cleanup time must be timezone-aware")
        allowed_kinds = {str(getattr(item, "value", item)) for item in source_kinds}
        allowed_ids = set(source_ids) if source_ids is not None else None
        removed_sources: list[str] = []
        removed_server_ids: set[str] = set()
        retained_server_ids: set[str] = set()

        def cleanup(state: dict[str, Any]) -> None:
            for source_id, source in list(state["config_sources"].items()):
                if allowed_ids is not None and source_id not in allowed_ids:
                    continue
                if str(source.get("source_kind") or "") not in allowed_kinds:
                    continue
                stale_after = source.get("stale_after_seconds")
                if stale_after is None:
                    continue
                last_seen = _parse_time(str(source.get("last_seen_at") or source.get("loaded_at") or ""))
                if selected_now.astimezone(timezone.utc) < last_seen + timedelta(seconds=float(stale_after)):
                    continue
                removed_sources.append(source_id)
                for config in (source.get("servers") or {}).values():
                    if isinstance(config, Mapping):
                        removed_server_ids.add(str(config.get("server_id") or ""))
                del state["config_sources"][source_id]
                prefix = f"{source_id}::"
                for key in list(state["approvals"]):
                    if key.startswith(prefix):
                        del state["approvals"][key]
            for source in state["config_sources"].values():
                for config in (source.get("servers") or {}).values():
                    if isinstance(config, Mapping):
                        retained_server_ids.add(str(config.get("server_id") or ""))
            orphaned = removed_server_ids - retained_server_ids
            for section in ("connections", "catalogs", "auth"):
                for server_id in orphaned:
                    state[section].pop(server_id, None)

        state = self.state_store.mutate(
            cleanup,
            expected_revision=expected_revision,
            actor="mcp_config_store",
            reason="stale_source_cleanup",
            event_kind=McpRuntimeEventKind.CONFIG_STALE_CLEANUP,
            journal_payload={"source_kinds": sorted(allowed_kinds)},
        )
        return McpStaleCleanupResult(
            revision=int(state["revision"]),
            removed_source_ids=tuple(sorted(removed_sources)),
            removed_server_ids=tuple(sorted(value for value in removed_server_ids if value)),
            retained_server_ids=tuple(sorted(value for value in retained_server_ids if value)),
        )

    cleanup_stale = cleanup_stale_sources

    def _set_project_approval(
        self,
        name: str,
        status: McpApprovalState,
        *,
        source_id: str | None,
        actor: str,
        reason: str,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        now = self._now_iso()
        selected: list[str] = []

        def update(state: dict[str, Any]) -> None:
            selected_source = self._find_project_source(state, name, source_id)
            selected.append(selected_source)
            raw = state["config_sources"][selected_source]["servers"][name]
            signature = str(raw.get("signature") or mcp_server_signature(raw) or "")
            key = _approval_key(selected_source, name)
            existing = state["approvals"].get(key)
            state["approvals"][key] = {
                "source_id": selected_source,
                "server_name": name,
                "signature": signature,
                "status": str(status),
                "revision": int(existing.get("revision") or 0) + 1 if isinstance(existing, Mapping) else 0,
                "decided_at": now if status is not McpApprovalState.PENDING else "",
                "actor": actor,
                "reason": reason,
            }

        kind = {
            McpApprovalState.APPROVED: McpRuntimeEventKind.CONFIG_APPROVED,
            McpApprovalState.REJECTED: McpRuntimeEventKind.CONFIG_REJECTED_BY_USER,
            McpApprovalState.PENDING: McpRuntimeEventKind.CONFIG_APPROVAL_PENDING,
        }[status]
        state = self.state_store.mutate(
            update,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
            event_kind=kind,
            source_id=source_id or "",
            journal_payload={"server_name": name, "status": str(status)},
        )
        return copy.deepcopy(state["approvals"][_approval_key(selected[0], name)])

    def _find_project_source(
        self,
        state: Mapping[str, Any],
        name: str,
        source_id: str | None,
    ) -> str:
        if source_id is not None:
            source = state["config_sources"].get(source_id)
            if not isinstance(source, Mapping) or str(source.get("scope") or "") != "project":
                raise McpProjectApprovalError(f"not a project MCP source: {source_id}")
            if name not in (source.get("servers") or {}):
                raise McpConfigNotFound(f"project MCP server {name!r} not found in {source_id}")
            return source_id
        matches = [
            candidate_id
            for candidate_id, source in state["config_sources"].items()
            if str(source.get("scope") or "") == "project" and name in (source.get("servers") or {})
        ]
        if not matches:
            raise McpConfigNotFound(f"unknown project MCP server: {name}")
        if len(matches) > 1:
            raise McpProjectApprovalError(
                f"project MCP server {name!r} is ambiguous across sources: {', '.join(sorted(matches))}"
            )
        return matches[0]

    def _normalize_config(
        self,
        name: str,
        config: Any,
        *,
        source_id: str,
        scope: McpConfigScope,
        source_revision: str,
        source_path: str,
    ) -> dict[str, Any]:
        name = _required_text(name, "server name", limit=_SERVER_NAME_LIMIT)
        raw = _config_mapping(config, include_templates=True)
        _validate_no_literal_secrets(raw, server_name=name)
        transport = _coerce_transport(raw)
        command = str(raw.get("command") or "").strip()
        args = _string_list(raw.get("args") or (), f"{name}.args")
        url = _extract_url(raw)
        if transport is McpTransportKind.STDIO:
            if not command:
                raise McpConfigValidationError(f"stdio MCP server {name} requires command")
            url = ""
        elif transport is McpTransportKind.STREAMABLE_HTTP:
            if not url:
                raise McpConfigValidationError(f"remote MCP server {name} requires url")
            command = ""
            args = []
        server_id = str(raw.get("server_id") or _stable_server_id(source_id, name))
        approval = (
            McpApprovalState.PENDING
            if scope is McpConfigScope.PROJECT
            else _coerce_approval(raw.get("approval"), McpApprovalState.NOT_REQUIRED)
        )
        env = _string_mapping(raw.get("env") or {}, f"{name}.env")
        headers = _string_mapping(raw.get("headers") or {}, f"{name}.headers")
        metadata = _string_metadata(raw.get("metadata") or {})
        policy_provenance = _string_list(raw.get("policy_provenance") or (), "policy_provenance")
        filter_provenance = _string_list(raw.get("filter_provenance") or (), "filter_provenance")
        model = McpServerConfig(
            server_id=server_id,
            name=name,
            transport=transport,
            scope=scope,
            command=command,
            args=tuple(args),
            url=url,
            env=env,
            headers=headers,
            disabled=bool(raw.get("disabled", False)),
            approval=approval,
            source_path=source_path or str(raw.get("source_path") or ""),
            source_revision=source_revision or str(raw.get("source_revision") or ""),
            policy_provenance=tuple(policy_provenance),
            filter_provenance=tuple(filter_provenance),
            connect_timeout_seconds=float(raw.get("connect_timeout_seconds", 10.0)),
            request_timeout_seconds=float(raw.get("request_timeout_seconds", 30.0)),
            max_response_bytes=int(raw.get("max_response_bytes", 8 * 1024 * 1024)),
            metadata=metadata,
        )
        value = model.to_dict(include_secrets=True)
        value["signature"] = mcp_server_signature(value)
        value["fingerprint"] = _config_fingerprint(value)
        value["allowed_tools"] = (
            _string_list(raw["allowed_tools"], f"{name}.allowed_tools")
            if "allowed_tools" in raw and raw["allowed_tools"] is not None
            else None
        )
        value["denied_tools"] = _string_list(raw.get("denied_tools") or (), f"{name}.denied_tools")
        value["required"] = bool(raw.get("required", False))
        if raw.get("credential_ref"):
            value["credential_ref"] = _required_text(raw["credential_ref"], "credential_ref", limit=2048)
        return value

    def _now_iso(self) -> str:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("MCP config clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class _Candidate:
    name: str
    config: McpServerConfig
    raw: Mapping[str, Any]
    provenance: McpSourceProvenance
    signature: str
    approval: McpApprovalState
    policy: McpPolicyDecision
    source_enabled: bool
    globally_disabled: bool

    @property
    def rank(self) -> tuple[int, int, str]:
        return self.provenance.precedence, self.provenance.generation, self.provenance.source_id

    def record(
        self,
        *,
        shadowed: tuple[McpSourceProvenance, ...] = (),
    ) -> McpEffectiveServer:
        allowed_raw = self.raw.get("allowed_tools")
        allowed = tuple(str(item) for item in allowed_raw) if isinstance(allowed_raw, list | tuple) else None
        denied = tuple(str(item) for item in self.raw.get("denied_tools") or ())
        return McpEffectiveServer(
            name=self.name,
            server_id=self.config.server_id,
            config=replace(self.config, approval=self.approval),
            signature=self.signature,
            provenance=self.provenance,
            approval=self.approval,
            policy=self.policy,
            enabled=self.source_enabled and not self.globally_disabled,
            shadowed_provenance=shadowed,
            allowed_tools=allowed,
            denied_tools=denied,
            required=bool(self.raw.get("required", False)),
        )


def _candidate_from_state(
    name: str,
    raw: Mapping[str, Any],
    source: Mapping[str, Any],
    provenance: McpSourceProvenance,
    approvals: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> _Candidate:
    config = _server_model(raw)
    signature = str(raw.get("signature") or mcp_server_signature(raw) or "")
    approval = config.approval
    if provenance.scope is McpConfigScope.PROJECT:
        record = approvals.get(_approval_key(provenance.source_id, name))
        if isinstance(record, Mapping) and str(record.get("signature") or "") == signature:
            approval = _coerce_approval(record.get("status"), McpApprovalState.PENDING)
        else:
            approval = McpApprovalState.PENDING
    disabled_patterns = [str(item) for item in policy.get("disabled_servers") or ()]
    globally_disabled = any(fnmatch.fnmatchcase(name, pattern) for pattern in disabled_patterns)
    return _Candidate(
        name=name,
        config=config,
        raw=raw,
        provenance=provenance,
        signature=signature,
        approval=approval,
        policy=_policy_decision(name, raw, policy),
        source_enabled=bool(source.get("enabled", True)),
        globally_disabled=globally_disabled,
    )


def _inactive_reason(record: McpEffectiveServer) -> McpSuppressionReason:
    if not record.provenance.metadata.get("source_enabled", True):
        return McpSuppressionReason.SOURCE_DISABLED
    if not record.enabled or record.config.disabled:
        return McpSuppressionReason.DISABLED
    if record.approval is McpApprovalState.PENDING:
        return McpSuppressionReason.PROJECT_PENDING
    if record.approval is McpApprovalState.REJECTED:
        return McpSuppressionReason.PROJECT_REJECTED
    if not record.policy.allowed:
        return McpSuppressionReason.POLICY_DENIED
    return McpSuppressionReason.NONE


def _policy_decision(
    name: str,
    config: Mapping[str, Any] | McpServerConfig,
    policy: Mapping[str, Any],
) -> McpPolicyDecision:
    denied = policy.get("denied_mcp_servers")
    if isinstance(denied, Sequence) and not isinstance(denied, str):
        for entry in denied:
            if _policy_entry_matches(name, config, entry):
                return McpPolicyDecision(
                    McpPolicyDecisionKind.DENY,
                    "denylist_match",
                    _policy_entry_dict(entry),
                    tuple(str(item) for item in policy.get("provenance") or ()),
                )
    allowed = policy.get("allowed_mcp_servers", None)
    if allowed is None:
        return McpPolicyDecision(McpPolicyDecisionKind.ALLOW, "no_allowlist")
    if not isinstance(allowed, Sequence) or isinstance(allowed, str):
        return McpPolicyDecision(McpPolicyDecisionKind.DENY, "invalid_allowlist")
    if not allowed:
        return McpPolicyDecision(McpPolicyDecisionKind.DENY, "empty_allowlist")

    value = _config_mapping(config, include_templates=True)
    is_stdio = _transport_text(value) == "stdio" or bool(value.get("command") and not _extract_url(value))
    is_remote = bool(_extract_url(value))
    has_command_entries = any(_entry_command(entry) is not None for entry in allowed)
    has_url_entries = any(_entry_url(entry) is not None for entry in allowed)
    for entry in allowed:
        if is_stdio and has_command_entries and _command_entry_matches(config, entry):
            return McpPolicyDecision(McpPolicyDecisionKind.ALLOW, "command_allowlist_match", _policy_entry_dict(entry))
        if is_remote and has_url_entries and _url_entry_matches(config, entry):
            return McpPolicyDecision(McpPolicyDecisionKind.ALLOW, "url_allowlist_match", _policy_entry_dict(entry))
        if not (is_stdio and has_command_entries) and not (is_remote and has_url_entries):
            if _name_entry_matches(name, entry):
                return McpPolicyDecision(McpPolicyDecisionKind.ALLOW, "name_allowlist_match", _policy_entry_dict(entry))
    return McpPolicyDecision(McpPolicyDecisionKind.DENY, "not_in_allowlist")


def _policy_entry_matches(name: str, config: Mapping[str, Any] | McpServerConfig, entry: Any) -> bool:
    return (
        _name_entry_matches(name, entry)
        or _command_entry_matches(config, entry)
        or _url_entry_matches(config, entry)
    )


def _name_entry_matches(name: str, entry: Any) -> bool:
    if isinstance(entry, str):
        return fnmatch.fnmatchcase(name, entry)
    if isinstance(entry, Mapping):
        pattern = entry.get("serverName", entry.get("server_name", entry.get("name")))
        return isinstance(pattern, str) and fnmatch.fnmatchcase(name, pattern)
    return False


def _entry_command(entry: Any) -> list[str] | None:
    if not isinstance(entry, Mapping):
        return None
    value = entry.get("serverCommand", entry.get("server_command", entry.get("command")))
    if isinstance(value, str):
        args = entry.get("args") or []
        return [value, *_string_list(args, "policy.args")]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return _string_list(value, "policy.server_command")
    return None


def _command_entry_matches(config: Mapping[str, Any] | McpServerConfig, entry: Any) -> bool:
    expected = _entry_command(entry)
    if expected is None:
        return False
    value = _config_mapping(config, include_templates=True)
    command = str(value.get("command") or "")
    actual = [command, *_string_list(value.get("args") or (), "args")] if command else []
    return actual == expected


def _entry_url(entry: Any) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    value = entry.get("serverUrl", entry.get("server_url", entry.get("url")))
    return str(value) if isinstance(value, str) and value else None


def _url_entry_matches(config: Mapping[str, Any] | McpServerConfig, entry: Any) -> bool:
    pattern = _entry_url(entry)
    if pattern is None:
        return False
    url = _extract_url(_config_mapping(config, include_templates=True))
    return bool(url and url_matches_pattern(unwrap_remote_url(url), unwrap_remote_url(pattern)))


def _policy_entry_dict(entry: Any) -> dict[str, Any]:
    if isinstance(entry, Mapping):
        return copy.deepcopy(dict(entry))
    return {"server_name": str(entry)}


def _extract_server_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        nested = value.get("mcpServers", value.get("mcp_servers"))
        if isinstance(nested, Mapping):
            value = nested
        return {str(key): copy.deepcopy(item) for key, item in value.items()}
    servers = getattr(value, "mcp_servers", getattr(value, "mcpServers", None))
    if isinstance(servers, Mapping):
        return {str(key): copy.deepcopy(item) for key, item in servers.items()}
    raise McpConfigValidationError("MCP source must contain a server mapping")


def _config_mapping(value: Any, *, include_templates: bool) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if isinstance(value, McpServerConfig):
        return copy.deepcopy(value.to_dict(include_secrets=include_templates))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            candidate = to_dict(include_secrets=include_templates)
        except TypeError:
            candidate = to_dict()
        if isinstance(candidate, Mapping):
            return copy.deepcopy(dict(candidate))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: copy.deepcopy(getattr(value, field.name)) for field in dataclasses.fields(value)}
    raise McpConfigValidationError(f"MCP server config must be mapping-like, got {type(value).__name__}")


def _server_model(raw: Mapping[str, Any]) -> McpServerConfig:
    allowed = {
        "server_id",
        "name",
        "transport",
        "scope",
        "command",
        "args",
        "url",
        "env",
        "headers",
        "disabled",
        "approval",
        "source_path",
        "source_revision",
        "policy_provenance",
        "filter_provenance",
        "connect_timeout_seconds",
        "request_timeout_seconds",
        "max_response_bytes",
        "metadata",
    }
    value = {key: copy.deepcopy(item) for key, item in raw.items() if key in allowed}
    return McpServerConfig.from_dict(value)


def _source_provenance(source_id: str, source: Mapping[str, Any]) -> McpSourceProvenance:
    metadata = copy.deepcopy(dict(source.get("metadata") or {}))
    metadata["source_enabled"] = bool(source.get("enabled", True))
    return McpSourceProvenance(
        source_id=source_id,
        source_kind=McpConfigSourceKind(str(source.get("source_kind") or "manual")),
        scope=McpConfigScope(str(source.get("scope") or "dynamic")),
        precedence=int(source.get("precedence") or 0),
        generation=int(source.get("generation") or 0),
        source_revision=str(source.get("source_revision") or ""),
        source_path=str(source.get("source_path") or ""),
        loaded_at=str(source.get("loaded_at") or ""),
        last_seen_at=str(source.get("last_seen_at") or ""),
        metadata=metadata,
    )


def _enterprise_source_ids(state: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        sorted(
            source_id
            for source_id, source in state["config_sources"].items()
            if bool(source.get("enabled", True))
            and (
                str(source.get("scope") or "") == "enterprise"
                or str(source.get("source_kind") or "") == "enterprise"
            )
        )
    )


def _cleanup_orphan_runtime_records(state: MutableMapping[str, Any]) -> None:
    retained = {
        str(config.get("server_id") or "")
        for source in state["config_sources"].values()
        for config in (source.get("servers") or {}).values()
        if isinstance(config, Mapping)
    }
    for section in ("connections", "catalogs", "auth"):
        for server_id in list(state[section]):
            if server_id not in retained:
                del state[section][server_id]


def _validate_no_literal_secrets(config: Mapping[str, Any], *, server_name: str) -> None:
    env = config.get("env") or {}
    if not isinstance(env, Mapping):
        raise McpConfigValidationError(f"{server_name}.env must be an object")
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise McpConfigValidationError(f"{server_name}.env entries must be strings")
        _validate_template_defaults(value, key=key, path=f"{server_name}.env.{key}")
        if _SENSITIVE_ENV_FRAGMENT.search(key) and not _contains_env_reference(value):
            raise McpConfigValidationError(
                f"{server_name}.env.{key} must reference an environment variable instead of persisting a secret"
            )
    headers = config.get("headers") or {}
    if not isinstance(headers, Mapping):
        raise McpConfigValidationError(f"{server_name}.headers must be an object")
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise McpConfigValidationError(f"{server_name}.headers entries must be strings")
        _validate_template_defaults(value, key=key, path=f"{server_name}.headers.{key}")
        if key.casefold() in _SENSITIVE_HEADER_NAMES and not _contains_env_reference(value):
            raise McpConfigValidationError(
                f"{server_name}.headers.{key} must use an environment variable template"
            )
    strings = [str(config.get("command") or ""), *_string_list(config.get("args") or (), "args")]
    for index, value in enumerate(strings):
        _validate_template_defaults(value, key="argument", path=f"{server_name}.command[{index}]")
        match = _SECRET_ARGUMENT.search(value)
        if match and not _contains_env_reference(match.group(1)):
            raise McpConfigValidationError(f"inline secret argument is forbidden for MCP server {server_name}")
    url = _extract_url(config)
    if url:
        try:
            parsed = urlsplit(url)
        except ValueError as error:
            raise McpConfigValidationError(f"invalid MCP URL for {server_name}: {error}") from error
        if parsed.username is not None or parsed.password is not None:
            raise McpConfigValidationError(f"credentials cannot be embedded in MCP URL for {server_name}")
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if _SENSITIVE_ENV_FRAGMENT.search(key) and value and not _contains_env_reference(value):
                raise McpConfigValidationError(
                    f"sensitive MCP URL query value {key!r} for {server_name} must use an environment template"
                )


def _validate_template_defaults(value: str, *, key: str, path: str) -> None:
    for match in _ENV_PATTERN.finditer(value):
        name, default = match.group(1), match.group(2)
        if default and (_SENSITIVE_ENV_FRAGMENT.search(name) or _SENSITIVE_ENV_FRAGMENT.search(key)):
            raise McpConfigValidationError(f"sensitive environment template at {path} cannot contain a default value")


def _contains_env_reference(value: str) -> bool:
    return bool(_ENV_PATTERN.search(value))


def _validate_policy_entries(value: Any, *, allow_none: bool) -> list[Any] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise McpConfigValidationError("MCP policy entries must be an array")
    output: list[Any] = []
    for entry in value:
        if isinstance(entry, str):
            output.append(_required_text(entry, "policy server name", limit=32768))
        elif isinstance(entry, Mapping):
            selected = copy.deepcopy(dict(entry))
            if not any(
                key in selected
                for key in ("serverName", "server_name", "name", "serverCommand", "server_command", "command", "serverUrl", "server_url", "url")
            ):
                raise McpConfigValidationError("MCP policy entry must select name, command, or URL")
            _validate_no_literal_secrets(selected, server_name="policy")
            output.append(selected)
        else:
            raise McpConfigValidationError("MCP policy entries must be strings or objects")
    return output


def _tool_policy_mapping(value: Any, name: str) -> dict[str, list[str] | None]:
    if not isinstance(value, Mapping):
        raise McpConfigValidationError(f"{name} must be an object")
    output: dict[str, list[str] | None] = {}
    for server, patterns in value.items():
        key = _required_text(server, f"{name} server", limit=512)
        output[key] = None if patterns is None else _string_list(patterns, f"{name}.{key}")
    return output


def _patterns_for_server(
    value: Any,
    server_name: str,
    *,
    missing_none: bool = False,
) -> tuple[str, ...] | None:
    if not isinstance(value, Mapping):
        return None if missing_none else ()
    patterns: list[str] = []
    matched = False
    for server_pattern, items in value.items():
        if fnmatch.fnmatchcase(server_name, str(server_pattern)):
            matched = True
            if items is not None:
                patterns.extend(str(item) for item in items)
    if missing_none and not matched:
        return None
    return tuple(patterns)


def _extract_url(value: Mapping[str, Any]) -> str:
    for key in ("url", "endpoint", "server_url", "serverUrl"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    for key in ("transport", "server", "remote"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            candidate = _extract_url(nested)
            if candidate:
                return candidate
    return ""


def _transport_text(value: Mapping[str, Any]) -> str:
    candidate = value.get("transport", value.get("type", ""))
    if isinstance(candidate, Mapping):
        candidate = candidate.get("type", candidate.get("kind", ""))
    return str(getattr(candidate, "value", candidate)).casefold().replace("-", "_")


def _coerce_transport(value: Mapping[str, Any]) -> McpTransportKind:
    text = _transport_text(value)
    if not text:
        text = "stdio" if value.get("command") else "streamable_http" if _extract_url(value) else "in_process"
    aliases = {
        "stdio": McpTransportKind.STDIO,
        "http": McpTransportKind.STREAMABLE_HTTP,
        "https": McpTransportKind.STREAMABLE_HTTP,
        "sse": McpTransportKind.STREAMABLE_HTTP,
        "ws": McpTransportKind.STREAMABLE_HTTP,
        "websocket": McpTransportKind.STREAMABLE_HTTP,
        "streamable_http": McpTransportKind.STREAMABLE_HTTP,
        "claudeai_proxy": McpTransportKind.STREAMABLE_HTTP,
        "in_process": McpTransportKind.IN_PROCESS,
        "inprocess": McpTransportKind.IN_PROCESS,
        "sdk": McpTransportKind.IN_PROCESS,
    }
    try:
        return aliases[text]
    except KeyError as error:
        raise McpConfigValidationError(f"unsupported MCP transport: {text}") from error


def _coerce_scope(value: McpConfigScope | str) -> McpConfigScope:
    text = str(getattr(value, "value", value)).casefold().replace("claude.ai", "claudeai")
    try:
        return McpConfigScope(text)
    except ValueError as error:
        raise McpConfigValidationError(f"unsupported MCP config scope: {value}") from error


def _coerce_source_kind(
    value: McpConfigSourceKind | str | None,
    scope: McpConfigScope,
) -> McpConfigSourceKind:
    if value is None:
        if scope is McpConfigScope.PLUGIN:
            return McpConfigSourceKind.PLUGIN
        if scope is McpConfigScope.CLAUDE_AI:
            return McpConfigSourceKind.CLAUDEAI
        if scope is McpConfigScope.ENTERPRISE:
            return McpConfigSourceKind.ENTERPRISE
        if scope is McpConfigScope.SDK:
            return McpConfigSourceKind.SDK
        if scope is McpConfigScope.DYNAMIC:
            return McpConfigSourceKind.DYNAMIC
        return McpConfigSourceKind.MANUAL
    text = str(getattr(value, "value", value)).casefold().replace("claude.ai", "claudeai")
    try:
        return McpConfigSourceKind(text)
    except ValueError as error:
        raise McpConfigValidationError(f"unsupported MCP source kind: {value}") from error


def _coerce_approval(value: Any, default: McpApprovalState) -> McpApprovalState:
    if value is None or value == "":
        return default
    try:
        return McpApprovalState(str(getattr(value, "value", value)))
    except ValueError as error:
        raise McpConfigValidationError(f"unsupported MCP approval state: {value}") from error


def _default_precedence(scope: McpConfigScope, source_kind: McpConfigSourceKind) -> int:
    scope_value = str(scope)
    if source_kind is McpConfigSourceKind.ENTERPRISE:
        return _SOURCE_PRECEDENCE["enterprise"]
    if source_kind is McpConfigSourceKind.CLAUDEAI:
        return _SOURCE_PRECEDENCE["claudeai"]
    if source_kind is McpConfigSourceKind.PLUGIN:
        return _SOURCE_PRECEDENCE["plugin"]
    return _SOURCE_PRECEDENCE.get(scope_value, _SOURCE_PRECEDENCE["dynamic"])


def _stable_server_id(source_id: str, name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").casefold() or "server"
    digest = hashlib.sha256(f"{source_id}\0{name}".encode("utf-8")).hexdigest()[:12]
    return f"mcp-{slug[:120]}-{digest}"


def _config_fingerprint(value: Mapping[str, Any]) -> str:
    safe = {
        "server_id": value.get("server_id"),
        "name": value.get("name"),
        "transport": value.get("transport"),
        "scope": value.get("scope"),
        "signature": mcp_server_signature(value),
        "disabled": bool(value.get("disabled", False)),
        "source_revision": value.get("source_revision"),
        "env_keys": sorted((value.get("env") or {}).keys()),
        "header_keys": sorted((value.get("headers") or {}).keys()),
        "required": bool(value.get("required", False)),
    }
    encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _approval_key(source_id: str, name: str) -> str:
    return f"{source_id}::{name}"


def _required_text(value: Any, name: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise McpConfigValidationError(f"{name} must be a string")
    selected = value.strip()
    if not selected:
        raise McpConfigValidationError(f"{name} is required")
    if len(selected) > limit:
        raise McpConfigValidationError(f"{name} exceeds {limit} characters")
    return selected


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise McpConfigValidationError(f"{name} must be an array of strings")
    output: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise McpConfigValidationError(f"{name} must contain only strings")
        output.append(item)
    return output


def _string_mapping(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise McpConfigValidationError(f"{name} must be an object")
    output: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise McpConfigValidationError(f"{name} entries must be strings")
        output[key] = item
    return output


def _string_metadata(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise McpConfigValidationError("metadata must be an object")
    output: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, str):
            output[str(key)] = item
        elif item is None or isinstance(item, bool | int | float):
            output[str(key)] = json.dumps(item, ensure_ascii=False)
        else:
            output[str(key)] = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return output


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).casefold()
        if _SENSITIVE_ENV_FRAGMENT.search(normalized):
            raise McpConfigValidationError(f"secret-bearing source metadata field is forbidden: {key}")
        if item is None or isinstance(item, bool | int | float | str):
            output[str(key)] = item
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            output[str(key)] = [str(child) for child in item]
        else:
            output[str(key)] = str(item)
    return output


def _parse_time(value: str) -> datetime:
    if not value:
        raise McpConfigValidationError("source timestamp is required for stale cleanup")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise McpConfigValidationError("source timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "McpConfigError",
    "McpConfigIssue",
    "McpConfigNotFound",
    "McpConfigResolution",
    "McpConfigSourceKind",
    "McpConfigStore",
    "McpConfigValidationError",
    "McpEffectiveServer",
    "McpEnterpriseExclusiveError",
    "McpEnvironmentExpansion",
    "McpEnvironmentExpansionError",
    "McpMaterializedServer",
    "McpPolicyDecision",
    "McpPolicyDecisionKind",
    "McpProjectApprovalError",
    "McpServerConfig",
    "McpSourceGenerationConflict",
    "McpSourceProvenance",
    "McpStaleCleanupResult",
    "McpSuppressionReason",
    "expand_env_string",
    "expand_environment",
    "get_mcp_server_signature",
    "mcp_server_signature",
    "unwrap_ccr_proxy_url",
    "unwrap_remote_url",
    "url_matches_pattern",
]
