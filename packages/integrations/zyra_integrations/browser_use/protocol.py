from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CdpProtocolError(RuntimeError):
    """Raised when a CDP wire payload violates the protocol contract."""

    def __init__(self, message: str, *, payload: Any = None) -> None:
        super().__init__(message)
        self.payload = payload


class CdpWireKind(StrEnum):
    COMMAND = "command"
    RESPONSE = "response"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class CdpSessionRoute:
    target_id: str = ""
    session_id: str = ""
    frame_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "target_id": self.target_id,
            "session_id": self.session_id,
            "frame_id": self.frame_id,
        }


@dataclass(frozen=True, slots=True)
class CdpCommand:
    request_id: int
    method: str
    params: Mapping[str, Any] = field(default_factory=dict)
    route: CdpSessionRoute = field(default_factory=CdpSessionRoute)

    def __post_init__(self) -> None:
        if self.request_id <= 0:
            raise ValueError("CDP request id must be positive")
        if not self.method or "." not in self.method:
            raise ValueError("CDP method must contain a domain and operation")
        object.__setattr__(self, "params", _json_mapping(self.params, label="CDP command params"))

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.request_id,
            "method": self.method,
            "params": dict(self.params),
        }
        if self.route.session_id:
            payload["sessionId"] = self.route.session_id
        return payload


@dataclass(frozen=True, slots=True)
class CdpError:
    code: int
    message: str
    data: Any = None

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> "CdpError":
        try:
            code = int(value.get("code", -1))
        except (TypeError, ValueError):
            code = -1
        return cls(code=code, message=str(value.get("message") or "unknown CDP error"), data=value.get("data"))

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "data": _json_value(self.data)}


@dataclass(frozen=True, slots=True)
class CdpResponse:
    request_id: int
    result: Mapping[str, Any] = field(default_factory=dict)
    error: CdpError | None = None
    route: CdpSessionRoute = field(default_factory=CdpSessionRoute)

    @property
    def ok(self) -> bool:
        return self.error is None

    def require_result(self) -> Mapping[str, Any]:
        if self.error is not None:
            raise CdpProtocolError(
                f"CDP request {self.request_id} failed: {self.error.message}",
                payload=self.error.to_dict(),
            )
        return self.result

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "ok": self.ok,
            "result": dict(self.result),
            "error": self.error.to_dict() if self.error else None,
            "route": self.route.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CdpEvent:
    method: str
    params: Mapping[str, Any] = field(default_factory=dict)
    route: CdpSessionRoute = field(default_factory=CdpSessionRoute)

    def __post_init__(self) -> None:
        if not self.method:
            raise ValueError("CDP event method is required")
        object.__setattr__(self, "params", _json_mapping(self.params, label="CDP event params"))

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method, "params": dict(self.params), "route": self.route.to_dict()}


@dataclass(frozen=True, slots=True)
class CdpWireMessage:
    kind: CdpWireKind
    command: CdpCommand | None = None
    response: CdpResponse | None = None
    event: CdpEvent | None = None

    def __post_init__(self) -> None:
        values = [self.command is not None, self.response is not None, self.event is not None]
        if sum(values) != 1:
            raise ValueError("CDP wire message must contain exactly one payload")
        expected = {
            CdpWireKind.COMMAND: self.command is not None,
            CdpWireKind.RESPONSE: self.response is not None,
            CdpWireKind.EVENT: self.event is not None,
        }
        if not expected[self.kind]:
            raise ValueError("CDP wire message kind does not match payload")


class RequestIdAllocator:
    """Thread-safe monotonic request id allocator.

    CDP ids are scoped to a connection.  Reconnect creates a new allocator
    generation, preventing a late response from satisfying a new request.
    """

    def __init__(self, *, first: int = 1, maximum: int = 2_147_483_647) -> None:
        if first <= 0 or maximum < first:
            raise ValueError("invalid request id range")
        self._first = first
        self._next = first
        self._maximum = maximum
        self._generation = 0
        self._lock = threading.Lock()

    def allocate(self) -> tuple[int, int]:
        with self._lock:
            value = self._next
            self._next += 1
            if self._next > self._maximum:
                self._next = self._first
                self._generation += 1
            return self._generation, value

    def reset(self) -> int:
        with self._lock:
            self._next = self._first
            self._generation += 1
            return self._generation

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation


class CdpMessageCodec:
    """Strict JSON codec for flattened CDP sessions."""

    def encode_command(self, command: CdpCommand) -> str:
        return json.dumps(command.to_wire(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def encode_mapping(self, payload: Mapping[str, Any]) -> str:
        return json.dumps(_json_mapping(payload, label="CDP payload"), ensure_ascii=False, separators=(",", ":"))

    def decode(self, payload: str | bytes | bytearray | memoryview) -> CdpWireMessage:
        if isinstance(payload, (bytes, bytearray, memoryview)):
            try:
                text = bytes(payload).decode("utf-8")
            except UnicodeDecodeError as error:
                raise CdpProtocolError("CDP payload is not UTF-8") from error
        else:
            text = payload
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as error:
            raise CdpProtocolError("CDP payload is not valid JSON", payload=text[:500]) from error
        if not isinstance(decoded, Mapping):
            raise CdpProtocolError("CDP payload must be an object", payload=decoded)
        return self.decode_mapping(decoded)

    def decode_mapping(self, decoded: Mapping[str, Any]) -> CdpWireMessage:
        route = CdpSessionRoute(
            target_id=str(decoded.get("targetId") or ""),
            session_id=str(decoded.get("sessionId") or ""),
            frame_id=str(decoded.get("frameId") or ""),
        )
        if "method" in decoded and "id" not in decoded:
            event = CdpEvent(
                method=str(decoded.get("method") or ""),
                params=_mapping_or_empty(decoded.get("params")),
                route=route,
            )
            return CdpWireMessage(kind=CdpWireKind.EVENT, event=event)
        if "id" in decoded and "method" not in decoded:
            request_id = _positive_id(decoded.get("id"))
            error_value = decoded.get("error")
            error = CdpError.from_wire(error_value) if isinstance(error_value, Mapping) else None
            response = CdpResponse(
                request_id=request_id,
                result=_mapping_or_empty(decoded.get("result")),
                error=error,
                route=route,
            )
            return CdpWireMessage(kind=CdpWireKind.RESPONSE, response=response)
        if "id" in decoded and "method" in decoded:
            command = CdpCommand(
                request_id=_positive_id(decoded.get("id")),
                method=str(decoded.get("method") or ""),
                params=_mapping_or_empty(decoded.get("params")),
                route=route,
            )
            return CdpWireMessage(kind=CdpWireKind.COMMAND, command=command)
        raise CdpProtocolError("unrecognized CDP wire message", payload=dict(decoded))


def _positive_id(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise CdpProtocolError("CDP request id is not an integer", payload=value) from error
    if parsed <= 0:
        raise CdpProtocolError("CDP request id must be positive", payload=value)
    return parsed


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_mapping(value: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    converted = _json_value(dict(value))
    if not isinstance(converted, dict):
        raise TypeError(f"{label} must be a mapping")
    return converted


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")
