from __future__ import annotations

"""MCP capability/main-path orchestration over existing Zyra owners.

The coordinator in this module owns no durable state.  It composes the existing
MCP runtime, 02C tool context, 03A-backed control port, session checkpoint port,
and canonical event port into one generation-bound view.  API, CLI, worker, and
future UI callers can share this view instead of creating parallel MCP clients.
"""

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, new_id, now_iso
from zyra_runtime import ToolExecutionContext

from .bootstrap import McpBootstrapMode, McpBootstrapReport, McpBootstrapRuntime
from .models import JsonValue, redact_value, stable_digest, to_json_value
from .resource_projection import (
    McpPromptCommandDescriptor,
    McpPromptCommandInvocation,
    McpResourceProjectionBundle,
    McpResourceProjectionRuntime,
    McpResourceProjectionStale,
)


MCP_MAIN_PATH_BOUNDARY: Mapping[str, Any] = {
    "schema": "zyra.mcp-main-path-boundary.v1",
    "runtime_entry": "zyra_integrations.mcp.main_path.McpMainPathRuntime",
    "state_owners": {
        "mcp": "McpClientRuntime",
        "session": "installed McpMainPathSessionPort",
        "permission_control": "installed McpMainPathControlPort",
        "events": "installed McpMainPathEventPort",
    },
    "composed_surfaces": (
        "02C dynamic tool registry",
        "03A exact permission handoff",
        "02D MCP instruction compact restore",
        "03D-compatible control command descriptors",
        "canonical event persistence",
    ),
    "forbidden_owners": (
        "parallel MCP client",
        "parallel session store",
        "parallel permission queue",
        "parallel command store",
    ),
}


class McpMainPathError(RuntimeError):
    pass


class McpMainPathDisabled(McpMainPathError):
    pass


class McpMainPathStale(McpMainPathError):
    pass


class McpMainPathControlRejected(McpMainPathError):
    pass


class McpMainPathCommandKind(StrEnum):
    STATUS = "status"
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    REFRESH = "refresh"
    ENABLE = "enable"
    DISABLE = "disable"
    APPROVE = "approve"
    REJECT = "reject"
    AUTH_REFRESH = "auth_refresh"
    AUTH_REVOKE = "auth_revoke"
    ELICITATION_RESOLVE = "elicitation_resolve"
    PROMPT = "prompt"


class McpMainPathRuntimePort(Protocol):
    def worker_projection(
        self,
        base_context: ToolExecutionContext,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        include_servers: Sequence[str] | None = None,
        include_resource_surfaces: bool = False,
        session_id: str = "",
        worker_request_id: str = "",
    ) -> Any: ...

    def prepare_worker_constraints(
        self,
        constraints: Mapping[str, Any],
        *,
        session_id: str,
    ) -> dict[str, Any]: ...

    def session_snapshot(self, session_id: str) -> Mapping[str, Any]: ...

    def diagnostics(self) -> Mapping[str, Any]: ...

    def drain_events(
        self,
        *,
        run_id: str = "",
        task_id: str = "",
    ) -> Sequence[EventRecord]: ...


class McpMainPathSessionPort(Protocol):
    def write_runtime_state(
        self,
        session_id: str,
        namespace: str,
        value: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        cause_event_id: str = "",
    ) -> Any: ...


class McpMainPathEventPort(Protocol):
    def append(self, event: EventRecord) -> Any: ...


class McpMainPathControlPort(Protocol):
    """03A/03D-owned mutation boundary.

    Implementations must perform deployment policy and exact permission checks
    before invoking the existing ``McpClientRuntime``.  The main-path runtime
    never calls mutation methods directly.
    """

    def execute_mcp_control(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class McpMainPathIdentity:
    run_id: str
    task_id: str
    session_id: str
    worker_request_id: str
    node_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "session_id", "worker_request_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
        }


