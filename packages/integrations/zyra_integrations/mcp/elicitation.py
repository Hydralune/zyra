from __future__ import annotations

"""Durable, exact-identity MCP elicitation queue."""

import copy
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, now_iso

from .models import (
    JsonValue,
    McpElicitationAction,
    McpElicitationRequest,
    McpElicitationResolution,
    McpElicitationStatus,
    McpModelError,
    redact_value,
    stable_digest,
)


class McpElicitationError(RuntimeError):
    pass


class McpElicitationConflict(McpElicitationError):
    pass


class McpElicitationNotFound(McpElicitationError):
    pass


class McpElicitationStatePort(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class McpElicitationRecord:
    request: McpElicitationRequest
    status: McpElicitationStatus
    revision: int
    created_at: str
    updated_at: str
    action: str = ""
    actor_id: str = ""
    idempotency_key: str = ""
    response_digest: str = ""
    terminal_reason: str = ""

    @property
    def terminal(self) -> bool:
        return self.status not in {
            McpElicitationStatus.PENDING,
        }

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "request": self.request.safe_dict(),
            "status": str(self.status),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "action": self.action,
            "actor_id": self.actor_id,
            "idempotency_key": self.idempotency_key,
            "response_digest": self.response_digest,
            "terminal_reason": self.terminal_reason,
            "raw_response_persisted": False,
        }


@dataclass(frozen=True, slots=True)
class McpElicitationQueueReceipt:
    record: McpElicitationRecord
    wire_response: Mapping[str, JsonValue]
    sensitive_fields: tuple[str, ...] = ()

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "record": self.record.safe_dict(),
            "wire_response": {
                key: "<redacted>" if key == "content" and self.sensitive_fields else value
                for key, value in self.wire_response.items()
            },
            "sensitive_fields": list(self.sensitive_fields),
        }


@dataclass(frozen=True, slots=True)
class _IdempotencyReceipt:
    """Bind one idempotency key to one exact resolution attempt.

    Only the digest and already-sanitized wire result are retained.  Raw
    accepted content (including credential material) is never duplicated into
    the idempotency index.
    """

    fingerprint: str
    receipt: McpElicitationQueueReceipt


