from __future__ import annotations

"""Connection and lifecycle owner for Zyra's MCP client runtime.

The transport classes deliberately own only bytes, JSON-RPC correlation and
carrier lifecycle.  This module owns the MCP state machine above them: auth
gating, initialize/initialized, capability replacement, notification refresh,
long-running tool tasks, progress, instruction custody and deterministic
disconnect cleanup.
"""

import contextlib
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, new_id, now_iso

from .auth import AuthNeedsInteraction, McpAuthRuntime
from .capabilities import (
    McpCapabilityCatalog,
    McpCapabilityDiff,
    McpCapabilitySnapshot,
    McpResourcePromptRuntime,
)
from .elicitation import McpElicitationQueue
from .instructions import McpInstructionsRuntime
from .models import (
    JsonValue,
    McpConnectionSnapshot,
    McpConnectionState,
    McpElicitationField,
    McpElicitationMode,
    McpElicitationRequest,
    McpServerConfig,
    McpTaskOptions,
    McpToolTaskSupport,
    redact_value,
    stable_digest,
    to_json_value,
)
from .tasks import McpTaskExecutionReceipt, McpTaskLifecycleRuntime
from .transport import McpTransport, build_transport


MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_MCP_PROTOCOL_VERSIONS = frozenset(
    {
        "2024-11-05",
        "2025-03-26",
        MCP_PROTOCOL_VERSION,
    }
)
MCP_CLIENT_INFO: Mapping[str, str] = {
    "name": "zyra-mcp-client-runtime",
    "version": "1.0.0",
}


class McpConnectionError(RuntimeError):
    pass


class McpConnectionRuntimeDisabled(McpConnectionError):
    pass


class McpServerNotFound(McpConnectionError):
    pass


class McpServerNotConnected(McpConnectionError):
    pass


class McpInitializeError(McpConnectionError):
    pass


class McpNeedsAuthentication(McpConnectionError):
    pass


class McpConnectionStatePort(Protocol):
    def read_state(self) -> Mapping[str, Any]: ...

    def set_connection(self, server_id: str, snapshot: Any, **kwargs: Any) -> Any: ...

    def update_section(self, section: str, values: Mapping[str, Any], **kwargs: Any) -> Any: ...


class McpSamplingPort(Protocol):
    def sample(
        self,
        request: Mapping[str, Any],
        *,
        session_id: str = "",
    ) -> Any: ...

    def advertised(self, server_id: str) -> bool: ...


TransportFactory = Callable[[McpServerConfig], McpTransport]
AuthFactory = Callable[[McpServerConfig], McpAuthRuntime | None]
InProcessProgram = Callable[[Any], Any]
RuntimeEventSink = Callable[[EventRecord], None]


@dataclass(frozen=True, slots=True)
class McpConnectionIdentity:
    server_id: str
    config_fingerprint: str
    connection_generation: int
    capability_generation: int
    protocol_version: str
    session_id: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "config_fingerprint": self.config_fingerprint,
            "connection_generation": self.connection_generation,
            "capability_generation": self.capability_generation,
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
        }


@dataclass(frozen=True, slots=True)
class McpConnectReceipt:
    snapshot: McpConnectionSnapshot
    catalog: McpCapabilitySnapshot | None
    diff: McpCapabilityDiff | None
    initialize_result: Mapping[str, JsonValue]
    already_connected: bool = False
    auth_cache_hit: bool = False
    events: tuple[EventRecord, ...] = ()

    @property
    def connected(self) -> bool:
        return self.snapshot.state is McpConnectionState.CONNECTED

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "catalog": self.catalog.safe_dict() if self.catalog else None,
            "diff": self.diff.to_dict() if self.diff else None,
            "initialize_result": _safe_initialize_result(self.initialize_result),
            "already_connected": self.already_connected,
            "auth_cache_hit": self.auth_cache_hit,
            "event_ids": [event.event_id for event in self.events],
            "connected": self.connected,
        }


@dataclass(frozen=True, slots=True)
class McpRefreshReceipt:
    server_id: str
    snapshot: McpCapabilitySnapshot
    diff: McpCapabilityDiff
    reason: str
    event: EventRecord | None = None

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "snapshot": self.snapshot.safe_dict(),
            "diff": self.diff.to_dict(),
            "reason": self.reason,
            "event_id": self.event.event_id if self.event else "",
        }


@dataclass(frozen=True, slots=True)
class McpCallReceipt:
    server_id: str
    tool_name: str
    result: Mapping[str, JsonValue]
    connection_generation: int
    capability_generation: int
    progress_count: int
    progress_digest: str
    task_receipt: McpTaskExecutionReceipt

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "result": redact_value(self.result),
            "connection_generation": self.connection_generation,
            "capability_generation": self.capability_generation,
            "progress_count": self.progress_count,
            "progress_digest": self.progress_digest,
            "task": self.task_receipt.safe_dict(),
        }


@dataclass(slots=True)
class _ConnectionHandle:
    config: McpServerConfig | None
    snapshot: McpConnectionSnapshot
    transport: McpTransport | None = None
    initialize_result: Mapping[str, JsonValue] = field(default_factory=dict)
    auth_runtime: McpAuthRuntime | None = None
    removers: list[Callable[[], None]] = field(default_factory=list)
    last_refresh_error: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)