@dataclass(frozen=True, slots=True)
class McpControlCommandDescriptor:
    name: str
    kind: McpMainPathCommandKind
    description: str
    input_schema: Mapping[str, JsonValue]
    mutates_runtime: bool
    requires_permission: bool
    remote_safe: bool
    source: str = "mcp"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("command name is required")
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "kind": str(self.kind),
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "mutates_runtime": self.mutates_runtime,
            "requires_permission": self.requires_permission,
            "remote_safe": self.remote_safe,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class McpMainPathCommandRequest:
    name: str
    arguments: Mapping[str, Any]
    identity: McpMainPathIdentity
    actor_id: str
    idempotency_key: str
    request_id: str = field(default_factory=lambda: new_id("mcpcontrol"))
    requested_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.name or not self.actor_id or not self.idempotency_key:
            raise ValueError("command name, actor_id and idempotency_key are required")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "arguments": redact_value(self.arguments),
            "identity": self.identity.safe_dict(),
            "actor_id": self.actor_id,
            "idempotency_key_digest": stable_digest(self.idempotency_key),
            "request_id": self.request_id,
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True, slots=True)
class McpMainPathCommandResult:
    request_id: str
    name: str
    ok: bool
    payload: Mapping[str, JsonValue]
    error_code: str = ""
    error_message: str = ""
    completed_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "request_id": self.request_id,
            "name": self.name,
            "ok": self.ok,
            "payload": redact_value(self.payload),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class McpMainPathProjection:
    projection_id: str
    identity: McpMainPathIdentity
    context: ToolExecutionContext
    tool_bundle: Any
    resource_bundle: McpResourceProjectionBundle
    command_descriptors: tuple[McpControlCommandDescriptor | McpPromptCommandDescriptor, ...]
    constraints: Mapping[str, Any]
    bootstrap_report: McpBootstrapReport | None
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_descriptors", tuple(self.command_descriptors))
        object.__setattr__(self, "constraints", MappingProxyType(dict(self.constraints)))

    @property
    def command_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.command_descriptors)

    def safe_dict(self) -> dict[str, JsonValue]:
        tool_safe = getattr(self.tool_bundle, "safe_dict", None)
        return {
            "schema": "zyra.mcp-main-path-projection.v1",
            "projection_id": self.projection_id,
            "identity": self.identity.safe_dict(),
            "tool_bundle": tool_safe() if callable(tool_safe) else {},
            "resource_bundle": self.resource_bundle.safe_dict(),
            "command_descriptors": [item.safe_dict() for item in self.command_descriptors],
            "command_names": list(self.command_names),
            "registry_tool_names": [item.name for item in self.context.registry.list()],
            "dynamic_handler_names": sorted(self.context.dynamic_handlers),
            "constraints_digest": stable_digest(self.constraints),
            "bootstrap": self.bootstrap_report.safe_dict() if self.bootstrap_report else None,
            "created_at": self.created_at,
            "owns_store": False,
        }


@dataclass(frozen=True, slots=True)
class McpMainPathOpenResult:
    projection: McpMainPathProjection
    persisted_events: tuple[EventRecord, ...]
    session_checkpoint: Mapping[str, Any] | None

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "projection": self.projection.safe_dict(),
            "persisted_event_count": len(self.persisted_events),
            "persisted_event_ids": [item.event_id for item in self.persisted_events],
            "session_checkpoint": redact_value(self.session_checkpoint),
        }


