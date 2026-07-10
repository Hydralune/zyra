from __future__ import annotations

"""Permission request delivery and response transport registry.

The transport layer deliberately does *not* own permission state.  Its job is
to deliver a redacted, immutable request envelope to an operator surface and
to turn a response into the exact :class:`PermissionResolutionResponse` that
the session-backed queue can validate.  A bridge, CLI, SDK, coordinator, or UI
therefore cannot become an alternate permission authority.

The design is a Zyra-owned productization of Claude's coordinator/swarm/
interactive handler split and Agent Framework's approval message adapters:

* durable lifecycle remains in ``PermissionStateStore``;
* delivery mailboxes are transient projections and may be rebuilt;
* every response echoes or inherits the complete immutable identity;
* channels are deployment-registered, bounded, observable, and fail closed;
* raw tool arguments and custody material never enter a transport envelope.
"""

from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from threading import RLock
from time import monotonic
from typing import Any, Protocol

from zyra_core import new_id, now_iso, to_jsonable

from .canonical import canonical_arguments_json
from .models import (
    PermissionEffect,
    PermissionRequestRecord,
    PermissionResolutionResponse,
    PermissionScope,
    ToolIdentity,
)


class PermissionTransportKind(StrEnum):
    API = "api"
    CLI = "cli"
    BRIDGE = "bridge"
    SDK = "sdk"
    STRUCTURED_IO = "structured_io"
    HOOK = "hook"
    GATEWAY = "gateway"
    COORDINATOR = "coordinator"
    SWARM_WORKER = "swarm_worker"
    INTERACTIVE = "interactive"


class PermissionDeliveryStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    BACKPRESSURE = "backpressure"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    SKIPPED = "skipped"


class PermissionTransportFailureMode(StrEnum):
    FAIL_CLOSED = "fail_closed"
    TRY_NEXT = "try_next"
    BEST_EFFORT = "best_effort"


class PermissionTransportError(RuntimeError):
    code = "permission_transport_error"


class PermissionTransportUnavailable(PermissionTransportError):
    code = "permission_transport_unavailable"


class PermissionTransportBackpressure(PermissionTransportError):
    code = "permission_transport_backpressure"


class PermissionTransportIdentityError(PermissionTransportError):
    code = "permission_transport_identity_mismatch"


class PermissionTransportResponseError(PermissionTransportError):
    code = "permission_transport_response_invalid"


@dataclass(frozen=True, slots=True)
class PermissionDeliveryEnvelope:
    request_id: str
    session_id: str
    task_id: str
    run_id: str
    worker_request_id: str
    tool_use_id: str
    tool_identity: ToolIdentity
    arguments_digest: str
    request_fingerprint: str
    scope: PermissionScope
    reason_code: str
    reason: str
    expires_at: str
    request_revision: int
    mode: str
    delivery_id: str = field(default_factory=lambda: new_id("permdelivery"))
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_request(
        cls,
        request: PermissionRequestRecord,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> "PermissionDeliveryEnvelope":
        return cls(
            request_id=request.request_id,
            session_id=request.session_id,
            task_id=request.task_id,
            run_id=request.run_id,
            worker_request_id=request.worker_request_id,
            tool_use_id=request.tool_use_id,
            tool_identity=request.tool_identity,
            arguments_digest=request.arguments_digest,
            request_fingerprint=request.request_fingerprint,
            scope=request.scope,
            reason_code=request.reason_code,
            reason=request.reason,
            expires_at=request.expires_at,
            request_revision=request.revision,
            mode=str(request.mode),
            metadata=_safe_metadata(
                {
                    "phase": str(request.phase),
                    "status": str(request.status),
                    "rule_snapshot_id": request.rule_snapshot_id,
                    **dict(metadata or {}),
                }
            ),
        )

    @property
    def identity_digest(self) -> str:
        payload = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "tool_use_id": self.tool_use_id,
            "tool_identity": self.tool_identity.to_dict(),
            "arguments_digest": self.arguments_digest,
            "request_fingerprint": self.request_fingerprint,
            "scope": self.scope.to_dict(),
            "expires_at": self.expires_at,
            "request_revision": self.request_revision,
        }
        encoded = canonical_arguments_json(payload)
        return f"sha256:{sha256(encoded.encode('utf-8')).hexdigest()}"

    @property
    def expired(self) -> bool:
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expiry

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.permission.delivery.v1",
            "delivery_id": self.delivery_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "worker_request_id": self.worker_request_id,
            "tool_use_id": self.tool_use_id,
            "tool_identity": self.tool_identity.to_dict(),
            "arguments_digest": self.arguments_digest,
            "request_fingerprint": self.request_fingerprint,
            "scope": self.scope.to_dict(),
            "reason_code": self.reason_code,
            "reason": self.reason,
            "expires_at": self.expires_at,
            "request_revision": self.request_revision,
            "mode": self.mode,
            "identity_digest": self.identity_digest,
            "created_at": self.created_at,
            "metadata": _safe_metadata(self.metadata),
            "raw_arguments_included": False,
        }