class _ProgressRequestPort:
    """Resolve the current carrier for every idempotent task operation.

    Long-running MCP tasks may reconnect after ``tasks/get`` or
    ``tasks/result`` fails.  Holding the carrier that performed tools/call
    would keep polling a closed session.  This port follows the connection
    owner and also reattaches the progress listener when the carrier changes.
    """

    def __init__(
        self,
        transport_resolver: Callable[[], McpTransport],
        token: str,
        progress_handler: Callable[[Any], None] | None = None,
    ) -> None:
        self._transport_resolver = transport_resolver
        self.token = token
        self._progress_handler = progress_handler
        self._bound_transport: McpTransport | None = None
        self._remove_progress: Callable[[], None] | None = None
        self._lock = threading.RLock()

    def _transport(self) -> McpTransport:
        transport = self._transport_resolver()
        with self._lock:
            if transport is not self._bound_transport:
                if self._remove_progress is not None:
                    with contextlib.suppress(Exception):
                        self._remove_progress()
                self._bound_transport = transport
                self._remove_progress = (
                    transport.add_notification_handler("notifications/progress", self._progress_handler)
                    if self._progress_handler is not None
                    else None
                )
        return transport

    def request(self, method: str, params: Any = None, *, timeout_seconds: float | None = None) -> JsonValue:
        selected = dict(params or {}) if isinstance(params, Mapping) else params
        if method == "tools/call" and isinstance(selected, dict):
            meta = dict(selected.get("_meta") or {}) if isinstance(selected.get("_meta"), Mapping) else {}
            meta["progressToken"] = self.token
            selected["_meta"] = meta
        return self._transport().request(method, selected, timeout_seconds=timeout_seconds)

    def close(self) -> None:
        with self._lock:
            if self._remove_progress is not None:
                with contextlib.suppress(Exception):
                    self._remove_progress()
            self._remove_progress = None
            self._bound_transport = None