class McpMainPathRuntime:
    def __init__(
        self,
        runtime: McpMainPathRuntimePort,
        resource_projection: McpResourceProjectionRuntime,
        bootstrap: McpBootstrapRuntime,
        *,
        session_port: McpMainPathSessionPort | None = None,
        event_port: McpMainPathEventPort | None = None,
        control_port: McpMainPathControlPort | None = None,
        disabled: bool = False,
    ) -> None:
        self.runtime = runtime
        self.resource_projection = resource_projection
        self.bootstrap = bootstrap
        self.session_port = session_port
        self.event_port = event_port
        self.control_port = control_port
        self.disabled = disabled
        self._lock = threading.RLock()

    def open(
        self,
        base_context: ToolExecutionContext,
        *,
        identity: McpMainPathIdentity,
        constraints: Mapping[str, Any] | None = None,
        include_servers: Sequence[str] | None = None,
        bootstrap_connections: bool = True,
        bootstrap_mode: McpBootstrapMode | str = McpBootstrapMode.SESSION_RESUME,
        checkpoint_session: bool = True,
    ) -> McpMainPathOpenResult:
        self._assert_enabled()
        with self._lock:
            bootstrap_report = None
            if bootstrap_connections:
                bootstrap_report = self.bootstrap.bootstrap(
                    mode=bootstrap_mode,
                    run_id=identity.run_id,
                    task_id=identity.task_id,
                    node_id=identity.node_id,
                    session_id=identity.session_id,
                    worker_request_id=identity.worker_request_id,
                    include_servers=include_servers,
                )
            tool_projection = self.runtime.worker_projection(
                base_context,
                run_id=identity.run_id,
                task_id=identity.task_id,
                node_id=identity.node_id,
                include_servers=include_servers,
                include_resource_surfaces=False,
                session_id=identity.session_id,
                worker_request_id=identity.worker_request_id,
            )
            tool_context = getattr(tool_projection, "context", None)
            if not isinstance(tool_context, ToolExecutionContext):
                raise McpMainPathError("runtime worker projection returned invalid context")
            reserved = tuple(item.name for item in tool_context.registry.list())
            resource_bundle = self.resource_projection.build_bundle(
                run_id=identity.run_id,
                task_id=identity.task_id,
                node_id=identity.node_id,
                session_id=identity.session_id,
                worker_request_id=identity.worker_request_id,
                reserved_names=reserved,
                include_servers=include_servers,
            )
            merged_context = replace(
                tool_context,
                registry=tool_context.registry.merged(list(resource_bundle.tool_specs)),
                dynamic_handlers={
                    **dict(tool_context.dynamic_handlers),
                    **dict(resource_bundle.handlers),
                },
            )
            projected_constraints = self.runtime.prepare_worker_constraints(
                dict(constraints or {}),
                session_id=identity.session_id,
            )
            commands = (*operational_command_descriptors(), *resource_bundle.prompt_commands)
            projection = McpMainPathProjection(
                new_id("mcpmainpath"),
                identity,
                merged_context,
                getattr(tool_projection, "bundle", None),
                resource_bundle,
                commands,
                projected_constraints,
                bootstrap_report,
            )
            events = self.flush_events(identity)
            checkpoint = self.checkpoint(projection) if checkpoint_session else None
            self._emit_open(projection)
            return McpMainPathOpenResult(projection, events, checkpoint)

    def open_for_worker(
        self,
        base_context: ToolExecutionContext,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
        constraints: Mapping[str, Any] | None = None,
        include_servers: Sequence[str] | None = None,
        bootstrap_connections: bool = False,
        checkpoint_session: bool = False,
    ) -> McpMainPathOpenResult:
        """Open the MCP view used by one CodeWorker request.

        Worker construction normally happens after API/session bootstrap and
        checkpoints through the worker's existing runtime-state owner.  The
        convenience defaults therefore avoid a second connection bootstrap or
        session write while preserving explicit opt-in for standalone callers.
        """

        identity = McpMainPathIdentity(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
        )
        return self.open(
            base_context,
            identity=identity,
            constraints=constraints,
            include_servers=include_servers,
            bootstrap_connections=bootstrap_connections,
            checkpoint_session=checkpoint_session,
        )

    def refresh(
        self,
        current: McpMainPathProjection,
        base_context: ToolExecutionContext,
        *,
        constraints: Mapping[str, Any] | None = None,
        include_servers: Sequence[str] | None = None,
    ) -> McpMainPathOpenResult:
        self._assert_enabled()
        return self.open(
            base_context,
            identity=current.identity,
            constraints=constraints or current.constraints,
            include_servers=include_servers,
            bootstrap_connections=False,
            checkpoint_session=True,
        )

    def require_fresh(self, projection: McpMainPathProjection) -> None:
        self._assert_enabled()
        try:
            self.resource_projection.validate_bundle(projection.resource_bundle)
        except McpResourceProjectionStale as error:
            raise McpMainPathStale(str(error)) from error

    def execute_command(
        self,
        projection: McpMainPathProjection,
        request: McpMainPathCommandRequest,
    ) -> McpMainPathCommandResult:
        self._assert_enabled()
        if request.identity != projection.identity:
            raise McpMainPathControlRejected("command identity does not own projection")
        descriptor = next(
            (item for item in projection.command_descriptors if item.name == request.name),
            None,
        )
        if descriptor is None:
            return McpMainPathCommandResult(
                request.request_id,
                request.name,
                False,
                {},
                "mcp_command_not_found",
                "MCP command is absent from current generation",
            )
        if isinstance(descriptor, McpPromptCommandDescriptor):
            try:
                invocation = self.resource_projection.invoke_prompt(
                    descriptor,
                    request.arguments,
                    run_id=request.identity.run_id,
                    task_id=request.identity.task_id,
                    node_id=request.identity.node_id,
                    session_id=request.identity.session_id,
                    worker_request_id=request.identity.worker_request_id,
                )
                result = McpMainPathCommandResult(
                    request.request_id,
                    request.name,
                    True,
                    invocation.safe_dict(),
                )
            except Exception as error:  # noqa: BLE001 - command boundary result.
                result = McpMainPathCommandResult(
                    request.request_id,
                    request.name,
                    False,
                    {},
                    type(error).__name__,
                    str(error)[:1000],
                )
        else:
            result = self._execute_operational(descriptor, request)
        self.flush_events(request.identity)
        self._emit_command(request, result)
        return result

    def _execute_operational(
        self,
        descriptor: McpControlCommandDescriptor,
        request: McpMainPathCommandRequest,
    ) -> McpMainPathCommandResult:
        if descriptor.kind is McpMainPathCommandKind.STATUS:
            payload = to_json_value(self.runtime.diagnostics())
            return McpMainPathCommandResult(
                request.request_id,
                request.name,
                True,
                payload if isinstance(payload, Mapping) else {"value": payload},
            )
        if self.control_port is None:
            return McpMainPathCommandResult(
                request.request_id,
                request.name,
                False,
                {},
                "mcp_control_port_unavailable",
                "No 03A/03D control port is installed",
            )
        try:
            payload = self.control_port.execute_mcp_control(
                str(descriptor.kind),
                request.arguments,
                run_id=request.identity.run_id,
                task_id=request.identity.task_id,
                node_id=request.identity.node_id,
                session_id=request.identity.session_id,
                worker_request_id=request.identity.worker_request_id,
                actor_id=request.actor_id,
                idempotency_key=request.idempotency_key,
            )
        except Exception as error:  # noqa: BLE001 - permission/control error is explicit.
            return McpMainPathCommandResult(
                request.request_id,
                request.name,
                False,
                {},
                type(error).__name__,
                str(error)[:1000],
            )
        safe = to_json_value(payload)
        return McpMainPathCommandResult(
            request.request_id,
            request.name,
            True,
            safe if isinstance(safe, Mapping) else {"value": safe},
        )

    def checkpoint(self, projection: McpMainPathProjection) -> Mapping[str, Any] | None:
        if self.session_port is None:
            return None
        value = {
            "schema": "zyra.mcp-main-path-checkpoint.v1",
            "runtime": self.runtime.session_snapshot(projection.identity.session_id),
            "projection": projection.safe_dict(),
            "credentials_included": False,
            "live_transports_included": False,
        }
        receipt = self.session_port.write_runtime_state(
            projection.identity.session_id,
            "mcp_runtime",
            value,
        )
        safe = to_json_value(receipt)
        return safe if isinstance(safe, Mapping) else {"receipt": safe}

    def flush_events(self, identity: McpMainPathIdentity) -> tuple[EventRecord, ...]:
        events = tuple(self.runtime.drain_events(run_id=identity.run_id, task_id=identity.task_id))
        if self.event_port is not None:
            for event in events:
                self.event_port.append(event)
        return events

    def _emit_open(self, projection: McpMainPathProjection) -> None:
        if self.event_port is None:
            return
        self.event_port.append(
            EventRecord(
                run_id=projection.identity.run_id,
                task_id=projection.identity.task_id,
                node_id=projection.identity.node_id,
                event_type=getattr(EventType, "MCP_CAPABILITIES_CHANGED", EventType.SYSTEM_NOTICE),
                payload={
                    "mcp_runtime": {
                        "schema": "zyra.mcp-main-path-opened.v1",
                        "runtime_id": "McpMainPathRuntime",
                        "projection": projection.safe_dict(),
                        "state_mutation": "mcp_main_path_opened",
                    }
                },
            )
        )

    def _emit_command(
        self,
        request: McpMainPathCommandRequest,
        result: McpMainPathCommandResult,
    ) -> None:
        if self.event_port is None:
            return
        self.event_port.append(
            EventRecord(
                run_id=request.identity.run_id,
                task_id=request.identity.task_id,
                node_id=request.identity.node_id,
                event_type=EventType.CONTROL_COMMAND,
                payload={
                    "mcp_runtime": {
                        "schema": "zyra.mcp-main-path-command.v1",
                        "runtime_id": "McpMainPathRuntime",
                        "request": request.safe_dict(),
                        "result": result.safe_dict(),
                        "state_mutation": "mcp_control_dispatched",
                    }
                },
            )
        )

    def _assert_enabled(self) -> None:
        if self.disabled:
            raise McpMainPathDisabled("McpMainPathRuntime is disabled")