class McpElicitationQueue:
    def __init__(
        self,
        *,
        state_store: McpElicitationStatePort | None = None,
        response_sink: Callable[[McpElicitationRequest, Mapping[str, JsonValue]], None] | None = None,
        sensitive_sink: Callable[[str, str, JsonValue], str] | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
        now: Callable[[], datetime] | None = None,
        disabled: bool = False,
    ) -> None:
        self.state_store = state_store
        self.response_sink = response_sink
        self.sensitive_sink = sensitive_sink
        self.event_sink = event_sink
        self.now = now or (lambda: datetime.now(UTC))
        self.disabled = disabled
        self._records: dict[tuple[str, str, str], McpElicitationRecord] = {}
        self._responses: dict[str, _IdempotencyReceipt] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._wire_by_request: dict[tuple[str, str, str], Mapping[str, JsonValue]] = {}

    def enqueue(
        self,
        request: McpElicitationRequest,
        *,
        run_id: str = "",
        task_id: str = "",
        node_id: str | None = None,
    ) -> McpElicitationRecord:
        if self.disabled:
            raise McpElicitationError("McpElicitationQueue is disabled")
        key = _key(request.server_id, request.session_id, request.request_id)
        with self._lock:
            current = self._records.get(key)
            if current is not None:
                if current.request.safe_dict() == request.safe_dict():
                    return current
                raise McpElicitationConflict("elicitation request id was reused with different content")
            timestamp = now_iso()
            record = McpElicitationRecord(
                request=request,
                status=McpElicitationStatus.PENDING,
                revision=max(1, request.revision),
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._records[key] = record
            self._persist(record)
            self._condition.notify_all()
        self._emit(record, run_id=run_id, task_id=task_id, node_id=node_id)
        return record

    def mark_delivered(
        self,
        *,
        server_id: str,
        session_id: str,
        request_id: str,
        expected_revision: int,
    ) -> McpElicitationRecord:
        return self._transition(
            server_id=server_id,
            session_id=session_id,
            request_id=request_id,
            expected_revision=expected_revision,
            # Delivery is a phase of a still-pending request.  The canonical
            # MCP status enum intentionally contains only terminal answers.
            status=McpElicitationStatus.PENDING,
        )

    def resolve(
        self,
        resolution: McpElicitationResolution,
        *,
        run_id: str = "",
        task_id: str = "",
        node_id: str | None = None,
    ) -> McpElicitationQueueReceipt:
        if self.disabled:
            raise McpElicitationError("McpElicitationQueue is disabled")
        key = _key(resolution.server_id, resolution.session_id, resolution.request_id)
        fingerprint = _resolution_fingerprint(resolution)
        with self._lock:
            existing = self._responses.get(resolution.idempotency_key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise McpElicitationConflict(
                        "elicitation idempotency key was reused for a different resolution"
                    )
                return McpElicitationQueueReceipt(
                    existing.receipt.record,
                    _copy_wire_response(existing.receipt.wire_response),
                    existing.receipt.sensitive_fields,
                )
            current = self._records.get(key)
            if current is None:
                raise McpElicitationNotFound(resolution.request_id)
            if current.terminal:
                raise McpElicitationConflict("elicitation request is already terminal")
            if current.revision != resolution.expected_revision:
                raise McpElicitationConflict("elicitation revision mismatch")
            if _expired(current.request, self.now()):
                expired = self._terminal(current, McpElicitationStatus.EXPIRED, reason="deadline elapsed")
                self._records[key] = expired
                self._persist(expired)
                raise McpElicitationConflict("elicitation request expired")
            try:
                if (
                    current.request.request_id != resolution.request_id
                    or current.request.session_id != resolution.session_id
                    or current.request.server_id != resolution.server_id
                ):
                    raise McpModelError("elicitation resolution identity mismatch")
                content = (
                    current.request.validate_content(resolution.content)
                    if resolution.action is McpElicitationAction.ACCEPT
                    else {}
                )
                wire: dict[str, JsonValue] = {"action": str(resolution.action)}
                if content:
                    wire["content"] = content
            except (McpModelError, TypeError, ValueError) as error:
                raise McpElicitationConflict(str(error)) from error
            sensitive_fields = tuple(field.name for field in current.request.fields if field.sensitive)
            outbound = dict(wire)
            if resolution.action is McpElicitationAction.ACCEPT and isinstance(wire.get("content"), Mapping):
                outbound_content = dict(wire["content"])
                for name in sensitive_fields:
                    if name not in outbound_content:
                        continue
                    if self.sensitive_sink is None:
                        raise McpElicitationConflict(
                            f"sensitive elicitation field {name!r} has no credential sink"
                        )
                    reference = self.sensitive_sink(
                        resolution.server_id,
                        name,
                        outbound_content[name],
                    )
                    outbound_content[name] = {"credentialReference": reference}
                outbound["content"] = outbound_content
            target_status = {
                McpElicitationAction.ACCEPT: McpElicitationStatus.RESOLVED,
                McpElicitationAction.DECLINE: McpElicitationStatus.DECLINED,
                McpElicitationAction.CANCEL: McpElicitationStatus.CANCELLED,
            }[resolution.action]
            response_digest = _digest(outbound)
            record = McpElicitationRecord(
                request=current.request,
                status=target_status,
                revision=current.revision + 1,
                created_at=current.created_at,
                updated_at=now_iso(),
                action=str(resolution.action),
                actor_id=resolution.actor_id,
                idempotency_key=resolution.idempotency_key,
                response_digest=response_digest,
                terminal_reason="resolved by control plane",
            )
            self._records[key] = record
            stored_receipt = McpElicitationQueueReceipt(
                record,
                _copy_wire_response(outbound),
                sensitive_fields,
            )
            self._responses[resolution.idempotency_key] = _IdempotencyReceipt(
                fingerprint=fingerprint,
                receipt=stored_receipt,
            )
            self._wire_by_request[key] = outbound
            self._persist(record)
            self._condition.notify_all()
        if self.response_sink is not None:
            self.response_sink(current.request, outbound)
        self._emit(record, run_id=run_id, task_id=task_id, node_id=node_id)
        return McpElicitationQueueReceipt(record, outbound, sensitive_fields)

    def cancel_for_server(
        self,
        server_id: str,
        *,
        reason: str = "server disconnected",
    ) -> tuple[McpElicitationRecord, ...]:
        changed: list[McpElicitationRecord] = []
        with self._lock:
            for key, record in list(self._records.items()):
                if key[0] != server_id or record.terminal:
                    continue
                updated = self._terminal(record, McpElicitationStatus.CANCELLED, reason=reason)
                self._records[key] = updated
                self._wire_by_request[key] = {"action": "cancel"}
                self._persist(updated)
                changed.append(updated)
            if changed:
                self._condition.notify_all()
        return tuple(changed)

    def expire_due(self) -> tuple[McpElicitationRecord, ...]:
        changed: list[McpElicitationRecord] = []
        current_time = self.now()
        with self._lock:
            for key, record in list(self._records.items()):
                if record.terminal or not _expired(record.request, current_time):
                    continue
                updated = self._terminal(record, McpElicitationStatus.EXPIRED, reason="deadline elapsed")
                self._records[key] = updated
                self._wire_by_request[key] = {"action": "cancel"}
                self._persist(updated)
                changed.append(updated)
            if changed:
                self._condition.notify_all()
        return tuple(changed)

    def wait_for_response(
        self,
        *,
        server_id: str,
        session_id: str,
        request_id: str,
        timeout_seconds: float,
    ) -> Mapping[str, JsonValue]:
        """Wait for the exact control-plane resolution with a hard deadline.

        The waiter is the JSON-RPC server-request worker, never the carrier
        reader.  API resolve, operator cancel, disconnect and expiry all wake
        the same condition.  A local timeout is persisted as cancellation and
        returns the protocol-level fail-closed response.
        """

        if timeout_seconds <= 0:
            raise ValueError("elicitation timeout_seconds must be positive")
        key = _key(server_id, session_id, request_id)
        deadline = time.monotonic() + float(timeout_seconds)
        with self._condition:
            while True:
                record = self._records.get(key)
                if record is None:
                    raise McpElicitationNotFound(request_id)
                if record.terminal:
                    return dict(self._wire_by_request.get(key) or _wire_for_terminal(record))
                if _expired(record.request, self.now()):
                    expired = self._terminal(
                        record,
                        McpElicitationStatus.EXPIRED,
                        reason="deadline elapsed while awaiting control response",
                    )
                    self._records[key] = expired
                    wire: Mapping[str, JsonValue] = {"action": "cancel"}
                    self._wire_by_request[key] = wire
                    self._persist(expired)
                    self._condition.notify_all()
                    return dict(wire)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cancelled = self._terminal(
                        record,
                        McpElicitationStatus.CANCELLED,
                        reason="bounded control response timeout",
                    )
                    self._records[key] = cancelled
                    wire = {"action": "cancel"}
                    self._wire_by_request[key] = wire
                    self._persist(cancelled)
                    self._condition.notify_all()
                    return dict(wire)
                self._condition.wait(timeout=min(remaining, 0.25))

    def get(self, server_id: str, session_id: str, request_id: str) -> McpElicitationRecord | None:
        with self._lock:
            return self._records.get(_key(server_id, session_id, request_id))

    def list(
        self,
        *,
        server_id: str = "",
        session_id: str = "",
        include_terminal: bool = True,
    ) -> tuple[McpElicitationRecord, ...]:
        with self._lock:
            values = [
                item
                for item in self._records.values()
                if (not server_id or item.request.server_id == server_id)
                and (not session_id or item.request.session_id == session_id)
                and (include_terminal or not item.terminal)
            ]
        return tuple(sorted(values, key=lambda item: (item.created_at, item.request.request_id)))

    def _transition(
        self,
        *,
        server_id: str,
        session_id: str,
        request_id: str,
        expected_revision: int,
        status: McpElicitationStatus,
    ) -> McpElicitationRecord:
        key = _key(server_id, session_id, request_id)
        with self._lock:
            current = self._records.get(key)
            if current is None:
                raise McpElicitationNotFound(request_id)
            if current.terminal or current.revision != expected_revision:
                raise McpElicitationConflict("elicitation state or revision changed")
            record = McpElicitationRecord(
                request=current.request,
                status=status,
                revision=current.revision + 1,
                created_at=current.created_at,
                updated_at=now_iso(),
            )
            self._records[key] = record
            self._persist(record)
            return record

    @staticmethod
    def _terminal(
        record: McpElicitationRecord,
        status: McpElicitationStatus,
        *,
        reason: str,
    ) -> McpElicitationRecord:
        return McpElicitationRecord(
            request=record.request,
            status=status,
            revision=record.revision + 1,
            created_at=record.created_at,
            updated_at=now_iso(),
            terminal_reason=reason,
        )

    def _persist(self, record: McpElicitationRecord) -> None:
        if self.state_store is None:
            return
        key = (
            f"elicitation.{record.request.server_id}."
            f"{record.request.session_id}.{record.request.request_id}"
        )
        self.state_store.set(key, record.safe_dict())

    def _emit(
        self,
        record: McpElicitationRecord,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> None:
        if self.event_sink is None or not run_id or not task_id:
            return
        event_type = getattr(EventType, "MCP_ELICITATION", EventType.SYSTEM_NOTICE)
        self.event_sink(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=event_type,
                payload={
                    "mcp_runtime": {
                        "schema": "zyra.mcp-elicitation-event.v1",
                        "runtime_id": "McpElicitationQueue",
                        "server_id": record.request.server_id,
                        "session_id": record.request.session_id,
                        "request_id": record.request.request_id,
                        "record": record.safe_dict(),
                    }
                },
            )
        )


def _key(server_id: str, session_id: str, request_id: str) -> tuple[str, str, str]:
    return server_id, session_id, request_id


def _expired(request: McpElicitationRequest, now: datetime) -> bool:
    if not request.expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(request.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return now >= deadline


def _digest(value: Mapping[str, Any]) -> str:
    import hashlib
    import json

    encoded = json.dumps(redact_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _resolution_fingerprint(resolution: McpElicitationResolution) -> str:
    """Hash the canonical, exact ownership boundary for an API resolution."""

    return stable_digest(
        {
            "schema": "zyra.mcp-elicitation-idempotency.v1",
            "server_id": resolution.server_id,
            "session_id": resolution.session_id,
            "request_id": resolution.request_id,
            "expected_revision": resolution.expected_revision,
            "action": str(resolution.action),
            "content": resolution.content,
            "actor_id": resolution.actor_id,
        }
    )


def _copy_wire_response(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    # JSON-only model validation makes deepcopy deterministic and prevents a
    # caller from mutating the cached receipt returned by a later retry.
    return copy.deepcopy(dict(value))


def _wire_for_terminal(record: McpElicitationRecord) -> Mapping[str, JsonValue]:
    if record.status is McpElicitationStatus.DECLINED:
        return {"action": "decline"}
    # Resolved records normally retain an in-memory response.  If a caller
    # reaches a restored terminal record without that content, fail closed
    # rather than inventing accepted fields.
    return {"action": "cancel"}


__all__ = [
    "McpElicitationConflict",
    "McpElicitationError",
    "McpElicitationNotFound",
    "McpElicitationQueue",
    "McpElicitationQueueReceipt",
    "McpElicitationRecord",
]
