from __future__ import annotations

"""Strict JSON-RPC and MCP wire contracts used by Zyra transports.

Transport implementations feed decoded objects into this module rather than
interpreting ad-hoc dictionaries themselves.  The validation boundary is
deliberately strict: malformed identifiers, ambiguous result/error responses,
oversized frames, repeated pagination cursors, and unsolicited responses are
reported explicitly and never treated as successful MCP operations.
"""

import codecs
import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any, Generic, TypeAlias, TypeVar, cast

from .models import JsonValue, McpPage, canonical_json, redact_value, require_mapping, to_json_value


JsonRpcId: TypeAlias = str | int
JsonRpcParams: TypeAlias = Mapping[str, JsonValue] | Sequence[JsonValue] | None
JsonRpcHandler: TypeAlias = Callable[[JsonRpcParams], Any]
NotificationHandler: TypeAlias = Callable[["JsonRpcNotification"], None]


class JsonRpcErrorCode(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    REQUEST_CANCELLED = -32800
    CONTENT_TOO_LARGE = -32001
    REQUEST_TIMEOUT = -32002
    TRANSPORT_CLOSED = -32003
    UNSOLICITED_RESPONSE = -32004


class JsonRpcMessageKind(StrEnum):
    REQUEST = "request"
    NOTIFICATION = "notification"
    SUCCESS = "success"
    ERROR = "error"


class JsonRpcProtocolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | JsonRpcErrorCode = JsonRpcErrorCode.INVALID_REQUEST,
        data: JsonValue = None,
        request_id: JsonRpcId | None = None,
    ) -> None:
        super().__init__(message)
        self.code = int(code)
        self.data = data
        self.request_id = request_id

    def to_error(self) -> "JsonRpcError":
        return JsonRpcError(code=self.code, message=str(self), data=self.data)


class JsonRpcParseError(JsonRpcProtocolError):
    def __init__(self, message: str, *, data: JsonValue = None) -> None:
        super().__init__(message, code=JsonRpcErrorCode.PARSE_ERROR, data=data)


class JsonRpcRemoteError(JsonRpcProtocolError):
    def __init__(self, error: "JsonRpcError", *, request_id: JsonRpcId) -> None:
        super().__init__(error.message, code=error.code, data=error.data, request_id=request_id)
        self.error = error


class JsonRpcRequestTimeout(JsonRpcProtocolError):
    def __init__(self, request_id: JsonRpcId, method: str, timeout_seconds: float) -> None:
        super().__init__(
            f"JSON-RPC request {method!r} timed out after {timeout_seconds:g} seconds",
            code=JsonRpcErrorCode.REQUEST_TIMEOUT,
            request_id=request_id,
            data={"method": method, "timeout_seconds": timeout_seconds},
        )
        self.method = method
        self.timeout_seconds = timeout_seconds


class JsonRpcTransportClosed(JsonRpcProtocolError):
    def __init__(self, reason: str = "JSON-RPC transport is closed") -> None:
        super().__init__(reason, code=JsonRpcErrorCode.TRANSPORT_CLOSED)