def operational_command_descriptors() -> tuple[McpControlCommandDescriptor, ...]:
    object_schema: dict[str, JsonValue] = {
        "type": "object",
        "additionalProperties": True,
    }
    definitions = (
        ("mcp.status", McpMainPathCommandKind.STATUS, "Inspect live MCP runtime state.", False, False, True),
        ("mcp.connect", McpMainPathCommandKind.CONNECT, "Connect one trusted configured MCP server.", True, True, False),
        ("mcp.disconnect", McpMainPathCommandKind.DISCONNECT, "Disconnect one MCP server.", True, True, False),
        ("mcp.reconnect", McpMainPathCommandKind.RECONNECT, "Reconnect one MCP server.", True, True, False),
        ("mcp.refresh", McpMainPathCommandKind.REFRESH, "Refresh MCP capabilities.", True, True, False),
        ("mcp.enable", McpMainPathCommandKind.ENABLE, "Enable an existing MCP server.", True, True, False),
        ("mcp.disable", McpMainPathCommandKind.DISABLE, "Disable an existing MCP server.", True, True, False),
        ("mcp.approve", McpMainPathCommandKind.APPROVE, "Approve a discovered project MCP server.", True, True, False),
        ("mcp.reject", McpMainPathCommandKind.REJECT, "Reject a discovered project MCP server.", True, True, False),
        ("mcp.auth.refresh", McpMainPathCommandKind.AUTH_REFRESH, "Refresh MCP authentication.", True, True, False),
        ("mcp.auth.revoke", McpMainPathCommandKind.AUTH_REVOKE, "Revoke MCP authentication.", True, True, False),
        ("mcp.elicitation.resolve", McpMainPathCommandKind.ELICITATION_RESOLVE, "Resolve or cancel a pending MCP elicitation.", True, True, False),
    )
    return tuple(
        McpControlCommandDescriptor(name, kind, description, object_schema, mutates, permission, remote_safe)
        for name, kind, description, mutates, permission, remote_safe in definitions
    )


class McpMainPathReadinessStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class McpMainPathReadinessCheck:
    name: str
    satisfied: bool
    blocking: bool
    reason: str
    evidence: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("readiness check name is required")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "satisfied": self.satisfied,
            "blocking": self.blocking,
            "reason": self.reason,
            "evidence": redact_value(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class McpMainPathReadinessReport:
    projection_id: str
    checks: tuple[McpMainPathReadinessCheck, ...]
    assessed_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks))

    @property
    def status(self) -> McpMainPathReadinessStatus:
        if any(not item.satisfied and item.blocking for item in self.checks):
            return McpMainPathReadinessStatus.BLOCKED
        if any(not item.satisfied for item in self.checks):
            return McpMainPathReadinessStatus.DEGRADED
        return McpMainPathReadinessStatus.READY

    @property
    def ready(self) -> bool:
        return self.status is McpMainPathReadinessStatus.READY

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-main-path-readiness.v1",
            "projection_id": self.projection_id,
            "status": str(self.status),
            "ready": self.ready,
            "checks": [item.safe_dict() for item in self.checks],
            "assessed_at": self.assessed_at,
        }


class McpMainPathCommandIndex:
    """Immutable, collision-strict view over one projection's commands."""

    def __init__(
        self,
        descriptors: Sequence[
            McpControlCommandDescriptor | McpPromptCommandDescriptor
        ],
    ) -> None:
        values: dict[
            str,
            McpControlCommandDescriptor | McpPromptCommandDescriptor,
        ] = {}
        aliases: dict[str, str] = {}
        for descriptor in descriptors:
            canonical = _canonical_command_name(descriptor.name)
            if canonical in values:
                raise McpMainPathError(
                    f"MCP command collision: {descriptor.name}"
                )
            values[canonical] = descriptor
            for alias in _command_aliases(descriptor):
                normalized = _canonical_command_name(alias)
                owner = aliases.get(normalized)
                if owner is not None and owner != canonical:
                    raise McpMainPathError(
                        f"MCP command alias collision: {alias}"
                    )
                aliases[normalized] = canonical
        self._values = MappingProxyType(values)
        self._aliases = MappingProxyType(aliases)

    def get(
        self,
        name: str,
    ) -> McpControlCommandDescriptor | McpPromptCommandDescriptor | None:
        normalized = _canonical_command_name(name)
        canonical = self._aliases.get(normalized, normalized)
        return self._values.get(canonical)

    def require(
        self,
        name: str,
    ) -> McpControlCommandDescriptor | McpPromptCommandDescriptor:
        descriptor = self.get(name)
        if descriptor is None:
            raise McpMainPathControlRejected(
                f"MCP command is not present in current projection: {name}"
            )
        return descriptor

    def list(
        self,
    ) -> tuple[McpControlCommandDescriptor | McpPromptCommandDescriptor, ...]:
        return tuple(self._values[key] for key in sorted(self._values))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-command-index.v1",
            "commands": [item.safe_dict() for item in self.list()],
            "aliases": dict(self._aliases),
            "command_count": len(self._values),
            "collision_policy": "fail_closed",
        }