class McpConnectionRuntime:
    """Authoritative runtime owner for live MCP clients.

    No transport, auth token or callback is serialized.  Durable state contains
    only redacted config provenance, connection/capability generations, task
    handles, elicitation metadata and artifact references.  A restored process
    must reconnect before any projected tool becomes reachable.
    """

    def __init__(
        self,
        *,
        catalog: McpCapabilityCatalog,
        resource_runtime: McpResourcePromptRuntime,
        state_store: McpConnectionStatePort | None = None,
        instructions_runtime: McpInstructionsRuntime | None = None,
        elicitation_queue: McpElicitationQueue | None = None,
        sampling_runtime: McpSamplingPort | None = None,
        task_runtime: McpTaskLifecycleRuntime | None = None,
        transport_factory: TransportFactory | None = None,
        auth_factory: AuthFactory | None = None,
        event_sink: RuntimeEventSink | None = None,
        client_capabilities: Mapping[str, Any] | None = None,
        elicitation_timeout_seconds: float = 120.0,
        disabled: bool = False,
    ) -> None:
        self.catalog = catalog
        self.resource_runtime = resource_runtime
        self.state_store = state_store
        self.instructions_runtime = instructions_runtime
        self.elicitation_queue = elicitation_queue
        self.sampling_runtime = sampling_runtime
        self.transport_factory = transport_factory
        self.auth_factory = auth_factory
        self.event_sink = event_sink
        if elicitation_timeout_seconds <= 0:
            raise ValueError("elicitation_timeout_seconds must be positive")
        self.client_capabilities = _sanitize_client_capabilities(
            client_capabilities
            if client_capabilities is not None
            else _default_client_capabilities(
                elicitation_enabled=elicitation_queue is not None,
                tasks_enabled=True,
            )
        )
        self.elicitation_timeout_seconds = float(elicitation_timeout_seconds)
        self.disabled = disabled
        self._handles: dict[str, _ConnectionHandle] = {}
        self._in_process_programs: dict[str, InProcessProgram] = {}
        self._lock = threading.RLock()
        self.task_runtime = task_runtime or McpTaskLifecycleRuntime(
            options=McpTaskOptions(),
            state_store=state_store,
            reconnect=self._task_reconnect,
            event_sink=event_sink,
        )
        self._hydrate_connections()

    def register_in_process(self, server_id: str, program: InProcessProgram, *, replace_existing: bool = False) -> None:
        if not server_id or not callable(program):
            raise ValueError("server_id and callable in-process program are required")
        with self._lock:
            if server_id in self._in_process_programs and not replace_existing:
                raise McpConnectionError(f"in-process MCP server {server_id!r} is already registered")
            self._in_process_programs[server_id] = program

    def unregister_in_process(self, server_id: str) -> bool:
        with self._lock:
            return self._in_process_programs.pop(server_id, None) is not None

    def connect(
        self,
        config: McpServerConfig,
        *,
        run_id: str = "",
        task_id: str = "",
        node_id: str | None = None,
        session_id: str = "",
        worker_request_id: str = "",
        cause_event_id: str = "",
        force: bool = False,
    ) -> McpConnectReceipt:
        self._assert_enabled()
        with self._lock:
            handle = self._handles.get(config.server_id)
            if handle is None:
                handle = _ConnectionHandle(
                    config=config,
                    snapshot=McpConnectionSnapshot(
                        server_id=config.server_id,
                        config_fingerprint=config.fingerprint,
                    ),
                )
                self._handles[config.server_id] = handle

        with handle.lock:
            if config.disabled or not config.connectable:
                self._close_transport(handle)
                if handle.snapshot.state in {
                    McpConnectionState.PENDING,
                    McpConnectionState.CONNECTED,
                    McpConnectionState.NEEDS_AUTH,
                    McpConnectionState.FAILED,
                    McpConnectionState.CLOSED,
                }:
                    snapshot = self._transition(handle, McpConnectionState.DISABLED)
                else:
                    snapshot = replace(
                        handle.snapshot,
                        state=McpConnectionState.DISABLED,
                        revision=handle.snapshot.revision + 1,
                        error_code="",
                        error_message="",
                        last_transition_at=now_iso(),
                    )
                    handle.snapshot = snapshot
                    self._persist_snapshot(snapshot)
                self._withdraw_catalog(config.server_id, reason="disabled_config")
                return McpConnectReceipt(snapshot, None, None, {})

            if (
                not force
                and handle.snapshot.state is McpConnectionState.CONNECTED
                and handle.config is not None
                and handle.config.fingerprint == config.fingerprint
                and handle.transport is not None
            ):
                return McpConnectReceipt(
                    handle.snapshot,
                    self.catalog.get(config.server_id),
                    None,
                    handle.initialize_result,
                    already_connected=True,
                )

            self._withdraw_catalog(config.server_id, reason="connection_start")
            if handle.transport is not None:
                self._close_transport(handle)
            handle.config = config
            handle.initialize_result = {}
            handle.last_refresh_error = ""
            if handle.snapshot.state is McpConnectionState.DISABLED:
                handle.snapshot = handle.snapshot.transition(McpConnectionState.PENDING)
                self._persist_snapshot(handle.snapshot)
            if handle.snapshot.state in {McpConnectionState.CONNECTED, McpConnectionState.RECONNECTING}:
                handle.snapshot = handle.snapshot.transition(McpConnectionState.CLOSING)
                self._persist_snapshot(handle.snapshot)
                handle.snapshot = handle.snapshot.transition(McpConnectionState.CLOSED)
                self._persist_snapshot(handle.snapshot)
            snapshot = self._transition(handle, McpConnectionState.CONNECTING, config_fingerprint=config.fingerprint)

            emitted: list[EventRecord] = []
            start_event = self._connection_event(
                snapshot,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                worker_request_id=worker_request_id,
                cause_event_id=cause_event_id,
            )
            if start_event:
                emitted.append(start_event)
                self._emit(start_event)

            auth_cache_hit = False
            try:
                auth_runtime = self._auth_runtime(config)
                handle.auth_runtime = auth_runtime
                transport_config = config
                if auth_runtime is not None:
                    outcome = auth_runtime.ensure_authorized()
                    auth_cache_hit = outcome.cache_hit
                    if not outcome.authorized:
                        raise McpNeedsAuthentication(
                            outcome.snapshot.error_code or "authentication required"
                        )
                    if str(outcome.snapshot.status) == "authenticated":
                        headers = dict(config.headers)
                        headers["Authorization"] = auth_runtime.authorization_header()
                        transport_config = replace(config, headers=headers)

                transport = self._build_transport(transport_config)
                handle.transport = transport
                self._install_handlers(
                    handle,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    session_id=session_id,
                    worker_request_id=worker_request_id,
                )
                transport.open()
                raw_initialize = transport.request(
                    "initialize",
                    {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": self._client_capabilities(config.server_id),
                        "clientInfo": dict(MCP_CLIENT_INFO),
                    },
                    timeout_seconds=config.connect_timeout_seconds,
                )
                if not isinstance(raw_initialize, Mapping):
                    raise McpInitializeError("initialize returned a non-object result")
                initialize_result = to_json_value(raw_initialize)
                if not isinstance(initialize_result, dict):
                    raise McpInitializeError("initialize result is not JSON object")
                protocol_version = str(initialize_result.get("protocolVersion") or "")
                if not protocol_version:
                    raise McpInitializeError("initialize result omitted protocolVersion")
                if protocol_version not in SUPPORTED_MCP_PROTOCOL_VERSIONS:
                    raise McpInitializeError(
                        f"initialize selected unsupported protocolVersion {protocol_version!r}"
                    )
                set_protocol_version = getattr(transport, "set_protocol_version", None)
                if callable(set_protocol_version):
                    set_protocol_version(protocol_version)
                transport.notify("notifications/initialized", {})

                generation = snapshot.generation
                current_catalog = self.catalog.get(config.server_id)
                next_capability_generation = (current_catalog.generation + 1) if current_catalog else 1
                catalog_snapshot, diff = self.resource_runtime.discover(
                    server_id=config.server_id,
                    request=transport,
                    initialize_result=initialize_result,
                    connection_generation=generation,
                    generation=next_capability_generation,
                    current=None,
                )
                handle.initialize_result = initialize_result
                transport_snapshot = transport.snapshot()
                snapshot = self._transition(
                    handle,
                    McpConnectionState.CONNECTED,
                    protocol_version=protocol_version,
                    session_id=str(transport_snapshot.metadata.get("session_id") or ""),
                    metadata={
                        "transport": str(config.transport),
                        "capability_generation": str(catalog_snapshot.generation),
                        "catalog_digest": catalog_snapshot.snapshot_digest,
                    },
                )
                self._persist_catalog(catalog_snapshot)
                self._update_instructions(
                    catalog_snapshot,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    session_id=session_id,
                    source_event_id=cause_event_id,
                )
                connected_event = self._connection_event(
                    snapshot,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    session_id=session_id,
                    worker_request_id=worker_request_id,
                    cause_event_id=cause_event_id,
                )
                capability_event = self.resource_runtime.event_for_diff(
                    diff,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    cause_event_id=connected_event.event_id if connected_event else cause_event_id,
                ) if run_id and task_id else None
                for event in (connected_event, capability_event):
                    if event is not None:
                        emitted.append(event)
                        self._emit(event)
                return McpConnectReceipt(
                    snapshot,
                    catalog_snapshot,
                    diff,
                    initialize_result,
                    auth_cache_hit=auth_cache_hit,
                    events=tuple(emitted),
                )
            except (AuthNeedsInteraction, McpNeedsAuthentication) as error:
                self._withdraw_catalog(config.server_id, reason="needs_auth")
                self._close_transport(handle)
                snapshot = self._transition(
                    handle,
                    McpConnectionState.NEEDS_AUTH,
                    error_code="needs_auth",
                    error_message=str(error),
                )
                event = self._connection_event(
                    snapshot,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    session_id=session_id,
                    worker_request_id=worker_request_id,
                    cause_event_id=cause_event_id,
                )
                if event:
                    emitted.append(event)
                    self._emit(event)
                return McpConnectReceipt(snapshot, None, None, {}, auth_cache_hit=auth_cache_hit, events=tuple(emitted))
            except Exception as error:
                self._withdraw_catalog(config.server_id, reason="connection_failed")
                self._close_transport(handle)
                state = McpConnectionState.NEEDS_AUTH if _looks_like_auth_failure(error) else McpConnectionState.FAILED
                snapshot = self._transition(
                    handle,
                    state,
                    error_code="needs_auth" if state is McpConnectionState.NEEDS_AUTH else _error_code(error),
                    error_message=str(error),
                )
                event = self._connection_event(
                    snapshot,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    session_id=session_id,
                    worker_request_id=worker_request_id,
                    cause_event_id=cause_event_id,
                )
                if event:
                    emitted.append(event)
                    self._emit(event)
                return McpConnectReceipt(snapshot, None, None, {}, auth_cache_hit=auth_cache_hit, events=tuple(emitted))

    def refresh(
        self,
        server_id: str,
        *,
        refresh_kinds: Sequence[str] = ("tools", "resources", "resource_templates", "prompts"),
        reason: str = "explicit_refresh",
        run_id: str = "",
        task_id: str = "",
        node_id: str | None = None,
        session_id: str = "",
        cause_event_id: str = "",
    ) -> McpRefreshReceipt:
        self._assert_enabled()
        handle = self._connected_handle(server_id)
        with handle.lock:
            current = self.catalog.get(server_id)
            if current is None:
                raise McpConnectionError("connected MCP server has no capability snapshot")
            try:
                snapshot, diff = self.resource_runtime.discover(
                    server_id=server_id,
                    request=handle.transport,
                    initialize_result=handle.initialize_result,
                    connection_generation=handle.snapshot.generation,
                    generation=current.generation + 1,
                    current=current,
                    refresh_kinds=refresh_kinds,
                )
            except Exception as error:
                handle.last_refresh_error = f"{type(error).__name__}: {error}"[:4096]
                raise
            handle.last_refresh_error = ""
            self._persist_catalog(snapshot)
            handle.snapshot = handle.snapshot.transition(
                McpConnectionState.CONNECTED,
                metadata={
                    "capability_generation": str(snapshot.generation),
                    "catalog_digest": snapshot.snapshot_digest,
                    "refresh_reason": reason,
                },
            )
            self._persist_snapshot(handle.snapshot)
            self._update_instructions(
                snapshot,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                source_event_id=cause_event_id,
            )
            event = self.resource_runtime.event_for_diff(
                diff,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                cause_event_id=cause_event_id,
            ) if run_id and task_id else None
            if event:
                self._emit(event)
            return McpRefreshReceipt(server_id, snapshot, diff, reason, event)

    def disconnect(
        self,
        server_id: str,
        *,
        run_id: str = "",
        task_id: str = "",
        node_id: str | None = None,
        session_id: str = "",
        worker_request_id: str = "",
        reason: str = "operator_disconnect",
    ) -> McpConnectionSnapshot:
        self._assert_enabled()
        handle = self._handle(server_id)
        with handle.lock:
            if handle.snapshot.state in {McpConnectionState.CLOSED, McpConnectionState.DISABLED}:
                return handle.snapshot
            if handle.snapshot.state not in {McpConnectionState.PENDING, McpConnectionState.FAILED, McpConnectionState.NEEDS_AUTH}:
                self._transition(handle, McpConnectionState.CLOSING, metadata={"disconnect_reason": reason})
            self._close_transport(handle)
            if handle.snapshot.state in {McpConnectionState.PENDING, McpConnectionState.FAILED, McpConnectionState.NEEDS_AUTH}:
                # The model permits these states to close through CLOSING only
                # for live paths; a never-opened handle is normalized directly.
                handle.snapshot = replace(
                    handle.snapshot,
                    state=McpConnectionState.CLOSED,
                    revision=handle.snapshot.revision + 1,
                    last_transition_at=now_iso(),
                    metadata={**handle.snapshot.metadata, "disconnect_reason": reason},
                )
                self._persist_snapshot(handle.snapshot)
            else:
                self._transition(handle, McpConnectionState.CLOSED, metadata={"disconnect_reason": reason})
            self._withdraw_catalog(server_id, reason=reason)
            if self.elicitation_queue is not None:
                self.elicitation_queue.cancel_for_server(server_id, reason="MCP connection closed")
            if self.instructions_runtime is not None and session_id and run_id and task_id:
                self.instructions_runtime.disable_server(
                    session_id=session_id,
                    server_id=server_id,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                )
            event = self._connection_event(
                handle.snapshot,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                worker_request_id=worker_request_id,
            )
            if event:
                self._emit(event)
            return handle.snapshot

    def reconnect(self, server_id: str, **context: Any) -> McpConnectReceipt:
        self._assert_enabled()
        handle = self._handle(server_id)
        with handle.lock:
            if handle.config is None:
                raise McpConnectionError(f"MCP server {server_id!r} has no restorable config")
            self._withdraw_catalog(server_id, reason="reconnecting")
            if handle.snapshot.state is McpConnectionState.CONNECTED:
                self._transition(handle, McpConnectionState.RECONNECTING)
            self._close_transport(handle)
            if handle.snapshot.state is McpConnectionState.RECONNECTING:
                handle.snapshot = replace(
                    handle.snapshot,
                    state=McpConnectionState.CLOSED,
                    revision=handle.snapshot.revision + 1,
                    last_transition_at=now_iso(),
                )
                self._persist_snapshot(handle.snapshot)
            return self.connect(handle.config, force=True, **context)

    def disable(self, server_id: str, **context: Any) -> McpConnectionSnapshot:
        self._assert_enabled()
        handle = self._handle(server_id)
        with handle.lock:
            self._close_transport(handle)
            if handle.snapshot.state is McpConnectionState.CONNECTED:
                snapshot = self._transition(handle, McpConnectionState.DISABLED)
            elif handle.snapshot.state is McpConnectionState.CLOSED:
                snapshot = self._transition(handle, McpConnectionState.DISABLED)
            elif handle.snapshot.state is McpConnectionState.PENDING:
                snapshot = self._transition(handle, McpConnectionState.DISABLED)
            else:
                snapshot = replace(
                    handle.snapshot,
                    state=McpConnectionState.DISABLED,
                    revision=handle.snapshot.revision + 1,
                    last_transition_at=now_iso(),
                )
                handle.snapshot = snapshot
                self._persist_snapshot(snapshot)
            handle.config = replace(handle.config, disabled=True)
            self._withdraw_catalog(server_id, reason="disabled")
            if self.elicitation_queue:
                self.elicitation_queue.cancel_for_server(server_id, reason="MCP server disabled")
            return snapshot

    def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        progress: Callable[[Mapping[str, Any]], None] | None = None,
        task_metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self._assert_enabled()
        handle = self._connected_handle(server_id)
        catalog = self.catalog.get(server_id)
        if catalog is None:
            raise McpServerNotConnected(f"MCP server {server_id!r} has no active catalog")
        descriptor = next((item for item in catalog.tools if item.remote_name == tool_name), None)
        if descriptor is None:
            raise McpConnectionError(f"MCP tool {server_id}/{tool_name} is not in the active snapshot")
        metadata = dict(task_metadata or {})
        token = str(metadata.get("progress_token") or new_id("mcpprogress"))
        progress_values: list[Mapping[str, Any]] = []

        def on_progress(params: Any) -> None:
            if not isinstance(params, Mapping) or str(params.get("progressToken") or "") != token:
                return
            safe = redact_value(params)
            if not isinstance(safe, Mapping):
                return
            progress_values.append(safe)
            if progress is not None:
                progress(safe)

        def resolve_transport() -> McpTransport:
            current = self._connected_handle(server_id)
            if current.transport is None:
                raise McpServerNotConnected(f"MCP server {server_id!r} has no live transport")
            return current.transport

        port = _ProgressRequestPort(resolve_transport, token, on_progress)
        use_task = descriptor.task_support is McpToolTaskSupport.REQUIRED or (
            descriptor.task_support is McpToolTaskSupport.OPTIONAL and bool(metadata.get("use_task"))
        )
        try:
            receipt = self.task_runtime.execute_tool(
                request=port,
                server_id=server_id,
                tool_name=tool_name,
                arguments=arguments,
                use_task=use_task,
                run_id=str(metadata.get("run_id") or ""),
                task_id=str(metadata.get("task_id") or ""),
                node_id=str(metadata.get("node_id") or "") or None,
                deadline_seconds=_optional_positive_float(metadata.get("deadline_seconds")),
            )
        finally:
            port.close()
        call_receipt = McpCallReceipt(
            server_id=server_id,
            tool_name=tool_name,
            result=receipt.result,
            connection_generation=handle.snapshot.generation,
            capability_generation=catalog.generation,
            progress_count=len(progress_values),
            progress_digest=stable_digest(progress_values) if progress_values else "",
            task_receipt=receipt,
        )
        result = dict(receipt.result)
        meta = dict(result.get("_meta") or {}) if isinstance(result.get("_meta"), Mapping) else {}
        meta["zyraMcp"] = {
            "serverId": server_id,
            "connectionGeneration": handle.snapshot.generation,
            "capabilityGeneration": catalog.generation,
            "progressCount": call_receipt.progress_count,
            "progressDigest": call_receipt.progress_digest,
            "taskId": receipt.task_id,
            "taskPollCount": receipt.poll_count,
        }
        result["_meta"] = meta
        return result

    def read_resource(self, server_id: str, uri: str, **context: Any) -> Any:
        handle = self._connected_handle(server_id)
        return self.resource_runtime.read_resource(
            server_id=server_id,
            uri=uri,
            request=handle.transport,
            run_id=str(context.get("run_id") or ""),
            task_id=str(context.get("task_id") or ""),
            node_id=str(context.get("node_id") or "") or None,
        )

    def get_prompt(self, server_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, JsonValue]:
        handle = self._connected_handle(server_id)
        return self.resource_runtime.get_prompt(
            server_id=server_id,
            name=name,
            arguments=arguments,
            request=handle.transport,
        )

    def snapshot(self, server_id: str) -> McpConnectionSnapshot:
        return self._handle(server_id).snapshot

    def snapshots(self) -> tuple[McpConnectionSnapshot, ...]:
        with self._lock:
            return tuple(self._handles[key].snapshot for key in sorted(self._handles))

    def identity(self, server_id: str) -> McpConnectionIdentity:
        handle = self._handle(server_id)
        catalog = self.catalog.get(server_id)
        return McpConnectionIdentity(
            server_id=server_id,
            config_fingerprint=(
                handle.config.fingerprint if handle.config is not None else handle.snapshot.config_fingerprint
            ),
            connection_generation=handle.snapshot.generation,
            capability_generation=catalog.generation if catalog else 0,
            protocol_version=handle.snapshot.protocol_version,
            session_id=handle.snapshot.session_id,
        )

    def health(self) -> dict[str, JsonValue]:
        snapshots = self.snapshots()
        counts: dict[str, int] = {}
        for snapshot in snapshots:
            counts[str(snapshot.state)] = counts.get(str(snapshot.state), 0) + 1
        connected = sum(1 for snapshot in snapshots if snapshot.healthy)
        return {
            "schema": "zyra.mcp-connection-health.v1",
            "runtime_id": "McpConnectionRuntime",
            "enabled": not self.disabled,
            "ok": not self.disabled and all(
                snapshot.state not in {
                    McpConnectionState.FAILED,
                    McpConnectionState.NEEDS_AUTH,
                    McpConnectionState.CONNECTING,
                    McpConnectionState.RECONNECTING,
                }
                for snapshot in snapshots
            ),
            "server_count": len(snapshots),
            "connected_count": connected,
            "state_counts": counts,
            "servers": [snapshot.to_dict() for snapshot in snapshots],
            "catalog": self.catalog.safe_dict(),
            "registered_in_process": sorted(self._in_process_programs),
            "fixed_ok_health": False,
        }

    def close_all(self) -> tuple[McpConnectionSnapshot, ...]:
        values: list[McpConnectionSnapshot] = []
        for snapshot in self.snapshots():
            with contextlib.suppress(Exception):
                values.append(self.disconnect(snapshot.server_id, reason="runtime_shutdown"))
        return tuple(values)

    def _build_transport(self, config: McpServerConfig) -> McpTransport:
        if self.transport_factory is not None:
            return self.transport_factory(config)
        program = self._in_process_programs.get(config.server_id)
        return build_transport(config, in_process_program=program)

    def _auth_runtime(self, config: McpServerConfig) -> McpAuthRuntime | None:
        if self.auth_factory is None:
            return None
        return self.auth_factory(config)

    def _install_handlers(self, handle: _ConnectionHandle, **context: Any) -> None:
        transport = handle.transport
        if transport is None:
            raise McpConnectionError("transport is not available")
        server_id = handle.snapshot.server_id
        for remover in handle.removers:
            with contextlib.suppress(Exception):
                remover()
        handle.removers.clear()
        for method in (
            "notifications/tools/list_changed",
            "notifications/resources/list_changed",
            "notifications/prompts/list_changed",
        ):
            handle.removers.append(
                transport.add_notification_handler(
                    method,
                    lambda params, selected=method: self._on_list_changed(
                        server_id,
                        selected,
                        params,
                        **context,
                    ),
                )
            )
        handle.removers.append(
            transport.add_request_handler(
                "sampling/createMessage",
                lambda params: self._on_sampling(server_id, params),
                replace=True,
            )
        )
        handle.removers.append(
            transport.add_request_handler(
                "elicitation/create",
                lambda params: self._on_elicitation(server_id, params, **context),
                replace=True,
            )
        )
        add_error_handler = getattr(transport, "add_error_handler", None)
        if callable(add_error_handler):
            handle.removers.append(
                add_error_handler(
                    lambda error: self._on_transport_error(
                        handle,
                        transport,
                        error,
                        **context,
                    )
                )
            )

    def _on_list_changed(self, server_id: str, method: str, params: Any, **context: Any) -> None:
        kinds = self.resource_runtime.notification_refresh_kinds(method)
        if not kinds:
            return
        handle = self._handle(server_id)
        try:
            self.refresh(
                server_id,
                refresh_kinds=kinds,
                reason=method,
                run_id=str(context.get("run_id") or ""),
                task_id=str(context.get("task_id") or ""),
                node_id=str(context.get("node_id") or "") or None,
                session_id=str(context.get("session_id") or ""),
            )
        except Exception as error:  # Notification dispatch cannot crash reader thread.
            handle.last_refresh_error = f"{type(error).__name__}: {error}"[:4096]

    def _on_sampling(self, server_id: str, params: Any) -> Mapping[str, Any]:
        if not isinstance(params, Mapping):
            raise McpConnectionError("sampling request params must be an object")
        request_id = str(params.get("requestId") or new_id("mcpsampling"))
        if self.sampling_runtime is None:
            raise McpConnectionError("MCP sampling is disabled by default")
        selected = {
            **dict(params),
            "server_id": server_id,
            "request_id": request_id,
        }
        resolution = self.sampling_runtime.sample(
            selected,
            session_id=str(params.get("sessionId") or "mcp-server-request"),
        )
        to_wire = getattr(resolution, "to_wire", None)
        if not callable(to_wire):
            raise McpConnectionError("sampling runtime returned an invalid resolution")
        wire = to_wire()
        if not isinstance(wire, Mapping):
            raise McpConnectionError("sampling resolution is not an object")
        return wire

    def _on_elicitation(self, server_id: str, params: Any, **context: Any) -> Mapping[str, Any]:
        if self.elicitation_queue is None:
            return {"action": "cancel"}
        if not isinstance(params, Mapping):
            raise McpConnectionError("elicitation request params must be an object")
        request = _elicitation_request(
            server_id,
            params,
            session_id=str(context.get("session_id") or "mcp-control"),
        )
        self.elicitation_queue.enqueue(
            request,
            run_id=str(context.get("run_id") or ""),
            task_id=str(context.get("task_id") or ""),
            node_id=str(context.get("node_id") or "") or None,
        )
        # Server-request dispatch runs on the transport's bounded callback
        # worker rather than its carrier reader.  It can therefore wait for an
        # exact API resolution without blocking inbound bytes.  The queue owns
        # timeout/disconnect cancellation and wakes this waiter deterministically.
        return self.elicitation_queue.wait_for_response(
            server_id=request.server_id,
            session_id=request.session_id,
            request_id=request.request_id,
            timeout_seconds=self.elicitation_timeout_seconds,
        )

    def _on_transport_error(
        self,
        handle: _ConnectionHandle,
        transport: McpTransport,
        error: BaseException,
        **context: Any,
    ) -> None:
        # Error callbacks run on carrier threads.  Never take handle.lock or
        # close/join the carrier from that callback; schedule ownership transfer
        # and verify the carrier identity after the connect path releases locks.
        if str(getattr(transport, "state", "")) != "failed":
            return
        thread = threading.Thread(
            target=self._apply_terminal_transport_error,
            args=(handle, transport, error, dict(context)),
            name=f"mcp-{handle.snapshot.server_id}-terminal-owner",
            daemon=True,
        )
        thread.start()

    def _apply_terminal_transport_error(
        self,
        handle: _ConnectionHandle,
        transport: McpTransport,
        error: BaseException,
        context: Mapping[str, Any],
    ) -> None:
        with handle.lock:
            if handle.transport is not transport:
                return
            server_id = handle.snapshot.server_id
            self._withdraw_catalog(server_id, reason="transport_terminal_error")
            self._close_transport(handle)
            if handle.snapshot.state in {
                McpConnectionState.CONNECTING,
                McpConnectionState.CONNECTED,
                McpConnectionState.RECONNECTING,
                McpConnectionState.CLOSING,
            }:
                snapshot = self._transition(
                    handle,
                    McpConnectionState.FAILED,
                    error_code=_error_code(error),
                    error_message=str(error),
                    metadata={"failure_owner": "carrier_terminal_error"},
                )
            else:
                snapshot = replace(
                    handle.snapshot,
                    state=McpConnectionState.FAILED,
                    revision=handle.snapshot.revision + 1,
                    error_code=_error_code(error),
                    error_message=str(error)[:4096],
                    last_transition_at=now_iso(),
                    metadata={
                        **handle.snapshot.metadata,
                        "failure_owner": "carrier_terminal_error",
                    },
                )
                handle.snapshot = snapshot
                self._persist_snapshot(snapshot)
            event = self._connection_event(
                snapshot,
                run_id=str(context.get("run_id") or ""),
                task_id=str(context.get("task_id") or ""),
                node_id=str(context.get("node_id") or "") or None,
                session_id=str(context.get("session_id") or ""),
                worker_request_id=str(context.get("worker_request_id") or ""),
            )
            if event is not None:
                self._emit(event)

    def _close_transport(self, handle: _ConnectionHandle) -> None:
        if self.elicitation_queue is not None:
            self.elicitation_queue.cancel_for_server(
                handle.snapshot.server_id,
                reason="MCP transport closed",
            )
        for remover in handle.removers:
            with contextlib.suppress(Exception):
                remover()
        handle.removers.clear()
        if handle.transport is not None:
            with contextlib.suppress(Exception):
                handle.transport.close()
        handle.transport = None

    def _transition(self, handle: _ConnectionHandle, state: McpConnectionState, **kwargs: Any) -> McpConnectionSnapshot:
        snapshot = handle.snapshot.transition(state, **kwargs)
        handle.snapshot = snapshot
        self._persist_snapshot(snapshot)
        return snapshot

    def _persist_snapshot(self, snapshot: McpConnectionSnapshot) -> None:
        if self.state_store is not None:
            self.state_store.set_connection(snapshot.server_id, snapshot)

    def _persist_catalog(self, snapshot: McpCapabilitySnapshot) -> None:
        if self.state_store is not None:
            self.state_store.update_section(
                "catalogs",
                {snapshot.server_id: snapshot.safe_dict()},
                actor="capability_runtime",
                reason="capability_snapshot_replaced",
                server_id=snapshot.server_id,
            )

    def _withdraw_catalog(self, server_id: str, *, reason: str) -> McpCapabilitySnapshot | None:
        removed = self.catalog.remove(server_id)
        delete = getattr(self.state_store, "delete", None) if self.state_store is not None else None
        if callable(delete):
            delete(
                server_id,
                section="catalogs",
                actor="capability_runtime",
                reason=f"capability_withdrawn:{reason}",
            )
        return removed

    def _client_capabilities(self, server_id: str) -> dict[str, JsonValue]:
        selected = dict(self.client_capabilities)
        advertised = getattr(self.sampling_runtime, "advertised", None)
        if callable(advertised) and bool(advertised(server_id)):
            selected["sampling"] = {}
        else:
            selected.pop("sampling", None)
        return to_json_value(selected)

    def _hydrate_connections(self) -> None:
        if self.disabled:
            return
        reader = getattr(self.state_store, "read_state", None) if self.state_store is not None else None
        if not callable(reader):
            return
        state = reader()
        raw_connections = state.get("connections", {}) if isinstance(state, Mapping) else {}
        if not isinstance(raw_connections, Mapping):
            raise McpConnectionError("durable MCP connections section is not an object")
        hydrated: dict[str, _ConnectionHandle] = {}
        for raw_server_id, raw in raw_connections.items():
            server_id = str(raw_server_id)
            snapshot = _connection_snapshot_from_state(server_id, raw)
            if snapshot.state in {McpConnectionState.CONNECTED, McpConnectionState.CONNECTING}:
                snapshot = replace(
                    snapshot,
                    state=McpConnectionState.RECONNECTING,
                    revision=snapshot.revision + 1,
                    error_code="",
                    error_message="",
                    last_transition_at=now_iso(),
                    metadata={
                        **snapshot.metadata,
                        "restore_reason": "live_transport_not_restorable",
                    },
                )
                self._persist_snapshot(snapshot)
            hydrated[server_id] = _ConnectionHandle(config=None, snapshot=snapshot)
        with self._lock:
            self._handles.update(hydrated)

    def _update_instructions(self, snapshot: McpCapabilitySnapshot, **context: Any) -> None:
        if self.instructions_runtime is None or not context.get("session_id"):
            return
        if not context.get("run_id") or not context.get("task_id"):
            return
        self.instructions_runtime.update(
            session_id=str(context["session_id"]),
            server_id=snapshot.server_id,
            connection_generation=snapshot.connection_generation,
            instructions=snapshot.instructions,
            run_id=str(context["run_id"]),
            task_id=str(context["task_id"]),
            node_id=str(context.get("node_id") or "") or None,
            source_event_id=str(context.get("source_event_id") or ""),
        )

    def _connection_event(self, snapshot: McpConnectionSnapshot, **context: Any) -> EventRecord | None:
        if not context.get("run_id") or not context.get("task_id"):
            return None
        return EventRecord(
            run_id=str(context["run_id"]),
            task_id=str(context["task_id"]),
            node_id=str(context.get("node_id") or "") or None,
            event_type=getattr(EventType, "MCP_CONNECTION_CHANGED", EventType.SYSTEM_NOTICE),
            payload={
                "mcp_runtime": {
                    "schema": "zyra.mcp-connection-event.v1",
                    "runtime_id": "McpConnectionRuntime",
                    "session_id": str(context.get("session_id") or ""),
                    "worker_request_id": str(context.get("worker_request_id") or ""),
                    "cause_event_id": str(context.get("cause_event_id") or ""),
                    "snapshot": snapshot.to_dict(),
                    "raw_credentials_included": False,
                }
            },
        )

    def _emit(self, event: EventRecord) -> None:
        if self.event_sink is not None:
            self.event_sink(event)

    def _handle(self, server_id: str) -> _ConnectionHandle:
        with self._lock:
            handle = self._handles.get(server_id)
        if handle is None:
            raise McpServerNotFound(server_id)
        return handle

    def _connected_handle(self, server_id: str) -> _ConnectionHandle:
        handle = self._handle(server_id)
        if handle.snapshot.state is not McpConnectionState.CONNECTED or handle.transport is None:
            raise McpServerNotConnected(f"MCP server {server_id!r} is {handle.snapshot.state}")
        return handle

    def _task_reconnect(self, server_id: str) -> None:
        receipt = self.reconnect(server_id)
        if not receipt.connected:
            raise McpServerNotConnected(f"MCP server {server_id!r} did not reconnect")

    def _assert_enabled(self) -> None:
        if self.disabled:
            raise McpConnectionRuntimeDisabled("McpConnectionRuntime is disabled")