@dataclass(frozen=True, slots=True)
class PermissionDeliveryReceipt:
    delivery_id: str
    request_id: str
    transport_id: str
    channel: PermissionTransportKind
    status: PermissionDeliveryStatus
    accepted: bool
    reason: str
    delivered_at: str = field(default_factory=now_iso)
    remote_reference: str = ""
    latency_ms: int = 0
    attempt: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "request_id": self.request_id,
            "transport_id": self.transport_id,
            "channel": str(self.channel),
            "status": str(self.status),
            "accepted": self.accepted,
            "reason": self.reason,
            "delivered_at": self.delivered_at,
            "remote_reference": self.remote_reference,
            "latency_ms": self.latency_ms,
            "attempt": self.attempt,
            "metadata": _safe_metadata(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PermissionTransportDescriptor:
    transport_id: str
    name: str
    kind: PermissionTransportKind
    enabled: bool
    priority: int
    failure_mode: PermissionTransportFailureMode
    authoritative_response_channel: bool
    max_pending: int
    source: str
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport_id": self.transport_id,
            "name": self.name,
            "kind": str(self.kind),
            "enabled": self.enabled,
            "priority": self.priority,
            "failure_mode": str(self.failure_mode),
            "authoritative_response_channel": self.authoritative_response_channel,
            "max_pending": self.max_pending,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


class PermissionTransport(Protocol):
    @property
    def descriptor(self) -> PermissionTransportDescriptor: ...

    def deliver(self, envelope: PermissionDeliveryEnvelope) -> PermissionDeliveryReceipt: ...


@dataclass(slots=True)
class MailboxPermissionTransport:
    """Bounded transient mailbox used by API/CLI/bridge/SDK projections.

    Mailbox loss never loses a pending approval: the durable request remains
    in ``PermissionStateStore`` and can be redelivered.  This is intentionally
    different from the upstream process-local pending registries that could
    accidentally become the only state owner.
    """

    descriptor: PermissionTransportDescriptor
    _items: deque[PermissionDeliveryEnvelope] = field(default_factory=deque, repr=False)
    _seen: set[str] = field(default_factory=set, repr=False)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def deliver(self, envelope: PermissionDeliveryEnvelope) -> PermissionDeliveryReceipt:
        started = monotonic()
        with self._lock:
            if not self.descriptor.enabled:
                return _receipt(
                    envelope,
                    self.descriptor,
                    PermissionDeliveryStatus.UNAVAILABLE,
                    False,
                    "transport is disabled",
                    started,
                )
            identity = f"{envelope.request_id}:{envelope.request_revision}:{envelope.identity_digest}"
            if identity in self._seen:
                return _receipt(
                    envelope,
                    self.descriptor,
                    PermissionDeliveryStatus.DUPLICATE,
                    True,
                    "exact request revision was already delivered",
                    started,
                )
            if len(self._items) >= max(1, self.descriptor.max_pending):
                return _receipt(
                    envelope,
                    self.descriptor,
                    PermissionDeliveryStatus.BACKPRESSURE,
                    False,
                    "transport mailbox reached its bounded capacity",
                    started,
                )
            self._items.append(envelope)
            self._seen.add(identity)
            return _receipt(
                envelope,
                self.descriptor,
                PermissionDeliveryStatus.ACCEPTED,
                True,
                "request accepted by transient delivery mailbox",
                started,
                remote_reference=f"mailbox:{self.descriptor.transport_id}:{len(self._items)}",
            )

    def pending(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        include_expired: bool = False,
    ) -> tuple[PermissionDeliveryEnvelope, ...]:
        with self._lock:
            return tuple(
                item
                for item in self._items
                if (session_id is None or item.session_id == session_id)
                and (task_id is None or item.task_id == task_id)
                and (include_expired or not item.expired)
            )

    def take(
        self,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> PermissionDeliveryEnvelope | None:
        with self._lock:
            selected: PermissionDeliveryEnvelope | None = None
            retained: deque[PermissionDeliveryEnvelope] = deque()
            while self._items:
                item = self._items.popleft()
                matches = (
                    selected is None
                    and (session_id is None or item.session_id == session_id)
                    and (request_id is None or item.request_id == request_id)
                )
                if matches:
                    selected = item
                else:
                    retained.append(item)
            self._items = retained
            return selected

    def acknowledge(self, request_id: str) -> int:
        with self._lock:
            before = len(self._items)
            self._items = deque(item for item in self._items if item.request_id != request_id)
            return before - len(self._items)

    def clear_expired(self) -> int:
        with self._lock:
            before = len(self._items)
            self._items = deque(item for item in self._items if not item.expired)
            return before - len(self._items)

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return {
                "pending": len(self._items),
                "expired": sum(item.expired for item in self._items),
                "seen_revisions": len(self._seen),
                "max_pending": self.descriptor.max_pending,
            }


@dataclass(slots=True)
class CallbackPermissionTransport:
    descriptor: PermissionTransportDescriptor
    callback: Callable[[PermissionDeliveryEnvelope], Any]

    def deliver(self, envelope: PermissionDeliveryEnvelope) -> PermissionDeliveryReceipt:
        started = monotonic()
        if not self.descriptor.enabled:
            return _receipt(
                envelope,
                self.descriptor,
                PermissionDeliveryStatus.UNAVAILABLE,
                False,
                "transport is disabled",
                started,
            )
        try:
            result = self.callback(envelope)
        except Exception as error:  # noqa: BLE001 - extension failure must be projected, not hidden.
            return _receipt(
                envelope,
                self.descriptor,
                PermissionDeliveryStatus.FAILED,
                False,
                f"transport callback failed: {type(error).__name__}",
                started,
                metadata={"error_type": type(error).__name__},
            )
        accepted, remote_reference, reason, metadata = _callback_result(result)
        return _receipt(
            envelope,
            self.descriptor,
            PermissionDeliveryStatus.ACCEPTED if accepted else PermissionDeliveryStatus.FAILED,
            accepted,
            reason,
            started,
            remote_reference=remote_reference,
            metadata=metadata,
        )


class PermissionTransportRegistry:
    """Deployment-owned registry for all pending approval delivery paths."""

    def __init__(self, transports: Iterable[PermissionTransport] = ()) -> None:
        self._lock = RLock()
        self._transports: dict[str, PermissionTransport] = {}
        self._delivery_count = 0
        self._failure_count = 0
        for transport in transports:
            self.register(transport)

    @classmethod
    def with_default_mailboxes(
        cls,
        *,
        enabled: Sequence[PermissionTransportKind | str] | None = None,
        max_pending: int = 1024,
    ) -> "PermissionTransportRegistry":
        selected = {
            PermissionTransportKind(str(getattr(item, "value", item)))
            for item in (enabled or tuple(PermissionTransportKind))
        }
        transports: list[MailboxPermissionTransport] = []
        for priority, kind in enumerate(PermissionTransportKind, start=1):
            transports.append(
                MailboxPermissionTransport(
                    PermissionTransportDescriptor(
                        transport_id=f"builtin-{kind.value}",
                        name=f"Zyra {kind.value} permission mailbox",
                        kind=kind,
                        enabled=kind in selected,
                        priority=priority * 10,
                        failure_mode=(
                            PermissionTransportFailureMode.FAIL_CLOSED
                            if kind in {
                                PermissionTransportKind.API,
                                PermissionTransportKind.CLI,
                                PermissionTransportKind.STRUCTURED_IO,
                            }
                            else PermissionTransportFailureMode.TRY_NEXT
                        ),
                        authoritative_response_channel=True,
                        max_pending=max_pending,
                        source="zyra_permission_integration",
                        metadata={
                            "owner_unit": "M1-S03A-02",
                            "durable_state_owner": "PermissionStateStore",
                            "mailbox_is_projection": "true",
                        },
                    )
                )
            )
        return cls(transports)

    def register(self, transport: PermissionTransport) -> PermissionTransportDescriptor:
        descriptor = transport.descriptor
        if not descriptor.transport_id:
            raise ValueError("permission transport requires transport_id")
        with self._lock:
            if descriptor.transport_id in self._transports:
                raise ValueError(f"duplicate permission transport: {descriptor.transport_id}")
            self._transports[descriptor.transport_id] = transport
        return descriptor

    def unregister(self, transport_id: str) -> bool:
        with self._lock:
            return self._transports.pop(transport_id, None) is not None

    def get(self, transport_id: str) -> PermissionTransport | None:
        with self._lock:
            return self._transports.get(transport_id)

    def list(
        self,
        *,
        kind: PermissionTransportKind | str | None = None,
        enabled_only: bool = False,
    ) -> tuple[PermissionTransport, ...]:
        selected_kind = (
            PermissionTransportKind(str(getattr(kind, "value", kind)))
            if kind is not None
            else None
        )
        with self._lock:
            values = tuple(self._transports.values())
        return tuple(
            sorted(
                (
                    item
                    for item in values
                    if (selected_kind is None or item.descriptor.kind is selected_kind)
                    and (not enabled_only or item.descriptor.enabled)
                ),
                key=lambda item: (item.descriptor.priority, item.descriptor.transport_id),
            )
        )

    def descriptors(self) -> list[dict[str, Any]]:
        return [item.descriptor.to_dict() for item in self.list()]

    def deliver(
        self,
        request: PermissionRequestRecord,
        *,
        channel: PermissionTransportKind | str,
        transport_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[PermissionDeliveryEnvelope, tuple[PermissionDeliveryReceipt, ...]]:
        selected_kind = PermissionTransportKind(str(getattr(channel, "value", channel)))
        envelope = PermissionDeliveryEnvelope.from_request(request, metadata=metadata)
        candidates = (
            tuple(item for item in self.list(kind=selected_kind) if item.descriptor.transport_id == transport_id)
            if transport_id
            else self.list(kind=selected_kind, enabled_only=True)
        )
        if not candidates:
            raise PermissionTransportUnavailable(
                f"no enabled permission transport for channel {selected_kind.value}"
            )
        receipts: list[PermissionDeliveryReceipt] = []
        for candidate in candidates:
            receipt = candidate.deliver(envelope)
            receipts.append(receipt)
            with self._lock:
                self._delivery_count += 1
                if not receipt.accepted:
                    self._failure_count += 1
            if receipt.accepted:
                break
            if candidate.descriptor.failure_mode is PermissionTransportFailureMode.FAIL_CLOSED:
                break
        if not any(item.accepted for item in receipts):
            if any(item.status is PermissionDeliveryStatus.BACKPRESSURE for item in receipts):
                raise PermissionTransportBackpressure("all selected permission transports are backpressured")
            raise PermissionTransportUnavailable("all selected permission transports rejected delivery")
        return envelope, tuple(receipts)

    def mailbox(
        self,
        channel: PermissionTransportKind | str,
        *,
        transport_id: str = "",
    ) -> MailboxPermissionTransport:
        selected_kind = PermissionTransportKind(str(getattr(channel, "value", channel)))
        for item in self.list(kind=selected_kind):
            if transport_id and item.descriptor.transport_id != transport_id:
                continue
            if isinstance(item, MailboxPermissionTransport):
                return item
        raise PermissionTransportUnavailable(
            f"permission channel {selected_kind.value} has no mailbox projection"
        )

    def normalize_response(
        self,
        request: PermissionRequestRecord,
        response: Mapping[str, Any],
        *,
        channel: PermissionTransportKind | str,
        require_identity_echo: bool = False,
    ) -> PermissionResolutionResponse:
        kind = PermissionTransportKind(str(getattr(channel, "value", channel)))
        descriptors = [item.descriptor for item in self.list(kind=kind, enabled_only=True)]
        if not descriptors or not any(item.authoritative_response_channel for item in descriptors):
            raise PermissionTransportUnavailable(
                f"permission channel {kind.value} is not registered for responses"
            )
        return normalize_permission_response(
            request,
            response,
            channel=kind,
            require_identity_echo=require_identity_echo,
        )

    def acknowledge(self, request_id: str) -> int:
        removed = 0
        for transport in self.list():
            if isinstance(transport, MailboxPermissionTransport):
                removed += transport.acknowledge(request_id)
        return removed

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            delivery_count = self._delivery_count
            failure_count = self._failure_count
        mailboxes = {
            item.descriptor.transport_id: item.metrics()
            for item in self.list()
            if isinstance(item, MailboxPermissionTransport)
        }
        return {
            "transport_count": len(self.list()),
            "enabled_transport_count": len(self.list(enabled_only=True)),
            "delivery_count": delivery_count,
            "failure_count": failure_count,
            "mailboxes": mailboxes,
        }


def normalize_permission_response(
    request: PermissionRequestRecord,
    response: Mapping[str, Any],
    *,
    channel: PermissionTransportKind | str,
    require_identity_echo: bool = False,
) -> PermissionResolutionResponse:
    """Build an exact response without trusting mutable client identity.

    If an identity field is supplied it must match the pending record.  If it
    is omitted, the server copies the immutable field from the record.  This
    permits compact UI responses while preventing forged tool names, MCP
    servers, arguments, scopes, or stale revisions.
    """

    if not isinstance(response, Mapping):
        raise PermissionTransportResponseError("permission response must be an object")
    item = dict(response)
    effect_text = str(item.get("effect") or item.get("decision") or item.get("status") or "").lower()
    aliases = {
        "approved": PermissionEffect.ALLOW,
        "approve": PermissionEffect.ALLOW,
        "allow": PermissionEffect.ALLOW,
        "denied": PermissionEffect.DENY,
        "deny": PermissionEffect.DENY,
        "rejected": PermissionEffect.DENY,
    }
    effect = aliases.get(effect_text)
    if effect is None:
        raise PermissionTransportResponseError("permission response effect must be allow or deny")
    actor_id = str(item.get("actor_id") or item.get("actor") or item.get("resolved_by") or "").strip()
    if not actor_id:
        raise PermissionTransportResponseError("permission response actor_id is required")
    idempotency_key = str(
        item.get("idempotency_key")
        or item.get("response_id")
        or item.get("resolution_id")
        or ""
    ).strip()
    if not idempotency_key:
        raise PermissionTransportResponseError("permission response idempotency_key is required")

    expected_revision = item.get("expected_revision", item.get("request_revision"))
    if expected_revision is None:
        if require_identity_echo:
            raise PermissionTransportResponseError("permission response expected_revision is required")
        expected_revision = request.revision
    try:
        expected_revision_int = int(expected_revision)
    except (TypeError, ValueError) as error:
        raise PermissionTransportResponseError("permission response expected_revision must be an integer") from error

    echoes = {
        "request_id": request.request_id,
        "session_id": request.session_id,
        "task_id": request.task_id,
        "run_id": request.run_id,
        "tool_use_id": request.tool_use_id,
        "arguments_digest": request.arguments_digest,
        "request_fingerprint": request.request_fingerprint,
    }
    aliases_by_name = {
        "tool_use_id": ("tool_use_id", "tool_call_id"),
        "arguments_digest": ("arguments_digest",),
        "request_fingerprint": ("request_fingerprint",),
        "request_id": ("request_id",),
        "session_id": ("session_id",),
        "task_id": ("task_id",),
        "run_id": ("run_id",),
    }
    for name, expected in echoes.items():
        keys = aliases_by_name[name]
        supplied = next((item[key] for key in keys if key in item), None)
        if supplied is None:
            if require_identity_echo:
                raise PermissionTransportResponseError(f"permission response missing {name}")
            continue
        if str(supplied) != expected:
            raise PermissionTransportIdentityError(f"permission response {name} does not match pending request")

    supplied_tool = item.get("tool_identity")
    if isinstance(supplied_tool, Mapping):
        candidate_tool = ToolIdentity.from_dict(supplied_tool)
        if candidate_tool != request.tool_identity:
            raise PermissionTransportIdentityError("permission response tool identity does not match pending request")
    elif require_identity_echo:
        raise PermissionTransportResponseError("permission response missing tool_identity")

    supplied_scope = item.get("scope")
    if isinstance(supplied_scope, Mapping):
        candidate_scope = PermissionScope.from_dict(supplied_scope)
        if canonical_arguments_json(candidate_scope.to_dict()) != canonical_arguments_json(request.scope.to_dict()):
            raise PermissionTransportIdentityError("permission response scope does not match pending request")
    elif require_identity_echo:
        raise PermissionTransportResponseError("permission response missing scope")

    create_rule = item.get("create_rule") is True
    rule_scope_value = item.get("rule_scope")
    rule_scope = (
        PermissionScope.from_dict(rule_scope_value)
        if isinstance(rule_scope_value, Mapping)
        else None
    )
    if create_rule and rule_scope is None:
        rule_scope = request.scope
    return PermissionResolutionResponse(
        request_id=request.request_id,
        session_id=request.session_id,
        tool_use_id=request.tool_use_id,
        tool_identity=request.tool_identity,
        arguments_digest=request.arguments_digest,
        request_fingerprint=request.request_fingerprint,
        scope=request.scope,
        effect=effect,
        actor_id=actor_id,
        expected_revision=expected_revision_int,
        channel=PermissionTransportKind(str(getattr(channel, "value", channel))).value,
        create_rule=create_rule,
        rule_scope=rule_scope,
        reason=str(item.get("reason") or ""),
        idempotency_key=idempotency_key,
        metadata=_safe_metadata(
            {
                **(
                    dict(item.get("metadata") or {})
                    if isinstance(item.get("metadata"), Mapping)
                    else {}
                ),
                "identity_inherited_from_pending": not require_identity_echo,
                "transport_normalized": True,
            }
        ),
    )


def _callback_result(value: Any) -> tuple[bool, str, str, dict[str, Any]]:
    if value is None:
        return True, "", "callback accepted delivery", {}
    if isinstance(value, bool):
        return value, "", "callback accepted delivery" if value else "callback rejected delivery", {}
    if isinstance(value, str):
        return True, value, "callback accepted delivery", {}
    if isinstance(value, Mapping):
        accepted = value.get("accepted", value.get("ok", True)) is True
        reference = str(value.get("remote_reference") or value.get("reference") or "")
        reason = str(value.get("reason") or ("callback accepted delivery" if accepted else "callback rejected delivery"))
        metadata = value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {}
        return accepted, reference, reason, _safe_metadata(metadata)
    return False, "", f"unsupported callback result: {type(value).__name__}", {}


def _receipt(
    envelope: PermissionDeliveryEnvelope,
    descriptor: PermissionTransportDescriptor,
    status: PermissionDeliveryStatus,
    accepted: bool,
    reason: str,
    started: float,
    *,
    remote_reference: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> PermissionDeliveryReceipt:
    return PermissionDeliveryReceipt(
        delivery_id=envelope.delivery_id,
        request_id=envelope.request_id,
        transport_id=descriptor.transport_id,
        channel=descriptor.kind,
        status=status,
        accepted=accepted,
        reason=reason,
        remote_reference=remote_reference,
        latency_ms=max(0, int((monotonic() - started) * 1000)),
        metadata=_safe_metadata(metadata or {}),
    )


_SENSITIVE_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "signature",
    "token",
    "raw_arguments",
    "tool_input",
)


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    def safe(item: Any, *, key: str = "", depth: int = 0) -> Any:
        normalized = key.casefold().replace("-", "_")
        if any(marker in normalized for marker in _SENSITIVE_MARKERS):
            return "[REDACTED]"
        if depth >= 10:
            return "[TRUNCATED_DEPTH]"
        if isinstance(item, Mapping):
            return {
                str(child_key): safe(child, key=str(child_key), depth=depth + 1)
                for child_key, child in list(item.items())[:256]
            }
        if isinstance(item, (list, tuple, set, frozenset)):
            return [safe(child, depth=depth + 1) for child in list(item)[:256]]
        if isinstance(item, (bytes, bytearray, memoryview)):
            return f"[REDACTED_BINARY bytes:{len(item)}]"
        if isinstance(item, str) and len(item) > 4096:
            return f"[TRUNCATED chars:{len(item)}]"
        return to_jsonable(item)

    return {str(key): safe(item, key=str(key)) for key, item in value.items()}


__all__ = [
    "CallbackPermissionTransport",
    "MailboxPermissionTransport",
    "PermissionDeliveryEnvelope",
    "PermissionDeliveryReceipt",
    "PermissionDeliveryStatus",
    "PermissionTransport",
    "PermissionTransportBackpressure",
    "PermissionTransportDescriptor",
    "PermissionTransportError",
    "PermissionTransportFailureMode",
    "PermissionTransportIdentityError",
    "PermissionTransportKind",
    "PermissionTransportRegistry",
    "PermissionTransportResponseError",
    "PermissionTransportUnavailable",
    "normalize_permission_response",
]