@dataclass(frozen=True, slots=True)
class McpMainPathRefreshSignal:
    server_id: str
    previous_connection_generation: int
    previous_capability_generation: int
    current_connection_generation: int
    current_capability_generation: int
    reason: str

    @property
    def changed(self) -> bool:
        return (
            self.previous_connection_generation,
            self.previous_capability_generation,
        ) != (
            self.current_connection_generation,
            self.current_capability_generation,
        )

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "previous_connection_generation": self.previous_connection_generation,
            "previous_capability_generation": self.previous_capability_generation,
            "current_connection_generation": self.current_connection_generation,
            "current_capability_generation": self.current_capability_generation,
            "reason": self.reason,
            "changed": self.changed,
        }


def assess_main_path_readiness(
    projection: McpMainPathProjection,
    *,
    resource_projection: McpResourceProjectionRuntime,
    control_port_installed: bool,
    session_port_installed: bool,
    event_port_installed: bool,
) -> McpMainPathReadinessReport:
    checks: list[McpMainPathReadinessCheck] = []
    try:
        resource_projection.validate_bundle(projection.resource_bundle)
        fresh = True
        freshness_reason = "all resource/prompt leases match live generations"
    except Exception as error:  # noqa: BLE001 - readiness projection only.
        fresh = False
        freshness_reason = f"{type(error).__name__}: {str(error)[:500]}"
    checks.append(
        McpMainPathReadinessCheck(
            "generation_freshness",
            fresh,
            True,
            freshness_reason,
            {
                "lease_count": len(projection.resource_bundle.leases),
                "bundle_id": projection.resource_bundle.bundle_id,
            },
        )
    )
    registry_names = {item.name for item in projection.context.registry.list()}
    handler_names = set(projection.context.dynamic_handlers)
    declared_dynamic = {
        item.name
        for item in projection.context.registry.list()
        if item.execution_provenance is not None
    }
    handlers_complete = declared_dynamic <= handler_names
    checks.append(
        McpMainPathReadinessCheck(
            "dynamic_handler_binding",
            handlers_complete,
            True,
            (
                "every dynamic ToolSpec has a handler"
                if handlers_complete
                else "dynamic ToolSpec is missing an executable handler"
            ),
            {
                "declared_dynamic": sorted(declared_dynamic),
                "handler_names": sorted(handler_names),
                "missing": sorted(declared_dynamic - handler_names),
            },
        )
    )
    resource_tools = set(projection.resource_bundle.tool_names)
    resource_tools_visible = resource_tools <= registry_names
    checks.append(
        McpMainPathReadinessCheck(
            "resource_tools_visible",
            resource_tools_visible,
            True,
            (
                "resource list/read tools are model-visible"
                if resource_tools_visible
                else "resource projection did not reach the 02C registry"
            ),
            {
                "resource_tools": sorted(resource_tools),
                "missing": sorted(resource_tools - registry_names),
            },
        )
    )
    command_index_ok = True
    command_reason = "command descriptors are collision-free"
    try:
        McpMainPathCommandIndex(projection.command_descriptors)
    except Exception as error:
        command_index_ok = False
        command_reason = f"{type(error).__name__}: {str(error)[:500]}"
    checks.append(
        McpMainPathReadinessCheck(
            "command_index",
            command_index_ok,
            True,
            command_reason,
            {"command_names": list(projection.command_names)},
        )
    )
    mutating_commands = [
        item.name
        for item in projection.command_descriptors
        if isinstance(item, McpControlCommandDescriptor)
        and item.mutates_runtime
    ]
    checks.append(
        McpMainPathReadinessCheck(
            "control_port",
            control_port_installed or not mutating_commands,
            True,
            (
                "03A/03D control port is installed"
                if control_port_installed
                else "mutating commands have no permission-owning control port"
            ),
            {"mutating_commands": mutating_commands},
        )
    )
    checks.append(
        McpMainPathReadinessCheck(
            "session_checkpoint_port",
            session_port_installed,
            False,
            (
                "session checkpoint port is installed"
                if session_port_installed
                else "projection can run but cannot checkpoint MCP handoff"
            ),
        )
    )
    checks.append(
        McpMainPathReadinessCheck(
            "canonical_event_port",
            event_port_installed,
            True,
            (
                "canonical events are persisted immediately"
                if event_port_installed
                else "events would remain in the process-local handoff buffer"
            ),
        )
    )
    runtime_owned_constraints = bool(
        isinstance(projection.constraints.get("mcp_runtime_projection"), Mapping)
        and projection.constraints["mcp_runtime_projection"].get("runtime_owned") is True
    )
    checks.append(
        McpMainPathReadinessCheck(
            "runtime_owned_restore_constraints",
            runtime_owned_constraints,
            True,
            (
                "02D restore consumes runtime-owned MCP constraints"
                if runtime_owned_constraints
                else "caller-controlled MCP restore constraints were not replaced"
            ),
        )
    )
    return McpMainPathReadinessReport(projection.projection_id, tuple(checks))