def _default_client_capabilities(
    *,
    elicitation_enabled: bool,
    tasks_enabled: bool,
) -> dict[str, JsonValue]:
    capabilities: dict[str, JsonValue] = {}
    if elicitation_enabled:
        # 2025-06-18 has one elicitation capability object.  ``form``/``url``
        # sub-capabilities belong to a later draft and must not be advertised.
        capabilities["elicitation"] = {}
    if tasks_enabled:
        capabilities["tasks"] = {"requests": {"tools": {"call": {}}}}
    return capabilities


def _sanitize_client_capabilities(value: Mapping[str, Any]) -> dict[str, JsonValue]:
    selected = to_json_value(value)
    if not isinstance(selected, dict):
        raise ValueError("MCP client capabilities must be an object")
    # Roots/listChanged requires roots/list request handling and root-change
    # notification ownership, neither of which this foundation implements.
    selected.pop("roots", None)
    # Sampling is server-specific and is injected only when the deterministic
    # sampling runtime is enabled and has a callback for that server.
    selected.pop("sampling", None)
    if "elicitation" in selected:
        selected["elicitation"] = {}
    return selected


def _connection_snapshot_from_state(server_id: str, raw: Any) -> McpConnectionSnapshot:
    if not isinstance(raw, Mapping):
        return McpConnectionSnapshot(
            server_id=server_id,
            state=McpConnectionState.FAILED,
            error_code="durable_connection_invalid",
            error_message="durable connection snapshot is not an object",
            metadata={"failure_owner": "state_hydration"},
        )
    try:
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
        selected_metadata = {str(key): str(value) for key, value in metadata.items()}
        if raw.get("restore_reason"):
            selected_metadata["restore_reason"] = str(raw["restore_reason"])
        return McpConnectionSnapshot(
            server_id=str(raw.get("server_id") or server_id),
            state=McpConnectionState(str(raw.get("state") or McpConnectionState.PENDING)),
            revision=max(0, int(raw.get("revision") or 0)),
            generation=max(0, int(raw.get("generation") or 0)),
            config_fingerprint=str(raw.get("config_fingerprint") or ""),
            protocol_version=str(raw.get("protocol_version") or ""),
            session_id=str(raw.get("session_id") or ""),
            error_code=str(raw.get("error_code") or ""),
            error_message=str(raw.get("error_message") or "")[:4096],
            last_transition_at=str(raw.get("last_transition_at") or now_iso()),
            connected_at=str(raw.get("connected_at") or ""),
            metadata=selected_metadata,
        )
    except (TypeError, ValueError) as error:
        return McpConnectionSnapshot(
            server_id=server_id,
            state=McpConnectionState.FAILED,
            error_code="durable_connection_invalid",
            error_message=f"{type(error).__name__}: {error}"[:4096],
            metadata={"failure_owner": "state_hydration"},
        )