@dataclass(frozen=True, slots=True)
class JsonRpcError:
    code: int
    message: str
    data: JsonValue = None

    def __post_init__(self) -> None:
        if isinstance(self.code, bool) or not isinstance(self.code, int):
            raise JsonRpcProtocolError("JSON-RPC error code must be an integer")
        if not isinstance(self.message, str) or not self.message.strip():
            raise JsonRpcProtocolError("JSON-RPC error message is required")
        object.__setattr__(self, "data", to_json_value(self.data))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JsonRpcError":
        if set(value) - {"code", "message", "data"}:
            # Extra fields are not fatal on the wire, but are deliberately
            # discarded so untrusted material cannot leak into durable state.
            value = {key: value[key] for key in ("code", "message", "data") if key in value}
        return cls(value.get("code"), value.get("message"), value.get("data"))

    def to_dict(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    request_id: JsonRpcId
    method: str
    params: JsonRpcParams = None
    jsonrpc: str = "2.0"

    kind: JsonRpcMessageKind = field(default=JsonRpcMessageKind.REQUEST, init=False)

    def __post_init__(self) -> None:
        _validate_version(self.jsonrpc)
        object.__setattr__(self, "request_id", validate_request_id(self.request_id))
        object.__setattr__(self, "method", validate_method(self.method))
        object.__setattr__(self, "params", validate_params(self.params))

    def to_dict(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {"jsonrpc": "2.0", "id": self.request_id, "method": self.method}
        if self.params is not None:
            payload["params"] = to_json_value(self.params)
        return payload


@dataclass(frozen=True, slots=True)
class JsonRpcNotification:
    method: str
    params: JsonRpcParams = None
    jsonrpc: str = "2.0"

    kind: JsonRpcMessageKind = field(default=JsonRpcMessageKind.NOTIFICATION, init=False)

    def __post_init__(self) -> None:
        _validate_version(self.jsonrpc)
        object.__setattr__(self, "method", validate_method(self.method))
        object.__setattr__(self, "params", validate_params(self.params))

    def to_dict(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {"jsonrpc": "2.0", "method": self.method}
        if self.params is not None:
            payload["params"] = to_json_value(self.params)
        return payload


@dataclass(frozen=True, slots=True)
class JsonRpcSuccessResponse:
    request_id: JsonRpcId
    result: JsonValue
    jsonrpc: str = "2.0"

    kind: JsonRpcMessageKind = field(default=JsonRpcMessageKind.SUCCESS, init=False)

    def __post_init__(self) -> None:
        _validate_version(self.jsonrpc)
        object.__setattr__(self, "request_id", validate_request_id(self.request_id))
        object.__setattr__(self, "result", to_json_value(self.result))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"jsonrpc": "2.0", "id": self.request_id, "result": self.result}


@dataclass(frozen=True, slots=True)
class JsonRpcErrorResponse:
    request_id: JsonRpcId | None
    error: JsonRpcError
    jsonrpc: str = "2.0"

    kind: JsonRpcMessageKind = field(default=JsonRpcMessageKind.ERROR, init=False)

    def __post_init__(self) -> None:
        _validate_version(self.jsonrpc)
        if self.request_id is not None:
            object.__setattr__(self, "request_id", validate_request_id(self.request_id))
        if not isinstance(self.error, JsonRpcError):
            raise JsonRpcProtocolError("JSON-RPC error response requires JsonRpcError")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"jsonrpc": "2.0", "id": self.request_id, "error": self.error.to_dict()}


JsonRpcMessage: TypeAlias = JsonRpcRequest | JsonRpcNotification | JsonRpcSuccessResponse | JsonRpcErrorResponse


def validate_request_id(value: Any) -> JsonRpcId:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise JsonRpcProtocolError("JSON-RPC id must be a string or integer")
    if isinstance(value, str):
        if not value:
            raise JsonRpcProtocolError("JSON-RPC string id cannot be empty")
        if len(value) > 1024:
            raise JsonRpcProtocolError("JSON-RPC id exceeds 1024 characters")
    return value


_METHOD_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]{1,512}$")


def validate_method(value: Any) -> str:
    if not isinstance(value, str) or not _METHOD_PATTERN.fullmatch(value):
        raise JsonRpcProtocolError("JSON-RPC method contains unsupported characters or length")
    return value


def validate_params(value: Any) -> JsonRpcParams:
    if value is None:
        return None
    if isinstance(value, Mapping):
        selected = to_json_value(value)
        assert isinstance(selected, dict)
        return selected
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray | memoryview):
        selected = to_json_value(value)
        assert isinstance(selected, list)
        return selected
    raise JsonRpcProtocolError("JSON-RPC params must be an object, array, or null", code=JsonRpcErrorCode.INVALID_PARAMS)


def _validate_version(value: Any) -> None:
    if value != "2.0":
        raise JsonRpcProtocolError("JSON-RPC version must be exactly '2.0'")


