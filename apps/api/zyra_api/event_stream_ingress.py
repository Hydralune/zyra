"""Read-only snapshot and cursor protocol for the browser event ingress.

The TypeScript runtime-event spine remains the canonical owner.  This module
only turns committed pages from that owner into resumable browser transport
frames.  It intentionally has no database, reducer, event renumbering, or
write fallback.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from http import HTTPStatus
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from zyra_runtime.runtime_events import (
    JsonValue,
    RuntimeEventContractError,
    RuntimeEventEnvelope,
    RuntimeEventQuery,
    RuntimeEventSpineBridge,
)


INGRESS_SCHEMA = "zyra.event-ingress/v1"
CAPABILITIES_SCHEMA = "zyra.event-ingress-capabilities/v1"
SNAPSHOT_SCHEMA = "zyra.event-ingress-snapshot/v1"
DELTA_SCHEMA = "zyra.event-ingress-delta/v1"
FRAME_SCHEMA = "zyra.event-ingress-frame/v1"
CURSOR_SCHEMA = "zyra.event-ingress-cursor/v1"
SSE_MEDIA_TYPE = "text/event-stream; charset=utf-8"
DEFAULT_PAGE_LIMIT = 256
MAX_PAGE_LIMIT = 1000
DEFAULT_WAIT_MS = 750
MAX_WAIT_MS = 25_000
DEFAULT_STREAM_MS = 15_000
MAX_STREAM_MS = 60_000
DEFAULT_HEARTBEAT_MS = 5_000
MIN_HEARTBEAT_MS = 250
CURSOR_TTL_MS = 30 * 60_000
MAX_CURSOR_BYTES = 4096
MAX_QUERY_TEXT_BYTES = 8192
MAX_FILTER_VALUES = 128
_PROCESS_CURSOR_SECRET = secrets.token_bytes(32)


class EventIngressError(RuntimeError):
    """A stable HTTP-facing protocol failure."""

    def __init__(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        details: Mapping[str, JsonValue] | None = None,
        retryable: bool = False,
        resync_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = dict(details or {})
        self.retryable = retryable
        self.resync_required = resync_required

    def response(self) -> dict[str, JsonValue]:
        return {
            "schema": INGRESS_SCHEMA,
            "ok": False,
            "error": self.code,
            "message": str(self),
            "details": dict(self.details),
            "retryable": self.retryable,
            "resyncRequired": self.resync_required,
            "canonicalWriteAllowed": False,
        }


class CursorKind(str, Enum):
    SNAPSHOT = "snapshot"
    DELTA = "delta"
    STREAM = "stream"


@dataclass(frozen=True, slots=True)
class IngressCursor:
    task_id: str
    sequence: int
    boundary: int
    generation: int
    kind: CursorKind
    issued_at_ms: int
    expires_at_ms: int
    filter_digest: str
    cursor_id: str
    schema: str = CURSOR_SCHEMA

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_task_required",
                "Cursor task id must not be empty.",
            )
        if self.sequence < 0 or self.boundary < 0:
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_sequence_invalid",
                "Cursor sequences must be non-negative.",
            )
        if self.generation < 1:
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_generation_invalid",
                "Cursor generation must be positive.",
            )
        if self.expires_at_ms <= self.issued_at_ms:
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_expiry_invalid",
                "Cursor expiry must follow its issue time.",
            )

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "schema": self.schema,
            "taskId": self.task_id,
            "sequence": self.sequence,
            "boundary": self.boundary,
            "generation": self.generation,
            "kind": self.kind.value,
            "issuedAtMs": self.issued_at_ms,
            "expiresAtMs": self.expires_at_ms,
            "filterDigest": self.filter_digest,
            "cursorId": self.cursor_id,
        }


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise EventIngressError(
            HTTPStatus.BAD_REQUEST,
            "cursor_encoding_invalid",
            "Event cursor is not valid URL-safe base64.",
            resync_required=True,
        ) from error


class EventCursorCodec:
    """Signs opaque, scoped, expiring cursors without owning cursor state."""

    def __init__(
        self,
        secret: bytes | str | None = None,
        *,
        ttl_ms: int = CURSOR_TTL_MS,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        configured = secret if secret is not None else os.environ.get("ZYRA_EVENT_CURSOR_SECRET")
        if isinstance(configured, str):
            material = configured.encode("utf-8")
        elif configured is None:
            material = _PROCESS_CURSOR_SECRET
        else:
            material = bytes(configured)
        if len(material) < 16:
            material = hashlib.sha256(material).digest()
        self._secret = hashlib.sha256(b"zyra.event-ingress.cursor\0" + material).digest()
        self._ttl_ms = max(30_000, min(int(ttl_ms), 24 * 60 * 60_000))
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def issue(
        self,
        *,
        task_id: str,
        sequence: int,
        boundary: int,
        generation: int,
        kind: CursorKind,
        filter_digest: str,
        cursor_id: str | None = None,
        issued_at_ms: int | None = None,
    ) -> tuple[str, IngressCursor]:
        now = self._now_ms() if issued_at_ms is None else int(issued_at_ms)
        cursor = IngressCursor(
            task_id=task_id.strip(),
            sequence=int(sequence),
            boundary=int(boundary),
            generation=int(generation),
            kind=kind,
            issued_at_ms=now,
            expires_at_ms=now + self._ttl_ms,
            filter_digest=filter_digest,
            cursor_id=cursor_id or secrets.token_hex(12),
        )
        encoded = _urlsafe_encode(_canonical_json(cursor.to_payload()))
        signature = _urlsafe_encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        token = f"{encoded}.{signature}"
        if len(token.encode("utf-8")) > MAX_CURSOR_BYTES:
            raise EventIngressError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "cursor_too_large",
                "Generated event cursor exceeds the protocol limit.",
            )
        return token, cursor

    def decode(
        self,
        token: str,
        *,
        task_id: str,
        filter_digest: str,
        allowed_kinds: Iterable[CursorKind] | None = None,
    ) -> IngressCursor:
        rendered = str(token or "").strip()
        if not rendered:
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_required",
                "An event cursor is required.",
                resync_required=True,
            )
        if len(rendered.encode("utf-8")) > MAX_CURSOR_BYTES:
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_too_large",
                "Event cursor exceeds the protocol limit.",
                resync_required=True,
            )
        encoded, separator, supplied_signature = rendered.partition(".")
        if not separator or not encoded or not supplied_signature:
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_format_invalid",
                "Event cursor has an invalid format.",
                resync_required=True,
            )
        expected_signature = _urlsafe_encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_signature_invalid",
                "Event cursor signature is invalid.",
                resync_required=True,
            )
        try:
            payload = json.loads(_urlsafe_decode(encoded).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_payload_invalid",
                "Event cursor payload is malformed.",
                resync_required=True,
            ) from error
        if not isinstance(payload, dict):
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_payload_invalid",
                "Event cursor payload must be an object.",
                resync_required=True,
            )
        try:
            cursor = IngressCursor(
                task_id=str(payload.get("taskId") or ""),
                sequence=int(payload.get("sequence", -1)),
                boundary=int(payload.get("boundary", -1)),
                generation=int(payload.get("generation", 0)),
                kind=CursorKind(str(payload.get("kind") or "")),
                issued_at_ms=int(payload.get("issuedAtMs", 0)),
                expires_at_ms=int(payload.get("expiresAtMs", 0)),
                filter_digest=str(payload.get("filterDigest") or ""),
                cursor_id=str(payload.get("cursorId") or ""),
                schema=str(payload.get("schema") or ""),
            )
        except (TypeError, ValueError) as error:
            raise EventIngressError(
                HTTPStatus.BAD_REQUEST,
                "cursor_payload_invalid",
                "Event cursor fields are invalid.",
                resync_required=True,
            ) from error
        if cursor.schema != CURSOR_SCHEMA:
            raise EventIngressError(
                HTTPStatus.UPGRADE_REQUIRED,
                "cursor_schema_unsupported",
                f"Unsupported cursor schema {cursor.schema or '[missing]'}.",
                details={"supported": CURSOR_SCHEMA},
                resync_required=True,
            )
        if not hmac.compare_digest(cursor.task_id, task_id.strip()):
            raise EventIngressError(
                HTTPStatus.CONFLICT,
                "cursor_scope_mismatch",
                "Event cursor belongs to a different task.",
                details={"expectedTaskId": task_id.strip()},
                resync_required=True,
            )
        if not hmac.compare_digest(cursor.filter_digest, filter_digest):
            raise EventIngressError(
                HTTPStatus.CONFLICT,
                "cursor_filter_mismatch",
                "Event cursor belongs to a different subscription filter.",
                resync_required=True,
            )
        if cursor.expires_at_ms <= self._now_ms():
            raise EventIngressError(
                HTTPStatus.GONE,
                "cursor_expired",
                "Event cursor has expired.",
                details={"expiredAtMs": cursor.expires_at_ms},
                resync_required=True,
            )
        allowed = set(allowed_kinds or ())
        if allowed and cursor.kind not in allowed:
            raise EventIngressError(
                HTTPStatus.CONFLICT,
                "cursor_kind_mismatch",
                "Event cursor cannot be used for this operation.",
                details={
                    "actual": cursor.kind.value,
                    "allowed": [item.value for item in sorted(allowed, key=lambda item: item.value)],
                },
                resync_required=True,
            )
        return cursor


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
    field_name: str,
) -> int:
    try:
        parsed = int(value if value not in (None, "") else default)
    except (TypeError, ValueError) as error:
        raise EventIngressError(
            HTTPStatus.BAD_REQUEST,
            "invalid_event_ingress_query",
            f"{field_name} must be an integer.",
            details={"field": field_name},
        ) from error
    if parsed < minimum or parsed > maximum:
        raise EventIngressError(
            HTTPStatus.BAD_REQUEST,
            "invalid_event_ingress_query",
            f"{field_name} must be between {minimum} and {maximum}.",
            details={"field": field_name, "minimum": minimum, "maximum": maximum},
        )
    return parsed


def _bounded_text(value: Any, field_name: str, *, allow_empty: bool = True) -> str:
    rendered = str(value or "").strip()
    if not rendered and not allow_empty:
        raise EventIngressError(
            HTTPStatus.BAD_REQUEST,
            "invalid_event_ingress_query",
            f"{field_name} must not be empty.",
            details={"field": field_name},
        )
    if len(rendered.encode("utf-8")) > MAX_QUERY_TEXT_BYTES:
        raise EventIngressError(
            HTTPStatus.BAD_REQUEST,
            "invalid_event_ingress_query",
            f"{field_name} exceeds {MAX_QUERY_TEXT_BYTES} bytes.",
            details={"field": field_name},
        )
    if any(character in rendered for character in ("\r", "\n", "\x00")):
        raise EventIngressError(
            HTTPStatus.BAD_REQUEST,
            "invalid_event_ingress_query",
            f"{field_name} contains control characters.",
            details={"field": field_name},
        )
    return rendered


def _filter_values(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, Sequence):
        raw = list(value)
    else:
        raise EventIngressError(
            HTTPStatus.BAD_REQUEST,
            "invalid_event_type_filter",
            "event_types must be a comma-separated string or array.",
        )
    values = tuple(
        item
        for item in (_bounded_text(entry, "event_types item") for entry in raw)
        if item
    )
    if len(values) > MAX_FILTER_VALUES:
        raise EventIngressError(
            HTTPStatus.BAD_REQUEST,
            "invalid_event_type_filter",
            f"event_types exceeds {MAX_FILTER_VALUES} values.",
        )
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True, slots=True)
class IngressFilter:
    event_types: tuple[str, ...] = ()
    intent: str = ""
    correlation_id: str = ""
    artifact_id: str = ""

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "IngressFilter":
        return cls(
            event_types=_filter_values(
                params.get("event_types", params.get("eventTypes"))
            ),
            intent=_bounded_text(params.get("intent"), "intent"),
            correlation_id=_bounded_text(
                params.get("correlation_id", params.get("correlationId")),
                "correlation_id",
            ),
            artifact_id=_bounded_text(
                params.get("artifact_id", params.get("artifactId")),
                "artifact_id",
            ),
        )

    def digest(self) -> str:
        payload = {
            "eventTypes": list(self.event_types),
            "intent": self.intent,
            "correlationId": self.correlation_id,
            "artifactId": self.artifact_id,
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def query(self, *, task_id: str, after_sequence: int, limit: int, descending: bool = False) -> RuntimeEventQuery:
        return RuntimeEventQuery(
            after_sequence=after_sequence,
            limit=limit,
            event_types=self.event_types,
            task_id=task_id,
            correlation_id=self.correlation_id or None,
            intent=self.intent or None,
            artifact_id=self.artifact_id or None,
            descending=descending,
        )

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "eventTypes": list(self.event_types),
            "intent": self.intent or None,
            "correlationId": self.correlation_id or None,
            "artifactId": self.artifact_id or None,
            "digest": self.digest(),
        }


@dataclass(frozen=True, slots=True)
class IngressRequest:
    task_id: str
    limit: int
    wait_ms: int
    stream_ms: int
    heartbeat_ms: int
    cursor: str
    generation: int
    filter: IngressFilter

    @classmethod
    def from_params(
        cls,
        task_id: str,
        params: Mapping[str, Any],
        *,
        operation: str,
    ) -> "IngressRequest":
        normalized_task = _bounded_text(task_id, "task_id", allow_empty=False)
        return cls(
            task_id=normalized_task,
            limit=_bounded_int(
                params.get("limit"),
                default=DEFAULT_PAGE_LIMIT,
                minimum=1,
                maximum=MAX_PAGE_LIMIT,
                field_name="limit",
            ),
            wait_ms=_bounded_int(
                params.get("wait_ms", params.get("waitMs")),
                default=DEFAULT_WAIT_MS,
                minimum=0,
                maximum=MAX_WAIT_MS,
                field_name="wait_ms",
            ),
            stream_ms=_bounded_int(
                params.get("stream_ms", params.get("streamMs")),
                default=DEFAULT_STREAM_MS,
                minimum=250,
                maximum=MAX_STREAM_MS,
                field_name="stream_ms",
            ),
            heartbeat_ms=_bounded_int(
                params.get("heartbeat_ms", params.get("heartbeatMs")),
                default=DEFAULT_HEARTBEAT_MS,
                minimum=MIN_HEARTBEAT_MS,
                maximum=MAX_STREAM_MS,
                field_name="heartbeat_ms",
            ),
            cursor=_bounded_text(params.get("cursor"), "cursor"),
            generation=_bounded_int(
                params.get("generation"),
                default=1,
                minimum=1,
                maximum=2_147_483_647,
                field_name="generation",
            ),
            filter=IngressFilter.from_params(params),
        )


@dataclass(frozen=True, slots=True)
class IngressApiResult:
    status: HTTPStatus
    body: Mapping[str, JsonValue]
    headers: Mapping[str, str] = field(default_factory=dict)


def _event_sequence(event: RuntimeEventEnvelope) -> int:
    return int(event.global_sequence)


def _event_payload(event: RuntimeEventEnvelope) -> dict[str, JsonValue]:
    return dict(event.to_jsonable())


def _event_frame(
    event: RuntimeEventEnvelope,
    *,
    task_id: str,
    previous_sequence: int,
    generation: int,
    source: str,
    observed_at_ms: int,
) -> dict[str, JsonValue]:
    canonical = _event_payload(event)
    identity = canonical.get("identity")
    identity_mapping = identity if isinstance(identity, Mapping) else {}
    if str(identity_mapping.get("taskId") or task_id) != task_id:
        raise EventIngressError(
            HTTPStatus.CONFLICT,
            "cross_task_event",
            "Canonical runtime query returned an event for another task.",
            details={
                "expectedTaskId": task_id,
                "actualTaskId": str(identity_mapping.get("taskId") or ""),
                "eventId": event.event_id,
            },
            resync_required=True,
        )
    return {
        "schema": FRAME_SCHEMA,
        "kind": "event",
        "source": source,
        "generation": generation,
        "taskId": task_id,
        "sequence": _event_sequence(event),
        "previousSequence": previous_sequence,
        "eventId": event.event_id,
        "eventType": event.event_type,
        "correlationId": event.correlation_id,
        "causationId": event.causation_id,
        "observedAtMs": observed_at_ms,
        "event": canonical,
    }


def _frames(
    events: Sequence[RuntimeEventEnvelope],
    *,
    task_id: str,
    previous_sequence: int,
    generation: int,
    source: str,
    observed_at_ms: int,
) -> list[dict[str, JsonValue]]:
    result: list[dict[str, JsonValue]] = []
    previous = previous_sequence
    for event in sorted(events, key=_event_sequence):
        result.append(
            _event_frame(
                event,
                task_id=task_id,
                previous_sequence=previous,
                generation=generation,
                source=source,
                observed_at_ms=observed_at_ms,
            )
        )
        previous = _event_sequence(event)
    return result


def _response_headers(
    *,
    cursor: str,
    sequence: int,
    boundary: int,
    generation: int,
) -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "X-Zyra-Event-Cursor": cursor,
        "X-Zyra-Event-Sequence": str(sequence),
        "X-Zyra-Event-Boundary": str(boundary),
        "X-Zyra-Event-Generation": str(generation),
        "X-Zyra-Event-State-Owner": "typescript.RuntimeEventSpine",
        "X-Zyra-Event-Ingress-Owner": "browser.EventIngressCoordinator",
        "X-Zyra-Canonical-Write-Allowed": "false",
    }


class EventIngressApiFacade:
    """Projects canonical runtime-event pages into browser ingress frames."""

    def __init__(
        self,
        bridge: RuntimeEventSpineBridge,
        *,
        codec: EventCursorCodec | None = None,
        now_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.bridge = bridge
        self.codec = codec or EventCursorCodec()
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._request_lock = threading.RLock()
        self._active_streams: dict[str, int] = {}

    def capabilities(self, task_id: str, params: Mapping[str, Any]) -> IngressApiResult:
        request = IngressRequest.from_params(task_id, params, operation="capabilities")
        base = f"/tasks/{request.task_id}/event-ingress"
        boundary = self._task_high_watermark(request)
        subscription_sequence = 0
        subscription_cursor_id: str | None = None
        if request.cursor:
            resumed = self.codec.decode(
                request.cursor,
                task_id=request.task_id,
                filter_digest=request.filter.digest(),
                allowed_kinds={
                    CursorKind.SNAPSHOT,
                    CursorKind.DELTA,
                    CursorKind.STREAM,
                },
            )
            subscription_sequence = min(resumed.sequence, boundary)
            subscription_cursor_id = resumed.cursor_id
        subscription_cursor, _ = self.codec.issue(
            task_id=request.task_id,
            sequence=subscription_sequence,
            boundary=boundary,
            generation=request.generation,
            kind=CursorKind.DELTA,
            filter_digest=request.filter.digest(),
            cursor_id=subscription_cursor_id,
        )
        body: dict[str, JsonValue] = {
            "schema": CAPABILITIES_SCHEMA,
            "protocol": INGRESS_SCHEMA,
            "taskId": request.task_id,
            "canonicalOwner": "typescript.RuntimeEventSpine",
            "ingressOwner": "browser.EventIngressCoordinator",
            "canonicalWriteAllowed": False,
            "snapshotRequired": True,
            "subscribeBeforeSnapshot": True,
            "subscriptionCursor": subscription_cursor,
            "subscriptionSequence": subscription_sequence,
            "subscriptionBoundary": boundary,
            "generation": request.generation,
            "transports": [
                {
                    "kind": "sse",
                    "available": True,
                    "priority": 10,
                    "path": f"{base}/sse",
                    "cursorMode": "query",
                    "customHeaders": True,
                },
                {
                    "kind": "websocket",
                    "available": False,
                    "priority": 20,
                    "path": f"{base}/websocket",
                    "cursorMode": "message",
                    "reason": "The embedded stdlib API does not upgrade HTTP connections.",
                },
                {
                    "kind": "long_poll",
                    "available": True,
                    "priority": 30,
                    "path": f"{base}/delta",
                    "cursorMode": "query",
                    "customHeaders": True,
                },
            ],
            "endpoints": {
                "capabilities": f"{base}/capabilities",
                "snapshot": f"{base}/snapshot",
                "delta": f"{base}/delta",
                "sse": f"{base}/sse",
                "websocket": f"{base}/websocket",
            },
            "limits": {
                "defaultPage": DEFAULT_PAGE_LIMIT,
                "maxPage": MAX_PAGE_LIMIT,
                "defaultWaitMs": DEFAULT_WAIT_MS,
                "maxWaitMs": MAX_WAIT_MS,
                "defaultStreamMs": DEFAULT_STREAM_MS,
                "maxStreamMs": MAX_STREAM_MS,
                "cursorTtlMs": CURSOR_TTL_MS,
            },
            "schemas": {
                "cursor": CURSOR_SCHEMA,
                "frame": FRAME_SCHEMA,
                "snapshot": SNAPSHOT_SCHEMA,
                "delta": DELTA_SCHEMA,
                "event": "zyra.runtime-event/v1",
            },
            "filter": request.filter.to_jsonable(),
        }
        return IngressApiResult(
            status=HTTPStatus.OK,
            body=body,
            headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Zyra-Event-State-Owner": "typescript.RuntimeEventSpine",
            },
        )

    def snapshot(self, task_id: str, params: Mapping[str, Any]) -> IngressApiResult:
        request = IngressRequest.from_params(task_id, params, operation="snapshot")
        digest = request.filter.digest()
        if request.cursor:
            current = self.codec.decode(
                request.cursor,
                task_id=request.task_id,
                filter_digest=digest,
                allowed_kinds={CursorKind.SNAPSHOT},
            )
            generation = current.generation
            start_sequence = current.sequence
            boundary = current.boundary
            cursor_id = current.cursor_id
        else:
            generation = request.generation
            start_sequence = 0
            boundary = self._task_high_watermark(request)
            cursor_id = secrets.token_hex(12)
        page = self.bridge.query(
            request.filter.query(
                task_id=request.task_id,
                after_sequence=start_sequence,
                limit=request.limit,
            )
        )
        selected = tuple(
            event
            for event in page.events
            if start_sequence < _event_sequence(event) <= boundary
        )
        observed_at = self._now_ms()
        frames = _frames(
            selected,
            task_id=request.task_id,
            previous_sequence=start_sequence,
            generation=generation,
            source="snapshot",
            observed_at_ms=observed_at,
        )
        self._attach_frame_cursors(
            frames,
            task_id=request.task_id,
            boundary=boundary,
            generation=generation,
            kind=CursorKind.SNAPSHOT,
            filter_digest=digest,
            cursor_id=cursor_id,
        )
        last_event_sequence = (
            _event_sequence(selected[-1]) if selected else start_sequence
        )
        page_progress = max(last_event_sequence, min(int(page.next_sequence), boundary))
        complete = (
            page_progress >= boundary
            or (not page.has_more and not selected)
            or (not page.has_more and int(page.next_sequence) >= boundary)
        )
        next_sequence = boundary if complete else page_progress
        next_kind = CursorKind.DELTA if complete else CursorKind.SNAPSHOT
        token, decoded = self.codec.issue(
            task_id=request.task_id,
            sequence=next_sequence,
            boundary=boundary,
            generation=generation,
            kind=next_kind,
            filter_digest=digest,
            cursor_id=cursor_id,
        )
        body: dict[str, JsonValue] = {
            "schema": SNAPSHOT_SCHEMA,
            "protocol": INGRESS_SCHEMA,
            "taskId": request.task_id,
            "generation": generation,
            "snapshotId": cursor_id,
            "boundary": boundary,
            "fromSequence": start_sequence,
            "nextSequence": next_sequence,
            "cursor": token,
            "cursorKind": decoded.kind.value,
            "complete": complete,
            "hasMore": not complete,
            "frames": frames,
            "filter": request.filter.to_jsonable(),
            "observedAtMs": observed_at,
            "canonicalOwner": "typescript.RuntimeEventSpine",
            "canonicalWriteAllowed": False,
        }
        return IngressApiResult(
            status=HTTPStatus.OK,
            body=body,
            headers=_response_headers(
                cursor=token,
                sequence=next_sequence,
                boundary=boundary,
                generation=generation,
            ),
        )

    def delta(self, task_id: str, params: Mapping[str, Any]) -> IngressApiResult:
        request = IngressRequest.from_params(task_id, params, operation="delta")
        digest = request.filter.digest()
        current = self.codec.decode(
            request.cursor,
            task_id=request.task_id,
            filter_digest=digest,
            allowed_kinds={CursorKind.DELTA, CursorKind.STREAM},
        )
        self._assert_cursor_not_ahead(request, current)
        page, waited_ms = self._wait_for_page(request, current.sequence)
        selected = tuple(
            event
            for event in page.events
            if _event_sequence(event) > current.sequence
        )
        observed_at = self._now_ms()
        frames = _frames(
            selected,
            task_id=request.task_id,
            previous_sequence=current.sequence,
            generation=current.generation,
            source="delta",
            observed_at_ms=observed_at,
        )
        self._attach_frame_cursors(
            frames,
            task_id=request.task_id,
            boundary=max(current.boundary, int(page.high_watermark)),
            generation=current.generation,
            kind=CursorKind.DELTA,
            filter_digest=digest,
            cursor_id=current.cursor_id,
        )
        last_event_sequence = (
            _event_sequence(selected[-1]) if selected else current.sequence
        )
        next_sequence = max(last_event_sequence, int(page.next_sequence))
        boundary = max(current.boundary, int(page.high_watermark), next_sequence)
        token, decoded = self.codec.issue(
            task_id=request.task_id,
            sequence=next_sequence,
            boundary=boundary,
            generation=current.generation,
            kind=CursorKind.DELTA,
            filter_digest=digest,
            cursor_id=current.cursor_id,
        )
        body: dict[str, JsonValue] = {
            "schema": DELTA_SCHEMA,
            "protocol": INGRESS_SCHEMA,
            "taskId": request.task_id,
            "generation": current.generation,
            "fromSequence": current.sequence,
            "nextSequence": next_sequence,
            "highWatermark": boundary,
            "cursor": token,
            "cursorKind": decoded.kind.value,
            "caughtUp": not page.has_more,
            "hasMore": page.has_more,
            "frames": frames,
            "waitedMs": waited_ms,
            "observedAtMs": observed_at,
            "canonicalOwner": "typescript.RuntimeEventSpine",
            "canonicalWriteAllowed": False,
        }
        return IngressApiResult(
            status=HTTPStatus.OK,
            body=body,
            headers=_response_headers(
                cursor=token,
                sequence=next_sequence,
                boundary=boundary,
                generation=current.generation,
            ),
        )

    def sse(
        self,
        task_id: str,
        params: Mapping[str, Any],
        *,
        disconnected: Callable[[], bool] | None = None,
    ) -> Iterator[bytes]:
        request = IngressRequest.from_params(task_id, params, operation="sse")
        digest = request.filter.digest()
        current = self.codec.decode(
            request.cursor,
            task_id=request.task_id,
            filter_digest=digest,
            allowed_kinds={CursorKind.DELTA, CursorKind.STREAM},
        )
        self._assert_cursor_not_ahead(request, current)
        stream_id = secrets.token_hex(12)
        with self._request_lock:
            self._active_streams[stream_id] = self._now_ms()
        started = self._monotonic()
        deadline = started + request.stream_ms / 1000
        heartbeat_at = started + request.heartbeat_ms / 1000
        cursor = current
        try:
            yield self._sse_retry(max(250, min(request.wait_ms or DEFAULT_WAIT_MS, 5_000)))
            yield self._sse_named(
                "ready",
                {
                    "schema": FRAME_SCHEMA,
                    "kind": "ready",
                    "taskId": request.task_id,
                    "generation": cursor.generation,
                    "sequence": cursor.sequence,
                    "streamId": stream_id,
                    "observedAtMs": self._now_ms(),
                },
                event_id=f"ready-{cursor.generation}-{cursor.sequence}",
            )
            while self._monotonic() < deadline:
                if disconnected and disconnected():
                    return
                remaining_ms = max(
                    0,
                    min(
                        request.wait_ms or DEFAULT_WAIT_MS,
                        int((deadline - self._monotonic()) * 1000),
                    ),
                )
                delta_params: dict[str, Any] = {
                    "cursor": self.codec.issue(
                        task_id=request.task_id,
                        sequence=cursor.sequence,
                        boundary=cursor.boundary,
                        generation=cursor.generation,
                        kind=CursorKind.STREAM,
                        filter_digest=digest,
                        cursor_id=cursor.cursor_id,
                    )[0],
                    "limit": request.limit,
                    "wait_ms": remaining_ms,
                    "event_types": ",".join(request.filter.event_types),
                    "intent": request.filter.intent,
                    "correlation_id": request.filter.correlation_id,
                    "artifact_id": request.filter.artifact_id,
                }
                result = self.delta(request.task_id, delta_params)
                frames = result.body.get("frames", [])
                if isinstance(frames, Sequence):
                    for item in frames:
                        if not isinstance(item, Mapping):
                            continue
                        sequence = int(item.get("sequence", cursor.sequence))
                        yield self._sse_named(
                            "event",
                            item,
                            event_id=f"{cursor.generation}-{sequence}",
                        )
                token = str(result.body.get("cursor") or "")
                cursor = self.codec.decode(
                    token,
                    task_id=request.task_id,
                    filter_digest=digest,
                    allowed_kinds={CursorKind.DELTA},
                )
                now = self._monotonic()
                if now >= heartbeat_at:
                    yield self._sse_named(
                        "heartbeat",
                        {
                            "schema": FRAME_SCHEMA,
                            "kind": "heartbeat",
                            "taskId": request.task_id,
                            "generation": cursor.generation,
                            "sequence": cursor.sequence,
                            "cursor": token,
                            "observedAtMs": self._now_ms(),
                        },
                        event_id=f"heartbeat-{cursor.generation}-{cursor.sequence}",
                    )
                    heartbeat_at = now + request.heartbeat_ms / 1000
            final_token, _ = self.codec.issue(
                task_id=request.task_id,
                sequence=cursor.sequence,
                boundary=cursor.boundary,
                generation=cursor.generation,
                kind=CursorKind.DELTA,
                filter_digest=digest,
                cursor_id=cursor.cursor_id,
            )
            yield self._sse_named(
                "close",
                {
                    "schema": FRAME_SCHEMA,
                    "kind": "close",
                    "reason": "stream_window_complete",
                    "taskId": request.task_id,
                    "generation": cursor.generation,
                    "sequence": cursor.sequence,
                    "cursor": final_token,
                    "retryable": True,
                    "observedAtMs": self._now_ms(),
                },
                event_id=f"close-{cursor.generation}-{cursor.sequence}",
            )
        finally:
            with self._request_lock:
                self._active_streams.pop(stream_id, None)

    def active_streams(self) -> Mapping[str, int]:
        with self._request_lock:
            return dict(self._active_streams)

    def _task_high_watermark(self, request: IngressRequest) -> int:
        page = self.bridge.query(
            request.filter.query(
                task_id=request.task_id,
                after_sequence=0,
                limit=1,
                descending=True,
            )
        )
        if page.events:
            return max(_event_sequence(event) for event in page.events)
        return 0

    def _attach_frame_cursors(
        self,
        frames: Sequence[dict[str, JsonValue]],
        *,
        task_id: str,
        boundary: int,
        generation: int,
        kind: CursorKind,
        filter_digest: str,
        cursor_id: str,
    ) -> None:
        for frame in frames:
            sequence = int(frame.get("sequence", 0) or 0)
            token, _ = self.codec.issue(
                task_id=task_id,
                sequence=sequence,
                boundary=max(boundary, sequence),
                generation=generation,
                kind=kind,
                filter_digest=filter_digest,
                cursor_id=cursor_id,
            )
            frame["cursor"] = token

    def _assert_cursor_not_ahead(
        self,
        request: IngressRequest,
        cursor: IngressCursor,
    ) -> None:
        high = self._task_high_watermark(request)
        if cursor.sequence <= high:
            return
        if high == 0 and cursor.sequence == 0:
            return
        raise EventIngressError(
            HTTPStatus.CONFLICT,
            "cursor_ahead_of_canonical_log",
            "Event cursor is ahead of the canonical task log.",
            details={
                "cursorSequence": cursor.sequence,
                "taskHighWatermark": high,
            },
            resync_required=True,
        )

    def _wait_for_page(
        self,
        request: IngressRequest,
        after_sequence: int,
    ) -> tuple[Any, int]:
        started = self._monotonic()
        deadline = started + request.wait_ms / 1000
        while True:
            page = self.bridge.query(
                request.filter.query(
                    task_id=request.task_id,
                    after_sequence=after_sequence,
                    limit=request.limit,
                )
            )
            if page.events or page.has_more or self._monotonic() >= deadline:
                waited_ms = max(0, int((self._monotonic() - started) * 1000))
                return page, waited_ms
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                waited_ms = max(0, int((self._monotonic() - started) * 1000))
                return page, waited_ms
            self._sleep(min(0.05, remaining))

    @staticmethod
    def _sse_retry(milliseconds: int) -> bytes:
        return f"retry: {int(milliseconds)}\n\n".encode("utf-8")

    @staticmethod
    def _sse_named(
        event: str,
        payload: Mapping[str, Any],
        *,
        event_id: str,
    ) -> bytes:
        data = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        lines = [f"id: {event_id}", f"event: {event}"]
        lines.extend(f"data: {line}" for line in data.splitlines() or ["{}"])
        return ("\n".join(lines) + "\n\n").encode("utf-8")


def sse_headers() -> dict[str, str]:
    return {
        "Content-Type": SSE_MEDIA_TYPE,
        "Cache-Control": "no-store, no-cache, must-revalidate",
        # The embedded HTTP/1.1 server emits a finite SSE window and does not
        # implement chunked transfer encoding.  Closing the connection is the
        # explicit message boundary that lets clients reconnect from the last
        # signed cursor instead of waiting forever on a keep-alive socket.
        "Connection": "close",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
        "X-Zyra-Event-State-Owner": "typescript.RuntimeEventSpine",
        "X-Zyra-Event-Ingress-Owner": "browser.EventIngressCoordinator",
        "X-Zyra-Canonical-Write-Allowed": "false",
    }


__all__ = [
    "CAPABILITIES_SCHEMA",
    "CURSOR_SCHEMA",
    "DELTA_SCHEMA",
    "EventCursorCodec",
    "EventIngressApiFacade",
    "EventIngressError",
    "FRAME_SCHEMA",
    "INGRESS_SCHEMA",
    "IngressApiResult",
    "IngressCursor",
    "IngressFilter",
    "IngressRequest",
    "SNAPSHOT_SCHEMA",
    "SSE_MEDIA_TYPE",
    "sse_headers",
]