def _safe_initialize_result(value: Mapping[str, Any]) -> dict[str, JsonValue]:
    return {
        "protocolVersion": str(value.get("protocolVersion") or ""),
        "serverInfo": redact_value(value.get("serverInfo") or {}),
        "capabilities": redact_value(value.get("capabilities") or {}),
        "instructions": "<present>" if value.get("instructions") else "",
        "instructions_hash": stable_digest(str(value.get("instructions") or "")) if value.get("instructions") else "",
    }


def _elicitation_request(server_id: str, params: Mapping[str, Any], *, session_id: str) -> McpElicitationRequest:
    mode = McpElicitationMode.URL if params.get("url") else McpElicitationMode.FORM
    schema = params.get("requestedSchema") if isinstance(params.get("requestedSchema"), Mapping) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    required = {str(value) for value in schema.get("required", []) if isinstance(value, str)}
    fields = tuple(
        McpElicitationField(
            name=str(name),
            schema=value if isinstance(value, Mapping) else {},
            required=str(name) in required,
            sensitive=bool(isinstance(value, Mapping) and value.get("x-sensitive") is True),
        )
        for name, value in properties.items()
    )
    return McpElicitationRequest(
        server_id=server_id,
        request_id=str(params.get("requestId") or new_id("mcpelicitation")),
        session_id=session_id,
        mode=mode,
        message=str(params.get("message") or "MCP server requested operator input"),
        fields=fields,
        url=str(params.get("url") or ""),
        expires_at=str(params.get("expiresAt") or ""),
        revision=max(1, int(params.get("revision") or 1)),
        metadata={"source": "server_request", "server_id": server_id},
    )


def _looks_like_auth_failure(error: BaseException) -> bool:
    text = f"{type(error).__name__}: {error}".casefold()
    return any(marker in text for marker in ("401", "403", "unauthorized", "needs_auth", "authentication required"))


def _error_code(error: BaseException) -> str:
    name = type(error).__name__
    selected = "".join(character.lower() if character.isalnum() else "_" for character in name)
    return selected.strip("_")[:128] or "mcp_connection_failed"


def _optional_positive_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    selected = float(value)
    if selected <= 0:
        raise ValueError("deadline_seconds must be positive")
    return selected


__all__ = [
    "MCP_CLIENT_INFO",
    "MCP_PROTOCOL_VERSION",
    "SUPPORTED_MCP_PROTOCOL_VERSIONS",
    "McpCallReceipt",
    "McpConnectReceipt",
    "McpConnectionError",
    "McpConnectionIdentity",
    "McpConnectionRuntime",
    "McpConnectionRuntimeDisabled",
    "McpInitializeError",
    "McpNeedsAuthentication",
    "McpRefreshReceipt",
    "McpServerNotConnected",
    "McpServerNotFound",
]