def parse_json_rpc_message(value: Any) -> JsonRpcMessage:
    if not isinstance(value, Mapping):
        raise JsonRpcProtocolError("JSON-RPC message must be an object")
    _validate_version(value.get("jsonrpc"))
    has_method = "method" in value
    has_id = "id" in value
    has_result = "result" in value
    has_error = "error" in value
    if has_method:
        if has_result or has_error:
            raise JsonRpcProtocolError("JSON-RPC request cannot contain result or error")
        allowed = {"jsonrpc", "id", "method", "params"}
        if set(value) - allowed:
            raise JsonRpcProtocolError("JSON-RPC request contains unknown top-level members")
        if has_id:
            return JsonRpcRequest(value["id"], value.get("method"), value.get("params"))
        return JsonRpcNotification(value.get("method"), value.get("params"))
    if not has_id:
        raise JsonRpcProtocolError("JSON-RPC response requires id")
    if has_result == has_error:
        raise JsonRpcProtocolError("JSON-RPC response must contain exactly one of result or error")
    allowed = {"jsonrpc", "id", "result", "error"}
    if set(value) - allowed:
        raise JsonRpcProtocolError("JSON-RPC response contains unknown top-level members")
    if has_result:
        if value["id"] is None:
            raise JsonRpcProtocolError("successful JSON-RPC response cannot use null id")
        return JsonRpcSuccessResponse(value["id"], value.get("result"))
    raw_error = value.get("error")
    if not isinstance(raw_error, Mapping):
        raise JsonRpcProtocolError("JSON-RPC error member must be an object")
    return JsonRpcErrorResponse(value.get("id"), JsonRpcError.from_dict(raw_error))


def parse_json_rpc_payload(value: Any, *, max_batch_size: int = 128) -> tuple[JsonRpcMessage, ...]:
    if isinstance(value, list):
        if not value:
            raise JsonRpcProtocolError("JSON-RPC batch cannot be empty")
        if len(value) > max_batch_size:
            raise JsonRpcProtocolError(
                f"JSON-RPC batch exceeds {max_batch_size} messages",
                code=JsonRpcErrorCode.CONTENT_TOO_LARGE,
            )
        return tuple(parse_json_rpc_message(item) for item in value)
    return (parse_json_rpc_message(value),)