@dataclass(slots=True)
class CallableMcpEventPort:
    callback: Any

    def append(self, event: EventRecord) -> Any:
        if not callable(self.callback):
            raise McpMainPathError("event callback is not callable")
        return self.callback(event)


@dataclass(slots=True)
class CallableMcpSessionPort:
    callback: Any

    def write_runtime_state(
        self,
        session_id: str,
        namespace: str,
        value: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
        cause_event_id: str = "",
    ) -> Any:
        if not callable(self.callback):
            raise McpMainPathError("session callback is not callable")
        return self.callback(
            session_id,
            namespace,
            value,
            expected_revision=expected_revision,
            cause_event_id=cause_event_id,
        )


@dataclass(slots=True)
class CallableMcpControlPort:
    callback: Any

    def execute_mcp_control(
        self,
        command: str,
        arguments: Mapping[str, Any],
        **context: Any,
    ) -> Mapping[str, Any]:
        if not callable(self.callback):
            raise McpMainPathError("control callback is not callable")
        value = self.callback(command, arguments, **context)
        if not isinstance(value, Mapping):
            raise McpMainPathError("control callback must return a mapping")
        return value


def _canonical_command_name(value: str) -> str:
    selected = str(value or "").strip().casefold().lstrip("/")
    return selected.replace("/", ".").replace(" ", "")


def _command_aliases(
    descriptor: McpControlCommandDescriptor | McpPromptCommandDescriptor,
) -> tuple[str, ...]:
    aliases = [descriptor.name]
    if descriptor.name.startswith("mcp."):
        aliases.append("/" + descriptor.name)
        aliases.append("/mcp " + descriptor.name[4:].replace(".", " "))
    elif descriptor.name.startswith("mcp__"):
        aliases.append("/" + descriptor.name)
    return tuple(dict.fromkeys(aliases))


__all__ = [
    "CallableMcpControlPort",
    "CallableMcpEventPort",
    "CallableMcpSessionPort",
    "McpControlCommandDescriptor",
    "McpMainPathCommandIndex",
    "McpMainPathCommandKind",
    "MCP_MAIN_PATH_BOUNDARY",
    "McpMainPathCommandRequest",
    "McpMainPathCommandResult",
    "McpMainPathControlPort",
    "McpMainPathControlRejected",
    "McpMainPathDisabled",
    "McpMainPathError",
    "McpMainPathEventPort",
    "McpMainPathIdentity",
    "McpMainPathOpenResult",
    "McpMainPathProjection",
    "McpMainPathReadinessCheck",
    "McpMainPathReadinessReport",
    "McpMainPathReadinessStatus",
    "McpMainPathRefreshSignal",
    "McpMainPathRuntime",
    "McpMainPathRuntimePort",
    "McpMainPathSessionPort",
    "McpMainPathStale",
    "assess_main_path_readiness",
    "operational_command_descriptors",
]
