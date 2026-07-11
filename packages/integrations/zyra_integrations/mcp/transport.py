from __future__ import annotations

"""Executable MCP JSON-RPC transports owned by Zyra.

The transport layer is intentionally independent from an MCP SDK.  It offers
one correlation/lifecycle implementation and three concrete carriers:

* a programmable in-process carrier for embedded servers and hermetic tests;
* a newline-framed stdio carrier with real subprocess cleanup;
* a streamable HTTP carrier supporting JSON and SSE responses plus an
  optional server-event GET stream.

All carriers share start-once semantics, pending-request cleanup, bounded
redacted observations, server request/notification routing, and deterministic
timeouts.
"""

import contextlib
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, TypeAlias, runtime_checkable

from .models import (
    JsonValue,
    McpServerConfig,
    McpTransportKind,
    canonical_json,
    is_sensitive_key,
    redact_value,
    to_json_value,
    utc_now_iso,
)
from .protocol import (
    JsonRpcError,
    JsonRpcErrorCode,
    JsonRpcErrorResponse,
    JsonRpcMessage,
    JsonRpcNotification,
    JsonRpcProtocolError,
    JsonRpcRequest,
    JsonRpcRequestIdAllocator,
    JsonRpcSuccessResponse,
    JsonRpcTransportClosed,
    LineJsonRpcCodec,
    NotificationDispatch,
    NotificationHandler,
    NotificationRouter,
    ObservationBuffer,
    PendingRequestRegistry,
    RequestDispatch,
    ServerRequestRouter,
    SseDecoder,
    decode_json_rpc,
    encode_json_rpc,
    parse_json_rpc_message,
    parse_json_rpc_payload,
    safe_json_rpc_dict,
    validate_method,
    validate_params,
)


class McpTransportError(RuntimeError):
    code = "mcp_transport_error"


class McpTransportDisabled(McpTransportError):
    code = "mcp_transport_disabled"


class McpTransportOpenError(McpTransportError):
    code = "mcp_transport_open_failed"


class McpTransportWriteError(McpTransportError):
    code = "mcp_transport_write_failed"


class McpTransportReadError(McpTransportError):
    code = "mcp_transport_read_failed"


