from __future__ import annotations

"""Long-lived durable StructuredIO hub shared by API, CLI and remote clients."""

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import Condition, RLock
from typing import Any, Callable, Mapping, Protocol, Sequence

from zyra_core import new_id, now_iso

from .dispatcher import RuntimeControlContext, RuntimeControlDispatcher
from .schemas import CommandOrigin, ControlCommandRequest, ControlCommandResponse


class ControlHubError(RuntimeError):
    pass


class ControlHubDisabled(ControlHubError):
    pass


class ControlFrameInvalid(ControlHubError, ValueError):
    pass


class ControlFrameConflict(ControlHubError):
    pass


class ControlFrameType(StrEnum):
    INITIALIZE = "initialize"
    REQUEST = "control_request"
    RESPONSE = "control_response"
    CANCEL = "control_cancel_request"
    EVENT = "stream_event"
    KEEPALIVE = "keepalive"
    CLOSE = "close"


class ControlFrameStatus(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    DISPATCHED = "dispatched"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def terminal(self) -> bool:
        return self in {
            ControlFrameStatus.RESOLVED,
            ControlFrameStatus.CANCELLED,
            ControlFrameStatus.REJECTED,
        }


@dataclass(frozen=True, slots=True)
class DurableControlFrame:
    frame_id: str
    stream_id: str
    message_id: str
    correlation_id: str
    frame_type: ControlFrameType
    protocol_version: int
    inbound_sequence: int
    outbound_sequence: int
    payload: Mapping[str, Any]
    payload_digest: str
    status: ControlFrameStatus
    request_id: str = ""
    response: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    revision: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        for name in ("frame_id", "stream_id", "message_id", "correlation_id", "payload_digest"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.protocol_version <= 0 or self.inbound_sequence < 0 or self.outbound_sequence < 0:
            raise ValueError("control frame counters are invalid")
        object.__setattr__(self, "payload", _safe_mapping(self.payload))
        object.__setattr__(self, "response", _safe_mapping(self.response))

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "stream_id": self.stream_id,
            "message_id": self.message_id,
            "correlation_id": self.correlation_id,
            "frame_type": self.frame_type.value,
            "protocol_version": self.protocol_version,
            "inbound_sequence": self.inbound_sequence,
            "outbound_sequence": self.outbound_sequence,
            "payload": _safe_mapping(self.payload),
            "payload_digest": self.payload_digest,
            "status": self.status.value,
            "request_id": self.request_id,
            "response": _safe_mapping(self.response),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DurableControlFrame":
        return cls(
            frame_id=str(value.get("frame_id") or new_id("controlframe")),
            stream_id=str(value.get("stream_id") or ""),
            message_id=str(value.get("message_id") or ""),
            correlation_id=str(value.get("correlation_id") or ""),
            frame_type=ControlFrameType(str(value.get("frame_type") or ControlFrameType.REQUEST.value)),
            protocol_version=max(1, int(value.get("protocol_version") or 1)),
            inbound_sequence=max(0, int(value.get("inbound_sequence") or 0)),
            outbound_sequence=max(0, int(value.get("outbound_sequence") or 0)),
            payload=_safe_mapping(value.get("payload")),
            payload_digest=str(value.get("payload_digest") or ""),
            status=ControlFrameStatus(str(value.get("status") or ControlFrameStatus.RECEIVED.value)),
            request_id=str(value.get("request_id") or ""),
            response=_safe_mapping(value.get("response")),
            error_code=str(value.get("error_code") or ""),
            error_message=str(value.get("error_message") or ""),
            revision=max(0, int(value.get("revision") or 0)),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
        )


@dataclass(frozen=True, slots=True)
class ControlStreamState:
    stream_id: str
    protocol_version: int
    last_inbound_sequence: int = 0
    last_outbound_sequence: int = 0
    initialized: bool = False
    closed: bool = False
    client_id: str = ""
    session_id: str = ""
    run_id: str = ""
    task_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "protocol_version": self.protocol_version,
            "last_inbound_sequence": self.last_inbound_sequence,
            "last_outbound_sequence": self.last_outbound_sequence,
            "initialized": self.initialized,
            "closed": self.closed,
            "client_id": self.client_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "metadata": _safe_mapping(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ControlStreamState":
        return cls(
            stream_id=str(value.get("stream_id") or ""),
            protocol_version=max(1, int(value.get("protocol_version") or 1)),
            last_inbound_sequence=max(0, int(value.get("last_inbound_sequence") or 0)),
            last_outbound_sequence=max(0, int(value.get("last_outbound_sequence") or 0)),
            initialized=bool(value.get("initialized", False)),
            closed=bool(value.get("closed", False)),
            client_id=str(value.get("client_id") or ""),
            session_id=str(value.get("session_id") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            metadata=_safe_mapping(value.get("metadata")),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
        )


class ControlFrameStore:
    schema = "zyra.control-frame-store/v1"

    def __init__(self, path: str | Path, *, disabled: bool = False, maximum_frames: int = 100_000) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.disabled = bool(disabled)
        self.maximum_frames = max(1024, int(maximum_frames))
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._streams: dict[str, ControlStreamState] = {}
        self._frames: dict[str, DurableControlFrame] = {}
        self._by_message: dict[str, str] = {}
        self._outbound: dict[str, list[str]] = {}
        self._load()

    def receive(self, envelope: Mapping[str, Any]) -> DurableControlFrame:
        self._require_enabled()
        with self._lock:
            parsed = self._parse(envelope)
            dedupe_key = f"{parsed.stream_id}:{parsed.message_id}"
            existing_id = self._by_message.get(dedupe_key)
            if existing_id:
                existing = self._frames[existing_id]
                if existing.payload_digest != parsed.payload_digest or existing.frame_type is not parsed.frame_type:
                    raise ControlFrameConflict("message_id reused with different control frame")
                return copy.deepcopy(existing)
            stream = self._streams.get(parsed.stream_id)
            if stream is None:
                stream = ControlStreamState(stream_id=parsed.stream_id, protocol_version=parsed.protocol_version)
            if stream.closed:
                raise ControlFrameInvalid("control stream is closed")
            expected = stream.last_inbound_sequence + 1
            if parsed.inbound_sequence != expected:
                raise ControlFrameConflict(
                    f"inbound sequence mismatch: expected {expected}, actual {parsed.inbound_sequence}"
                )
            if parsed.protocol_version != stream.protocol_version:
                raise ControlFrameInvalid("control protocol version changed within stream")
            stream = replace(stream, last_inbound_sequence=parsed.inbound_sequence, updated_at=now_iso())
            self._streams[stream.stream_id] = stream
            self._frames[parsed.frame_id] = parsed
            self._by_message[dedupe_key] = parsed.frame_id
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(parsed)

    def mark(self, frame_id: str, status: ControlFrameStatus, *, request_id: str = "", error: Exception | None = None) -> DurableControlFrame:
        self._require_enabled()
        with self._lock:
            current = self._require_frame(frame_id)
            if current.terminal:
                if current.status is status:
                    return copy.deepcopy(current)
                raise ControlFrameConflict("control frame is terminal")
            changed = replace(
                current,
                status=status,
                request_id=request_id or current.request_id,
                error_code=type(error).__name__ if error else current.error_code,
                error_message=str(error) if error else current.error_message,
                revision=current.revision + 1,
                updated_at=now_iso(),
            )
            self._frames[frame_id] = changed
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(changed)

    def resolve(self, frame_id: str, response: Mapping[str, Any], *, cancelled: bool = False) -> DurableControlFrame:
        self._require_enabled()
        with self._lock:
            current = self._require_frame(frame_id)
            if current.terminal:
                if current.response == _safe_mapping(response):
                    return copy.deepcopy(current)
                raise ControlFrameConflict("control frame already resolved differently")
            stream = self._streams[current.stream_id]
            outbound_sequence = stream.last_outbound_sequence + 1
            status = ControlFrameStatus.CANCELLED if cancelled else ControlFrameStatus.RESOLVED
            changed = replace(
                current,
                status=status,
                response=_safe_mapping(response),
                outbound_sequence=outbound_sequence,
                revision=current.revision + 1,
                updated_at=now_iso(),
            )
            self._frames[frame_id] = changed
            self._streams[current.stream_id] = replace(
                stream,
                last_outbound_sequence=outbound_sequence,
                updated_at=now_iso(),
            )
            self._outbound.setdefault(current.stream_id, []).append(frame_id)
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(changed)

    def initialize_stream(self, frame_id: str, *, client_id: str, session_id: str, run_id: str, task_id: str, metadata: Mapping[str, Any] | None = None) -> ControlStreamState:
        self._require_enabled()
        with self._lock:
            frame = self._require_frame(frame_id)
            stream = self._streams[frame.stream_id]
            if stream.initialized:
                identity = (stream.client_id, stream.session_id, stream.run_id, stream.task_id)
                if identity != (client_id, session_id, run_id, task_id):
                    raise ControlFrameConflict("control stream initialize identity changed")
                return copy.deepcopy(stream)
            changed = replace(
                stream,
                initialized=True,
                client_id=client_id,
                session_id=session_id,
                run_id=run_id,
                task_id=task_id,
                metadata=_safe_mapping(metadata),
                updated_at=now_iso(),
            )
            self._streams[stream.stream_id] = changed
            self._persist()
            return copy.deepcopy(changed)

    def close_stream(self, stream_id: str) -> ControlStreamState:
        self._require_enabled()
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                raise ControlFrameInvalid("control stream not found")
            changed = replace(stream, closed=True, updated_at=now_iso())
            self._streams[stream_id] = changed
            self._persist()
            return copy.deepcopy(changed)

    def get(self, frame_id: str) -> DurableControlFrame:
        self._require_enabled()
        with self._lock:
            return copy.deepcopy(self._require_frame(frame_id))

    def outbound(self, stream_id: str, *, after_sequence: int = 0, limit: int = 100) -> tuple[DurableControlFrame, ...]:
        self._require_enabled()
        with self._lock:
            values = [
                self._frames[frame_id]
                for frame_id in self._outbound.get(stream_id, ())
                if self._frames[frame_id].outbound_sequence > after_sequence
            ]
            values.sort(key=lambda item: item.outbound_sequence)
            return tuple(copy.deepcopy(item) for item in values[: max(1, min(int(limit), 1000))])

    def recover(self) -> tuple[DurableControlFrame, ...]:
        """Reject uncertain dispatched frames instead of replaying effects."""

        recovered = []
        with self._lock:
            for frame_id, current in list(self._frames.items()):
                if current.status is ControlFrameStatus.DISPATCHED:
                    changed = replace(
                        current,
                        status=ControlFrameStatus.REJECTED,
                        error_code="control_effect_outcome_unknown_after_restart",
                        error_message="dispatcher receipt must be queried; frame will not be replayed",
                        revision=current.revision + 1,
                        updated_at=now_iso(),
                    )
                    self._frames[frame_id] = changed
                    recovered.append(copy.deepcopy(changed))
            if recovered:
                self._persist()
                self._changed.notify_all()
        return tuple(recovered)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            frames = sorted(self._frames.values(), key=lambda item: (item.created_at, item.frame_id))[-self.maximum_frames :]
            body = {
                "schema": self.schema,
                "owner": "M1-03D ControlFrameStore",
                "streams": [item.to_dict() for item in sorted(self._streams.values(), key=lambda value: value.stream_id)],
                "frames": [item.to_dict() for item in frames],
                "by_message": dict(sorted(self._by_message.items())),
                "outbound": {key: list(values) for key, values in sorted(self._outbound.items())},
            }
            return {**body, "checksum": _digest(body)}

    def _parse(self, envelope: Mapping[str, Any]) -> DurableControlFrame:
        version = int(envelope.get("protocol_version") or envelope.get("version") or 0)
        if version != 1:
            raise ControlFrameInvalid(f"unsupported control protocol version: {version}")
        stream_id = str(envelope.get("stream_id") or "").strip()
        message_id = str(envelope.get("message_id") or "").strip()
        correlation_id = str(envelope.get("correlation_id") or envelope.get("request_id") or message_id).strip()
        sequence = int(envelope.get("sequence") or 0)
        if not stream_id or not message_id or not correlation_id or sequence <= 0:
            raise ControlFrameInvalid("stream_id, message_id, correlation_id and positive sequence are required")
        try:
            frame_type = ControlFrameType(str(envelope.get("type") or envelope.get("frame_type") or ""))
        except ValueError as error:
            raise ControlFrameInvalid("unsupported control frame type") from error
        payload = _safe_mapping(envelope.get("payload"))
        if _char_size(payload) > 2_000_000:
            raise ControlFrameInvalid("control frame payload exceeds limit")
        return DurableControlFrame(
            frame_id=new_id("controlframe"),
            stream_id=stream_id,
            message_id=message_id,
            correlation_id=correlation_id,
            frame_type=frame_type,
            protocol_version=version,
            inbound_sequence=sequence,
            outbound_sequence=0,
            payload=payload,
            payload_digest=_digest(payload),
            status=ControlFrameStatus.RECEIVED,
        )

    def _require_frame(self, frame_id: str) -> DurableControlFrame:
        value = self._frames.get(frame_id)
        if value is None:
            raise ControlFrameInvalid(f"control frame not found: {frame_id}")
        return value

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ControlHubError(f"cannot load control frame store: {error}") from error
        if not isinstance(value, Mapping):
            raise ControlHubError("control frame store root must be an object")
        body = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
        expected = str(value.get("checksum") or "")
        if expected and expected != _digest(body):
            raise ControlHubError("control frame store checksum mismatch")
        for item in value.get("streams") or ():
            if isinstance(item, Mapping):
                stream = ControlStreamState.from_dict(item)
                self._streams[stream.stream_id] = stream
        for item in value.get("frames") or ():
            if isinstance(item, Mapping):
                frame = DurableControlFrame.from_dict(item)
                self._frames[frame.frame_id] = frame
        raw_by_message = value.get("by_message")
        if isinstance(raw_by_message, Mapping):
            self._by_message = {str(key): str(item) for key, item in raw_by_message.items()}
        raw_outbound = value.get("outbound")
        if isinstance(raw_outbound, Mapping):
            self._outbound = {
                str(key): [str(item) for item in values]
                for key, values in raw_outbound.items()
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes))
            }

    def _persist(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise ControlHubDisabled("ControlFrameStore is disabled")


class ControlContextResolver(Protocol):
    def __call__(self, run_id: str, task_id: str, session_id: str) -> RuntimeControlContext: ...


class StructuredControlHub:
    def __init__(
        self,
        store: ControlFrameStore,
        dispatcher: RuntimeControlDispatcher,
        context_resolver: ControlContextResolver,
        *,
        registry_snapshot: Callable[[], Mapping[str, Any]] | None = None,
        disabled: bool = False,
    ) -> None:
        self.store = store
        self.dispatcher = dispatcher
        self.context_resolver = context_resolver
        self.registry_snapshot = registry_snapshot
        self.disabled = bool(disabled)

    def handle(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require_enabled()
        frame = self.store.receive(envelope)
        if frame.terminal:
            return self._outbound(frame)
        try:
            self.store.mark(frame.frame_id, ControlFrameStatus.VALIDATED)
            if frame.frame_type is ControlFrameType.INITIALIZE:
                response = self._initialize(frame)
                resolved = self.store.resolve(frame.frame_id, response)
            elif frame.frame_type is ControlFrameType.REQUEST:
                resolved = self._request(frame)
            elif frame.frame_type is ControlFrameType.CANCEL:
                resolved = self._cancel(frame)
            elif frame.frame_type is ControlFrameType.KEEPALIVE:
                resolved = self.store.resolve(frame.frame_id, {"ok": True, "keepalive": True, "at": now_iso()})
            elif frame.frame_type is ControlFrameType.CLOSE:
                self.store.close_stream(frame.stream_id)
                resolved = self.store.resolve(frame.frame_id, {"ok": True, "closed": True})
            elif frame.frame_type is ControlFrameType.RESPONSE:
                raise ControlFrameInvalid("host-to-Zyra control_response is unsupported on this endpoint")
            else:
                raise ControlFrameInvalid(f"unsupported frame type {frame.frame_type.value}")
            return self._outbound(resolved)
        except Exception as error:
            try:
                self.store.mark(frame.frame_id, ControlFrameStatus.REJECTED, error=error)
                rejected = self.store.resolve(frame.frame_id, {
                    "ok": False,
                    "error_code": type(error).__name__,
                    "error_message": str(error),
                })
            except Exception:
                raise error
            return self._outbound(rejected)

    def _initialize(self, frame: DurableControlFrame) -> Mapping[str, Any]:
        payload = frame.payload
        stream = self.store.initialize_stream(
            frame.frame_id,
            client_id=str(payload.get("client_id") or "anonymous"),
            session_id=str(payload.get("session_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            metadata=_safe_mapping(payload.get("metadata")),
        )
        return {
            "ok": True,
            "initialized": True,
            "stream": stream.to_dict(),
            "commands": _safe_mapping(self.registry_snapshot()) if self.registry_snapshot else {},
        }

    def _request(self, frame: DurableControlFrame) -> DurableControlFrame:
        payload = frame.payload
        request_raw = _mapping(payload.get("request")) or payload
        request = ControlCommandRequest.from_dict({
            **request_raw,
            "origin": str(request_raw.get("origin") or CommandOrigin.REMOTE.value),
        })
        context = self.context_resolver(request.run_id, request.task_id, request.session_id)
        self.store.mark(frame.frame_id, ControlFrameStatus.DISPATCHED, request_id=request.request_id)
        response = self.dispatcher.submit(request, context)
        return self.store.resolve(frame.frame_id, response.to_dict())

    def _cancel(self, frame: DurableControlFrame) -> DurableControlFrame:
        payload = frame.payload
        request_id = str(payload.get("request_id") or payload.get("target_request_id") or "")
        run_id = str(payload.get("run_id") or "")
        task_id = str(payload.get("task_id") or "")
        session_id = str(payload.get("session_id") or "")
        if not request_id or not run_id or not task_id or not session_id:
            raise ControlFrameInvalid("cancel requires request_id, run_id, task_id and session_id")
        context = self.context_resolver(run_id, task_id, session_id)
        response = self.dispatcher.cancel(
            request_id,
            reason=str(payload.get("reason") or "structured_control_cancelled"),
            context=context,
        )
        return self.store.resolve(frame.frame_id, response.to_dict(), cancelled=True)

    @staticmethod
    def _outbound(frame: DurableControlFrame) -> Mapping[str, Any]:
        return {
            "protocol_version": frame.protocol_version,
            "stream_id": frame.stream_id,
            "message_id": new_id("outbound"),
            "correlation_id": frame.correlation_id,
            "sequence": frame.outbound_sequence,
            "type": ControlFrameType.RESPONSE.value,
            "payload": _safe_mapping(frame.response),
            "frame_id": frame.frame_id,
            "status": frame.status.value,
        }

    def _require_enabled(self) -> None:
        if self.disabled:
            raise ControlHubDisabled("StructuredControlHub is disabled")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        name = str(key)
        if any(token in name.casefold() for token in ("secret", "token", "password", "authorization", "credential")):
            result[name] = "<redacted>"
        elif isinstance(item, Mapping):
            result[name] = _safe_mapping(item)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            result[name] = [_safe_value(entry) for entry in item]
        else:
            result[name] = _safe_value(item)
    return result


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return _safe_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_value(item) for item in value]
    return str(value)


def _char_size(value: Any) -> int:
    return len(json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True))


def _digest(value: Any) -> str:
    payload = json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


__all__ = [
    "ControlContextResolver",
    "ControlFrameConflict",
    "ControlFrameInvalid",
    "ControlFrameStatus",
    "ControlFrameStore",
    "ControlFrameType",
    "ControlHubDisabled",
    "ControlHubError",
    "ControlStreamState",
    "DurableControlFrame",
    "StructuredControlHub",
]