def decode_json_rpc(data: bytes | str, *, max_bytes: int = 8 * 1024 * 1024) -> tuple[JsonRpcMessage, ...]:
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if len(raw) > max_bytes:
        raise JsonRpcParseError("JSON-RPC payload exceeds configured byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise JsonRpcParseError("JSON-RPC payload is not valid UTF-8") from error
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as error:
        raise JsonRpcParseError("JSON-RPC payload is not valid JSON") from error
    return parse_json_rpc_payload(value)


def encode_json_rpc(message: JsonRpcMessage | Sequence[JsonRpcMessage]) -> bytes:
    if isinstance(message, Sequence) and not isinstance(message, JsonRpcRequest | JsonRpcNotification | JsonRpcSuccessResponse | JsonRpcErrorResponse):
        if not message:
            raise JsonRpcProtocolError("cannot encode an empty JSON-RPC batch")
        payload: JsonValue = [item.to_dict() for item in message]
    else:
        payload = cast(JsonRpcMessage, message).to_dict()
    return canonical_json(payload).encode("utf-8")


def safe_json_rpc_dict(message: JsonRpcMessage) -> dict[str, JsonValue]:
    selected = redact_value(message.to_dict())
    assert isinstance(selected, dict)
    return selected


class JsonRpcRequestIdAllocator:
    """Monotonic per-connection request IDs, safe across worker threads."""

    def __init__(self, *, prefix: str = "zyra", initial: int = 0) -> None:
        if not prefix or len(prefix) > 64:
            raise ValueError("request id prefix must contain 1-64 characters")
        if initial < 0:
            raise ValueError("initial request id cannot be negative")
        self._prefix = prefix
        self._value = initial
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            self._value += 1
            return f"{self._prefix}-{self._value}"

    @property
    def current(self) -> int:
        with self._lock:
            return self._value


@dataclass(slots=True)
class _PendingCall:
    request_id: JsonRpcId
    method: str
    created_monotonic: float
    event: threading.Event = field(default_factory=threading.Event)
    response: JsonRpcSuccessResponse | JsonRpcErrorResponse | None = None
    failure: BaseException | None = None


class PendingRequestRegistry:
    """Correlates responses and guarantees close/timeout cleanup."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._pending: dict[JsonRpcId, _PendingCall] = {}
        self._lock = threading.RLock()

    def register(self, request: JsonRpcRequest) -> None:
        with self._lock:
            if request.request_id in self._pending:
                raise JsonRpcProtocolError(f"duplicate pending JSON-RPC id: {request.request_id}")
            self._pending[request.request_id] = _PendingCall(request.request_id, request.method, self._clock())

    def resolve(self, response: JsonRpcSuccessResponse | JsonRpcErrorResponse) -> bool:
        if response.request_id is None:
            return False
        with self._lock:
            pending = self._pending.get(response.request_id)
            if pending is None:
                return False
            if pending.response is not None or pending.failure is not None:
                return False
            pending.response = response
            pending.event.set()
            return True

    def fail(self, request_id: JsonRpcId, error: BaseException) -> bool:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.response is not None or pending.failure is not None:
                return False
            pending.failure = error
            pending.event.set()
            return True

    def wait(self, request_id: JsonRpcId, timeout_seconds: float) -> JsonValue:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                raise JsonRpcProtocolError(f"JSON-RPC request is not pending: {request_id}")
        completed = pending.event.wait(timeout_seconds)
        with self._lock:
            self._pending.pop(request_id, None)
        if not completed:
            raise JsonRpcRequestTimeout(request_id, pending.method, timeout_seconds)
        if pending.failure is not None:
            raise pending.failure
        response = pending.response
        if response is None:
            raise JsonRpcProtocolError("pending JSON-RPC request completed without a response")
        if isinstance(response, JsonRpcErrorResponse):
            raise JsonRpcRemoteError(response.error, request_id=request_id)
        return response.result

    def abandon(self, request_id: JsonRpcId) -> bool:
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return False
        pending.failure = JsonRpcProtocolError("JSON-RPC request was abandoned", code=JsonRpcErrorCode.REQUEST_CANCELLED)
        pending.event.set()
        return True

    def fail_all(self, error: BaseException) -> int:
        with self._lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for call in pending:
            call.failure = error
            call.event.set()
        return len(pending)

    def snapshot(self) -> tuple[dict[str, JsonValue], ...]:
        now = self._clock()
        with self._lock:
            return tuple(
                {
                    "request_id": str(item.request_id),
                    "method": item.method,
                    "age_seconds": max(0.0, now - item.created_monotonic),
                    "settled": item.event.is_set(),
                }
                for item in self._pending.values()
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)


@dataclass(frozen=True, slots=True)
class NotificationDispatch:
    method: str
    handler_count: int
    delivered_count: int
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "method": self.method,
            "handler_count": self.handler_count,
            "delivered_count": self.delivered_count,
            "failures": list(self.failures),
            "ok": self.ok,
        }


class NotificationRouter:
    """Thread-safe handler registry; callbacks are never run under its lock."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[NotificationHandler]] = {}
        self._wildcard_handlers: list[NotificationHandler] = []
        self._lock = threading.RLock()

    def add(self, method: str, handler: NotificationHandler) -> Callable[[], None]:
        if method != "*":
            validate_method(method)
        if not callable(handler):
            raise TypeError("notification handler must be callable")
        with self._lock:
            target = self._wildcard_handlers if method == "*" else self._handlers.setdefault(method, [])
            if handler not in target:
                target.append(handler)

        def remove() -> None:
            self.remove(method, handler)

        return remove

    def remove(self, method: str, handler: NotificationHandler) -> bool:
        with self._lock:
            target = self._wildcard_handlers if method == "*" else self._handlers.get(method, [])
            try:
                target.remove(handler)
            except ValueError:
                return False
            if method != "*" and not target:
                self._handlers.pop(method, None)
            return True

    def dispatch(self, notification: JsonRpcNotification) -> NotificationDispatch:
        with self._lock:
            handlers = tuple(self._handlers.get(notification.method, ())) + tuple(self._wildcard_handlers)
        failures: list[str] = []
        delivered = 0
        for handler in handlers:
            try:
                handler(notification)
                delivered += 1
            except Exception as error:  # noqa: BLE001 - one extension must not stop protocol receive loop.
                failures.append(f"{type(error).__name__}: {error}"[:4096])
        return NotificationDispatch(notification.method, len(handlers), delivered, tuple(failures))

    def clear(self) -> int:
        with self._lock:
            count = len(self._wildcard_handlers) + sum(len(items) for items in self._handlers.values())
            self._wildcard_handlers.clear()
            self._handlers.clear()
            return count


@dataclass(frozen=True, slots=True)
class RequestDispatch:
    response: JsonRpcSuccessResponse | JsonRpcErrorResponse
    handled: bool
    elapsed_ms: int


class ServerRequestRouter:
    """Routes sampling/elicitation and other server-initiated requests."""

    def __init__(self) -> None:
        self._handlers: dict[str, JsonRpcHandler] = {}
        self._lock = threading.RLock()

    def add(self, method: str, handler: JsonRpcHandler, *, replace: bool = False) -> Callable[[], None]:
        validate_method(method)
        if not callable(handler):
            raise TypeError("request handler must be callable")
        with self._lock:
            if method in self._handlers and not replace:
                raise JsonRpcProtocolError(f"server request handler already registered: {method}")
            self._handlers[method] = handler

        def remove() -> None:
            self.remove(method, handler)

        return remove

    def remove(self, method: str, handler: JsonRpcHandler | None = None) -> bool:
        with self._lock:
            current = self._handlers.get(method)
            if current is None or (handler is not None and current is not handler):
                return False
            self._handlers.pop(method, None)
            return True

    def dispatch(self, request: JsonRpcRequest) -> RequestDispatch:
        started = time.monotonic()
        with self._lock:
            handler = self._handlers.get(request.method)
        if handler is None:
            response: JsonRpcSuccessResponse | JsonRpcErrorResponse = JsonRpcErrorResponse(
                request.request_id,
                JsonRpcError(JsonRpcErrorCode.METHOD_NOT_FOUND, f"No handler for server request {request.method!r}"),
            )
            handled = False
        else:
            handled = True
            try:
                result = handler(request.params)
                response = JsonRpcSuccessResponse(request.request_id, to_json_value(result))
            except JsonRpcProtocolError as error:
                response = JsonRpcErrorResponse(request.request_id, error.to_error())
            except Exception as error:  # noqa: BLE001 - callback errors become bounded protocol errors.
                response = JsonRpcErrorResponse(
                    request.request_id,
                    JsonRpcError(
                        JsonRpcErrorCode.INTERNAL_ERROR,
                        f"server request handler failed: {type(error).__name__}",
                    ),
                )
        return RequestDispatch(response, handled, int((time.monotonic() - started) * 1000))

    def clear(self) -> int:
        with self._lock:
            count = len(self._handlers)
            self._handlers.clear()
            return count


class LineJsonRpcCodec:
    """Incremental UTF-8 newline framed JSON-RPC codec for stdio."""

    def __init__(self, *, max_frame_bytes: int = 8 * 1024 * 1024) -> None:
        if max_frame_bytes < 1024:
            raise ValueError("max_frame_bytes must be at least 1024")
        self.max_frame_bytes = max_frame_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._buffer = ""
        self._buffer_bytes = 0

    def feed(self, data: bytes) -> tuple[JsonRpcMessage, ...]:
        if not isinstance(data, bytes):
            raise TypeError("stdio codec accepts bytes")
        try:
            self._buffer += self._decoder.decode(data)
        except UnicodeDecodeError as error:
            self.reset()
            raise JsonRpcParseError("stdio JSON-RPC frame is not valid UTF-8") from error
        messages: list[JsonRpcMessage] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > self.max_frame_bytes:
                self.reset()
                raise JsonRpcParseError("stdio JSON-RPC frame exceeds configured byte limit")
            messages.extend(decode_json_rpc(line, max_bytes=self.max_frame_bytes))
        self._buffer_bytes = len(self._buffer.encode("utf-8"))
        if self._buffer_bytes > self.max_frame_bytes:
            self.reset()
            raise JsonRpcParseError("stdio JSON-RPC frame exceeds configured byte limit")
        return tuple(messages)

    def finish(self) -> tuple[JsonRpcMessage, ...]:
        try:
            self._buffer += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as error:
            self.reset()
            raise JsonRpcParseError("truncated UTF-8 at end of stdio stream") from error
        if not self._buffer.strip():
            self.reset()
            return ()
        data = self._buffer
        self.reset()
        return decode_json_rpc(data, max_bytes=self.max_frame_bytes)

    def encode(self, message: JsonRpcMessage | Sequence[JsonRpcMessage]) -> bytes:
        payload = encode_json_rpc(message)
        if len(payload) > self.max_frame_bytes:
            raise JsonRpcProtocolError("outbound stdio frame exceeds configured byte limit", code=JsonRpcErrorCode.CONTENT_TOO_LARGE)
        return payload + b"\n"

    def reset(self) -> None:
        self._decoder.reset()
        self._buffer = ""
        self._buffer_bytes = 0


@dataclass(frozen=True, slots=True)
class SseEvent:
    data: str
    event: str = "message"
    event_id: str = ""
    retry_ms: int | None = None

    def json_rpc_messages(self, *, max_bytes: int = 8 * 1024 * 1024) -> tuple[JsonRpcMessage, ...]:
        return decode_json_rpc(self.data, max_bytes=max_bytes)


class SseDecoder:
    """Incremental WHATWG event-stream decoder with bounded event data."""

    def __init__(self, *, max_event_bytes: int = 8 * 1024 * 1024) -> None:
        self.max_event_bytes = max_event_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._line_buffer = ""
        self._data_lines: list[str] = []
        self._event = "message"
        self._event_id = ""
        self._retry_ms: int | None = None
        self._event_bytes = 0

    def feed(self, data: bytes) -> tuple[SseEvent, ...]:
        try:
            self._line_buffer += self._decoder.decode(data)
        except UnicodeDecodeError as error:
            self.reset()
            raise JsonRpcParseError("SSE stream is not valid UTF-8") from error
        events: list[SseEvent] = []
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            line = line.rstrip("\r")
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
        return tuple(events)

    def finish(self) -> tuple[SseEvent, ...]:
        try:
            self._line_buffer += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as error:
            self.reset()
            raise JsonRpcParseError("truncated UTF-8 at end of SSE stream") from error
        events: list[SseEvent] = []
        if self._line_buffer:
            event = self._consume_line(self._line_buffer.rstrip("\r"))
            if event is not None:
                events.append(event)
        event = self._consume_line("")
        if event is not None:
            events.append(event)
        self._line_buffer = ""
        return tuple(events)

    def _consume_line(self, line: str) -> SseEvent | None:
        if line == "":
            if not self._data_lines:
                self._reset_event()
                return None
            event = SseEvent("\n".join(self._data_lines), self._event or "message", self._event_id, self._retry_ms)
            self._reset_event()
            return event
        if line.startswith(":"):
            return None
        field_name, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field_name == "data":
            self._event_bytes += len(value.encode("utf-8"))
            if self._event_bytes > self.max_event_bytes:
                self.reset()
                raise JsonRpcParseError("SSE event exceeds configured byte limit")
            self._data_lines.append(value)
        elif field_name == "event":
            self._event = value
        elif field_name == "id" and "\x00" not in value:
            self._event_id = value
        elif field_name == "retry" and value.isdigit():
            self._retry_ms = int(value)
        return None

    def _reset_event(self) -> None:
        self._data_lines = []
        self._event = "message"
        self._retry_ms = None
        self._event_bytes = 0

    def reset(self) -> None:
        self._decoder.reset()
        self._line_buffer = ""
        self._event_id = ""
        self._reset_event()


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PaginationResult(Generic[T]):
    items: tuple[T, ...]
    cursors: tuple[str, ...]
    page_count: int

    def to_dict(self, serializer: Callable[[T], JsonValue] = to_json_value) -> dict[str, JsonValue]:
        return {"items": [serializer(item) for item in self.items], "cursors": list(self.cursors), "page_count": self.page_count}


def collect_paginated(
    request: Callable[[str], Mapping[str, Any]],
    *,
    item_key: str,
    parse_item: Callable[[Mapping[str, JsonValue]], T],
    max_pages: int = 100,
    max_items: int = 10000,
) -> PaginationResult[T]:
    """Fetch all pages while rejecting cursor cycles and hostile growth."""

    if max_pages <= 0 or max_items <= 0:
        raise ValueError("pagination limits must be positive")
    cursor = ""
    seen: set[str] = set()
    cursors: list[str] = []
    items: list[T] = []
    for page_index in range(max_pages):
        result = request(cursor)
        if not isinstance(result, Mapping):
            raise JsonRpcProtocolError("paginated MCP method returned non-object result")
        page = McpPage.from_result(result, item_key=item_key, request_cursor=cursor, page_index=page_index)
        items.extend(parse_item(item) for item in page.items)
        if len(items) > max_items:
            raise JsonRpcProtocolError(
                f"paginated MCP result exceeds {max_items} items",
                code=JsonRpcErrorCode.CONTENT_TOO_LARGE,
            )
        if page.terminal:
            return PaginationResult(tuple(items), tuple(cursors), page_index + 1)
        if page.next_cursor in seen or page.next_cursor == cursor:
            raise JsonRpcProtocolError("MCP pagination cursor cycle detected")
        seen.add(page.next_cursor)
        cursors.append(page.next_cursor)
        cursor = page.next_cursor
    raise JsonRpcProtocolError(
        f"MCP pagination exceeded {max_pages} pages",
        code=JsonRpcErrorCode.CONTENT_TOO_LARGE,
    )


@dataclass(frozen=True, slots=True)
class ProtocolObservation:
    direction: str
    kind: JsonRpcMessageKind
    method: str
    request_id: str
    safe_payload: Mapping[str, JsonValue]
    created_monotonic: float = field(default_factory=time.monotonic)

    @classmethod
    def from_message(cls, direction: str, message: JsonRpcMessage) -> "ProtocolObservation":
        method = message.method if isinstance(message, JsonRpcRequest | JsonRpcNotification) else ""
        request_id = str(message.request_id) if isinstance(message, JsonRpcRequest | JsonRpcSuccessResponse | JsonRpcErrorResponse) else ""
        return cls(direction, message.kind, method, request_id, safe_json_rpc_dict(message))


class ObservationBuffer:
    """Bounded redacted protocol trace for transport diagnostics."""

    def __init__(self, capacity: int = 256) -> None:
        if capacity <= 0:
            raise ValueError("observation capacity must be positive")
        self._items: deque[ProtocolObservation] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def append(self, direction: str, message: JsonRpcMessage) -> ProtocolObservation:
        item = ProtocolObservation.from_message(direction, message)
        with self._lock:
            self._items.append(item)
        return item

    def snapshot(self) -> tuple[ProtocolObservation, ...]:
        with self._lock:
            return tuple(self._items)

    def clear(self) -> int:
        with self._lock:
            count = len(self._items)
            self._items.clear()
            return count