class McpTransportHttpError(McpTransportError):
    code = "mcp_transport_http_error"

    def __init__(self, status: int, message: str, *, body_preview: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body_preview = body_preview


class McpTransportState(StrEnum):
    NEW = "new"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class McpTransportSnapshot:
    server_id: str
    kind: McpTransportKind
    state: McpTransportState
    generation: int
    open_count: int
    close_count: int
    request_count: int
    notification_count: int
    inbound_count: int
    response_count: int
    protocol_error_count: int
    notification_failure_count: int
    unsolicited_response_count: int
    pending_count: int
    opened_at: str
    closed_at: str
    last_error: str
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state is McpTransportState.OPEN

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "server_id": self.server_id,
            "kind": str(self.kind),
            "state": str(self.state),
            "ready": self.ready,
            "generation": self.generation,
            "open_count": self.open_count,
            "close_count": self.close_count,
            "request_count": self.request_count,
            "notification_count": self.notification_count,
            "inbound_count": self.inbound_count,
            "response_count": self.response_count,
            "protocol_error_count": self.protocol_error_count,
            "notification_failure_count": self.notification_failure_count,
            "unsolicited_response_count": self.unsolicited_response_count,
            "pending_count": self.pending_count,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "last_error": self.last_error,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class McpTransport(Protocol):
    @property
    def server_id(self) -> str: ...

    @property
    def kind(self) -> McpTransportKind: ...

    @property
    def state(self) -> McpTransportState: ...

    def open(self) -> bool: ...

    def request(self, method: str, params: Any = None, *, timeout_seconds: float | None = None) -> JsonValue: ...

    def notify(self, method: str, params: Any = None) -> None: ...

    def add_notification_handler(self, method: str, handler: NotificationHandler) -> Callable[[], None]: ...

    def add_request_handler(self, method: str, handler: Callable[[Any], Any], *, replace: bool = False) -> Callable[[], None]: ...

    def add_error_handler(self, handler: "ErrorHandler") -> Callable[[], None]: ...

    def set_protocol_version(self, protocol_version: str) -> None: ...

    def close(self) -> bool: ...

    def snapshot(self) -> McpTransportSnapshot: ...


ErrorHandler: TypeAlias = Callable[[BaseException], None]


class BaseMcpTransport:
    """Shared lifecycle, correlation, routing and diagnostics implementation."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        auto_open: bool = True,
        request_id_prefix: str | None = None,
        observation_capacity: int = 256,
        notification_queue_size: int = 1024,
    ) -> None:
        if config.transport is not self.expected_kind:
            raise ValueError(f"{type(self).__name__} requires transport kind {self.expected_kind}")
        self.config = config
        self.auto_open = auto_open
        self._state = McpTransportState.NEW
        self._condition = threading.Condition(threading.RLock())
        self._write_lock = threading.RLock()
        self._ids = JsonRpcRequestIdAllocator(prefix=request_id_prefix or _safe_id_prefix(config.server_id))
        self._pending = PendingRequestRegistry()
        self._notifications = NotificationRouter()
        self._requests = ServerRequestRouter()
        self._observations = ObservationBuffer(observation_capacity)
        if notification_queue_size <= 0:
            raise ValueError("notification_queue_size must be positive")
        self._notification_queue: queue.Queue[JsonRpcNotification | None] = queue.Queue(
            maxsize=notification_queue_size
        )
        self._server_request_queue: queue.Queue[JsonRpcRequest | None] = queue.Queue(
            maxsize=notification_queue_size
        )
        self._notification_stop = threading.Event()
        self._notification_thread: threading.Thread | None = None
        self._server_request_thread: threading.Thread | None = None
        self._error_handlers: list[ErrorHandler] = []
        self._generation = 0
        self._open_count = 0
        self._close_count = 0
        self._request_count = 0
        self._notification_count = 0
        self._inbound_count = 0
        self._response_count = 0
        self._protocol_error_count = 0
        self._notification_failure_count = 0
        self._unsolicited_response_count = 0
        self._opened_at = ""
        self._closed_at = ""
        self._last_error = ""
        self._protocol_version = ""

    @property
    def expected_kind(self) -> McpTransportKind:
        raise NotImplementedError

    @property
    def server_id(self) -> str:
        return self.config.server_id

    @property
    def kind(self) -> McpTransportKind:
        return self.expected_kind

    @property
    def state(self) -> McpTransportState:
        with self._condition:
            return self._state

    @property
    def is_open(self) -> bool:
        return self.state is McpTransportState.OPEN

    @property
    def observations(self) -> tuple[Any, ...]:
        return self._observations.snapshot()

    @property
    def serialize_writes(self) -> bool:
        """Whether a carrier requires one writer to own the complete write.

        Stdio is a byte stream and must serialize complete frames.  Streamable
        HTTP is request/response based and deliberately overrides this: a
        server request delivered inside one POST's SSE response can require a
        concurrent POST carrying the client response before that first POST
        completes.
        """

        return True

    @property
    def protocol_version(self) -> str:
        with self._condition:
            return self._protocol_version

    def set_protocol_version(self, protocol_version: str) -> None:
        selected = str(protocol_version).strip()
        if not selected or len(selected) > 64 or any(character.isspace() for character in selected):
            raise ValueError("invalid MCP protocol version")
        with self._condition:
            self._protocol_version = selected

    def add_error_handler(self, handler: ErrorHandler) -> Callable[[], None]:
        if not callable(handler):
            raise TypeError("error handler must be callable")
        with self._condition:
            if handler not in self._error_handlers:
                self._error_handlers.append(handler)

        def remove() -> None:
            with self._condition:
                with contextlib.suppress(ValueError):
                    self._error_handlers.remove(handler)

        return remove

    def add_notification_handler(self, method: str, handler: NotificationHandler) -> Callable[[], None]:
        return self._notifications.add(method, handler)

    def add_request_handler(
        self,
        method: str,
        handler: Callable[[Any], Any],
        *,
        replace: bool = False,
    ) -> Callable[[], None]:
        return self._requests.add(method, handler, replace=replace)

    def open(self) -> bool:
        """Open once even when several threads race to start the transport."""

        with self._condition:
            while self._state in {McpTransportState.OPENING, McpTransportState.CLOSING}:
                self._condition.wait()
            if self._state is McpTransportState.OPEN:
                return False
            if self.config.disabled or not self.config.connectable:
                raise McpTransportDisabled(f"MCP server {self.server_id!r} is disabled or not approved")
            self._state = McpTransportState.OPENING
            target_generation = self._generation + 1
        try:
            self._start_callback_workers(target_generation)
            self._open_impl(target_generation)
        except Exception as error:
            self._stop_callback_workers()
            wrapped = error if isinstance(error, McpTransportError) else McpTransportOpenError(
                f"failed to open {self.kind} transport: {type(error).__name__}: {error}"
            )
            with self._condition:
                self._state = McpTransportState.FAILED
                self._last_error = str(wrapped)[:4096]
                self._condition.notify_all()
            self._pending.fail_all(wrapped)
            self._emit_error(wrapped)
            raise wrapped from error
        with self._condition:
            self._generation = target_generation
            self._open_count += 1
            self._opened_at = utc_now_iso()
            self._closed_at = ""
            self._last_error = ""
            self._state = McpTransportState.OPEN
            self._condition.notify_all()
        return True

    def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout_seconds: float | None = None,
    ) -> JsonValue:
        validate_method(method)
        selected_params = validate_params(params)
        self._ensure_open()
        request = JsonRpcRequest(self._ids.next(), method, selected_params)
        self._pending.register(request)
        with self._condition:
            self._request_count += 1
        try:
            self._send_message(request)
        except Exception as error:
            wrapped = (
                error
                if isinstance(error, McpTransportError | JsonRpcProtocolError)
                else McpTransportWriteError(f"failed to send JSON-RPC request: {type(error).__name__}: {error}")
            )
            self._pending.fail(request.request_id, wrapped)
        timeout = timeout_seconds if timeout_seconds is not None else self.config.request_timeout_seconds
        try:
            return self._pending.wait(request.request_id, timeout)
        except Exception as error:
            if isinstance(error, JsonRpcProtocolError) and error.code == JsonRpcErrorCode.REQUEST_TIMEOUT:
                # Best effort MCP cancellation is a notification; timeout must
                # not wait on another response and must not resurrect pending.
                with contextlib.suppress(Exception):
                    self.notify("notifications/cancelled", {"requestId": request.request_id, "reason": "client timeout"})
            raise

    def notify(self, method: str, params: Any = None) -> None:
        validate_method(method)
        selected_params = validate_params(params)
        self._ensure_open()
        notification = JsonRpcNotification(method, selected_params)
        with self._condition:
            self._notification_count += 1
        self._send_message(notification)

    def close(self) -> bool:
        with self._condition:
            while self._state is McpTransportState.OPENING:
                self._condition.wait()
            if self._state in {McpTransportState.NEW, McpTransportState.CLOSED}:
                if self._state is McpTransportState.NEW:
                    self._state = McpTransportState.CLOSED
                    self._closed_at = utc_now_iso()
                return False
            if self._state is McpTransportState.CLOSING:
                while self._state is McpTransportState.CLOSING:
                    self._condition.wait()
                return False
            self._state = McpTransportState.CLOSING
        self._notification_stop.set()
        closed_error = JsonRpcTransportClosed(f"MCP transport {self.server_id!r} closed")
        self._pending.fail_all(closed_error)
        failure: BaseException | None = None
        try:
            self._close_impl()
        except Exception as error:  # noqa: BLE001 - cleanup continues and pending callers are released.
            failure = error
        self._stop_callback_workers()
        with self._condition:
            self._close_count += 1
            self._closed_at = utc_now_iso()
            self._state = McpTransportState.CLOSED
            if failure is not None:
                self._last_error = f"{type(failure).__name__}: {failure}"[:4096]
            self._condition.notify_all()
        if failure is not None:
            self._emit_error(failure)
        return True

    def dispose(self) -> None:
        self.close()
        self._notifications.clear()
        self._requests.clear()
        with self._condition:
            self._error_handlers.clear()
        self._observations.clear()

    def snapshot(self) -> McpTransportSnapshot:
        with self._condition:
            return McpTransportSnapshot(
                server_id=self.server_id,
                kind=self.kind,
                state=self._state,
                generation=self._generation,
                open_count=self._open_count,
                close_count=self._close_count,
                request_count=self._request_count,
                notification_count=self._notification_count,
                inbound_count=self._inbound_count,
                response_count=self._response_count,
                protocol_error_count=self._protocol_error_count,
                notification_failure_count=self._notification_failure_count,
                unsolicited_response_count=self._unsolicited_response_count,
                pending_count=len(self._pending),
                opened_at=self._opened_at,
                closed_at=self._closed_at,
                last_error=self._last_error,
                metadata={
                    "notification_queue_depth": self._notification_queue.qsize(),
                    "notification_worker_alive": bool(
                        self._notification_thread and self._notification_thread.is_alive()
                    ),
                    "server_request_queue_depth": self._server_request_queue.qsize(),
                    "server_request_worker_alive": bool(
                        self._server_request_thread and self._server_request_thread.is_alive()
                    ),
                    **self._snapshot_metadata(),
                },
            )

    def _ensure_open(self) -> None:
        current = self.state
        if current is McpTransportState.OPEN:
            return
        if self.auto_open and current in {McpTransportState.NEW, McpTransportState.CLOSED, McpTransportState.FAILED}:
            self.open()
            return
        raise JsonRpcTransportClosed(f"MCP transport {self.server_id!r} is not open ({current})")

    def _send_message(self, message: JsonRpcMessage) -> None:
        with self._condition:
            if self._state is not McpTransportState.OPEN:
                raise JsonRpcTransportClosed(f"MCP transport {self.server_id!r} closed before write")
        self._observations.append("outbound", message)
        try:
            if self.serialize_writes:
                with self._write_lock:
                    self._write_message(message)
            else:
                self._write_message(message)
        except Exception as error:
            wrapped = error if isinstance(error, McpTransportError | JsonRpcProtocolError) else McpTransportWriteError(
                f"failed to write JSON-RPC message: {type(error).__name__}: {error}"
            )
            self._emit_error(wrapped)
            raise wrapped from error

    def accept(self, message: JsonRpcMessage | Mapping[str, Any]) -> None:
        """Accept one inbound message from a concrete carrier or test peer."""

        try:
            selected = parse_json_rpc_message(message) if isinstance(message, Mapping) else message
            if not isinstance(selected, JsonRpcRequest | JsonRpcNotification | JsonRpcSuccessResponse | JsonRpcErrorResponse):
                raise JsonRpcProtocolError("unsupported inbound JSON-RPC message type")
            self._observations.append("inbound", selected)
            with self._condition:
                self._inbound_count += 1
            if isinstance(selected, JsonRpcSuccessResponse | JsonRpcErrorResponse):
                accepted = self._pending.resolve(selected)
                with self._condition:
                    if accepted:
                        self._response_count += 1
                    else:
                        self._unsolicited_response_count += 1
                if not accepted:
                    self._emit_error(
                        JsonRpcProtocolError(
                            f"unsolicited or duplicate JSON-RPC response id: {selected.request_id}",
                            code=JsonRpcErrorCode.UNSOLICITED_RESPONSE,
                        )
                    )
                return
            if isinstance(selected, JsonRpcNotification):
                try:
                    self._notification_queue.put_nowait(selected)
                except queue.Full:
                    with self._condition:
                        self._notification_failure_count += 1
                    self._emit_error(McpTransportReadError("MCP notification queue reached bounded capacity"))
                return
            try:
                self._server_request_queue.put_nowait(selected)
            except queue.Full:
                self._send_message(
                    JsonRpcErrorResponse(
                        selected.request_id,
                        JsonRpcError(
                            JsonRpcErrorCode.INTERNAL_ERROR,
                            "Zyra MCP server-request queue reached bounded capacity",
                        ),
                    )
                )
        except Exception as error:
            self._handle_read_error(error)
            raise

    def accept_payload(self, payload: bytes | str | Any) -> int:
        if isinstance(payload, bytes | str):
            messages = decode_json_rpc(payload, max_bytes=self.config.max_response_bytes)
        else:
            messages = parse_json_rpc_payload(payload)
        for message in messages:
            self.accept(message)
        return len(messages)

    def _handle_read_error(self, error: BaseException, *, terminal: bool = False) -> None:
        wrapped = error if isinstance(error, JsonRpcProtocolError | McpTransportError) else McpTransportReadError(
            f"failed to read JSON-RPC message: {type(error).__name__}: {error}"
        )
        with self._condition:
            self._protocol_error_count += 1
            self._last_error = str(wrapped)[:4096]
            if terminal and self._state not in {McpTransportState.CLOSING, McpTransportState.CLOSED}:
                self._state = McpTransportState.FAILED
                self._condition.notify_all()
        if terminal:
            self._pending.fail_all(wrapped)
        self._emit_error(wrapped)

    def _emit_error(self, error: BaseException) -> None:
        with self._condition:
            handlers = tuple(self._error_handlers)
        for handler in handlers:
            with contextlib.suppress(Exception):
                handler(error)

    def _start_callback_workers(self, generation: int) -> None:
        self._notification_stop.clear()
        self._drain_callback_queues()
        thread = threading.Thread(
            target=self._notification_loop,
            args=(generation,),
            name=f"mcp-{self.server_id}-notifications-{generation}",
            daemon=True,
        )
        self._notification_thread = thread
        request_thread = threading.Thread(
            target=self._server_request_loop,
            args=(generation,),
            name=f"mcp-{self.server_id}-server-requests-{generation}",
            daemon=True,
        )
        self._server_request_thread = request_thread
        thread.start()
        request_thread.start()

    def _notification_loop(self, generation: int) -> None:
        while not self._notification_stop.is_set():
            try:
                notification = self._notification_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if notification is None or self._notification_stop.is_set():
                return
            with self._condition:
                while self._state is McpTransportState.OPENING and not self._notification_stop.is_set():
                    self._condition.wait(timeout=0.05)
                if self._state is not McpTransportState.OPEN or generation != self._generation:
                    continue
            dispatch = self._notifications.dispatch(notification)
            if dispatch.failures:
                with self._condition:
                    self._notification_failure_count += len(dispatch.failures)
                self._emit_error(McpTransportReadError("; ".join(dispatch.failures)))
            self._notification_dispatched(dispatch)

    def _server_request_loop(self, generation: int) -> None:
        while not self._notification_stop.is_set():
            try:
                request = self._server_request_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if request is None or self._notification_stop.is_set():
                return
            with self._condition:
                while self._state is McpTransportState.OPENING and not self._notification_stop.is_set():
                    self._condition.wait(timeout=0.05)
                if self._state is not McpTransportState.OPEN or generation != self._generation:
                    continue
            dispatch = self._requests.dispatch(request)
            self._server_request_dispatched(dispatch)
            try:
                self._send_message(dispatch.response)
            except Exception as error:
                if not self._notification_stop.is_set():
                    self._handle_read_error(error)

    def _stop_callback_workers(self) -> None:
        self._notification_stop.set()
        with contextlib.suppress(queue.Full):
            self._notification_queue.put_nowait(None)
        with contextlib.suppress(queue.Full):
            self._server_request_queue.put_nowait(None)
        for thread in (self._notification_thread, self._server_request_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._notification_thread = None
        self._server_request_thread = None
        self._drain_callback_queues()

    def _drain_callback_queues(self) -> None:
        for selected_queue in (self._notification_queue, self._server_request_queue):
            while True:
                try:
                    selected_queue.get_nowait()
                except queue.Empty:
                    break

    def _open_impl(self, generation: int) -> None:
        raise NotImplementedError

    def _write_message(self, message: JsonRpcMessage) -> None:
        raise NotImplementedError

    def _close_impl(self) -> None:
        raise NotImplementedError

    def _snapshot_metadata(self) -> Mapping[str, JsonValue]:
        return {}

    def _notification_dispatched(self, dispatch: NotificationDispatch) -> None:
        return

    def _server_request_dispatched(self, dispatch: RequestDispatch) -> None:
        return

    def __enter__(self) -> "BaseMcpTransport":
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class InProcessExchange:
    """Programmable peer result for delayed/drop/error and multi-message cases."""

    messages: tuple[JsonRpcMessage | Mapping[str, Any], ...] = ()
    delay_seconds: float = 0.0
    drop: bool = False
    error: BaseException | None = None

    def __post_init__(self) -> None:
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")


InProcessProgram: TypeAlias = Callable[[JsonRpcMessage, "InProcessMcpTransport"], Any]


class InProcessMcpTransport(BaseMcpTransport):
    """Stateful in-process carrier suitable for embedded MCP servers."""

    def __init__(
        self,
        config: McpServerConfig,
        program: InProcessProgram,
        *,
        open_hook: Callable[["InProcessMcpTransport"], None] | None = None,
        close_hook: Callable[["InProcessMcpTransport"], None] | None = None,
        auto_open: bool = True,
    ) -> None:
        super().__init__(config, auto_open=auto_open)
        if not callable(program):
            raise TypeError("in-process MCP program must be callable")
        self._program = program
        self._open_hook = open_hook
        self._close_hook = close_hook
        self._stop = threading.Event()
        self._delivery_threads: set[threading.Thread] = set()
        self._delivery_lock = threading.RLock()
        self._sent: deque[JsonRpcMessage] = deque(maxlen=1024)
        self._delivered_count = 0
        self._dropped_count = 0

    @property
    def expected_kind(self) -> McpTransportKind:
        return McpTransportKind.IN_PROCESS

    @property
    def sent_messages(self) -> tuple[JsonRpcMessage, ...]:
        with self._delivery_lock:
            return tuple(self._sent)

    def emit(self, message: JsonRpcMessage | Mapping[str, Any], *, delay_seconds: float = 0.0) -> None:
        exchange = InProcessExchange((message,), delay_seconds=delay_seconds)
        self._deliver_exchange(exchange)

    def emit_notification(self, method: str, params: Any = None, *, delay_seconds: float = 0.0) -> None:
        self.emit(JsonRpcNotification(method, validate_params(params)), delay_seconds=delay_seconds)

    def _open_impl(self, generation: int) -> None:
        self._stop.clear()
        if self._open_hook is not None:
            self._open_hook(self)

    def _write_message(self, message: JsonRpcMessage) -> None:
        with self._delivery_lock:
            self._sent.append(message)
        result = self._program(message, self)
        exchange = _coerce_exchange(result, outbound=message)
        self._deliver_exchange(exchange)

    def _deliver_exchange(self, exchange: InProcessExchange) -> None:
        if exchange.error is not None and exchange.delay_seconds <= 0:
            raise exchange.error
        if exchange.drop:
            with self._delivery_lock:
                self._dropped_count += 1
            return
        if exchange.delay_seconds <= 0:
            if exchange.error is not None:
                raise exchange.error
            for message in exchange.messages:
                self.accept(message)
                with self._delivery_lock:
                    self._delivered_count += 1
            return

        def delayed() -> None:
            try:
                if self._stop.wait(exchange.delay_seconds):
                    return
                if exchange.error is not None:
                    self._handle_read_error(exchange.error)
                    return
                for item in exchange.messages:
                    if self._stop.is_set():
                        return
                    self.accept(item)
                    with self._delivery_lock:
                        self._delivered_count += 1
            finally:
                with self._delivery_lock:
                    self._delivery_threads.discard(threading.current_thread())

        thread = threading.Thread(target=delayed, name=f"mcp-inprocess-{self.server_id}-delivery", daemon=True)
        with self._delivery_lock:
            self._delivery_threads.add(thread)
        thread.start()

    def _close_impl(self) -> None:
        self._stop.set()
        if self._close_hook is not None:
            self._close_hook(self)
        with self._delivery_lock:
            threads = tuple(self._delivery_threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)
        with self._delivery_lock:
            self._delivery_threads = {thread for thread in self._delivery_threads if thread.is_alive()}

    def _snapshot_metadata(self) -> Mapping[str, JsonValue]:
        with self._delivery_lock:
            return {
                "sent_count": len(self._sent),
                "delivered_count": self._delivered_count,
                "dropped_count": self._dropped_count,
                "delivery_thread_count": sum(thread.is_alive() for thread in self._delivery_threads),
            }


class StdioMcpTransport(BaseMcpTransport):
    """Real newline-framed JSON-RPC subprocess transport."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        cwd: str | Path | None = None,
        inherit_environment: bool = True,
        shutdown_timeout_seconds: float = 3.0,
        stderr_limit_bytes: int = 65536,
        auto_open: bool = True,
    ) -> None:
        super().__init__(config, auto_open=auto_open)
        self.cwd = Path(cwd).resolve() if cwd is not None else None
        if self.cwd is not None and (not self.cwd.exists() or not self.cwd.is_dir()):
            raise ValueError("stdio MCP cwd must be an existing directory")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        if stderr_limit_bytes < 1024:
            raise ValueError("stderr_limit_bytes must be at least 1024")
        self.inherit_environment = inherit_environment
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.stderr_limit_bytes = stderr_limit_bytes
        self._process: subprocess.Popen[bytes] | None = None
        self._codec = LineJsonRpcCodec(max_frame_bytes=config.max_response_bytes)
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stderr_lock = threading.Lock()
        self._stderr = bytearray()
        self._exit_code: int | None = None

    @property
    def expected_kind(self) -> McpTransportKind:
        return McpTransportKind.STDIO

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None else None

    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def stderr_tail(self) -> str:
        with self._stderr_lock:
            text = bytes(self._stderr).decode("utf-8", errors="replace")
        return _redact_diagnostic_text(text)

    def _open_impl(self, generation: int) -> None:
        command = [self.config.command, *self.config.args]
        environment = dict(os.environ) if self.inherit_environment else {}
        environment.update(self.config.env)
        creationflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = int(subprocess.CREATE_NO_WINDOW)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.cwd) if self.cwd is not None else None,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as error:
            raise McpTransportOpenError(f"could not start MCP stdio process: {error}") from error
        if process.stdin is None or process.stdout is None or process.stderr is None:
            with contextlib.suppress(Exception):
                process.kill()
            raise McpTransportOpenError("MCP stdio process did not expose all pipes")
        self._process = process
        self._exit_code = None
        self._stop.clear()
        self._codec.reset()
        with self._stderr_lock:
            self._stderr.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(process,),
            name=f"mcp-stdio-{self.server_id}-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            args=(process,),
            name=f"mcp-stdio-{self.server_id}-stderr",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()

    def _write_message(self, message: JsonRpcMessage) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise McpTransportWriteError("MCP stdio process is not running")
        frame = self._codec.encode(message)
        try:
            process.stdin.write(frame)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise McpTransportWriteError("MCP stdio pipe closed during write") from error

    def _reader_loop(self, process: subprocess.Popen[bytes]) -> None:
        stdout = process.stdout
        assert stdout is not None
        try:
            while not self._stop.is_set():
                chunk = stdout.read(65536)
                if not chunk:
                    break
                for message in self._codec.feed(chunk):
                    self.accept(message)
            for message in self._codec.finish():
                self.accept(message)
        except Exception as error:  # noqa: BLE001 - receive loop terminates and wakes all pending calls.
            if not self._stop.is_set():
                self._handle_read_error(error, terminal=True)
        finally:
            self._exit_code = process.poll()
            if not self._stop.is_set() and self.state not in {McpTransportState.CLOSING, McpTransportState.CLOSED}:
                self._handle_read_error(
                    McpTransportReadError(f"MCP stdio process exited unexpectedly with code {self._exit_code}"),
                    terminal=True,
                )

    def _stderr_loop(self, process: subprocess.Popen[bytes]) -> None:
        stderr = process.stderr
        assert stderr is not None
        while not self._stop.is_set():
            chunk = stderr.read(4096)
            if not chunk:
                return
            with self._stderr_lock:
                self._stderr.extend(chunk)
                if len(self._stderr) > self.stderr_limit_bytes:
                    del self._stderr[: len(self._stderr) - self.stderr_limit_bytes]

    def _close_impl(self) -> None:
        self._stop.set()
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(Exception):
                process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self.shutdown_timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=self.shutdown_timeout_seconds)
        self._exit_code = process.returncode
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=self.shutdown_timeout_seconds)
        self._process = None
        self._reader_thread = None
        self._stderr_thread = None

    def _snapshot_metadata(self) -> Mapping[str, JsonValue]:
        return {
            "pid": self.pid,
            "alive": self.alive,
            "exit_code": self._exit_code,
            "stderr_bytes": len(self.stderr_tail.encode("utf-8")),
            "command": self.config.command,
            "argument_count": len(self.config.args),
            "cwd": str(self.cwd) if self.cwd is not None else "",
            "raw_environment_included": False,
        }


