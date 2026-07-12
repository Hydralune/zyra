from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Mapping

from zyra_core import new_id, now_iso

from .schemas import (
    CommandStatus,
    ControlCommandRequest,
    ControlCommandResponse,
    ControlError,
    ControlErrorCode,
)


class StructuredMessageType(str):
    CONTROL_REQUEST = "control_request"
    CONTROL_RESPONSE = "control_response"
    CONTROL_CANCEL_REQUEST = "control_cancel_request"
    CONTROL_CANCEL_RESPONSE = "control_cancel_response"
    STREAM_EVENT = "stream_event"
    KEEPALIVE = "keepalive"


@dataclass(frozen=True, slots=True)
class StructuredEnvelope:
    message_type: str
    payload: dict[str, Any]
    message_id: str = field(default_factory=lambda: new_id("structured"))
    protocol_version: str = "zyra.structured-io/v1"
    sequence: int = 0
    correlation_id: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "message_type": self.message_type,
            "message_id": self.message_id,
            "sequence": self.sequence,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
            "payload": copy.deepcopy(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StructuredEnvelope":
        version = str(raw.get("protocol_version") or "")
        if version != "zyra.structured-io/v1":
            raise ValueError(f"unsupported structured IO version: {version}")
        message_type = str(raw.get("message_type") or raw.get("type") or "")
        if message_type not in {
            StructuredMessageType.CONTROL_REQUEST,
            StructuredMessageType.CONTROL_RESPONSE,
            StructuredMessageType.CONTROL_CANCEL_REQUEST,
            StructuredMessageType.CONTROL_CANCEL_RESPONSE,
            StructuredMessageType.STREAM_EVENT,
            StructuredMessageType.KEEPALIVE,
        }:
            raise ValueError(f"unknown structured message type: {message_type}")
        payload = raw.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("structured IO payload must be an object")
        return cls(
            message_type=message_type,
            payload=dict(payload),
            message_id=str(raw.get("message_id") or new_id("structured")),
            protocol_version=version,
            sequence=int(raw.get("sequence") or 0),
            correlation_id=str(raw.get("correlation_id") or ""),
            created_at=str(raw.get("created_at") or now_iso()),
        )


@dataclass(slots=True)
class PendingControl:
    request: ControlCommandRequest
    waiter: Event = field(default_factory=Event, repr=False)
    response: ControlCommandResponse | None = None
    cancelled: bool = False
    created_at: str = field(default_factory=now_iso)


class StructuredControlIO:
    """Versioned NDJSON control protocol with ordered output and dedupe."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        dispatcher: Callable[[ControlCommandRequest], ControlCommandResponse] | None = None,
        cancel_callback: Callable[[str], bool] | None = None,
        disabled: bool = False,
    ) -> None:
        self.state_path = Path(state_path).resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.dispatcher = dispatcher
        self.cancel_callback = cancel_callback
        self.disabled = disabled
        self._lock = RLock()
        self._pending: dict[str, PendingControl] = {}
        self._resolved: dict[str, dict[str, Any]] = {}
        self._outbound: list[StructuredEnvelope] = []
        self._sequence = 0
        self._closed = False
        self._audit: list[dict[str, Any]] = []
        self._load()

    def decode_line(self, line: str) -> StructuredEnvelope:
        self._require_open()
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("structured IO line is not valid JSON") from error
        if not isinstance(raw, Mapping):
            raise ValueError("structured IO line must contain an object")
        return StructuredEnvelope.from_dict(raw)

    def encode(self, envelope: StructuredEnvelope) -> str:
        return json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def handle_line(self, line: str) -> StructuredEnvelope | None:
        envelope = self.decode_line(line)
        return self.handle(envelope)

    def handle(self, envelope: StructuredEnvelope) -> StructuredEnvelope | None:
        self._require_open()
        if envelope.message_type == StructuredMessageType.CONTROL_REQUEST:
            return self._handle_request(envelope)
        if envelope.message_type == StructuredMessageType.CONTROL_RESPONSE:
            self.inject_response(ControlCommandResponse.from_dict(envelope.payload))
            return None
        if envelope.message_type == StructuredMessageType.CONTROL_CANCEL_REQUEST:
            return self._handle_cancel(envelope)
        if envelope.message_type in {StructuredMessageType.STREAM_EVENT, StructuredMessageType.KEEPALIVE}:
            self._audit.append({
                "kind": envelope.message_type,
                "message_id": envelope.message_id,
                "sequence": envelope.sequence,
                "created_at": now_iso(),
            })
            return None
        return None

    def send_request(self, request: ControlCommandRequest) -> StructuredEnvelope:
        self._require_open()
        with self._lock:
            if request.request_id in self._pending:
                existing = self._pending[request.request_id]
                if existing.request.body_digest != request.body_digest:
                    raise ValueError("pending request body conflict")
                return self._enqueue(
                    StructuredMessageType.CONTROL_REQUEST,
                    existing.request.to_dict(),
                    correlation_id=request.request_id,
                )
            if request.request_id in self._resolved:
                raise ValueError("control request was already resolved")
            self._pending[request.request_id] = PendingControl(request=request)
            return self._enqueue(
                StructuredMessageType.CONTROL_REQUEST,
                request.to_dict(),
                correlation_id=request.request_id,
            )

    def inject_response(self, response: ControlCommandResponse) -> bool:
        self._require_open()
        with self._lock:
            existing = self._resolved.get(response.request_id)
            if existing is not None:
                if existing == response.to_dict():
                    self._audit.append({
                        "kind": "duplicate_response_ignored",
                        "request_id": response.request_id,
                        "created_at": now_iso(),
                    })
                    return False
                raise ValueError("duplicate response conflicts with the durable resolved result")
            pending = self._pending.get(response.request_id)
            if pending is None:
                self._audit.append({
                    "kind": "orphan_response",
                    "request_id": response.request_id,
                    "response": response.to_dict(),
                    "created_at": now_iso(),
                })
                self._resolved[response.request_id] = response.to_dict()
                self._persist()
                return False
            if pending.response is not None:
                if pending.response.to_dict() == response.to_dict():
                    return False
                raise ValueError("pending request received conflicting responses")
            pending.response = response
            pending.waiter.set()
            self._resolved[response.request_id] = response.to_dict()
            self._pending.pop(response.request_id, None)
            self._persist()
            return True

    def wait(self, request_id: str, timeout: float | None = None) -> ControlCommandResponse | None:
        with self._lock:
            resolved = self._resolved.get(request_id)
            if resolved is not None:
                return ControlCommandResponse.from_dict(resolved)
            pending = self._pending.get(request_id)
            if pending is None:
                return None
            waiter = pending.waiter
        waiter.wait(timeout=timeout)
        with self._lock:
            resolved = self._resolved.get(request_id)
            return ControlCommandResponse.from_dict(resolved) if resolved else None

    def cancel(self, request_id: str) -> bool:
        self._require_open()
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            pending.cancelled = True
        callback_result = self.cancel_callback(request_id) if self.cancel_callback else False
        response = ControlCommandResponse(
            request_id=request_id,
            command_id=pending.request.command_id,
            status=CommandStatus.CANCELLED,
            registry_generation=pending.request.registry_generation,
            error=ControlError(ControlErrorCode.CANCELLED, "control request was cancelled"),
            metadata={"cancel_callback_applied": callback_result},
        )
        self.inject_response(response)
        return True

    def write_stream_event(self, payload: Mapping[str, Any], *, correlation_id: str = "") -> StructuredEnvelope:
        self._require_open()
        with self._lock:
            return self._enqueue(StructuredMessageType.STREAM_EVENT, dict(payload), correlation_id=correlation_id)

    def drain_outbound(self, *, limit: int | None = None) -> tuple[StructuredEnvelope, ...]:
        with self._lock:
            size = len(self._outbound) if limit is None else max(0, min(limit, len(self._outbound)))
            selected = tuple(self._outbound[:size])
            del self._outbound[:size]
            return selected

    def close(self, *, reason: str = "structured input closed") -> None:
        with self._lock:
            if self._closed:
                return
            pending = list(self._pending.values())
            self._closed = True
        for item in pending:
            response = ControlCommandResponse(
                request_id=item.request.request_id,
                command_id=item.request.command_id,
                status=CommandStatus.FAILED,
                registry_generation=item.request.registry_generation,
                error=ControlError(ControlErrorCode.DEPENDENCY_UNAVAILABLE, reason, retryable=True),
            )
            with self._lock:
                self._resolved[item.request.request_id] = response.to_dict()
                self._pending.pop(item.request.request_id, None)
                item.response = response
                item.waiter.set()
        self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "zyra.structured-io-state/v1",
                "resolved": copy.deepcopy(self._resolved),
                "sequence": self._sequence,
                "closed": self._closed,
                "audit": copy.deepcopy(self._audit[-1000:]),
            }

    def _handle_request(self, envelope: StructuredEnvelope) -> StructuredEnvelope:
        request = ControlCommandRequest.from_dict(envelope.payload)
        if self.dispatcher is None:
            response = ControlCommandResponse(
                request_id=request.request_id,
                command_id=request.command_id,
                status=CommandStatus.FAILED,
                registry_generation=request.registry_generation,
                error=ControlError(ControlErrorCode.DEPENDENCY_UNAVAILABLE, "RuntimeControlDispatcher is unavailable"),
            )
        else:
            response = self.dispatcher(request)
        self._resolved[request.request_id] = response.to_dict()
        self._persist()
        return self._enqueue(
            StructuredMessageType.CONTROL_RESPONSE,
            response.to_dict(),
            correlation_id=request.request_id,
        )

    def _handle_cancel(self, envelope: StructuredEnvelope) -> StructuredEnvelope:
        request_id = str(envelope.payload.get("request_id") or "")
        cancelled = self.cancel(request_id)
        return self._enqueue(
            StructuredMessageType.CONTROL_CANCEL_RESPONSE,
            {"request_id": request_id, "cancelled": cancelled},
            correlation_id=request_id,
        )

    def _enqueue(self, message_type: str, payload: Mapping[str, Any], *, correlation_id: str) -> StructuredEnvelope:
        self._sequence += 1
        envelope = StructuredEnvelope(
            message_type=message_type,
            payload=dict(payload),
            sequence=self._sequence,
            correlation_id=correlation_id,
        )
        self._outbound.append(envelope)
        return envelope

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._resolved = {
            str(key): dict(value)
            for key, value in (raw.get("resolved") or {}).items()
            if isinstance(value, Mapping)
        }
        self._sequence = int(raw.get("sequence") or 0)
        self._audit = [dict(item) for item in raw.get("audit") or () if isinstance(item, Mapping)]

    def _persist(self) -> None:
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.state_path)

    def _require_open(self) -> None:
        if self.disabled:
            raise RuntimeError("StructuredControlIO is disabled")
        if self._closed:
            raise RuntimeError("StructuredControlIO is closed")

