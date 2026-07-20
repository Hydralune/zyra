from __future__ import annotations

"""Authoritative permission control plane for API/CLI/bridge/SDK integration.

This module closes the gap between the foundation guard and interactive
control surfaces.  It is intentionally a facade over the *same*
``PermissionStateStore`` used by ``ToolPermissionRuntime``; it never mirrors
requests in a second approval table.  Delivery, resolution, cancellation,
expiry, standing-rule mutation, mode updates, and retry preparation are
performed through revisioned state transitions and returned with causal Zyra
events.

Security invariants:

* a session id is a selector, never authority;
* every public operation verifies the run/task/workspace-bound custody token;
* actor/channel/capabilities come from a server-created authority object;
* clients cannot choose ``hook``, ``sealed``, ``policy`` or gateway identity;
* allow/deny responses bind the exact immutable pending request;
* denied exact calls receive a durable action deny guard and recovery input;
* an approved request is still not an execution grant; the normal tool guard
  must re-evaluate and atomically claim it at the final side-effect boundary;
* raw arguments, bearer tokens, secrets, and execution grants are never
  projected by this facade.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
import hmac
from pathlib import Path
from threading import RLock
from typing import Any

from zyra_core import EventRecord, new_id, now_iso, to_jsonable

from .canonical import canonical_arguments_json
from .custody import (
    PermissionSessionCustodyBinding,
    PermissionSessionCustodyReceipt,
    PermissionSessionCustodyStore,
)
from .continuation import (
    PermissionContinuationPhase,
    PermissionContinuationRecord,
    PermissionContinuationRuntime,
)
from .events import (
    PermissionEventEnvelope,
    PermissionEventProjector,
    PermissionRuntimeEventKind,
)
from .models import (
    PermissionEffect,
    PermissionEvaluationRequest,
    PermissionMode,
    PermissionRecoveryInput,
    PermissionRequestPhase,
    PermissionRequestRecord,
    PermissionRequestStatus,
    PermissionResolutionResponse,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
)
from .request_queue import (
    PermissionRequestQueue,
    PermissionResolutionCode,
    PermissionResolutionOutcome,
)
from .store import (
    PermissionIdentityMismatch,
    PermissionRequestExpired,
    PermissionRequestTerminal,
    PermissionRuleStore,
    PermissionStateConflict,
    PermissionStateDisabled,
    PermissionStateStore,
)
from .transports import (
    PermissionDeliveryEnvelope,
    PermissionDeliveryReceipt,
    PermissionTransportKind,
    PermissionTransportRegistry,
)


PERMISSION_CONTROL_SCHEMA = "zyra.permission-control.v1"
PERMISSION_CONTROL_OWNER_UNIT = "M1-S03A-02"


class PermissionControlCapability(StrEnum):
    QUERY = "request:query"
    CREATE = "request:create"
    DELIVER = "request:deliver"
    RESOLVE = "request:resolve"
    CANCEL = "request:cancel"
    ABORT = "request:abort"
    EXPIRE = "request:expire"
    RETRY = "request:retry"
    RULE_READ = "rule:read"
    RULE_WRITE = "rule:write"
    RULE_BROADEN = "rule:broaden"
    MODE_READ = "mode:read"
    MODE_WRITE = "mode:write"
    SESSION_REVOKE = "session:revoke"
    AUDIT_READ = "audit:read"


class PermissionControlCode(StrEnum):
    OK = "ok"
    CREATED = "created"
    DELIVERED = "delivered"
    RESOLVED = "resolved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
    RETRY_READY = "retry_ready"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    INVALID = "invalid"
    IDENTITY_MISMATCH = "identity_mismatch"
    DISABLED = "disabled"
    TRANSPORT_FAILED = "transport_failed"


class PermissionControlError(RuntimeError):
    code = PermissionControlCode.INVALID


class PermissionControlForbidden(PermissionControlError):
    code = PermissionControlCode.FORBIDDEN


class PermissionControlNotFound(PermissionControlError):
    code = PermissionControlCode.NOT_FOUND


class PermissionControlIdentityError(PermissionControlError):
    code = PermissionControlCode.IDENTITY_MISMATCH


class PermissionControlDisabled(PermissionControlError):
    code = PermissionControlCode.DISABLED


@dataclass(frozen=True, slots=True)
class PermissionControlAuthority:
    actor_id: str
    channel: PermissionTransportKind
    session_id: str
    run_id: str
    task_id: str
    workspace_root: str
    custody_fingerprint: str
    capabilities: frozenset[PermissionControlCapability]
    principal_id: str = ""
    authority_id: str = field(default_factory=lambda: new_id("permauthority"))
    issued_at: str = field(default_factory=now_iso)
    metadata: dict[str, str] = field(default_factory=dict)
    redaction_secrets: tuple[str, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        if not all(
            (
                self.actor_id,
                self.session_id,
                self.run_id,
                self.task_id,
                self.workspace_root,
                self.custody_fingerprint,
            )
        ):
            raise ValueError("permission control authority requires exact session custody identity")
        object.__setattr__(self, "workspace_root", str(Path(self.workspace_root).resolve()))
        object.__setattr__(
            self,
            "redaction_secrets",
            tuple(dict.fromkeys(str(item) for item in self.redaction_secrets if str(item))),
        )

    def require(self, capability: PermissionControlCapability) -> None:
        if capability not in self.capabilities:
            raise PermissionControlForbidden(
                f"authority {self.authority_id} lacks capability {capability.value}"
            )

    def owns(self, request: PermissionRequestRecord) -> bool:
        return (
            request.session_id == self.session_id
            and request.run_id == self.run_id
            and request.task_id == self.task_id
            and (
                not request.scope.workspace_root
                or Path(request.scope.workspace_root).resolve() == Path(self.workspace_root).resolve()
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority_id": self.authority_id,
            "actor_id": self.actor_id,
            "channel": str(self.channel),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "workspace_root": self.workspace_root,
            "principal_id": self.principal_id,
            "capabilities": sorted(item.value for item in self.capabilities),
            "issued_at": self.issued_at,
            "metadata": dict(self.metadata),
            "custody_verified": True,
            "custody_token_projected": False,
        }


@dataclass(frozen=True, slots=True)
class PermissionQuery:
    session_id: str = ""
    task_id: str = ""
    run_id: str = ""
    request_id: str = ""
    status: PermissionRequestStatus | None = None
    phases: tuple[PermissionRequestPhase, ...] = ()
    tool_name: str = ""
    namespace: str = ""
    server_id: str = ""
    pending_only: bool = False
    include_terminal: bool = True
    offset: int = 0
    limit: int = 100

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("permission query offset cannot be negative")
        if self.limit < 1 or self.limit > 1000:
            raise ValueError("permission query limit must be in [1, 1000]")


@dataclass(frozen=True, slots=True)
class PermissionQueryPage:
    items: tuple[PermissionRequestRecord, ...]
    total: int
    offset: int
    limit: int
    next_offset: int | None
    state_revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PERMISSION_CONTROL_SCHEMA,
            "items": [project_permission_request(item) for item in self.items],
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "next_offset": self.next_offset,
            "state_revision": self.state_revision,
        }


@dataclass(frozen=True, slots=True)
class PermissionRetryDescriptor:
    retry_id: str
    request_id: str
    session_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    tool_use_id: str
    tool_namespace: str
    tool_name: str
    server_id: str
    tool_version: str
    tool_schema_digest: str
    arguments_digest: str
    request_fingerprint: str
    scope_digest: str
    request_revision: int
    created_at: str
    expires_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_request(
        cls,
        request: PermissionRequestRecord,
        *,
        ttl_seconds: float = 120.0,
        metadata: Mapping[str, Any] | None = None,
    ) -> "PermissionRetryDescriptor":
        if request.status is not PermissionRequestStatus.APPROVED:
            raise ValueError("only approved permission requests can prepare retry")
        if request.phase is not PermissionRequestPhase.RESOLVED:
            raise ValueError("permission retry requires a resolved request")
        if request.metadata.get("execution_claim_decision_id"):
            raise ValueError("permission approval was already claimed for execution")
        created = datetime.now(timezone.utc)
        request_expiry = _parse_time(request.expires_at)
        expiry = min(request_expiry, created + timedelta(seconds=max(1.0, ttl_seconds)))
        scope_digest = _digest(request.scope.to_dict())
        return cls(
            retry_id=new_id("permretry"),
            request_id=request.request_id,
            session_id=request.session_id,
            run_id=request.run_id,
            task_id=request.task_id,
            worker_request_id=request.worker_request_id,
            tool_use_id=request.tool_use_id,
            tool_namespace=request.tool_identity.namespace,
            tool_name=request.tool_identity.name,
            server_id=request.tool_identity.server_id,
            tool_version=request.tool_identity.version,
            tool_schema_digest=request.tool_identity.schema_digest,
            arguments_digest=request.arguments_digest,
            request_fingerprint=request.request_fingerprint,
            scope_digest=scope_digest,
            request_revision=request.revision,
            created_at=created.isoformat(),
            expires_at=expiry.isoformat(),
            metadata=_safe_metadata(metadata or {}),
        )

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= _parse_time(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.permission-retry.v1",
            "retry_id": self.retry_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "tool_use_id": self.tool_use_id,
            "tool_identity": {
                "namespace": self.tool_namespace,
                "name": self.tool_name,
                "server_id": self.server_id,
                "version": self.tool_version,
                "schema_digest": self.tool_schema_digest,
            },
            "arguments_digest": self.arguments_digest,
            "request_fingerprint": self.request_fingerprint,
            "scope_digest": self.scope_digest,
            "request_revision": self.request_revision,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "metadata": _safe_metadata(self.metadata),
            "execution_authority": False,
            "raw_arguments_included": False,
        }


@dataclass(frozen=True, slots=True)
class PermissionControlResult:
    ok: bool
    code: PermissionControlCode
    operation: str
    request: PermissionRequestRecord | None = None
    resolution: PermissionResolutionOutcome | None = None
    delivery_envelope: PermissionDeliveryEnvelope | None = None
    delivery_receipts: tuple[PermissionDeliveryReceipt, ...] = ()
    retry: PermissionRetryDescriptor | None = None
    rule: PermissionRuleRecord | None = None
    events: tuple[EventRecord, ...] = ()
    state_revision: int = 0
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PERMISSION_CONTROL_SCHEMA,
            "ok": self.ok,
            "code": str(self.code),
            "operation": self.operation,
            "request": project_permission_request(self.request) if self.request else None,
            "resolution": self.resolution.to_dict() if self.resolution else None,
            "delivery": self.delivery_envelope.to_dict() if self.delivery_envelope else None,
            "delivery_receipts": [item.to_dict() for item in self.delivery_receipts],
            "retry": self.retry.to_dict() if self.retry else None,
            "rule": project_permission_rule(self.rule) if self.rule else None,
            "event_ids": [item.event_id for item in self.events],
            "state_revision": self.state_revision,
            "message": self.message,
            "metadata": _safe_metadata(self.metadata),
        }


class PermissionControlPlane:
    """Single entry point for permission control surfaces."""

    def __init__(
        self,
        state_store: PermissionStateStore,
        *,
        transport_registry: PermissionTransportRegistry | None = None,
        event_projector: PermissionEventProjector | None = None,
        disabled: bool = False,
        allow_standing_rules: bool = False,
        allow_mode_updates: bool = True,
    ) -> None:
        if not isinstance(state_store, PermissionStateStore):
            raise TypeError("PermissionControlPlane requires PermissionStateStore")
        self.state_store = state_store
        self.custody_store = PermissionSessionCustodyStore(state_store)
        self.transport_registry = transport_registry or PermissionTransportRegistry.with_default_mailboxes()
        self.event_projector = event_projector or PermissionEventProjector(
            owner_unit=PERMISSION_CONTROL_OWNER_UNIT,
            runtime_id="zyra-permission-control-plane",
        )
        self.disabled = bool(disabled)
        self.allow_standing_rules = bool(allow_standing_rules)
        self.allow_mode_updates = bool(allow_mode_updates)
        self._lock = RLock()
        self._operation_counts: dict[str, int] = {}

    @classmethod
    def from_path(
        cls,
        state_path: str | Path,
        **kwargs: Any,
    ) -> "PermissionControlPlane":
        return cls(PermissionStateStore(state_path), **kwargs)

    def authority_from_custody(
        self,
        *,
        binding: PermissionSessionCustodyBinding,
        custody_token: str,
        actor_id: str,
        channel: PermissionTransportKind | str,
        capabilities: Iterable[PermissionControlCapability | str] | None = None,
        principal_id: str = "",
        metadata: Mapping[str, str] | None = None,
    ) -> PermissionControlAuthority:
        self._ensure_available()
        receipt = self.custody_store.verify(binding, presented_token=custody_token)
        selected = (
            frozenset(
                PermissionControlCapability(str(getattr(item, "value", item)))
                for item in capabilities
            )
            if capabilities is not None
            else _default_capabilities_for_channel(
                PermissionTransportKind(str(getattr(channel, "value", channel)))
            )
        )
        return self._authority_from_receipt(
            receipt,
            actor_id=actor_id,
            channel=PermissionTransportKind(str(getattr(channel, "value", channel))),
            capabilities=selected,
            principal_id=principal_id,
            metadata=metadata,
            redaction_secrets=(custody_token,),
        )

    def authority_from_receipt(
        self,
        receipt: PermissionSessionCustodyReceipt,
        *,
        actor_id: str,
        channel: PermissionTransportKind | str,
        capabilities: Iterable[PermissionControlCapability | str] | None = None,
        principal_id: str = "",
        metadata: Mapping[str, str] | None = None,
    ) -> PermissionControlAuthority:
        self._ensure_available()
        selected_kind = PermissionTransportKind(str(getattr(channel, "value", channel)))
        selected = (
            frozenset(
                PermissionControlCapability(str(getattr(item, "value", item)))
                for item in capabilities
            )
            if capabilities is not None
            else _default_capabilities_for_channel(selected_kind)
        )
        return self._authority_from_receipt(
            receipt,
            actor_id=actor_id,
            channel=selected_kind,
            capabilities=selected,
            principal_id=principal_id,
            metadata=metadata,
            redaction_secrets=(receipt.token,) if receipt.token else (),
        )

    def _authority_from_receipt(
        self,
        receipt: PermissionSessionCustodyReceipt,
        *,
        actor_id: str,
        channel: PermissionTransportKind,
        capabilities: frozenset[PermissionControlCapability],
        principal_id: str,
        metadata: Mapping[str, str] | None,
        redaction_secrets: Iterable[str] = (),
    ) -> PermissionControlAuthority:
        if not receipt.verified:
            raise PermissionControlForbidden("permission session custody is not verified")
        protected = tuple(redaction_secrets)
        _reject_secret_echo(
            {
                "actor_id": actor_id,
                "principal_id": principal_id,
                "metadata": dict(metadata or {}),
            },
            protected,
        )
        return PermissionControlAuthority(
            actor_id=actor_id,
            channel=channel,
            session_id=receipt.binding.session_id,
            run_id=receipt.binding.run_id,
            task_id=receipt.binding.task_id,
            workspace_root=receipt.binding.workspace_root,
            custody_fingerprint=receipt.custody_fingerprint,
            capabilities=capabilities,
            principal_id=principal_id,
            metadata={
                "custody_id": receipt.custody_id,
                "custody_epoch": str(receipt.epoch),
                "owner_unit": PERMISSION_CONTROL_OWNER_UNIT,
                **dict(metadata or {}),
            },
            redaction_secrets=protected,
        )

    def query(
        self,
        authority: PermissionControlAuthority,
        query: PermissionQuery | None = None,
    ) -> PermissionQueryPage:
        self._ensure_available()
        authority.require(PermissionControlCapability.QUERY)
        selected = query or PermissionQuery(session_id=authority.session_id)
        if selected.session_id and selected.session_id != authority.session_id:
            raise PermissionControlIdentityError("permission query cannot cross session custody")
        if selected.task_id and selected.task_id != authority.task_id:
            raise PermissionControlIdentityError("permission query cannot cross task custody")
        if selected.run_id and selected.run_id != authority.run_id:
            raise PermissionControlIdentityError("permission query cannot cross run custody")
        records = self.state_store.list_requests(session_id=authority.session_id)
        filtered = [
            item
            for item in records
            if authority.owns(item)
            and (not selected.request_id or item.request_id == selected.request_id)
            and (selected.status is None or item.status is selected.status)
            and (not selected.phases or item.phase in selected.phases)
            and (not selected.pending_only or not item.terminal)
            and (selected.include_terminal or not item.terminal)
            and (not selected.tool_name or item.tool_identity.name == selected.tool_name)
            and (not selected.namespace or item.tool_identity.namespace == selected.namespace)
            and (not selected.server_id or item.tool_identity.server_id == selected.server_id)
        ]
        filtered.sort(key=lambda item: (item.created_at, item.request_id), reverse=True)
        page = tuple(filtered[selected.offset : selected.offset + selected.limit])
        next_offset = (
            selected.offset + selected.limit
            if selected.offset + selected.limit < len(filtered)
            else None
        )
        self._count("query")
        return PermissionQueryPage(
            items=page,
            total=len(filtered),
            offset=selected.offset,
            limit=selected.limit,
            next_offset=next_offset,
            state_revision=int(self.state_store.read_state().get("revision") or 0),
        )

    def get_request(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
    ) -> PermissionRequestRecord:
        page = self.query(
            authority,
            PermissionQuery(session_id=authority.session_id, request_id=request_id, limit=1),
        )
        if not page.items:
            raise PermissionControlNotFound(f"permission request not found: {request_id}")
        return page.items[0]

    def create(
        self,
        authority: PermissionControlAuthority,
        evaluation: PermissionEvaluationRequest,
        *,
        reason_code: str,
        reason: str,
        ttl_seconds: float = 300.0,
        metadata: Mapping[str, Any] | None = None,
    ) -> PermissionControlResult:
        """Register an ASK produced by a trusted runtime/SDK boundary.

        The created row is not executable authority.  A normal guard must
        later find an exact approved request and issue/consume its own grant.
        """

        self._ensure_available()
        authority.require(PermissionControlCapability.CREATE)
        _reject_secret_echo(
            {"reason": reason, "metadata": dict(metadata or {})},
            authority.redaction_secrets,
        )
        self._assert_evaluation_owned(authority, evaluation)
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("permission request ttl must be in (0, 3600]")
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        scope = PermissionScope(
            kind=PermissionScopeKind.ACTION,
            session_id=evaluation.session_id,
            task_id=evaluation.task_id,
            run_id=evaluation.run_id,
            workspace_root=evaluation.workspace_root,
            principal_id=evaluation.principal_id,
            tool_namespace=evaluation.tool_identity.namespace,
            tool_name=evaluation.tool_identity.name,
            server_id=evaluation.tool_identity.server_id,
            argument_digest=evaluation.arguments_digest,
            request_fingerprint=evaluation.request_fingerprint,
            metadata={"exact_identity": True, "owner_unit": PERMISSION_CONTROL_OWNER_UNIT},
        )
        record = PermissionRequestRecord(
            session_id=evaluation.session_id,
            task_id=evaluation.task_id,
            run_id=evaluation.run_id,
            worker_request_id=evaluation.worker_request_id,
            tool_use_id=evaluation.tool_use_id,
            tool_identity=evaluation.tool_identity,
            arguments_digest=evaluation.arguments_digest,
            request_fingerprint=evaluation.request_fingerprint,
            scope=scope,
            expires_at=expiry,
            reason_code=reason_code,
            reason=reason,
            mode=evaluation.mode,
            metadata=_safe_metadata(
                {
                    "created_by_authority": authority.authority_id,
                    "created_by_channel": authority.channel.value,
                    "runtime_attested": True,
                    "execution_authority": False,
                    **dict(metadata or {}),
                }
            ),
        )
        queue = self._queue(authority.session_id)
        stored = queue.create(record)
        event = self.event_projector.request_event(
            stored,
            kind=PermissionRuntimeEventKind.REQUEST_CREATED,
            run_id=stored.run_id,
            task_id=stored.task_id,
            node_id=evaluation.node_id,
            worker_request_id=stored.worker_request_id,
        )
        self._link_event(
            stored.request_id,
            phase="request_created",
            event_id=event.event_id,
            cause_event_id="",
        )
        self._count("create")
        return self._result(
            True,
            PermissionControlCode.CREATED,
            "create",
            request=stored,
            events=(event,),
            message="permission request registered; no execution authority was created",
        )

    def deliver(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        *,
        channel: PermissionTransportKind | str | None = None,
        transport_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        node_id: str | None = None,
    ) -> PermissionControlResult:
        self._ensure_available()
        authority.require(PermissionControlCapability.DELIVER)
        _reject_secret_echo(
            {"transport_id": transport_id, "metadata": dict(metadata or {})},
            authority.redaction_secrets,
        )
        request = self.get_request(authority, request_id)
        if request.terminal:
            raise PermissionRequestTerminal(f"cannot deliver terminal request {request_id}")
        selected_channel = (
            PermissionTransportKind(str(getattr(channel, "value", channel)))
            if channel is not None
            else authority.channel
        )
        # A caller may choose among its own UI surfaces, never impersonate a
        # privileged runtime/policy channel.
        if selected_channel is not authority.channel and selected_channel not in {
            PermissionTransportKind.API,
            PermissionTransportKind.CLI,
            PermissionTransportKind.INTERACTIVE,
            PermissionTransportKind.STRUCTURED_IO,
        }:
            raise PermissionControlForbidden("authority cannot select a privileged delivery channel")
        envelope, receipts = self.transport_registry.deliver(
            request,
            channel=selected_channel,
            transport_id=transport_id,
            metadata={
                "authority_id": authority.authority_id,
                "actor_id": authority.actor_id,
                **dict(metadata or {}),
            },
        )
        marked = self._queue(authority.session_id).mark_delivered(
            request.request_id,
            expected_request_revision=request.revision,
            channel=selected_channel.value,
        )
        continuation = self._sync_continuation_delivery(marked)
        event = self.event_projector.request_event(
            marked,
            kind=PermissionRuntimeEventKind.REQUEST_DELIVERED,
            run_id=marked.run_id,
            task_id=marked.task_id,
            node_id=node_id,
            worker_request_id=marked.worker_request_id,
            cause_event_id=self._latest_event_id(request.request_id),
        )
        self._link_event(
            marked.request_id,
            phase="request_delivered",
            event_id=event.event_id,
            cause_event_id=self._latest_event_id(marked.request_id),
        )
        self._count("deliver")
        return self._result(
            True,
            PermissionControlCode.DELIVERED,
            "deliver",
            request=marked,
            delivery_envelope=envelope,
            delivery_receipts=receipts,
            events=(event,),
            message="permission request delivered through registered transport",
            metadata={
                "continuation_phase": continuation.phase.value if continuation else "not_parked",
            },
        )

    def resolve(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        response: Mapping[str, Any],
        *,
        require_identity_echo: bool = False,
        node_id: str | None = None,
    ) -> PermissionControlResult:
        self._ensure_available()
        authority.require(PermissionControlCapability.RESOLVE)
        _reject_secret_echo(response, authority.redaction_secrets)
        request = self.get_request(authority, request_id)
        supplied_actor = str(
            response.get("actor_id")
            or response.get("actor")
            or response.get("resolved_by")
            or ""
        ).strip()
        if supplied_actor and supplied_actor != authority.actor_id:
            raise PermissionControlIdentityError(
                "permission response actor does not match authenticated authority"
            )
        supplied_channel = str(response.get("channel") or "").strip()
        if supplied_channel and supplied_channel != authority.channel.value:
            raise PermissionControlForbidden(
                "permission response channel is owned by the authenticated transport"
            )
        stamped_response = {
            **dict(response),
            "actor_id": authority.actor_id,
            "channel": authority.channel.value,
        }
        normalized = self.transport_registry.normalize_response(
            request,
            stamped_response,
            channel=authority.channel,
            require_identity_echo=require_identity_echo,
        )
        normalized = self._restrict_rule_scope(authority, request, normalized)
        outcome = self._queue(authority.session_id).resolve(normalized)
        if not outcome.accepted:
            event = self._resolution_rejected_event(
                authority,
                request,
                outcome,
                node_id=node_id,
            )
            self._count(f"resolve_rejected:{outcome.code.value}")
            return self._result(
                False,
                _control_code_for_resolution(outcome.code),
                "resolve",
                request=outcome.request or request,
                resolution=outcome,
                events=(event,),
                message=outcome.reason,
                metadata={"pending_unchanged": not (outcome.request and outcome.request.terminal)},
            )
        resolved = outcome.request
        if resolved is None:
            raise PermissionStateConflict("accepted permission resolution returned no request")
        continuation = self._sync_continuation_resolution(resolved)
        extra_events: list[EventRecord] = []
        if resolved.status is PermissionRequestStatus.DENIED:
            deny_rule = self._install_exact_deny_rule(resolved, authority)
            recovery = self._recovery_for_terminal(resolved, reason="user denied exact permission request")
            extra_events.append(
                self._recovery_event(
                    resolved,
                    recovery,
                    node_id=node_id,
                    cause_event_id="",
                )
            )
        else:
            deny_rule = None
        resolved_cause = self._latest_event_id(resolved.request_id)
        resolved_event = self.event_projector.request_event(
            resolved,
            kind=PermissionRuntimeEventKind.REQUEST_RESOLVED,
            run_id=resolved.run_id,
            task_id=resolved.task_id,
            node_id=node_id,
            worker_request_id=resolved.worker_request_id,
            cause_event_id=resolved_cause,
        )
        self._link_event(
            resolved.request_id,
            phase="request_resolved",
            event_id=resolved_event.event_id,
            cause_event_id=resolved_cause,
        )
        if extra_events:
            # Rebuild recovery event with a causal edge from resolution.
            recovery = self._recovery_for_terminal(resolved, reason="user denied exact permission request")
            extra_events = [
                self._recovery_event(
                    resolved,
                    recovery,
                    node_id=node_id,
                    cause_event_id=resolved_event.event_id,
                )
            ]
        self.transport_registry.acknowledge(resolved.request_id)
        self._count("resolve_allow" if resolved.status is PermissionRequestStatus.APPROVED else "resolve_deny")
        return self._result(
            True,
            PermissionControlCode.RESOLVED
            if resolved.status is PermissionRequestStatus.APPROVED
            else PermissionControlCode.DENIED,
            "resolve",
            request=resolved,
            resolution=outcome,
            rule=deny_rule,
            events=(resolved_event, *extra_events),
            message=(
                "exact permission request approved; normal guard must still claim it"
                if resolved.status is PermissionRequestStatus.APPROVED
                else "exact permission request denied and recovery guard installed"
            ),
            metadata={
                "execution_grant_issued": False,
                "human_intervention": authority.channel
                in {PermissionTransportKind.API, PermissionTransportKind.CLI, PermissionTransportKind.INTERACTIVE},
                "continuation_phase": continuation.phase.value if continuation else "not_parked",
            },
        )

    def cancel(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        *,
        reason: str,
        node_id: str | None = None,
    ) -> PermissionControlResult:
        return self._terminal_transition(
            authority,
            request_id,
            capability=PermissionControlCapability.CANCEL,
            phase=PermissionRequestPhase.CANCELLED,
            code=PermissionControlCode.CANCELLED,
            kind=PermissionRuntimeEventKind.REQUEST_CANCELLED,
            reason=reason,
            node_id=node_id,
        )

    def abort(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        *,
        reason: str,
        node_id: str | None = None,
    ) -> PermissionControlResult:
        return self._terminal_transition(
            authority,
            request_id,
            capability=PermissionControlCapability.ABORT,
            phase=PermissionRequestPhase.ABORTED,
            code=PermissionControlCode.ABORTED,
            kind=PermissionRuntimeEventKind.REQUEST_ABORTED,
            reason=reason,
            node_id=node_id,
        )

    def expire_due(
        self,
        authority: PermissionControlAuthority,
        *,
        node_id: str | None = None,
    ) -> tuple[PermissionControlResult, ...]:
        self._ensure_available()
        authority.require(PermissionControlCapability.EXPIRE)
        expired = self._queue(authority.session_id).expire_due()
        results: list[PermissionControlResult] = []
        for request in expired:
            if not authority.owns(request):
                continue
            continuation = self._sync_continuation_expiry(request)
            event = self.event_projector.request_event(
                request,
                kind=PermissionRuntimeEventKind.REQUEST_EXPIRED,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=node_id,
                worker_request_id=request.worker_request_id,
                cause_event_id=self._latest_event_id(request.request_id),
            )
            self._link_event(
                request.request_id,
                phase="request_expired",
                event_id=event.event_id,
                cause_event_id=self._latest_event_id(request.request_id),
            )
            recovery = self._recovery_for_terminal(request, reason="permission request expired")
            recovery_event = self._recovery_event(
                request,
                recovery,
                node_id=node_id,
                cause_event_id=event.event_id,
            )
            self.transport_registry.acknowledge(request.request_id)
            results.append(
                self._result(
                    True,
                    PermissionControlCode.EXPIRED,
                    "expire",
                    request=request,
                    events=(event, recovery_event),
                    message="permission request expired and recovery input was produced",
                    metadata={
                        "continuation_phase": continuation.phase.value if continuation else "not_parked",
                    },
                )
            )
        self._count("expire", delta=len(results))
        return tuple(results)

    def prepare_retry(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        *,
        ttl_seconds: float = 120.0,
        metadata: Mapping[str, Any] | None = None,
        node_id: str | None = None,
    ) -> PermissionControlResult:
        self._ensure_available()
        authority.require(PermissionControlCapability.RETRY)
        _reject_secret_echo(
            {"metadata": dict(metadata or {})},
            authority.redaction_secrets,
        )
        request = self.get_request(authority, request_id)
        self._sync_continuation_resolution(request)
        if request.status is PermissionRequestStatus.DENIED:
            recovery = self._recovery_for_terminal(request, reason="denied request cannot retry unchanged")
            event = self._recovery_event(request, recovery, node_id=node_id, cause_event_id="")
            return self._result(
                False,
                PermissionControlCode.DENIED,
                "prepare_retry",
                request=request,
                events=(event,),
                message="denied exact call must be narrowed or replanned",
            )
        retry = PermissionRetryDescriptor.from_request(
            request,
            ttl_seconds=ttl_seconds,
            metadata={
                "authority_id": authority.authority_id,
                "payload_owner": "02B session store or caller exact replay",
                **dict(metadata or {}),
            },
        )
        self._persist_retry_descriptor(authority, retry)
        retry_cause = self._latest_event_id(request.request_id)
        event = self.event_projector.event(
            PermissionEventEnvelope(
                kind=PermissionRuntimeEventKind.STATE_RESTORED,
                session_id=request.session_id,
                worker_request_id=request.worker_request_id,
                tool_call_id=request.tool_use_id,
                request_id=request.request_id,
                arguments_digest=request.arguments_digest,
                phase="permission_retry_prepared",
                cause_event_id=retry_cause,
                payload=retry.to_dict(),
            ),
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=node_id,
        )
        self._link_event(
            request.request_id,
            phase="permission_retry_prepared",
            event_id=event.event_id,
            cause_event_id=retry_cause,
        )
        self._count("prepare_retry")
        return self._result(
            True,
            PermissionControlCode.RETRY_READY,
            "prepare_retry",
            request=request,
            retry=retry,
            events=(event,),
            message="retry metadata prepared; normal guard remains execution authority",
        )

    def retry_descriptor(
        self,
        authority: PermissionControlAuthority,
        retry_id: str,
    ) -> PermissionRetryDescriptor:
        self._ensure_available()
        authority.require(PermissionControlCapability.RETRY)
        state = self.state_store.read_state()
        integration = state.get("metadata", {}).get("permission_integration", {})
        retries = integration.get("retry_descriptors", {}) if isinstance(integration, Mapping) else {}
        value = retries.get(retry_id) if isinstance(retries, Mapping) else None
        if not isinstance(value, Mapping):
            raise PermissionControlNotFound(f"permission retry not found: {retry_id}")
        retry = _retry_from_mapping(value)
        if (
            retry.session_id != authority.session_id
            or retry.task_id != authority.task_id
            or retry.run_id != authority.run_id
        ):
            raise PermissionControlIdentityError("permission retry belongs to another custody scope")
        if retry.expired:
            raise PermissionRequestExpired(f"permission retry expired: {retry_id}")
        return retry

    def verify_retry_replay(
        self,
        authority: PermissionControlAuthority,
        retry_id: str,
        evaluation: PermissionEvaluationRequest,
    ) -> PermissionRetryDescriptor:
        retry = self.retry_descriptor(authority, retry_id)
        self._assert_evaluation_owned(authority, evaluation)
        mismatches: list[str] = []
        expected = {
            "session_id": retry.session_id,
            "run_id": retry.run_id,
            "task_id": retry.task_id,
            "tool_use_id": retry.tool_use_id,
            "tool_namespace": retry.tool_namespace,
            "tool_name": retry.tool_name,
            "server_id": retry.server_id,
            "tool_version": retry.tool_version,
            "tool_schema_digest": retry.tool_schema_digest,
            "arguments_digest": retry.arguments_digest,
            "request_fingerprint": retry.request_fingerprint,
        }
        actual = {
            "session_id": evaluation.session_id,
            "run_id": evaluation.run_id,
            "task_id": evaluation.task_id,
            "tool_use_id": evaluation.tool_use_id,
            "tool_namespace": evaluation.tool_identity.namespace,
            "tool_name": evaluation.tool_identity.name,
            "server_id": evaluation.tool_identity.server_id,
            "tool_version": evaluation.tool_identity.version,
            "tool_schema_digest": evaluation.tool_identity.schema_digest,
            "arguments_digest": evaluation.arguments_digest,
            "request_fingerprint": evaluation.request_fingerprint,
        }
        for name, expected_value in expected.items():
            actual_value = actual[name]
            if name in {"arguments_digest", "request_fingerprint", "tool_schema_digest"}:
                if not hmac.compare_digest(expected_value, actual_value):
                    mismatches.append(name)
            elif expected_value != actual_value:
                mismatches.append(name)
        if mismatches:
            raise PermissionControlIdentityError(
                "permission retry replay mismatch: " + ", ".join(mismatches)
            )
        return retry

    def list_rules(
        self,
        authority: PermissionControlAuthority,
        *,
        include_inactive: bool = True,
    ) -> tuple[PermissionRuleRecord, ...]:
        self._ensure_available()
        authority.require(PermissionControlCapability.RULE_READ)
        return tuple(
            self._rule_store(authority.session_id).list(
                effective=True,
                include_inactive=include_inactive,
            )
        )

    def add_rule(
        self,
        authority: PermissionControlAuthority,
        rule: PermissionRuleRecord,
        *,
        node_id: str | None = None,
    ) -> PermissionControlResult:
        self._ensure_available()
        authority.require(PermissionControlCapability.RULE_WRITE)
        _reject_secret_echo(rule.to_dict(), authority.redaction_secrets)
        if rule.source not in {PermissionRuleSource.USER, PermissionRuleSource.SESSION, PermissionRuleSource.CLI}:
            raise PermissionControlForbidden("interactive control cannot impersonate policy/builtin rule source")
        if rule.scope.session_id and rule.scope.session_id != authority.session_id:
            raise PermissionControlIdentityError("permission rule scope crosses session custody")
        if rule.scope.task_id and rule.scope.task_id != authority.task_id:
            raise PermissionControlIdentityError("permission rule scope crosses task custody")
        if rule.scope.run_id and rule.scope.run_id != authority.run_id:
            raise PermissionControlIdentityError("permission rule scope crosses run custody")
        if rule.scope.workspace_root and Path(rule.scope.workspace_root).resolve() != Path(authority.workspace_root).resolve():
            raise PermissionControlIdentityError("permission rule workspace crosses custody")
        if rule.scope.kind is not PermissionScopeKind.ACTION:
            authority.require(PermissionControlCapability.RULE_BROADEN)
            if not self.allow_standing_rules:
                raise PermissionControlForbidden("deployment disabled standing permission rules")
        stored = self._rule_store(authority.session_id).add(
            replace(
                rule,
                source=PermissionRuleSource.SESSION,
                metadata={
                    **rule.metadata,
                    "authority_id": authority.authority_id,
                    "actor_id": authority.actor_id,
                    "channel": authority.channel.value,
                    "owner_unit": PERMISSION_CONTROL_OWNER_UNIT,
                },
            )
        )
        event = self._control_event(
            authority,
            phase="permission_rule_added",
            payload={"rule": project_permission_rule(stored)},
            node_id=node_id,
        )
        self._count("add_rule")
        return self._result(
            True,
            PermissionControlCode.CREATED,
            "add_rule",
            rule=stored,
            events=(event,),
            message="permission rule added to session overlay",
        )

    def remove_rule(
        self,
        authority: PermissionControlAuthority,
        rule_id: str,
        *,
        node_id: str | None = None,
    ) -> PermissionControlResult:
        self._ensure_available()
        authority.require(PermissionControlCapability.RULE_WRITE)
        rules = {item.rule_id: item for item in self.list_rules(authority)}
        rule = rules.get(rule_id)
        if rule is None:
            raise PermissionControlNotFound(f"permission rule not found: {rule_id}")
        if rule.source is PermissionRuleSource.BUILTIN_SAFETY:
            raise PermissionControlForbidden("builtin safety rules cannot be removed")
        removed = self._rule_store(authority.session_id).remove(rule_id)
        if not removed:
            raise PermissionControlNotFound(f"permission rule not found: {rule_id}")
        event = self._control_event(
            authority,
            phase="permission_rule_removed",
            payload={"rule_id": rule_id},
            node_id=node_id,
        )
        self._count("remove_rule")
        return self._result(
            True,
            PermissionControlCode.OK,
            "remove_rule",
            events=(event,),
            message="permission rule removed from session overlay",
        )

    def session_mode(self, session_id: str, *, fallback: str = "default") -> str:
        self._ensure_available()
        state = self.state_store.read_state()
        integration = state.get("metadata", {}).get("permission_integration", {})
        modes = integration.get("session_modes", {}) if isinstance(integration, Mapping) else {}
        value = modes.get(session_id) if isinstance(modes, Mapping) else None
        if not isinstance(value, Mapping):
            return fallback
        mode = str(value.get("mode") or fallback)
        try:
            return PermissionMode(mode).value
        except ValueError:
            return fallback

    def update_mode(
        self,
        authority: PermissionControlAuthority,
        mode: PermissionMode | str,
        *,
        expected_mode_revision: int | None = None,
        node_id: str | None = None,
    ) -> PermissionControlResult:
        self._ensure_available()
        authority.require(PermissionControlCapability.MODE_WRITE)
        if not self.allow_mode_updates:
            raise PermissionControlForbidden("deployment disabled permission mode updates")
        selected = PermissionMode(str(getattr(mode, "value", mode)))
        if selected in {PermissionMode.BYPASS, PermissionMode.AUTO}:
            raise PermissionControlForbidden("interactive control cannot enable bypass/auto authority")
        holder: list[dict[str, Any]] = []

        def mutate(state: dict[str, Any]) -> None:
            integration = state.setdefault("metadata", {}).setdefault(
                "permission_integration",
                _empty_integration_state(),
            )
            modes = integration.setdefault("session_modes", {})
            current = modes.get(authority.session_id) if isinstance(modes, Mapping) else None
            current_revision = int(current.get("revision") or 0) if isinstance(current, Mapping) else 0
            current_mode = (
                str(current.get("mode") or PermissionMode.DEFAULT.value)
                if isinstance(current, Mapping)
                else PermissionMode.DEFAULT.value
            )
            if expected_mode_revision is not None and current_revision != expected_mode_revision:
                raise PermissionStateConflict(
                    f"permission mode revision conflict: expected {expected_mode_revision}, actual {current_revision}"
                )
            if current_mode == PermissionMode.SEALED.value and selected is not PermissionMode.SEALED:
                raise PermissionControlForbidden(
                    "sealed permission mode is sticky for the current session"
                )
            record = {
                "session_id": authority.session_id,
                "mode": selected.value,
                "previous_mode": current_mode,
                "revision": current_revision + 1,
                "updated_at": now_iso(),
                "actor_id": authority.actor_id,
                "authority_id": authority.authority_id,
                "channel": authority.channel.value,
                "bypass_available": False,
                "auto_available": False,
            }
            modes[authority.session_id] = record
            holder.append(record)

        self.state_store.mutate(mutate)
        record = holder[0]
        event = self.event_projector.event(
            PermissionEventEnvelope(
                kind=PermissionRuntimeEventKind.MODE_TRANSITIONED,
                session_id=authority.session_id,
                worker_request_id="",
                phase="permission_mode_transitioned",
                payload=record,
            ),
            run_id=authority.run_id,
            task_id=authority.task_id,
            node_id=node_id,
        )
        self._count("update_mode")
        return self._result(
            True,
            PermissionControlCode.OK,
            "update_mode",
            events=(event,),
            message=f"permission session mode changed to {selected.value}",
            metadata={"mode": record},
        )

    def decisions(
        self,
        authority: PermissionControlAuthority,
        *,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        self._ensure_available()
        authority.require(PermissionControlCapability.AUDIT_READ)
        records = self.state_store.list_decisions(session_id=authority.session_id)
        selected = [
            item
            for item in records
            if item.run_id == authority.run_id and item.task_id == authority.task_id
        ]
        selected.sort(key=lambda item: (item.created_at, item.decision_id), reverse=True)
        return tuple(project_permission_decision(item) for item in selected[: max(1, min(limit, 1000))])

    def metrics(self) -> dict[str, Any]:
        self._ensure_available()
        state = self.state_store.read_state()
        with self._lock:
            operations = dict(self._operation_counts)
        integration = state.get("metadata", {}).get("permission_integration", {})
        if not isinstance(integration, Mapping):
            integration = {}
        return {
            "schema": PERMISSION_CONTROL_SCHEMA,
            "owner_unit": PERMISSION_CONTROL_OWNER_UNIT,
            "state_revision": int(state.get("revision") or 0),
            "request_count": len(state.get("requests") or {}),
            "decision_count": len(state.get("decisions") or []),
            "operation_counts": operations,
            "transport": self.transport_registry.metrics(),
            "integration_counts": {
                "continuations": _mapping_size(integration.get("continuations")),
                "retry_descriptors": _mapping_size(integration.get("retry_descriptors")),
                "session_modes": _mapping_size(integration.get("session_modes")),
                "event_link_requests": _mapping_size(integration.get("event_links")),
                "exact_deny_guards": _mapping_size(integration.get("exact_deny_guards")),
            },
            "integration_details_require_custody": True,
            "legacy_store_is_authority": False,
        }

    def _latest_event_id(self, request_id: str) -> str:
        state = self.state_store.read_state()
        integration = state.get("metadata", {}).get("permission_integration", {})
        links = integration.get("event_links", {}) if isinstance(integration, Mapping) else {}
        request_links = links.get(request_id) if isinstance(links, Mapping) else None
        if not isinstance(request_links, Sequence) or isinstance(request_links, (str, bytes)):
            return ""
        for value in reversed(request_links):
            if isinstance(value, Mapping) and str(value.get("event_id") or ""):
                return str(value["event_id"])
        return ""

    def _link_event(
        self,
        request_id: str,
        *,
        phase: str,
        event_id: str,
        cause_event_id: str,
    ) -> None:
        if not all((request_id, phase, event_id)):
            raise ValueError("permission event link requires request/phase/event identity")

        def mutate(state: dict[str, Any]) -> None:
            integration = state.setdefault("metadata", {}).setdefault(
                "permission_integration",
                _empty_integration_state(),
            )
            links = integration.setdefault("event_links", {})
            history = links.setdefault(request_id, [])
            if not isinstance(history, list):
                raise PermissionStateConflict("permission event link history is corrupt")
            if any(
                isinstance(item, Mapping) and item.get("event_id") == event_id
                for item in history
            ):
                return
            history.append(
                {
                    "phase": str(phase),
                    "event_id": str(event_id),
                    "cause_event_id": str(cause_event_id or ""),
                    "linked_at": now_iso(),
                }
            )
            del history[:-32]

        self.state_store.mutate(mutate)

    def _continuation_for(
        self,
        request: PermissionRequestRecord,
    ) -> tuple[PermissionContinuationRuntime, PermissionContinuationRecord] | None:
        runtime = PermissionContinuationRuntime(
            self.state_store,
            session_id=request.session_id,
        )
        try:
            return runtime, runtime.get(request.request_id)
        except KeyError:
            return None

    def _sync_continuation_delivery(
        self,
        request: PermissionRequestRecord,
    ) -> PermissionContinuationRecord | None:
        selected = self._continuation_for(request)
        if selected is None:
            return None
        runtime, record = selected
        if record.phase is PermissionContinuationPhase.PARKED:
            return runtime.deliver(
                request,
                expected_record_revision=record.revision,
            )
        return record

    def _sync_continuation_resolution(
        self,
        request: PermissionRequestRecord,
    ) -> PermissionContinuationRecord | None:
        if (
            request.phase is not PermissionRequestPhase.RESOLVED
            or request.status
            not in {PermissionRequestStatus.APPROVED, PermissionRequestStatus.DENIED}
        ):
            return None
        selected = self._continuation_for(request)
        if selected is None:
            return None
        runtime, record = selected
        if record.phase in {
            PermissionContinuationPhase.PARKED,
            PermissionContinuationPhase.DELIVERED,
        }:
            return runtime.resolution_ready(
                request,
                expected_record_revision=record.revision,
            )
        return record

    def _sync_continuation_cancel(
        self,
        request: PermissionRequestRecord,
        *,
        reason: str,
    ) -> PermissionContinuationRecord | None:
        selected = self._continuation_for(request)
        if selected is None:
            return None
        runtime, record = selected
        if record.phase in {
            PermissionContinuationPhase.PARKED,
            PermissionContinuationPhase.DELIVERED,
            PermissionContinuationPhase.RESOLUTION_READY,
        }:
            return runtime.cancel(
                request.request_id,
                reason=reason,
                expected_record_revision=record.revision,
            )
        return record

    def _sync_continuation_expiry(
        self,
        request: PermissionRequestRecord,
    ) -> PermissionContinuationRecord | None:
        selected = self._continuation_for(request)
        if selected is None:
            return None
        runtime, record = selected
        if record.phase in {
            PermissionContinuationPhase.PARKED,
            PermissionContinuationPhase.DELIVERED,
            PermissionContinuationPhase.RESOLUTION_READY,
        }:
            return runtime.expire(
                request.request_id,
                expected_record_revision=record.revision,
                reason="permission_request_expired",
                force=True,
            )
        return record

    def _terminal_transition(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        *,
        capability: PermissionControlCapability,
        phase: PermissionRequestPhase,
        code: PermissionControlCode,
        kind: PermissionRuntimeEventKind,
        reason: str,
        node_id: str | None,
    ) -> PermissionControlResult:
        self._ensure_available()
        authority.require(capability)
        _reject_secret_echo({"reason": reason}, authority.redaction_secrets)
        request = self.get_request(authority, request_id)
        queue = self._queue(authority.session_id)
        if phase is PermissionRequestPhase.CANCELLED:
            transitioned = queue.cancel(
                request_id,
                reason=reason,
                actor=authority.actor_id,
                expected_request_revision=request.revision,
            )
        elif phase is PermissionRequestPhase.ABORTED:
            transitioned = queue.abort(
                request_id,
                reason=reason,
                actor=authority.actor_id,
                expected_request_revision=request.revision,
            )
        else:
            raise ValueError(f"unsupported terminal transition: {phase}")
        continuation = self._sync_continuation_cancel(
            transitioned,
            reason=reason or f"permission request {phase.value}",
        )
        terminal_cause = self._latest_event_id(transitioned.request_id)
        event = self.event_projector.request_event(
            transitioned,
            kind=kind,
            run_id=transitioned.run_id,
            task_id=transitioned.task_id,
            node_id=node_id,
            worker_request_id=transitioned.worker_request_id,
            cause_event_id=terminal_cause,
        )
        self._link_event(
            transitioned.request_id,
            phase=f"request_{phase.value}",
            event_id=event.event_id,
            cause_event_id=terminal_cause,
        )
        recovery = self._recovery_for_terminal(
            transitioned,
            reason=reason or f"permission request {phase.value}",
        )
        recovery_event = self._recovery_event(
            transitioned,
            recovery,
            node_id=node_id,
            cause_event_id=event.event_id,
        )
        self.transport_registry.acknowledge(transitioned.request_id)
        self._count(phase.value)
        return self._result(
            True,
            code,
            phase.value,
            request=transitioned,
            events=(event, recovery_event),
            message=f"permission request {phase.value}; recovery input produced",
            metadata={
                "continuation_phase": continuation.phase.value if continuation else "not_parked",
            },
        )

    def _install_exact_deny_rule(
        self,
        request: PermissionRequestRecord,
        authority: PermissionControlAuthority,
    ) -> PermissionRuleRecord:
        rule = PermissionRuleRecord(
            effect=PermissionEffect.DENY,
            source=PermissionRuleSource.SESSION,
            scope=request.scope,
            tool_pattern=request.tool_identity.name,
            namespace_pattern=request.tool_identity.namespace,
            server_pattern=request.tool_identity.server_id or "*",
            operation_pattern="*",
            reason=f"exact request denied by {authority.actor_id}",
            expires_at=request.expires_at,
            priority=1000,
            metadata={
                "permission_request_id": request.request_id,
                "exact_next_guard": True,
                "authority_id": authority.authority_id,
                "actor_id": authority.actor_id,
                "channel": authority.channel.value,
                "owner_unit": PERMISSION_CONTROL_OWNER_UNIT,
            },
        )
        try:
            return self._rule_store(request.session_id).add(rule)
        except PermissionStateConflict:
            matches = [
                item
                for item in self._rule_store(request.session_id).list(effective=True, include_inactive=True)
                if item.metadata.get("permission_request_id") == request.request_id
                and item.effect is PermissionEffect.DENY
            ]
            if not matches:
                raise
            return matches[-1]

    def _restrict_rule_scope(
        self,
        authority: PermissionControlAuthority,
        request: PermissionRequestRecord,
        response: PermissionResolutionResponse,
    ) -> PermissionResolutionResponse:
        if not response.create_rule:
            return response
        requested_scope = response.rule_scope or request.scope
        if canonical_arguments_json(requested_scope.to_dict()) == canonical_arguments_json(request.scope.to_dict()):
            return response
        authority.require(PermissionControlCapability.RULE_BROADEN)
        if not self.allow_standing_rules:
            raise PermissionControlForbidden("deployment disabled standing permission rules")
        if not _scope_bound_to_authority(requested_scope, authority):
            raise PermissionControlIdentityError("standing permission rule scope exceeds custody")
        return response

    def _resolution_rejected_event(
        self,
        authority: PermissionControlAuthority,
        request: PermissionRequestRecord,
        outcome: PermissionResolutionOutcome,
        *,
        node_id: str | None,
    ) -> EventRecord:
        return self.event_projector.event(
            PermissionEventEnvelope(
                kind=PermissionRuntimeEventKind.REQUEST_RESOLVED,
                session_id=request.session_id,
                worker_request_id=request.worker_request_id,
                tool_call_id=request.tool_use_id,
                request_id=request.request_id,
                arguments_digest=request.arguments_digest,
                phase="permission_resolution_rejected",
                payload={
                    "accepted": False,
                    "code": outcome.code.value,
                    "reason": outcome.reason,
                    "mismatch_fields": list(outcome.mismatch_fields),
                    "response_id": outcome.response_id,
                    "winner_resolution_id": outcome.winner_resolution_id,
                    "authority": authority.to_dict(),
                    "pending_preserved": not request.terminal,
                },
            ),
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=node_id,
        )

    def _recovery_for_terminal(
        self,
        request: PermissionRequestRecord,
        *,
        reason: str,
    ) -> PermissionRecoveryInput:
        alternatives = (
            {
                "strategy": "narrow_scope",
                "description": "reduce the requested command, domain, path, or external side effect",
            },
            {
                "strategy": "read_only_diagnostic",
                "description": "replace the action with a typed read/search/metadata operation",
            },
            {
                "strategy": "manual_step",
                "description": "emit an auditable manual step without executing it",
            },
            {
                "strategy": "reroute",
                "description": "route to a worker whose capability and permission scope satisfy the task",
            },
        )
        return PermissionRecoveryInput(
            decision_id=str(request.metadata.get("decision_id") or ""),
            session_id=request.session_id,
            task_id=request.task_id,
            run_id=request.run_id,
            tool_use_id=request.tool_use_id,
            reason_code=f"permission_request.{request.phase.value}",
            retryable=request.phase
            not in {PermissionRequestPhase.ABORTED, PermissionRequestPhase.CANCELLED},
            alternatives=alternatives,
            constraints={
                "denied_request_id": request.request_id,
                "denied_tool_identity": request.tool_identity.to_dict(),
                "denied_arguments_digest": request.arguments_digest,
                "must_change_exact_fingerprint": True,
                "manual_approval_required": False,
            },
            metadata={
                "reason": reason,
                "owner_unit": PERMISSION_CONTROL_OWNER_UNIT,
                "human_intervention_count": 0,
            },
        )

    def _recovery_event(
        self,
        request: PermissionRequestRecord,
        recovery: PermissionRecoveryInput,
        *,
        node_id: str | None,
        cause_event_id: str,
    ) -> EventRecord:
        return self.event_projector.event(
            PermissionEventEnvelope(
                kind=PermissionRuntimeEventKind.RECOVERY_INPUT,
                session_id=request.session_id,
                worker_request_id=request.worker_request_id,
                tool_call_id=request.tool_use_id,
                request_id=request.request_id,
                arguments_digest=request.arguments_digest,
                cause_event_id=cause_event_id,
                payload=recovery.to_dict(),
            ),
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=node_id,
        )

    def _control_event(
        self,
        authority: PermissionControlAuthority,
        *,
        phase: str,
        payload: Mapping[str, Any],
        node_id: str | None,
    ) -> EventRecord:
        return self.event_projector.event(
            PermissionEventEnvelope(
                kind=PermissionRuntimeEventKind.MODE_TRANSITIONED,
                session_id=authority.session_id,
                worker_request_id="",
                phase=phase,
                payload={
                    **dict(payload),
                    "authority": authority.to_dict(),
                },
            ),
            run_id=authority.run_id,
            task_id=authority.task_id,
            node_id=node_id,
        )

    def _persist_retry_descriptor(
        self,
        authority: PermissionControlAuthority,
        retry: PermissionRetryDescriptor,
    ) -> None:
        def mutate(state: dict[str, Any]) -> None:
            integration = state.setdefault("metadata", {}).setdefault(
                "permission_integration",
                _empty_integration_state(),
            )
            retries = integration.setdefault("retry_descriptors", {})
            # Opportunistically remove expired descriptors under the same
            # revisioned mutation so process restarts do not accumulate junk.
            for retry_id, value in list(retries.items()):
                if not isinstance(value, Mapping):
                    retries.pop(retry_id, None)
                    continue
                try:
                    if datetime.now(timezone.utc) >= _parse_time(str(value.get("expires_at") or "")):
                        retries.pop(retry_id, None)
                except ValueError:
                    retries.pop(retry_id, None)
            retries[retry.retry_id] = {
                **retry.to_dict(),
                "authority_id": authority.authority_id,
                "created_by_actor": authority.actor_id,
            }

        self.state_store.mutate(mutate)

    def _assert_evaluation_owned(
        self,
        authority: PermissionControlAuthority,
        evaluation: PermissionEvaluationRequest,
    ) -> None:
        if (
            evaluation.session_id != authority.session_id
            or evaluation.run_id != authority.run_id
            or evaluation.task_id != authority.task_id
        ):
            raise PermissionControlIdentityError("permission evaluation crosses custody identity")
        if not evaluation.workspace_root:
            raise PermissionControlIdentityError("permission evaluation workspace is required")
        if Path(evaluation.workspace_root).resolve() != Path(authority.workspace_root).resolve():
            raise PermissionControlIdentityError("permission evaluation workspace crosses custody")

    def _queue(self, session_id: str) -> PermissionRequestQueue:
        return PermissionRequestQueue(self.state_store, session_id=session_id)

    def _rule_store(self, session_id: str) -> PermissionRuleStore:
        return PermissionRuleStore(self.state_store, session_id=session_id)

    def _ensure_available(self) -> None:
        if self.disabled:
            raise PermissionControlDisabled("PermissionControlPlane is disabled")
        if self.state_store.disabled:
            raise PermissionControlDisabled("PermissionStateStore is disabled")

    def _count(self, operation: str, *, delta: int = 1) -> None:
        with self._lock:
            self._operation_counts[operation] = self._operation_counts.get(operation, 0) + delta

    def _result(
        self,
        ok: bool,
        code: PermissionControlCode,
        operation: str,
        **kwargs: Any,
    ) -> PermissionControlResult:
        revision = int(self.state_store.read_state().get("revision") or 0)
        return PermissionControlResult(
            ok=ok,
            code=code,
            operation=operation,
            state_revision=revision,
            **kwargs,
        )


def project_permission_request(request: PermissionRequestRecord | None) -> dict[str, Any]:
    if request is None:
        return {}
    payload = request.to_dict()
    payload["metadata"] = _safe_metadata(payload.get("metadata") or {})
    payload["scope"]["metadata"] = _safe_metadata(payload["scope"].get("metadata") or {})
    payload["raw_arguments_included"] = False
    payload["execution_authority"] = False
    return payload


def project_permission_rule(rule: PermissionRuleRecord | None) -> dict[str, Any]:
    if rule is None:
        return {}
    payload = rule.to_dict()
    payload["metadata"] = _safe_metadata(payload.get("metadata") or {})
    payload["scope"]["metadata"] = _safe_metadata(payload["scope"].get("metadata") or {})
    return payload


def project_permission_decision(decision: Any) -> dict[str, Any]:
    payload = decision.to_dict() if hasattr(decision, "to_dict") else to_jsonable(decision)
    if not isinstance(payload, Mapping):
        return {"value": str(payload)}
    item = dict(payload)
    item["metadata"] = _safe_metadata(item.get("metadata") or {})
    item.pop("arguments", None)
    item.pop("execution_grant", None)
    return item


def default_api_capabilities() -> frozenset[PermissionControlCapability]:
    return frozenset(
        {
            PermissionControlCapability.QUERY,
            PermissionControlCapability.DELIVER,
            PermissionControlCapability.RESOLVE,
            PermissionControlCapability.CANCEL,
            PermissionControlCapability.EXPIRE,
            PermissionControlCapability.RETRY,
            PermissionControlCapability.RULE_READ,
            PermissionControlCapability.RULE_WRITE,
            PermissionControlCapability.MODE_READ,
            PermissionControlCapability.MODE_WRITE,
            PermissionControlCapability.AUDIT_READ,
        }
    )


def default_internal_capabilities() -> frozenset[PermissionControlCapability]:
    return frozenset(PermissionControlCapability)


def _default_capabilities_for_channel(
    channel: PermissionTransportKind,
) -> frozenset[PermissionControlCapability]:
    if channel in {
        PermissionTransportKind.API,
        PermissionTransportKind.CLI,
        PermissionTransportKind.INTERACTIVE,
    }:
        return default_api_capabilities()
    if channel in {
        PermissionTransportKind.BRIDGE,
        PermissionTransportKind.SDK,
        PermissionTransportKind.STRUCTURED_IO,
        PermissionTransportKind.COORDINATOR,
        PermissionTransportKind.SWARM_WORKER,
    }:
        return frozenset(
            {
                PermissionControlCapability.QUERY,
                PermissionControlCapability.CREATE,
                PermissionControlCapability.DELIVER,
                PermissionControlCapability.RESOLVE,
                PermissionControlCapability.CANCEL,
                PermissionControlCapability.ABORT,
                PermissionControlCapability.EXPIRE,
                PermissionControlCapability.RETRY,
                PermissionControlCapability.RULE_READ,
                PermissionControlCapability.AUDIT_READ,
            }
        )
    # hook/gateway are installed by deployment code and may deliver/resolve,
    # but still cannot broaden rules or enable modes unless explicitly granted.
    return frozenset(
        {
            PermissionControlCapability.QUERY,
            PermissionControlCapability.CREATE,
            PermissionControlCapability.DELIVER,
            PermissionControlCapability.RESOLVE,
            PermissionControlCapability.ABORT,
            PermissionControlCapability.EXPIRE,
            PermissionControlCapability.RETRY,
            PermissionControlCapability.RULE_READ,
            PermissionControlCapability.AUDIT_READ,
        }
    )


def _control_code_for_resolution(code: PermissionResolutionCode) -> PermissionControlCode:
    return {
        PermissionResolutionCode.NOT_FOUND: PermissionControlCode.NOT_FOUND,
        PermissionResolutionCode.IDENTITY_MISMATCH: PermissionControlCode.IDENTITY_MISMATCH,
        PermissionResolutionCode.EXPIRED: PermissionControlCode.EXPIRED,
        PermissionResolutionCode.VERSION_CONFLICT: PermissionControlCode.CONFLICT,
        PermissionResolutionCode.DUPLICATE: PermissionControlCode.CONFLICT,
        PermissionResolutionCode.NOT_PENDING: PermissionControlCode.CONFLICT,
        PermissionResolutionCode.INVALID_EFFECT: PermissionControlCode.INVALID,
        PermissionResolutionCode.INVALID_SOURCE: PermissionControlCode.FORBIDDEN,
        PermissionResolutionCode.MALFORMED: PermissionControlCode.INVALID,
        PermissionResolutionCode.ACCEPTED: PermissionControlCode.OK,
    }[code]


def _scope_bound_to_authority(
    scope: PermissionScope,
    authority: PermissionControlAuthority,
) -> bool:
    if scope.session_id and scope.session_id != authority.session_id:
        return False
    if scope.task_id and scope.task_id != authority.task_id:
        return False
    if scope.run_id and scope.run_id != authority.run_id:
        return False
    if scope.principal_id and authority.principal_id and scope.principal_id != authority.principal_id:
        return False
    if scope.workspace_root:
        try:
            Path(scope.workspace_root).resolve().relative_to(Path(authority.workspace_root).resolve())
        except (ValueError, OSError):
            return False
    return scope.kind not in {PermissionScopeKind.GLOBAL, PermissionScopeKind.PROJECT, PermissionScopeKind.USER}


def _retry_from_mapping(value: Mapping[str, Any]) -> PermissionRetryDescriptor:
    tool = value.get("tool_identity") if isinstance(value.get("tool_identity"), Mapping) else {}
    return PermissionRetryDescriptor(
        retry_id=str(value.get("retry_id") or ""),
        request_id=str(value.get("request_id") or ""),
        session_id=str(value.get("session_id") or ""),
        run_id=str(value.get("run_id") or ""),
        task_id=str(value.get("task_id") or ""),
        worker_request_id=str(value.get("worker_request_id") or ""),
        tool_use_id=str(value.get("tool_use_id") or ""),
        tool_namespace=str(tool.get("namespace") or ""),
        tool_name=str(tool.get("name") or ""),
        server_id=str(tool.get("server_id") or ""),
        tool_version=str(tool.get("version") or ""),
        tool_schema_digest=str(tool.get("schema_digest") or ""),
        arguments_digest=str(value.get("arguments_digest") or ""),
        request_fingerprint=str(value.get("request_fingerprint") or ""),
        scope_digest=str(value.get("scope_digest") or ""),
        request_revision=int(value.get("request_revision") or 0),
        created_at=str(value.get("created_at") or ""),
        expires_at=str(value.get("expires_at") or ""),
        metadata=_safe_metadata(value.get("metadata") or {}),
    )


def _empty_integration_state() -> dict[str, Any]:
    return {
        "schema": "zyra.permission-integration-state.v1",
        "owner_unit": PERMISSION_CONTROL_OWNER_UNIT,
        "retry_descriptors": {},
        "session_modes": {},
        "event_links": {},
        "metadata": {"legacy_store_is_authority": False},
    }


def _digest(value: Any) -> str:
    encoded = canonical_arguments_json(to_jsonable(value))
    return f"sha256:{sha256(encoded.encode('utf-8')).hexdigest()}"


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _mapping_size(value: Any) -> int:
    return len(value) if isinstance(value, Mapping) else 0


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


def _reject_secret_echo(value: Any, secrets: Iterable[str]) -> None:
    """Reject control data that would persist an authenticated capability.

    Key-name redaction is not enough: an operator can accidentally paste a
    bearer value into an ordinary ``reason`` or ``note``.  Authorities retain
    presented capabilities only as repr-hidden, non-serialized comparison
    material so the control boundary can reject that value before CAS state or
    event construction.
    """

    protected = tuple(dict.fromkeys(str(item) for item in secrets if str(item)))
    if not protected:
        return

    def inspect(item: Any, *, depth: int = 0) -> None:
        if depth > 12:
            raise PermissionControlForbidden("permission response nesting exceeds safety limit")
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                raw_key_text = str(raw_key)
                if any(secret in raw_key_text for secret in protected):
                    raise PermissionControlForbidden(
                        "permission response cannot use an authenticated capability as a field"
                    )
                key = raw_key_text.casefold().replace("-", "_")
                if any(marker in key for marker in _SENSITIVE_MARKERS):
                    raise PermissionControlForbidden(
                        "permission response cannot contain credential-bearing fields"
                    )
                inspect(child, depth=depth + 1)
            return
        if isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                inspect(child, depth=depth + 1)
            return
        if isinstance(item, str) and any(secret in item for secret in protected):
            raise PermissionControlForbidden(
                "permission response cannot echo an authenticated capability"
            )

    inspect(value)


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
    "PERMISSION_CONTROL_OWNER_UNIT",
    "PERMISSION_CONTROL_SCHEMA",
    "PermissionControlAuthority",
    "PermissionControlCapability",
    "PermissionControlCode",
    "PermissionControlDisabled",
    "PermissionControlError",
    "PermissionControlForbidden",
    "PermissionControlIdentityError",
    "PermissionControlNotFound",
    "PermissionControlPlane",
    "PermissionControlResult",
    "PermissionQuery",
    "PermissionQueryPage",
    "PermissionRetryDescriptor",
    "default_api_capabilities",
    "default_internal_capabilities",
    "project_permission_decision",
    "project_permission_request",
    "project_permission_rule",
]