class StreamableHttpMcpTransport(BaseMcpTransport):
    """MCP streamable HTTP carrier using only urllib and the stdlib."""

    SESSION_HEADER = "Mcp-Session-Id"
    PROTOCOL_HEADER = "MCP-Protocol-Version"

    def __init__(
        self,
        config: McpServerConfig,
        *,
        protocol_version: str = "2025-06-18",
        listen_for_server_events: bool = False,
        event_retry_seconds: float = 0.25,
        terminate_session: bool = True,
        opener: urllib.request.OpenerDirector | None = None,
        auto_open: bool = True,
    ) -> None:
        super().__init__(config, auto_open=auto_open)
        if event_retry_seconds <= 0:
            raise ValueError("event_retry_seconds must be positive")
        self.set_protocol_version(protocol_version)
        self.listen_for_server_events = listen_for_server_events
        self.event_retry_seconds = event_retry_seconds
        self.terminate_session = terminate_session
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler())
        self._session_id = ""
        self._session_lock = threading.RLock()
        self._stop = threading.Event()
        self._event_thread: threading.Thread | None = None
        self._active_event_response: Any | None = None
        self._event_reconnect_count = 0
        self._http_request_count = 0
        self._http_status_counts: dict[int, int] = {}
        self._http_lock = threading.RLock()
        self._last_event_id = ""

    @property
    def expected_kind(self) -> McpTransportKind:
        return McpTransportKind.STREAMABLE_HTTP

    @property
    def serialize_writes(self) -> bool:
        # MCP Streamable HTTP explicitly permits a server request to arrive in
        # a still-open POST SSE response.  Its JSON-RPC response is another
        # POST, so serializing the entire HTTP exchange deadlocks the session.
        return False

    @property
    def session_id(self) -> str:
        with self._session_lock:
            return self._session_id

    def _open_impl(self, generation: int) -> None:
        self._stop.clear()
        if self.listen_for_server_events:
            self._event_thread = threading.Thread(
                target=self._event_loop,
                name=f"mcp-http-{self.server_id}-events",
                daemon=True,
            )
            self._event_thread.start()

    def _write_message(self, message: JsonRpcMessage) -> None:
        body = encode_json_rpc(message)
        if len(body) > self.config.max_response_bytes:
            raise McpTransportWriteError("outbound HTTP JSON-RPC body exceeds configured limit")
        headers = self._request_headers(
            {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
        )
        request = urllib.request.Request(self.config.url, data=body, headers=headers, method="POST")
        try:
            with self._open_url(request, timeout=self.config.request_timeout_seconds) as response:
                self._record_http_response(response)
                status = int(getattr(response, "status", 200))
                if status == 202:
                    return
                content_type = str(response.headers.get("Content-Type", "application/json")).casefold()
                if "text/event-stream" in content_type:
                    self._consume_sse_response(response)
                    return
                payload = self._bounded_read(response)
                if not payload.strip():
                    if isinstance(message, JsonRpcRequest):
                        # The response may arrive on the GET event stream.
                        return
                    return
                self.accept_payload(payload)
        except urllib.error.HTTPError as error:
            body_preview = _safe_http_error_body(error, self.config.max_response_bytes)
            self._record_http_status(error.code)
            raise McpTransportHttpError(
                error.code,
                f"MCP HTTP request failed with status {error.code}",
                body_preview=body_preview,
            ) from error
        except urllib.error.URLError as error:
            raise McpTransportWriteError(f"MCP HTTP request failed: {type(error.reason).__name__}") from error

    def _event_loop(self) -> None:
        with self._condition:
            while self._state is McpTransportState.OPENING and not self._stop.is_set():
                self._condition.wait(timeout=0.05)
        while not self._stop.is_set():
            headers = self._request_headers({"Accept": "text/event-stream"})
            if self._last_event_id:
                headers["Last-Event-ID"] = self._last_event_id
            request = urllib.request.Request(self.config.url, headers=headers, method="GET")
            try:
                response = self._open_url(request, timeout=self.config.request_timeout_seconds)
                self._active_event_response = response
                try:
                    self._record_http_response(response)
                    content_type = str(response.headers.get("Content-Type", "")).casefold()
                    if "text/event-stream" not in content_type:
                        raise McpTransportReadError("MCP event GET did not return text/event-stream")
                    self._consume_sse_response(response, streaming=True)
                finally:
                    self._active_event_response = None
                    response.close()
            except urllib.error.HTTPError as error:
                self._record_http_status(error.code)
                if error.code in {404, 405}:
                    return
                if not self._stop.is_set():
                    self._handle_read_error(McpTransportHttpError(error.code, "MCP event stream failed"))
            except Exception as error:  # noqa: BLE001 - bounded retry while transport remains open.
                if not self._stop.is_set():
                    self._handle_read_error(error)
            if self._stop.wait(self.event_retry_seconds):
                return
            self._event_reconnect_count += 1

    def _consume_sse_response(self, response: Any, *, streaming: bool = False) -> None:
        decoder = SseDecoder(max_event_bytes=self.config.max_response_bytes)
        # ``HTTPResponse.read(n)`` may wait to fill ``n`` across SSE chunks,
        # preventing a just-delivered server request from being dispatched.
        # ``read1`` returns currently available carrier bytes and is the
        # correct streaming primitive when the response exposes it.
        reader = getattr(response, "read1", response.read)
        while not self._stop.is_set():
            chunk = reader(65536)
            if not chunk:
                break
            for event in decoder.feed(chunk):
                if event.event_id:
                    self._last_event_id = event.event_id
                for message in event.json_rpc_messages(max_bytes=self.config.max_response_bytes):
                    self.accept(message)
            if not streaming and getattr(response, "closed", False):
                break
        for event in decoder.finish():
            if event.event_id:
                self._last_event_id = event.event_id
            for message in event.json_rpc_messages(max_bytes=self.config.max_response_bytes):
                self.accept(message)

    def _close_impl(self) -> None:
        self._stop.set()
        active = self._active_event_response
        if active is not None:
            with contextlib.suppress(Exception):
                active.close()
        event_thread = self._event_thread
        if event_thread is not None and event_thread is not threading.current_thread():
            event_thread.join(timeout=min(5.0, self.config.request_timeout_seconds + 0.5))
        self._event_thread = None
        if self.terminate_session and self.session_id:
            request = urllib.request.Request(
                self.config.url,
                headers=self._request_headers({"Accept": "application/json"}),
                method="DELETE",
            )
            with contextlib.suppress(Exception):
                with self._open_url(request, timeout=min(2.0, self.config.request_timeout_seconds)) as response:
                    self._record_http_response(response)
                    self._bounded_read(response)
        with self._session_lock:
            self._session_id = ""

    def _request_headers(self, additions: Mapping[str, str]) -> dict[str, str]:
        headers = {str(key): str(value) for key, value in self.config.headers.items()}
        headers.update(additions)
        headers[self.PROTOCOL_HEADER] = self.protocol_version
        session = self.session_id
        if session:
            headers[self.SESSION_HEADER] = session
        return headers

    def _open_url(self, request: urllib.request.Request, *, timeout: float) -> Any:
        with self._http_lock:
            self._http_request_count += 1
        return self._opener.open(request, timeout=timeout)

    def _record_http_response(self, response: Any) -> None:
        status = int(getattr(response, "status", 200))
        self._record_http_status(status)
        session_id = response.headers.get(self.SESSION_HEADER)
        if session_id:
            if len(session_id) > 4096 or "\r" in session_id or "\n" in session_id:
                raise McpTransportReadError("invalid MCP session id response header")
            with self._session_lock:
                if self._session_id and self._session_id != session_id:
                    raise McpTransportReadError("MCP server changed session id within one connection")
                self._session_id = session_id

    def _record_http_status(self, status: int) -> None:
        with self._http_lock:
            self._http_status_counts[status] = self._http_status_counts.get(status, 0) + 1

    def _bounded_read(self, response: Any) -> bytes:
        payload = response.read(self.config.max_response_bytes + 1)
        if len(payload) > self.config.max_response_bytes:
            raise McpTransportReadError("MCP HTTP response exceeds configured byte limit")
        return payload

    def _snapshot_metadata(self) -> Mapping[str, JsonValue]:
        with self._http_lock:
            request_count = self._http_request_count
            status_counts = dict(self._http_status_counts)
        return {
            "url": _redacted_url(self.config.url),
            "session_id": self.session_id,
            "protocol_version": self.protocol_version,
            "listen_for_server_events": self.listen_for_server_events,
            "event_thread_alive": bool(self._event_thread and self._event_thread.is_alive()),
            "event_reconnect_count": self._event_reconnect_count,
            "http_request_count": request_count,
            "http_status_counts": {str(key): value for key, value in status_counts.items()},
            "last_event_id": self._last_event_id,
            "header_names": sorted(self.config.headers),
            "raw_headers_included": False,
        }


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so credentials never cross origin implicitly."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def build_transport(
    config: McpServerConfig,
    *,
    in_process_program: InProcessProgram | None = None,
    cwd: str | Path | None = None,
    listen_for_server_events: bool = False,
) -> McpTransport:
    if config.transport is McpTransportKind.IN_PROCESS:
        if in_process_program is None:
            raise ValueError("in-process MCP transport requires program")
        return InProcessMcpTransport(config, in_process_program)
    if config.transport is McpTransportKind.STDIO:
        return StdioMcpTransport(config, cwd=cwd)
    if config.transport is McpTransportKind.STREAMABLE_HTTP:
        return StreamableHttpMcpTransport(config, listen_for_server_events=listen_for_server_events)
    raise ValueError(f"unsupported MCP transport kind: {config.transport}")


