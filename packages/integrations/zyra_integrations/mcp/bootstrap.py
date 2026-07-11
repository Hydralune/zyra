from __future__ import annotations

"""Policy-preserving MCP startup and reconnect selection.

Bootstrap never discovers new configuration, approves a project server, edits
policy, resolves credentials, or owns connection state.  It reads the existing
config/state owners and reconnects only records that are both currently
connectable and demonstrably trusted.  Ordinary optional servers require a
previous-live durable connection record; explicitly required trusted servers
may be started for the first time.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, now_iso

from .models import (
    JsonValue,
    McpApprovalState,
    McpConfigScope,
    McpConnectionState,
    McpTransportKind,
    redact_value,
    stable_digest,
)


class McpBootstrapError(RuntimeError):
    pass


class McpBootstrapDisabled(McpBootstrapError):
    pass


class McpBootstrapMode(StrEnum):
    STARTUP = "startup"
    SESSION_RESUME = "session_resume"
    EXPLICIT_RECOVERY = "explicit_recovery"


class McpBootstrapDisposition(StrEnum):
    CONNECTED = "connected"
    RECONNECTED = "reconnected"
    ALREADY_LIVE = "already_live"
    SKIPPED_UNTRUSTED = "skipped_untrusted"
    SKIPPED_NOT_PREVIOUSLY_LIVE = "skipped_not_previously_live"
    SKIPPED_NOT_CONNECTABLE = "skipped_not_connectable"
    SKIPPED_NOT_SELECTED = "skipped_not_selected"
    FAILED = "failed"


class McpBootstrapConfigPort(Protocol):
    def list_servers(self, *, include_inactive: bool = False) -> Mapping[str, Any]: ...

    def materialize_server(
        self,
        name: str,
        *,
        include_inactive: bool = False,
        require_complete: bool = True,
    ) -> Any: ...


class McpBootstrapConnectionPort(Protocol):
    def snapshot(self, server_id: str) -> Any: ...

    def connect(self, config: Any, **context: Any) -> Any: ...

    def reconnect(self, server_id: str, **context: Any) -> Any: ...


class McpBootstrapStatePort(Protocol):
    def get_connection(self, server_id: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class McpBootstrapTrustPolicy:
    """Deployment trust boundary; permission cannot widen these sets."""

    trusted_scopes: frozenset[McpConfigScope] = frozenset(
        {
            McpConfigScope.ENTERPRISE,
            McpConfigScope.USER,
            McpConfigScope.LOCAL,
            McpConfigScope.SDK,
        }
    )
    trusted_source_prefixes: tuple[str, ...] = (
        "enterprise",
        "managed",
        "user",
        "local",
        "sdk",
        "manual:user",
        "manual:local",
    )
    allow_project_when_approved: bool = True
    allow_plugin_when_required: bool = True
    allow_dynamic_when_required: bool = False
    allow_first_connect_for_required: bool = True
    reconnect_previous_states: frozenset[McpConnectionState] = frozenset(
        {
            McpConnectionState.CONNECTED,
            McpConnectionState.RECONNECTING,
            McpConnectionState.CLOSED,
        }
    )
    never_resume_states: frozenset[McpConnectionState] = frozenset(
        {
            McpConnectionState.DISABLED,
            McpConnectionState.NEEDS_AUTH,
            McpConnectionState.FAILED,
        }
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "trusted_scopes", frozenset(self.trusted_scopes))
        object.__setattr__(self, "trusted_source_prefixes", tuple(self.trusted_source_prefixes))
        object.__setattr__(self, "reconnect_previous_states", frozenset(self.reconnect_previous_states))
        object.__setattr__(self, "never_resume_states", frozenset(self.never_resume_states))

    def source_trusted(self, record: Any) -> tuple[bool, str]:
        config = getattr(record, "config", None)
        provenance = getattr(record, "provenance", None)
        scope = _enum(McpConfigScope, getattr(config, "scope", ""))
        approval = _enum(McpApprovalState, getattr(record, "approval", getattr(config, "approval", "")))
        required = bool(getattr(record, "required", False))
        source_id = str(getattr(provenance, "source_id", "") or "").casefold()
        if scope in self.trusted_scopes:
            return True, "trusted_scope"
        if any(source_id.startswith(prefix.casefold()) for prefix in self.trusted_source_prefixes):
            return True, "trusted_source"
        if scope is McpConfigScope.PROJECT:
            if self.allow_project_when_approved and approval is McpApprovalState.APPROVED:
                return True, "approved_project"
            return False, "project_not_approved"
        if scope is McpConfigScope.PLUGIN:
            return (self.allow_plugin_when_required and required, "required_plugin" if required else "optional_plugin")
        if scope is McpConfigScope.DYNAMIC:
            return (self.allow_dynamic_when_required and required, "required_dynamic" if required else "dynamic_not_bootstrapped")
        return False, "source_not_trusted"

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "trusted_scopes": sorted(str(item) for item in self.trusted_scopes),
            "trusted_source_prefixes": list(self.trusted_source_prefixes),
            "allow_project_when_approved": self.allow_project_when_approved,
            "allow_plugin_when_required": self.allow_plugin_when_required,
            "allow_dynamic_when_required": self.allow_dynamic_when_required,
            "allow_first_connect_for_required": self.allow_first_connect_for_required,
            "reconnect_previous_states": sorted(str(item) for item in self.reconnect_previous_states),
            "never_resume_states": sorted(str(item) for item in self.never_resume_states),
            "policy_expansion_allowed": False,
        }


@dataclass(frozen=True, slots=True)
class McpBootstrapCandidate:
    name: str
    server_id: str
    scope: str
    source_id: str
    required: bool
    connectable: bool
    trusted: bool
    trust_reason: str
    previous_state: str
    previous_live: bool
    selected: bool
    selection_reason: str
    config_fingerprint: str

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "server_id": self.server_id,
            "scope": self.scope,
            "source_id": self.source_id,
            "required": self.required,
            "connectable": self.connectable,
            "trusted": self.trusted,
            "trust_reason": self.trust_reason,
            "previous_state": self.previous_state,
            "previous_live": self.previous_live,
            "selected": self.selected,
            "selection_reason": self.selection_reason,
            "config_fingerprint": self.config_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class McpBootstrapResult:
    candidate: McpBootstrapCandidate
    disposition: McpBootstrapDisposition
    connection_state: str = ""
    connection_generation: int = 0
    capability_generation: int = 0
    error_code: str = ""
    error_message: str = ""
    completed_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.disposition not in {McpBootstrapDisposition.FAILED}

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "candidate": self.candidate.safe_dict(),
            "disposition": str(self.disposition),
            "ok": self.ok,
            "connection_state": self.connection_state,
            "connection_generation": self.connection_generation,
            "capability_generation": self.capability_generation,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class McpBootstrapReport:
    mode: McpBootstrapMode
    results: tuple[McpBootstrapResult, ...]
    policy: McpBootstrapTrustPolicy
    run_id: str
    task_id: str
    session_id: str
    started_at: str
    completed_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.results)

    @property
    def connected_server_ids(self) -> tuple[str, ...]:
        accepted = {
            McpBootstrapDisposition.CONNECTED,
            McpBootstrapDisposition.RECONNECTED,
            McpBootstrapDisposition.ALREADY_LIVE,
        }
        return tuple(item.candidate.server_id for item in self.results if item.disposition in accepted)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-bootstrap-report.v1",
            "mode": str(self.mode),
            "ok": self.ok,
            "results": [item.safe_dict() for item in self.results],
            "connected_server_ids": list(self.connected_server_ids),
            "policy": self.policy.safe_dict(),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "config_mutated": False,
            "policy_mutated": False,
            "approval_mutated": False,
        }


class McpBootstrapRuntime:
    def __init__(
        self,
        config_store: McpBootstrapConfigPort,
        connection_runtime: McpBootstrapConnectionPort,
        state_store: McpBootstrapStatePort,
        *,
        policy: McpBootstrapTrustPolicy | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
        disabled: bool = False,
    ) -> None:
        self.config_store = config_store
        self.connection_runtime = connection_runtime
        self.state_store = state_store
        self.policy = policy or McpBootstrapTrustPolicy()
        self.event_sink = event_sink
        self.disabled = disabled
        self._lock = threading.RLock()
        self._last_report: McpBootstrapReport | None = None

    @property
    def last_report(self) -> McpBootstrapReport | None:
        with self._lock:
            return self._last_report

    def plan(
        self,
        *,
        include_servers: Sequence[str] | None = None,
    ) -> tuple[McpBootstrapCandidate, ...]:
        self._assert_enabled()
        included = frozenset(str(item) for item in (include_servers or ()) if str(item))
        records = self.config_store.list_servers(include_inactive=True)
        candidates: list[McpBootstrapCandidate] = []
        for name in sorted(records):
            record = records[name]
            server_id = str(getattr(record, "server_id", "") or getattr(getattr(record, "config", None), "server_id", ""))
            trusted, trust_reason = self.policy.source_trusted(record)
            durable = self.state_store.get_connection(server_id) or {}
            previous_state = str(durable.get("state") or "")
            previous_enum = _enum(McpConnectionState, previous_state)
            previous_live = previous_enum in self.policy.reconnect_previous_states and previous_enum not in self.policy.never_resume_states
            connectable = bool(getattr(record, "connectable", False))
            required = bool(getattr(record, "required", False))
            explicitly_selected = not included or name in included or server_id in included
            selected = connectable and trusted and explicitly_selected and (
                previous_live or (required and self.policy.allow_first_connect_for_required)
            )
            reason = "selected" if selected else _selection_reason(
                connectable=connectable,
                trusted=trusted,
                explicitly_selected=explicitly_selected,
                previous_live=previous_live,
                required=required,
                allow_required=self.policy.allow_first_connect_for_required,
            )
            config = getattr(record, "config", None)
            provenance = getattr(record, "provenance", None)
            candidates.append(
                McpBootstrapCandidate(
                    name=name,
                    server_id=server_id,
                    scope=str(getattr(config, "scope", "")),
                    source_id=str(getattr(provenance, "source_id", "")),
                    required=required,
                    connectable=connectable,
                    trusted=trusted,
                    trust_reason=trust_reason,
                    previous_state=previous_state,
                    previous_live=previous_live,
                    selected=selected,
                    selection_reason=reason,
                    config_fingerprint=str(getattr(config, "fingerprint", "")),
                )
            )
        return tuple(candidates)

    def bootstrap(
        self,
        *,
        mode: McpBootstrapMode | str = McpBootstrapMode.STARTUP,
        run_id: str = "",
        task_id: str = "",
        node_id: str | None = None,
        session_id: str = "",
        worker_request_id: str = "",
        include_servers: Sequence[str] | None = None,
        fail_required: bool = True,
    ) -> McpBootstrapReport:
        self._assert_enabled()
        selected_mode = McpBootstrapMode(str(mode))
        started_at = now_iso()
        results: list[McpBootstrapResult] = []
        with self._lock:
            for candidate in self.plan(include_servers=include_servers):
                if not candidate.selected:
                    results.append(_skipped(candidate))
                    continue
                results.append(
                    self._connect_candidate(
                        candidate,
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        session_id=session_id,
                        worker_request_id=worker_request_id,
                    )
                )
            report = McpBootstrapReport(
                selected_mode,
                tuple(results),
                self.policy,
                run_id,
                task_id,
                session_id,
                started_at,
            )
            self._last_report = report
        self._emit(report, node_id=node_id)
        failed_required = [item for item in results if item.candidate.required and not item.ok]
        if fail_required and failed_required:
            names = ", ".join(item.candidate.server_id for item in failed_required)
            raise McpBootstrapError(f"required MCP bootstrap failed: {names}")
        return report

    def _connect_candidate(self, candidate: McpBootstrapCandidate, **context: Any) -> McpBootstrapResult:
        try:
            try:
                live = self.connection_runtime.snapshot(candidate.server_id)
            except Exception:
                live = None
            if live is not None and bool(getattr(live, "healthy", False)):
                return _result(candidate, McpBootstrapDisposition.ALREADY_LIVE, live)
            materialized = self.config_store.materialize_server(candidate.name)
            config = getattr(materialized, "config", materialized)
            if not bool(getattr(config, "connectable", False)):
                return McpBootstrapResult(candidate, McpBootstrapDisposition.SKIPPED_NOT_CONNECTABLE)
            if candidate.previous_live and live is not None:
                receipt = self.connection_runtime.reconnect(candidate.server_id, **context)
                disposition = McpBootstrapDisposition.RECONNECTED
            else:
                receipt = self.connection_runtime.connect(config, **context)
                disposition = McpBootstrapDisposition.CONNECTED
            snapshot = getattr(receipt, "snapshot", receipt)
            return _result(candidate, disposition, snapshot, receipt=receipt)
        except Exception as error:  # noqa: BLE001 - report startup failure without policy mutation.
            return McpBootstrapResult(
                candidate,
                McpBootstrapDisposition.FAILED,
                error_code=type(error).__name__,
                error_message=str(error)[:1000],
            )

    def _emit(self, report: McpBootstrapReport, *, node_id: str | None) -> None:
        if self.event_sink is None or not report.run_id or not report.task_id:
            return
        event_type = getattr(EventType, "MCP_CONNECTION_CHANGED", EventType.SYSTEM_NOTICE)
        self.event_sink(
            EventRecord(
                run_id=report.run_id,
                task_id=report.task_id,
                node_id=node_id,
                event_type=event_type,
                payload={
                    "mcp_runtime": {
                        "schema": "zyra.mcp-bootstrap-event.v1",
                        "runtime_id": "McpBootstrapRuntime",
                        "report": report.safe_dict(),
                        "state_mutation": "trusted_connections_bootstrapped",
                    }
                },
            )
        )

    def _assert_enabled(self) -> None:
        if self.disabled:
            raise McpBootstrapDisabled("McpBootstrapRuntime is disabled")


def _result(candidate: McpBootstrapCandidate, disposition: McpBootstrapDisposition, snapshot: Any, *, receipt: Any = None) -> McpBootstrapResult:
    catalog = getattr(receipt, "catalog", None)
    return McpBootstrapResult(
        candidate,
        disposition,
        connection_state=str(getattr(snapshot, "state", "")),
        connection_generation=int(getattr(snapshot, "generation", 0)),
        capability_generation=int(getattr(catalog, "generation", 0)),
    )


def _skipped(candidate: McpBootstrapCandidate) -> McpBootstrapResult:
    mapping = {
        "not_connectable": McpBootstrapDisposition.SKIPPED_NOT_CONNECTABLE,
        "untrusted": McpBootstrapDisposition.SKIPPED_UNTRUSTED,
        "not_selected": McpBootstrapDisposition.SKIPPED_NOT_SELECTED,
        "not_previously_live": McpBootstrapDisposition.SKIPPED_NOT_PREVIOUSLY_LIVE,
    }
    return McpBootstrapResult(candidate, mapping.get(candidate.selection_reason, McpBootstrapDisposition.SKIPPED_NOT_PREVIOUSLY_LIVE))


def _selection_reason(*, connectable: bool, trusted: bool, explicitly_selected: bool, previous_live: bool, required: bool, allow_required: bool) -> str:
    if not connectable:
        return "not_connectable"
    if not trusted:
        return "untrusted"
    if not explicitly_selected:
        return "not_selected"
    if not previous_live and not (required and allow_required):
        return "not_previously_live"
    return "selected"


def _enum(enum_type: type[Any], value: Any) -> Any:
    try:
        return enum_type(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "McpBootstrapCandidate",
    "McpBootstrapConfigPort",
    "McpBootstrapConnectionPort",
    "McpBootstrapDisabled",
    "McpBootstrapDisposition",
    "McpBootstrapError",
    "McpBootstrapMode",
    "McpBootstrapReport",
    "McpBootstrapResult",
    "McpBootstrapRuntime",
    "McpBootstrapStatePort",
    "McpBootstrapTrustPolicy",
]