def _coerce_exchange(result: Any, *, outbound: JsonRpcMessage) -> InProcessExchange:
    if isinstance(result, InProcessExchange):
        return result
    if result is None:
        return InProcessExchange(drop=True)
    if isinstance(result, BaseException):
        return InProcessExchange(error=result)
    if isinstance(result, JsonRpcRequest | JsonRpcNotification | JsonRpcSuccessResponse | JsonRpcErrorResponse):
        return InProcessExchange((result,))
    if isinstance(result, Mapping):
        if "jsonrpc" in result:
            return InProcessExchange((result,))
        if isinstance(outbound, JsonRpcRequest):
            return InProcessExchange((JsonRpcSuccessResponse(outbound.request_id, to_json_value(result)),))
        return InProcessExchange()
    if isinstance(result, Sequence) and not isinstance(result, str | bytes | bytearray | memoryview):
        messages: list[JsonRpcMessage | Mapping[str, Any]] = []
        for item in result:
            if isinstance(item, JsonRpcRequest | JsonRpcNotification | JsonRpcSuccessResponse | JsonRpcErrorResponse | Mapping):
                messages.append(item)
            else:
                raise TypeError(f"unsupported in-process MCP message: {type(item).__name__}")
        return InProcessExchange(tuple(messages))
    if isinstance(outbound, JsonRpcRequest):
        return InProcessExchange((JsonRpcSuccessResponse(outbound.request_id, to_json_value(result)),))
    return InProcessExchange()


def _safe_id_prefix(server_id: str) -> str:
    selected = re.sub(r"[^A-Za-z0-9_-]+", "-", server_id).strip("-") or "mcp"
    return selected[:48]


def _redact_diagnostic_text(text: str) -> str:
    patterns = (
        re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"),
        re.compile(r"(?i)((?:access|refresh|id)[_-]?token\s*[:=]\s*)([^\s,;]+)"),
        re.compile(r"(?i)((?:api[_-]?key|password|secret)\s*[:=]\s*)([^\s,;]+)"),
    )
    output = text
    for pattern in patterns:
        output = pattern.sub(r"\1<redacted>", output)
    return output


def _redacted_url(url: str) -> str:
    try:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parsed = urlsplit(url)
        query = urlencode(
            [(key, "<redacted>" if is_sensitive_key(key) else value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
        )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    except Exception:
        return "<invalid-url>"


def _safe_http_error_body(error: urllib.error.HTTPError, max_bytes: int) -> str:
    try:
        raw = error.read(min(max_bytes, 4096))
        return _redact_diagnostic_text(raw.decode("utf-8", errors="replace"))
    except Exception:
        return ""
