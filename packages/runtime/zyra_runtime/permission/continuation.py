from __future__ import annotations

"""Durable, identity-bound continuation state for pending tool approvals.

The permission request queue deliberately persists only canonical digests, not
raw tool arguments.  This module gives QueryEngine/API integrations a second
piece of *metadata* in the same :class:`PermissionStateStore`: an immutable
locator for the session-owned payload and a small compare-and-swap lifecycle.

It is intentionally not an authorization service.  A continuation claim only
proves that one caller won the right to replay a previously parked payload.
The replayed call must still pass ``ToolPermissionRuntime.guard`` and consume a
one-use execution grant at the executor boundary.
"""

import copy
import hashlib
import hmac
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from threading import RLock
from typing import Any
from uuid import uuid4

from .canonical import arguments_digest, build_request_fingerprint, canonical_arguments_json
from .models import (
    PermissionEffect,
    PermissionRequestPhase,
    PermissionRequestRecord,
    PermissionRequestStatus,
    PermissionScope,
    ToolIdentity,
)
from .store import (
    PermissionStateConflict,
    PermissionStateDisabled,
    PermissionStateStore,
)


PERMISSION_CONTINUATION_SCHEMA = "zyra.permission-continuations"
PERMISSION_CONTINUATION_VERSION = 1
PERMISSION_CONTINUATION_SNAPSHOT_SCHEMA = "zyra.permission-continuation-snapshot"
PERMISSION_CONTINUATION_SNAPSHOT_VERSION = 1
# The tool runtime permits executions up to 600 seconds.  A continuation
# claim must outlive that boundary plus settlement overhead; request approval
# expiry only constrains when the claim starts, not an already-authorized
# execution transaction.
PERMISSION_CONTINUATION_CLAIM_LEASE_SECONDS = 900.0
PERMISSION_CONTINUATION_METADATA_KEY = "permission_continuations"


class PermissionContinuationPhase(StrEnum):
    PARKED = "parked"
    DELIVERED = "delivered"
    RESOLUTION_READY = "resolution_ready"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PermissionContinuationError(RuntimeError):
    """Base continuation error."""


class PermissionContinuationDisabledError(PermissionContinuationError):
    pass


class PermissionContinuationCorruptError(PermissionContinuationError):
    pass


class PermissionContinuationConflict(PermissionContinuationError):
    pass


class PermissionContinuationIdentityError(PermissionContinuationError):
    pass


class PermissionContinuationStateError(PermissionContinuationError):
    pass


class PermissionContinuationPayloadMissing(PermissionContinuationError):
    pass


class PermissionContinuationOutcomeUnknown(PermissionContinuationStateError):
    """The grant transaction advanced but its external side effect is unknown."""


class PermissionContinuationAlreadyClaimed(PermissionContinuationConflict):
    pass


_TERMINAL_PHASES = frozenset(
    {
        PermissionContinuationPhase.COMPLETED,
        PermissionContinuationPhase.FAILED,
        PermissionContinuationPhase.CANCELLED,
        PermissionContinuationPhase.EXPIRED,
    }
)
_PRECLAIM_PHASES = frozenset(
    {
        PermissionContinuationPhase.PARKED,
        PermissionContinuationPhase.DELIVERED,
        PermissionContinuationPhase.RESOLUTION_READY,
    }
)
_ACTIVE_PHASES = frozenset(
    {
        *_PRECLAIM_PHASES,
        PermissionContinuationPhase.CLAIMED,
    }
)
_PHASE_RANK = {
    PermissionContinuationPhase.PARKED: 0,
    PermissionContinuationPhase.DELIVERED: 1,
    PermissionContinuationPhase.RESOLUTION_READY: 2,
    PermissionContinuationPhase.CLAIMED: 3,
    PermissionContinuationPhase.COMPLETED: 4,
    PermissionContinuationPhase.FAILED: 4,
    PermissionContinuationPhase.CANCELLED: 4,
    PermissionContinuationPhase.EXPIRED: 4,
}
_LOCATOR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]{0,31}:[^\s\x00-\x1f\x7f]{1,2000}$")
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "argument",
        "arguments",
        "canonical_argument",
        "canonical_arguments",
        "raw_argument",
        "raw_arguments",
        "tool_arguments",
        "input_payload",
        "request_payload",
    }
)


@dataclass(frozen=True, slots=True)
class PermissionContinuationRecord:
    continuation_id: str
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
    payload_locator: str
    session_sequence: int
    permission_request_revision: int
    expires_at: str
    phase: PermissionContinuationPhase = PermissionContinuationPhase.PARKED
    revision: int = 0
    resolution_effect: PermissionEffect | None = None
    resolution_id: str = ""
    delivery_channel: str = ""
    claim_id: str = ""
    claimed_by: str = ""
    claim_idempotency_key: str = ""
    claim_expires_at: str | None = None
    claim_attempt: int = 0
    created_at: str = field(default_factory=lambda: _now_iso())
    delivered_at: str | None = None
    resolution_ready_at: str | None = None
    claimed_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    cancelled_at: str | None = None
    expired_at: str | None = None
    failure_code: str = ""
    identity_digest: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = (
            self.continuation_id,
            self.request_id,
            self.session_id,
            self.task_id,
            self.run_id,
            self.tool_use_id,
            self.arguments_digest,
            self.request_fingerprint,
            self.payload_locator,
            self.expires_at,
        )
        if not all(str(value).strip() for value in required):
            raise ValueError("continuation exact identity, locator and expiry are required")
        if self.session_sequence < 1:
            raise ValueError("continuation session_sequence must be positive")
        if self.permission_request_revision < 0 or self.revision < 0 or self.claim_attempt < 0:
            raise ValueError("continuation revisions cannot be negative")
        _validate_locator(self.payload_locator)
        _require_aware_time(self.expires_at, "continuation expires_at")
        if self.claim_expires_at is not None:
            _require_aware_time(self.claim_expires_at, "continuation claim_expires_at")
        safe_metadata = _safe_metadata(self.metadata)
        object.__setattr__(self, "metadata", safe_metadata)
        expected_digest = _record_identity_digest(self)
        if self.identity_digest and not hmac.compare_digest(self.identity_digest, expected_digest):
            raise PermissionContinuationCorruptError("continuation immutable identity digest mismatch")
        object.__setattr__(self, "identity_digest", expected_digest)
        self._validate_phase_fields()

    @property
    def terminal(self) -> bool:
        return self.phase in _TERMINAL_PHASES

    @property
    def pending(self) -> bool:
        return self.phase in _PRECLAIM_PHASES

    @property
    def claimed(self) -> bool:
        return self.phase is PermissionContinuationPhase.CLAIMED

    @property
    def authorizes_execution(self) -> bool:
        """A continuation is never an execution capability."""

        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "continuation_id": self.continuation_id,
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
            "payload_locator": self.payload_locator,
            "session_sequence": self.session_sequence,
            "permission_request_revision": self.permission_request_revision,
            "expires_at": self.expires_at,
            "phase": str(self.phase),
            "revision": self.revision,
            "resolution_effect": str(self.resolution_effect) if self.resolution_effect else None,
            "resolution_id": self.resolution_id,
            "delivery_channel": self.delivery_channel,
            "claim_id": self.claim_id,
            "claimed_by": self.claimed_by,
            "claim_idempotency_key": self.claim_idempotency_key,
            "claim_expires_at": self.claim_expires_at,
            "claim_attempt": self.claim_attempt,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "resolution_ready_at": self.resolution_ready_at,
            "claimed_at": self.claimed_at,
            "completed_at": self.completed_at,
            "failed_at": self.failed_at,
            "cancelled_at": self.cancelled_at,
            "expired_at": self.expired_at,
            "failure_code": self.failure_code,
            "identity_digest": self.identity_digest,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PermissionContinuationRecord":
        item = dict(value)
        _reject_raw_arguments(item)
        effect = item.get("resolution_effect")
        phase = PermissionContinuationPhase(str(item.get("phase") or "parked"))
        claim_id = str(item.get("claim_id") or "")
        claim_expires_at = _optional_text(item.get("claim_expires_at"))
        if claim_expires_at is None and phase in {
            PermissionContinuationPhase.CLAIMED,
            PermissionContinuationPhase.COMPLETED,
            PermissionContinuationPhase.FAILED,
        }:
            # Backward-compatible recovery for pre-lease snapshots: the
            # permission request expiry is the conservative lease ceiling.
            claim_expires_at = str(item.get("expires_at") or "") or None
        return cls(
            continuation_id=str(item.get("continuation_id") or ""),
            request_id=str(item.get("request_id") or ""),
            session_id=str(item.get("session_id") or ""),
            task_id=str(item.get("task_id") or ""),
            run_id=str(item.get("run_id") or ""),
            worker_request_id=str(item.get("worker_request_id") or ""),
            tool_use_id=str(item.get("tool_use_id") or item.get("tool_call_id") or ""),
            tool_identity=ToolIdentity.from_dict(_mapping(item.get("tool_identity"))),
            arguments_digest=str(item.get("arguments_digest") or ""),
            request_fingerprint=str(item.get("request_fingerprint") or ""),
            scope=PermissionScope.from_dict(_mapping(item.get("scope"))),
            payload_locator=str(item.get("payload_locator") or ""),
            session_sequence=int(item.get("session_sequence") or 0),
            permission_request_revision=int(item.get("permission_request_revision") or 0),
            expires_at=str(item.get("expires_at") or ""),
            phase=phase,
            revision=int(item.get("revision") or 0),
            resolution_effect=PermissionEffect(str(effect)) if effect else None,
            resolution_id=str(item.get("resolution_id") or ""),
            delivery_channel=str(item.get("delivery_channel") or ""),
            claim_id=claim_id,
            claimed_by=str(item.get("claimed_by") or ""),
            claim_idempotency_key=str(item.get("claim_idempotency_key") or ""),
            claim_expires_at=claim_expires_at,
            claim_attempt=int(item.get("claim_attempt") or (1 if claim_id else 0)),
            created_at=str(item.get("created_at") or _now_iso()),
            delivered_at=_optional_text(item.get("delivered_at")),
            resolution_ready_at=_optional_text(item.get("resolution_ready_at")),
            claimed_at=_optional_text(item.get("claimed_at")),
            completed_at=_optional_text(item.get("completed_at")),
            failed_at=_optional_text(item.get("failed_at")),
            cancelled_at=_optional_text(item.get("cancelled_at")),
            expired_at=_optional_text(item.get("expired_at")),
            failure_code=str(item.get("failure_code") or ""),
            identity_digest=str(item.get("identity_digest") or ""),
            metadata=_mapping(item.get("metadata")),
        )

    def _validate_phase_fields(self) -> None:
        if self.phase is PermissionContinuationPhase.DELIVERED and not self.delivered_at:
            raise ValueError("delivered continuation requires delivered_at")
        if self.phase is PermissionContinuationPhase.RESOLUTION_READY:
            if not self.resolution_ready_at or self.resolution_effect is None:
                raise ValueError("resolution-ready continuation requires an effect and timestamp")
        if self.phase in {
            PermissionContinuationPhase.CLAIMED,
            PermissionContinuationPhase.COMPLETED,
            PermissionContinuationPhase.FAILED,
        }:
            if not all(
                (
                    self.claim_id,
                    self.claimed_by,
                    self.claimed_at,
                    self.claim_expires_at,
                    self.resolution_ready_at,
                    self.claim_attempt >= 1,
                )
            ):
                raise ValueError("claimed continuation requires claim and resolution metadata")
        if self.phase is PermissionContinuationPhase.COMPLETED and not self.completed_at:
            raise ValueError("completed continuation requires completed_at")
        if self.phase is PermissionContinuationPhase.FAILED and not self.failed_at:
            raise ValueError("failed continuation requires failed_at")
        if self.phase is PermissionContinuationPhase.CANCELLED and not self.cancelled_at:
            raise ValueError("cancelled continuation requires cancelled_at")
        if self.phase is PermissionContinuationPhase.EXPIRED and not self.expired_at:
            raise ValueError("expired continuation requires expired_at")


@dataclass(frozen=True, slots=True)
class PermissionContinuationReplay:
    session_id: str
    task_id: str
    run_id: str
    tool_use_id: str
    tool_identity: ToolIdentity
    arguments_digest: str
    request_fingerprint: str
    scope: PermissionScope
    payload_locator: str
    session_sequence: int
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.task_id,
                self.run_id,
                self.tool_use_id,
                self.arguments_digest,
                self.request_fingerprint,
                self.payload_locator,
            )
        ):
            raise ValueError("replay exact identity and payload locator are required")
        if self.session_sequence < 1:
            raise ValueError("replay session_sequence must be positive")
        _validate_locator(self.payload_locator)
        object.__setattr__(self, "metadata", _safe_metadata(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "tool_use_id": self.tool_use_id,
            "tool_identity": self.tool_identity.to_dict(),
            "arguments_digest": self.arguments_digest,
            "request_fingerprint": self.request_fingerprint,
            "scope": self.scope.to_dict(),
            "payload_locator": self.payload_locator,
            "session_sequence": self.session_sequence,
            "source": self.source,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_value(
        cls,
        value: "PermissionContinuationReplay | Mapping[str, Any]",
        *,
        source: str = "",
    ) -> "PermissionContinuationReplay":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("continuation replay must be a mapping")
        item = dict(value)
        identity_value = item.get("tool_identity")
        if isinstance(identity_value, ToolIdentity):
            identity = identity_value
        else:
            identity_map = _mapping(identity_value)
            if not identity_map:
                identity_map = {
                    "namespace": item.get("tool_namespace", item.get("namespace")),
                    "name": item.get("tool_name"),
                    "server_id": item.get("server_id", item.get("server_name")),
                    "version": item.get("tool_version", item.get("version", "")),
                    "schema_digest": item.get("tool_schema_digest", item.get("schema_digest", "")),
                }
            identity = ToolIdentity.from_dict(identity_map)
        raw_arguments = item.get("arguments")
        digest = str(item.get("arguments_digest") or "")
        if isinstance(raw_arguments, Mapping):
            computed = arguments_digest(raw_arguments)
            if digest and not hmac.compare_digest(digest, computed):
                raise PermissionContinuationIdentityError("replay raw arguments do not match arguments_digest")
            digest = computed
        if not digest:
            raise ValueError("replay arguments or arguments_digest are required")
        session_id = str(item.get("session_id") or "")
        task_id = str(item.get("task_id") or "")
        run_id = str(item.get("run_id") or "")
        tool_use_id = str(item.get("tool_use_id") or item.get("tool_call_id") or "")
        fingerprint = str(item.get("request_fingerprint") or "")
        if not fingerprint and all((session_id, task_id, run_id, tool_use_id)):
            fingerprint = build_request_fingerprint(
                identity,
                digest,
                session_id=session_id,
                tool_use_id=tool_use_id,
                run_id=run_id,
                task_id=task_id,
            )
        scope_value = item.get("scope")
        if isinstance(scope_value, PermissionScope):
            scope = scope_value
        elif isinstance(scope_value, Mapping):
            scope = PermissionScope.from_dict(scope_value)
        else:
            raise ValueError("replay scope is required")
        return cls(
            session_id=session_id,
            task_id=task_id,
            run_id=run_id,
            tool_use_id=tool_use_id,
            tool_identity=identity,
            arguments_digest=digest,
            request_fingerprint=fingerprint,
            scope=scope,
            payload_locator=str(item.get("payload_locator") or ""),
            session_sequence=int(item.get("session_sequence") or 0),
            source=source or str(item.get("source") or ""),
            metadata=_mapping(item.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class PermissionContinuationClaim:
    record: PermissionContinuationRecord
    presented: PermissionContinuationReplay
    authoritative: PermissionContinuationReplay
    resolved_payload: Any = field(default=None, repr=False, compare=False)

    @property
    def claim_id(self) -> str:
        return self.record.claim_id

    @property
    def resolution_effect(self) -> PermissionEffect | None:
        return self.record.resolution_effect

    @property
    def authorizes_execution(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "presented": self.presented.to_dict(),
            "authoritative": self.authoritative.to_dict(),
            "payload_resolved": self.resolved_payload is not None,
            "authorizes_execution": False,
        }


PayloadResolver = Callable[[PermissionContinuationRecord], Mapping[str, Any] | PermissionContinuationReplay | None]


class PermissionContinuationStore:
    """CAS facade over ``PermissionStateStore.metadata``.

    No file path is accepted here: callers must provide the existing state
    owner, which prevents a second permission JSON repository from emerging.
    """

    def __init__(
        self,
        state_store: PermissionStateStore,
        *,
        disabled: bool = False,
        claim_lease_seconds: float = PERMISSION_CONTINUATION_CLAIM_LEASE_SECONDS,
        external_permission_authority: bool = False,
    ) -> None:
        if not isinstance(state_store, PermissionStateStore):
            raise TypeError("PermissionContinuationStore requires PermissionStateStore")
        self.state_store = state_store
        self.disabled = bool(disabled)
        self.external_permission_authority = bool(external_permission_authority)
        self.claim_lease_seconds = float(claim_lease_seconds)
        if not math.isfinite(self.claim_lease_seconds) or not 1.0 <= self.claim_lease_seconds <= 3600.0:
            raise ValueError("claim_lease_seconds must be finite and in [1, 3600]")
        self._lock = RLock()

    @property
    def generation(self) -> int:
        container = self._read_container()
        return int(container.get("revision") or 0)

    def park(
        self,
        request: PermissionRequestRecord,
        *,
        payload_locator: str,
        session_sequence: int,
        continuation_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        self._ensure_enabled()
        if not isinstance(request, PermissionRequestRecord):
            raise TypeError("park requires PermissionRequestRecord")
        if request.terminal or request.status is not PermissionRequestStatus.PENDING:
            raise PermissionContinuationStateError("only a live pending request can be parked")
        _validate_locator(payload_locator)
        if int(session_sequence) < 1:
            raise ValueError("session_sequence must be positive")
        now = self._now_iso()
        candidate = PermissionContinuationRecord(
            continuation_id=continuation_id or f"permcont_{uuid4().hex}",
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
            payload_locator=payload_locator,
            session_sequence=int(session_sequence),
            permission_request_revision=request.revision,
            expires_at=request.expires_at,
            phase=(
                PermissionContinuationPhase.DELIVERED
                if request.phase is PermissionRequestPhase.DELIVERED
                else PermissionContinuationPhase.PARKED
            ),
            delivered_at=request.delivered_at,
            created_at=now,
            metadata=_safe_metadata(metadata or {}),
        )
        selected: list[PermissionContinuationRecord] = []

        def mutate(state: dict[str, Any]) -> None:
            authoritative = self._resolve_authoritative_request(
                state,
                request.request_id,
                supplied=request,
            )
            _assert_request_records_equal(authoritative, request)
            container = _container(state, create=True, now=now)
            by_request = container["by_request_id"]
            existing_id = str(by_request.get(request.request_id) or "")
            if existing_id:
                existing = _record_from_container(container, existing_id)
                _assert_same_immutable_identity(existing, candidate)
                selected.append(existing)
                return
            if candidate.continuation_id in container["records"]:
                raise PermissionStateConflict(
                    f"permission continuation already exists: {candidate.continuation_id}"
                )
            current_sequence = int(container["session_sequences"].get(candidate.session_id) or 0)
            if candidate.session_sequence <= current_sequence:
                raise PermissionContinuationConflict(
                    "continuation session sequence must advance monotonically"
                )
            container["records"][candidate.continuation_id] = candidate.to_dict()
            by_request[candidate.request_id] = candidate.continuation_id
            container["session_sequences"][candidate.session_id] = candidate.session_sequence
            _touch_container(container, now)
            selected.append(candidate)

        self.state_store.mutate(mutate, expected_revision=expected_state_revision)
        return selected[0]

    def deliver(
        self,
        request: PermissionRequestRecord,
        *,
        expected_record_revision: int,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        if request.phase is not PermissionRequestPhase.DELIVERED:
            raise PermissionContinuationStateError("delivery requires a delivered permission request")
        if request.status is not PermissionRequestStatus.PENDING:
            raise PermissionContinuationStateError("delivered permission request must remain pending")
        return self._transition_request_record(
            request,
            expected_record_revision=expected_record_revision,
            expected_state_revision=expected_state_revision,
            transition="deliver",
        )

    def resolution_ready(
        self,
        request: PermissionRequestRecord,
        *,
        expected_record_revision: int,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        if request.phase is not PermissionRequestPhase.RESOLVED:
            raise PermissionContinuationStateError("resolution-ready requires a resolved permission request")
        if request.status not in {PermissionRequestStatus.APPROVED, PermissionRequestStatus.DENIED}:
            raise PermissionContinuationStateError("resolved request must be approved or denied")
        if request.resolution_effect not in {PermissionEffect.ALLOW, PermissionEffect.DENY}:
            raise PermissionContinuationStateError("resolved request effect must be allow or deny")
        return self._transition_request_record(
            request,
            expected_record_revision=expected_record_revision,
            expected_state_revision=expected_state_revision,
            transition="resolution_ready",
        )

    def claim_once(
        self,
        request_id: str,
        *,
        claimant: str,
        idempotency_key: str,
        expected_record_revision: int,
        authoritative_request: PermissionRequestRecord | None = None,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        self._ensure_enabled()
        if not all((str(request_id).strip(), str(claimant).strip(), str(idempotency_key).strip())):
            raise ValueError("request_id, claimant and idempotency_key are required")
        now_value = self._now()
        now = now_value.isoformat()
        updated: list[PermissionContinuationRecord] = []

        def mutate(state: dict[str, Any]) -> None:
            container = _container(state, create=False, now=now)
            record = _record_by_request(container, request_id)
            _expect_record_revision(record, expected_record_revision)
            authoritative = self._resolve_authoritative_request(
                state,
                request_id,
                supplied=authoritative_request,
            )
            _assert_request_identity(record, authoritative)
            if (
                authoritative.phase is not PermissionRequestPhase.RESOLVED
                or authoritative.status not in {
                    PermissionRequestStatus.APPROVED,
                    PermissionRequestStatus.DENIED,
                }
                or authoritative.resolution_effect is not record.resolution_effect
            ):
                raise PermissionContinuationStateError(
                    "authoritative permission request is not the parked resolution"
                )
            recovering_claim = False
            if record.phase is PermissionContinuationPhase.CLAIMED:
                lease_expiry = _parse_time(str(record.claim_expires_at or record.expires_at))
                if lease_expiry > now_value:
                    raise PermissionContinuationAlreadyClaimed(
                        "permission continuation was already claimed"
                    )
                recovering_claim = True
            if authoritative.revision != record.permission_request_revision:
                if recovering_claim and _approval_execution_claimed(authoritative):
                    outcome_unknown = _approval_consumed_outcome_unknown(
                        record,
                        authoritative,
                        now=now,
                    )
                    _put_record(container, outcome_unknown)
                    _touch_container(container, now)
                    updated.append(outcome_unknown)
                    return
                raise PermissionContinuationStateError(
                    "authoritative permission request revision changed before claim"
                )
            if record.terminal:
                raise PermissionContinuationStateError(
                    f"permission continuation is terminal: {record.phase}"
                )
            if (
                not recovering_claim
                and record.phase is not PermissionContinuationPhase.RESOLUTION_READY
            ):
                raise PermissionContinuationStateError(
                    "permission continuation is not resolution-ready"
                )
            if not recovering_claim and _parse_time(record.expires_at) <= now_value:
                expired = replace(
                    record,
                    phase=PermissionContinuationPhase.EXPIRED,
                    revision=record.revision + 1,
                    expired_at=now,
                    metadata={**record.metadata, "expiry_reason": "claim_after_expiry"},
                )
                _put_record(container, expired)
                _touch_container(container, now)
                updated.append(expired)
                return
            claimed = replace(
                record,
                phase=PermissionContinuationPhase.CLAIMED,
                revision=record.revision + 1,
                claim_id=f"permcontclaim_{uuid4().hex}",
                claimed_by=str(claimant),
                claim_idempotency_key=str(idempotency_key),
                claimed_at=now,
                claim_expires_at=(
                    now_value + timedelta(seconds=self.claim_lease_seconds)
                ).isoformat(),
                claim_attempt=record.claim_attempt + 1,
                metadata={
                    **record.metadata,
                    "claim_recovered_after_lease": recovering_claim,
                },
            )
            _put_record(container, claimed)
            _touch_container(container, now)
            updated.append(claimed)

        self.state_store.mutate(mutate, expected_revision=expected_state_revision)
        selected = updated[0]
        if selected.phase is PermissionContinuationPhase.EXPIRED:
            raise PermissionContinuationStateError("permission continuation expired before claim")
        if (
            selected.phase is PermissionContinuationPhase.FAILED
            and selected.failure_code == "approval_consumed_outcome_unknown"
        ):
            raise PermissionContinuationOutcomeUnknown(
                "permission approval was consumed but execution outcome is unknown"
            )
        return selected

    def reconcile_expired_claim(
        self,
        request_id: str,
        *,
        expected_record_revision: int,
        authoritative_request: PermissionRequestRecord | None = None,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        """Fence a crashed claim whose one-use approval was already consumed.

        Claim-lease expiry alone cannot prove whether execution started.  The
        authoritative request revision does: the permission guard advances it
        atomically when consuming the approval.  An advanced revision with a
        still-CLAIMED continuation therefore has an unknown external outcome,
        so exact replay is terminally fenced and only a different recovery
        action may continue.  An unconsumed claim remains available to the
        normal exact-replay reclaim path only while its approval is valid;
        otherwise it expires without requiring a dangerous replay attempt.
        """

        self._ensure_enabled()
        now_value = self._now()
        now = now_value.isoformat()
        updated: list[PermissionContinuationRecord] = []

        def mutate(state: dict[str, Any]) -> None:
            container = _container(state, create=False, now=now)
            record = _record_by_request(container, request_id)
            _expect_record_revision(record, expected_record_revision)
            if record.phase is not PermissionContinuationPhase.CLAIMED:
                raise PermissionContinuationStateError(
                    "only a claimed continuation can be reconciled"
                )
            lease_expiry = _parse_time(str(record.claim_expires_at or record.expires_at))
            if lease_expiry > now_value:
                raise PermissionContinuationAlreadyClaimed(
                    "permission continuation claim lease is still active"
                )
            authoritative = self._resolve_authoritative_request(
                state,
                request_id,
                supplied=authoritative_request,
            )
            _assert_request_identity(record, authoritative)
            if authoritative.revision == record.permission_request_revision:
                if _parse_time(record.expires_at) <= now_value:
                    expired = replace(
                        record,
                        phase=PermissionContinuationPhase.EXPIRED,
                        revision=record.revision + 1,
                        expired_at=now,
                        metadata={
                            **record.metadata,
                            "terminal_reason": (
                                "unconsumed_approval_expired_after_claim_lease"
                            ),
                            "execution_outcome_unknown": False,
                        },
                    )
                    _put_record(container, expired)
                    _touch_container(container, now)
                    updated.append(expired)
                    return
                updated.append(record)
                return
            if not _approval_execution_claimed(authoritative):
                if authoritative.phase is PermissionRequestPhase.EXPIRED:
                    expired = replace(
                        record,
                        phase=PermissionContinuationPhase.EXPIRED,
                        revision=record.revision + 1,
                        expired_at=now,
                        metadata={
                            **record.metadata,
                            "terminal_reason": (
                                "authoritative_unconsumed_approval_expired"
                            ),
                            "execution_outcome_unknown": False,
                        },
                    )
                    _put_record(container, expired)
                    _touch_container(container, now)
                    updated.append(expired)
                    return
                raise PermissionContinuationStateError(
                    "authoritative permission request revision changed without "
                    "an execution-claim proof"
                )
            outcome_unknown = _approval_consumed_outcome_unknown(
                record,
                authoritative,
                now=now,
            )
            _put_record(container, outcome_unknown)
            _touch_container(container, now)
            updated.append(outcome_unknown)

        self.state_store.mutate(mutate, expected_revision=expected_state_revision)
        return updated[0]

    def complete(
        self,
        request_id: str,
        *,
        claim_id: str,
        expected_record_revision: int,
        metadata: Mapping[str, Any] | None = None,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        return self._finish_claim(
            request_id,
            claim_id=claim_id,
            expected_record_revision=expected_record_revision,
            phase=PermissionContinuationPhase.COMPLETED,
            failure_code="",
            metadata=metadata,
            expected_state_revision=expected_state_revision,
        )

    def release_claim(
        self,
        request_id: str,
        *,
        claim_id: str,
        expected_record_revision: int,
        reason: str,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        """Return an unexecuted claim to ``RESOLUTION_READY`` using CAS.

        This is the compensation path for partial batch claims and failures
        before the permission guard/executor transaction starts.  Releasing a
        claim changes its revision and clears its capability-like identity, so
        the former claimant can never finish it later.
        """

        self._ensure_enabled()
        if not str(claim_id).strip():
            raise ValueError("claim_id is required")
        now = self._now_iso()
        updated: list[PermissionContinuationRecord] = []

        def mutate(state: dict[str, Any]) -> None:
            container = _container(state, create=False, now=now)
            record = _record_by_request(container, request_id)
            _expect_record_revision(record, expected_record_revision)
            if record.phase is not PermissionContinuationPhase.CLAIMED:
                raise PermissionContinuationStateError(
                    "only a claimed continuation can be released"
                )
            if not hmac.compare_digest(record.claim_id, claim_id):
                raise PermissionContinuationIdentityError("continuation claim_id mismatch")
            released = replace(
                record,
                phase=PermissionContinuationPhase.RESOLUTION_READY,
                revision=record.revision + 1,
                claim_id="",
                claimed_by="",
                claim_idempotency_key="",
                claim_expires_at=None,
                claimed_at=None,
                metadata={
                    **record.metadata,
                    "last_claim_release_reason": str(reason)[:500],
                    "last_claim_released_at": now,
                },
            )
            _put_record(container, released)
            _touch_container(container, now)
            updated.append(released)

        self.state_store.mutate(mutate, expected_revision=expected_state_revision)
        return updated[0]

    def fail(
        self,
        request_id: str,
        *,
        claim_id: str,
        failure_code: str,
        expected_record_revision: int,
        metadata: Mapping[str, Any] | None = None,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        if not str(failure_code).strip():
            raise ValueError("failure_code is required")
        return self._finish_claim(
            request_id,
            claim_id=claim_id,
            expected_record_revision=expected_record_revision,
            phase=PermissionContinuationPhase.FAILED,
            failure_code=str(failure_code),
            metadata=metadata,
            expected_state_revision=expected_state_revision,
        )

    def cancel(
        self,
        request_id: str,
        *,
        reason: str,
        expected_record_revision: int,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        return self._terminal_preclaim(
            request_id,
            expected_record_revision=expected_record_revision,
            phase=PermissionContinuationPhase.CANCELLED,
            reason=reason,
            expected_state_revision=expected_state_revision,
        )

    def expire(
        self,
        request_id: str,
        *,
        expected_record_revision: int,
        reason: str = "ttl_elapsed",
        force: bool = False,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        if not force:
            record = self.get_by_request(request_id)
            if _parse_time(record.expires_at) > self._now():
                raise PermissionContinuationStateError("permission continuation has not expired")
        return self._terminal_preclaim(
            request_id,
            expected_record_revision=expected_record_revision,
            phase=PermissionContinuationPhase.EXPIRED,
            reason=reason,
            expected_state_revision=expected_state_revision,
        )

    def expire_due(
        self,
        *,
        session_id: str | None = None,
        expected_state_revision: int | None = None,
    ) -> tuple[PermissionContinuationRecord, ...]:
        self._ensure_enabled()
        now_value = self._now()
        now = now_value.isoformat()
        expired: list[PermissionContinuationRecord] = []

        def mutate(state: dict[str, Any]) -> None:
            container = _container(state, create=True, now=now)
            for continuation_id, raw in list(container["records"].items()):
                record = PermissionContinuationRecord.from_dict(_mapping(raw))
                if record.continuation_id != continuation_id:
                    raise PermissionContinuationCorruptError("continuation key/id mismatch")
                if record.phase not in _PRECLAIM_PHASES:
                    continue
                if session_id is not None and record.session_id != session_id:
                    continue
                if _parse_time(record.expires_at) > now_value:
                    continue
                next_record = replace(
                    record,
                    phase=PermissionContinuationPhase.EXPIRED,
                    revision=record.revision + 1,
                    expired_at=now,
                    metadata={**record.metadata, "terminal_reason": "ttl_elapsed"},
                )
                _put_record(container, next_record)
                expired.append(next_record)
            if expired:
                _touch_container(container, now)

        # Avoid a no-op file revision when there is nothing to expire.
        candidates = [
            item
            for item in self.list(session_id=session_id)
            if item.phase in _PRECLAIM_PHASES and _parse_time(item.expires_at) <= now_value
        ]
        if not candidates:
            return ()
        self.state_store.mutate(mutate, expected_revision=expected_state_revision)
        return tuple(expired)

    def get(self, continuation_id: str) -> PermissionContinuationRecord:
        container = self._read_container()
        return _record_from_container(container, continuation_id)

    def get_by_request(self, request_id: str) -> PermissionContinuationRecord:
        container = self._read_container()
        return _record_by_request(container, request_id)

    def list(
        self,
        *,
        session_id: str | None = None,
        phases: Iterable[PermissionContinuationPhase | str] | None = None,
    ) -> tuple[PermissionContinuationRecord, ...]:
        container = self._read_container()
        phase_values = (
            {PermissionContinuationPhase(str(getattr(item, "value", item))) for item in phases}
            if phases is not None
            else None
        )
        records = [
            PermissionContinuationRecord.from_dict(_mapping(value))
            for value in container["records"].values()
        ]
        records = [
            item
            for item in records
            if (session_id is None or item.session_id == session_id)
            and (phase_values is None or item.phase in phase_values)
        ]
        return tuple(sorted(records, key=lambda item: (item.session_id, item.session_sequence, item.continuation_id)))

    def snapshot(self, *, session_id: str) -> dict[str, Any]:
        if not str(session_id).strip():
            raise ValueError("session_id is required")
        records = [item.to_dict() for item in self.list(session_id=session_id)]
        container = self._read_container()
        snapshot = {
            "schema": PERMISSION_CONTINUATION_SNAPSHOT_SCHEMA,
            "schema_version": PERMISSION_CONTINUATION_SNAPSHOT_VERSION,
            "snapshot_id": f"permcontsnapshot_{uuid4().hex}",
            "session_id": session_id,
            "captured_at": self._now_iso(),
            "store_revision": int(self.state_store.read_state().get("revision") or 0),
            "container_revision": int(container.get("revision") or 0),
            "session_sequence": int(container["session_sequences"].get(session_id) or 0),
            "records": records,
        }
        _reject_raw_arguments(snapshot)
        snapshot["checksum"] = _snapshot_checksum(snapshot)
        return snapshot

    def restore(
        self,
        snapshot: Mapping[str, Any],
        *,
        session_id: str,
        expected_state_revision: int | None = None,
    ) -> tuple[PermissionContinuationRecord, ...]:
        self._ensure_enabled()
        raw = copy.deepcopy(dict(snapshot))
        _reject_raw_arguments(raw)
        if raw.get("schema") != PERMISSION_CONTINUATION_SNAPSHOT_SCHEMA:
            raise PermissionContinuationCorruptError("continuation snapshot schema mismatch")
        if int(raw.get("schema_version") or 0) != PERMISSION_CONTINUATION_SNAPSHOT_VERSION:
            raise PermissionContinuationCorruptError("unsupported continuation snapshot version")
        captured_session = str(raw.get("session_id") or "")
        if not session_id or captured_session != session_id:
            raise PermissionContinuationIdentityError("continuation snapshot session mismatch")
        checksum = str(raw.get("checksum") or "")
        if not checksum or not hmac.compare_digest(checksum, _snapshot_checksum(raw)):
            raise PermissionContinuationCorruptError("continuation snapshot checksum mismatch")
        restored = tuple(
            PermissionContinuationRecord.from_dict(_mapping(item))
            for item in raw.get("records", [])
        )
        if any(item.session_id != session_id for item in restored):
            raise PermissionContinuationIdentityError("snapshot record belongs to another session")
        now = self._now_iso()

        def mutate(state: dict[str, Any]) -> None:
            container = _container(state, create=True, now=now)
            for incoming in restored:
                existing_id = str(container["by_request_id"].get(incoming.request_id) or "")
                if not existing_id:
                    if incoming.continuation_id in container["records"]:
                        raise PermissionContinuationConflict("snapshot continuation id collision")
                    _put_record(container, incoming)
                    container["by_request_id"][incoming.request_id] = incoming.continuation_id
                    continue
                existing = _record_from_container(container, existing_id)
                _assert_same_immutable_identity(existing, incoming)
                if incoming.revision > existing.revision:
                    if _PHASE_RANK[incoming.phase] < _PHASE_RANK[existing.phase]:
                        raise PermissionContinuationConflict("snapshot continuation would move backwards")
                    _put_record(container, incoming)
                elif incoming.revision == existing.revision and incoming.to_dict() != existing.to_dict():
                    raise PermissionContinuationConflict("snapshot continuation conflicts at same revision")
            restored_sequence = int(raw.get("session_sequence") or 0)
            container["session_sequences"][session_id] = max(
                int(container["session_sequences"].get(session_id) or 0),
                restored_sequence,
                *(item.session_sequence for item in restored),
            )
            container["last_restored_snapshot_id"] = str(raw.get("snapshot_id") or "")
            container["last_restore_at"] = now
            _touch_container(container, now)

        self.state_store.mutate(mutate, expected_revision=expected_state_revision)
        return self.list(session_id=session_id)

    def _transition_request_record(
        self,
        request: PermissionRequestRecord,
        *,
        expected_record_revision: int,
        expected_state_revision: int | None,
        transition: str,
    ) -> PermissionContinuationRecord:
        self._ensure_enabled()
        now = self._now_iso()
        updated: list[PermissionContinuationRecord] = []

        def mutate(state: dict[str, Any]) -> None:
            container = _container(state, create=False, now=now)
            record = _record_by_request(container, request.request_id)
            _expect_record_revision(record, expected_record_revision)
            authoritative = self._resolve_authoritative_request(
                state,
                request.request_id,
                supplied=request,
            )
            _assert_request_records_equal(authoritative, request)
            _assert_request_identity(record, request)
            if record.terminal or record.phase is PermissionContinuationPhase.CLAIMED:
                raise PermissionContinuationStateError(
                    f"continuation cannot transition from {record.phase}"
                )
            if request.revision < record.permission_request_revision:
                raise PermissionContinuationConflict("permission request revision moved backwards")
            if transition == "deliver":
                if record.phase is PermissionContinuationPhase.DELIVERED:
                    updated.append(record)
                    return
                if record.phase is not PermissionContinuationPhase.PARKED:
                    raise PermissionContinuationStateError("continuation is not parked")
                next_record = replace(
                    record,
                    phase=PermissionContinuationPhase.DELIVERED,
                    revision=record.revision + 1,
                    permission_request_revision=request.revision,
                    delivery_channel=request.resolution_channel
                    or str(request.metadata.get("delivery_channel") or ""),
                    delivered_at=request.delivered_at or now,
                )
            else:
                if record.phase is PermissionContinuationPhase.RESOLUTION_READY:
                    if record.resolution_effect is not request.resolution_effect:
                        raise PermissionContinuationConflict("resolution effect changed")
                    updated.append(record)
                    return
                if record.phase not in {
                    PermissionContinuationPhase.PARKED,
                    PermissionContinuationPhase.DELIVERED,
                }:
                    raise PermissionContinuationStateError("continuation cannot accept a resolution")
                next_record = replace(
                    record,
                    phase=PermissionContinuationPhase.RESOLUTION_READY,
                    revision=record.revision + 1,
                    permission_request_revision=request.revision,
                    resolution_effect=request.resolution_effect,
                    resolution_id=str(request.metadata.get("resolution_idempotency_key") or ""),
                    resolution_ready_at=request.resolved_at or now,
                    delivered_at=record.delivered_at or request.delivered_at,
                    delivery_channel=record.delivery_channel or request.resolution_channel,
                )
            _put_record(container, next_record)
            _touch_container(container, now)
            updated.append(next_record)

        self.state_store.mutate(mutate, expected_revision=expected_state_revision)
        return updated[0]

    def _finish_claim(
        self,
        request_id: str,
        *,
        claim_id: str,
        expected_record_revision: int,
        phase: PermissionContinuationPhase,
        failure_code: str,
        metadata: Mapping[str, Any] | None,
        expected_state_revision: int | None,
    ) -> PermissionContinuationRecord:
        self._ensure_enabled()
        if not str(claim_id).strip():
            raise ValueError("claim_id is required")
        safe_metadata = _safe_metadata(metadata or {})
        now_value = self._now()
        now = now_value.isoformat()
        updated: list[PermissionContinuationRecord] = []

        def mutate(state: dict[str, Any]) -> None:
            container = _container(state, create=False, now=now)
            record = _record_by_request(container, request_id)
            _expect_record_revision(record, expected_record_revision)
            if record.phase is not PermissionContinuationPhase.CLAIMED:
                raise PermissionContinuationStateError("only a claimed continuation can finish")
            if not hmac.compare_digest(record.claim_id, claim_id):
                raise PermissionContinuationIdentityError("continuation claim_id mismatch")
            if _parse_time(str(record.claim_expires_at or record.expires_at)) <= now_value:
                raise PermissionContinuationStateError(
                    "permission continuation claim lease expired before finish"
                )
            next_record = replace(
                record,
                phase=phase,
                revision=record.revision + 1,
                completed_at=now if phase is PermissionContinuationPhase.COMPLETED else None,
                failed_at=now if phase is PermissionContinuationPhase.FAILED else None,
                failure_code=failure_code,
                metadata={**record.metadata, **safe_metadata},
            )
            _put_record(container, next_record)
            _touch_container(container, now)
            updated.append(next_record)

        self.state_store.mutate(mutate, expected_revision=expected_state_revision)
        return updated[0]

    def _terminal_preclaim(
        self,
        request_id: str,
        *,
        expected_record_revision: int,
        phase: PermissionContinuationPhase,
        reason: str,
        expected_state_revision: int | None,
    ) -> PermissionContinuationRecord:
        self._ensure_enabled()
        now = self._now_iso()
        updated: list[PermissionContinuationRecord] = []

        def mutate(state: dict[str, Any]) -> None:
            container = _container(state, create=False, now=now)
            record = _record_by_request(container, request_id)
            _expect_record_revision(record, expected_record_revision)
            if record.phase not in _PRECLAIM_PHASES:
                raise PermissionContinuationStateError(
                    f"continuation cannot transition from {record.phase} to {phase}"
                )
            next_record = replace(
                record,
                phase=phase,
                revision=record.revision + 1,
                cancelled_at=now if phase is PermissionContinuationPhase.CANCELLED else None,
                expired_at=now if phase is PermissionContinuationPhase.EXPIRED else None,
                metadata={**record.metadata, "terminal_reason": str(reason or phase)},
            )
            _put_record(container, next_record)
            _touch_container(container, now)
            updated.append(next_record)

        self.state_store.mutate(mutate, expected_revision=expected_state_revision)
        return updated[0]

    def _read_container(self) -> dict[str, Any]:
        self._ensure_enabled()
        state = self.state_store.read_state()
        return copy.deepcopy(_container(state, create=True, now=self._now_iso(), persist=False))

    def _resolve_authoritative_request(
        self,
        state: Mapping[str, Any],
        request_id: str,
        *,
        supplied: PermissionRequestRecord | None,
    ) -> PermissionRequestRecord:
        if self.external_permission_authority:
            if supplied is None:
                raise PermissionContinuationStateError(
                    "external permission authority did not supply the current request"
                )
            if supplied.request_id != request_id:
                raise PermissionContinuationIdentityError(
                    "external permission authority returned another request"
                )
            return supplied
        authoritative = _authoritative_request(state, request_id)
        if supplied is not None:
            _assert_request_records_equal(authoritative, supplied)
        return authoritative

    def _ensure_enabled(self) -> None:
        if self.disabled:
            raise PermissionContinuationDisabledError("PermissionContinuationStore is disabled")
        if getattr(self.state_store, "disabled", False):
            raise PermissionStateDisabled("PermissionStateStore is disabled")

    def _now(self) -> datetime:
        value = self.state_store.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return self._now().isoformat()


class PermissionContinuationRuntime:
    """Integration facade for API/QueryEngine pending-call handoff.

    ``prepare_resume`` validates *both* the caller's replay identity and the
    session store payload resolved from the immutable locator before claiming
    the continuation.  It never calls a permission evaluator, issues a grant,
    or reports execution as authorized.
    """

    def __init__(
        self,
        state_store: PermissionStateStore,
        *,
        session_id: str,
        payload_resolver: PayloadResolver | None = None,
        disabled: bool = False,
        claim_lease_seconds: float = PERMISSION_CONTINUATION_CLAIM_LEASE_SECONDS,
        external_permission_authority: bool = False,
    ) -> None:
        if not str(session_id).strip():
            raise ValueError("PermissionContinuationRuntime requires session_id")
        self.session_id = str(session_id)
        self.store = PermissionContinuationStore(
            state_store,
            disabled=disabled,
            claim_lease_seconds=claim_lease_seconds,
            external_permission_authority=external_permission_authority,
        )
        self.payload_resolver = payload_resolver
        self.disabled = bool(disabled)

    def park(
        self,
        request: PermissionRequestRecord,
        *,
        payload_locator: str,
        session_sequence: int,
        continuation_id: str = "",
        metadata: Mapping[str, Any] | None = None,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        self._ensure_owned_request(request)
        return self.store.park(
            request,
            payload_locator=payload_locator,
            session_sequence=session_sequence,
            continuation_id=continuation_id,
            metadata=metadata,
            expected_state_revision=expected_state_revision,
        )

    def deliver(
        self,
        request: PermissionRequestRecord,
        *,
        expected_record_revision: int,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        self._ensure_owned_request(request)
        return self.store.deliver(
            request,
            expected_record_revision=expected_record_revision,
            expected_state_revision=expected_state_revision,
        )

    def resolution_ready(
        self,
        request: PermissionRequestRecord,
        *,
        expected_record_revision: int,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationRecord:
        self._ensure_owned_request(request)
        return self.store.resolution_ready(
            request,
            expected_record_revision=expected_record_revision,
            expected_state_revision=expected_state_revision,
        )

    def prepare_resume(
        self,
        request_id: str,
        presented: PermissionContinuationReplay | Mapping[str, Any],
        *,
        claimant: str,
        idempotency_key: str,
        expected_record_revision: int | None = None,
        payload_resolver: PayloadResolver | None = None,
        authoritative_request: PermissionRequestRecord | None = None,
        expected_state_revision: int | None = None,
    ) -> PermissionContinuationClaim:
        self._ensure_enabled()
        if authoritative_request is not None:
            self._ensure_owned_request(authoritative_request)
            if authoritative_request.request_id != request_id:
                raise PermissionContinuationIdentityError(
                    "authoritative permission request id does not match continuation"
                )
        record = self.store.get_by_request(request_id)
        self._ensure_owned(record)
        if record.phase is PermissionContinuationPhase.CLAIMED:
            claim_expires_at = _parse_time(str(record.claim_expires_at or record.expires_at))
            if claim_expires_at > self.store._now():
                raise PermissionContinuationAlreadyClaimed(
                    "permission continuation was already claimed"
                )
        elif record.phase is not PermissionContinuationPhase.RESOLUTION_READY:
            raise PermissionContinuationStateError("permission continuation is not resolution-ready")
        resolver = payload_resolver or self.payload_resolver
        if resolver is None:
            raise PermissionContinuationPayloadMissing(
                "no session payload resolver is configured; continuation remains unclaimed"
            )
        try:
            resolved_payload = resolver(record)
        except PermissionContinuationError:
            raise
        except Exception as error:  # noqa: BLE001 - external payload lookup must fail closed.
            raise PermissionContinuationPayloadMissing(
                f"session payload resolver failed: {type(error).__name__}"
            ) from error
        if resolved_payload is None:
            raise PermissionContinuationPayloadMissing(
                "session payload resolver did not find the parked payload"
            )
        try:
            presented_replay = PermissionContinuationReplay.from_value(presented, source="client")
            authoritative_replay = PermissionContinuationReplay.from_value(
                resolved_payload,
                source="session_store",
            )
        except PermissionContinuationError:
            raise
        except (TypeError, ValueError, KeyError) as error:
            raise PermissionContinuationIdentityError(
                f"continuation replay identity is malformed: {type(error).__name__}"
            ) from error
        _assert_replay_identity(record, presented_replay, source="presented")
        _assert_replay_identity(record, authoritative_replay, source="session_store")
        _assert_same_replay(presented_replay, authoritative_replay)
        claimed = self.store.claim_once(
            request_id,
            claimant=claimant,
            idempotency_key=idempotency_key,
            expected_record_revision=(
                record.revision if expected_record_revision is None else expected_record_revision
            ),
            authoritative_request=authoritative_request,
            expected_state_revision=expected_state_revision,
        )
        return PermissionContinuationClaim(
            record=claimed,
            presented=presented_replay,
            authoritative=authoritative_replay,
            resolved_payload=resolved_payload,
        )

    def claim_once(
        self,
        request_id: str,
        presented: PermissionContinuationReplay | Mapping[str, Any],
        **kwargs: Any,
    ) -> PermissionContinuationClaim:
        """Alias emphasizing that validated resume is the only claim path."""

        return self.prepare_resume(request_id, presented, **kwargs)

    def complete(self, request_id: str, **kwargs: Any) -> PermissionContinuationRecord:
        record = self.store.get_by_request(request_id)
        self._ensure_owned(record)
        return self.store.complete(request_id, **kwargs)

    def release_claim(self, request_id: str, **kwargs: Any) -> PermissionContinuationRecord:
        record = self.store.get_by_request(request_id)
        self._ensure_owned(record)
        return self.store.release_claim(request_id, **kwargs)

    def reconcile_expired_claim(
        self,
        request_id: str,
        **kwargs: Any,
    ) -> PermissionContinuationRecord:
        record = self.store.get_by_request(request_id)
        self._ensure_owned(record)
        return self.store.reconcile_expired_claim(request_id, **kwargs)

    def fail(self, request_id: str, **kwargs: Any) -> PermissionContinuationRecord:
        record = self.store.get_by_request(request_id)
        self._ensure_owned(record)
        return self.store.fail(request_id, **kwargs)

    def cancel(self, request_id: str, **kwargs: Any) -> PermissionContinuationRecord:
        record = self.store.get_by_request(request_id)
        self._ensure_owned(record)
        return self.store.cancel(request_id, **kwargs)

    def expire(self, request_id: str, **kwargs: Any) -> PermissionContinuationRecord:
        record = self.store.get_by_request(request_id)
        self._ensure_owned(record)
        return self.store.expire(request_id, **kwargs)

    def expire_due(self, **kwargs: Any) -> tuple[PermissionContinuationRecord, ...]:
        kwargs.pop("session_id", None)
        return self.store.expire_due(session_id=self.session_id, **kwargs)

    def get(self, request_id: str) -> PermissionContinuationRecord:
        record = self.store.get_by_request(request_id)
        self._ensure_owned(record)
        return record

    def pending(self) -> tuple[PermissionContinuationRecord, ...]:
        return self.store.list(session_id=self.session_id, phases=_PRECLAIM_PHASES)

    def active(self) -> tuple[PermissionContinuationRecord, ...]:
        return self.store.list(session_id=self.session_id, phases=_ACTIVE_PHASES)

    def records(self) -> tuple[PermissionContinuationRecord, ...]:
        return self.store.list(session_id=self.session_id)

    def claim_lease_expired(self, record: PermissionContinuationRecord) -> bool:
        self._ensure_owned(record)
        if record.phase is not PermissionContinuationPhase.CLAIMED:
            return False
        return _parse_time(str(record.claim_expires_at or record.expires_at)) <= self.store._now()

    def snapshot(self) -> dict[str, Any]:
        return self.store.snapshot(session_id=self.session_id)

    def restore(
        self,
        snapshot: Mapping[str, Any],
        *,
        expected_state_revision: int | None = None,
    ) -> tuple[PermissionContinuationRecord, ...]:
        return self.store.restore(
            snapshot,
            session_id=self.session_id,
            expected_state_revision=expected_state_revision,
        )

    def _ensure_owned_request(self, request: PermissionRequestRecord) -> None:
        self._ensure_enabled()
        if request.session_id != self.session_id:
            raise PermissionContinuationIdentityError(
                "permission request belongs to another continuation runtime session"
            )

    def _ensure_owned(self, record: PermissionContinuationRecord) -> None:
        self._ensure_enabled()
        if record.session_id != self.session_id:
            raise PermissionContinuationIdentityError(
                "permission continuation belongs to another runtime session"
            )

    def _ensure_enabled(self) -> None:
        if self.disabled:
            raise PermissionContinuationDisabledError("PermissionContinuationRuntime is disabled")
        self.store._ensure_enabled()


def _container(
    state: dict[str, Any],
    *,
    create: bool,
    now: str,
    persist: bool = True,
) -> dict[str, Any]:
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        raise PermissionContinuationCorruptError("permission state metadata is not an object")
    raw = metadata.get(PERMISSION_CONTINUATION_METADATA_KEY)
    if raw is None:
        container = {
            "schema": PERMISSION_CONTINUATION_SCHEMA,
            "schema_version": PERMISSION_CONTINUATION_VERSION,
            "revision": 0,
            "created_at": now,
            "updated_at": now,
            "records": {},
            "by_request_id": {},
            "session_sequences": {},
            "last_restored_snapshot_id": "",
            "last_restore_at": "",
        }
        if create and persist:
            metadata[PERMISSION_CONTINUATION_METADATA_KEY] = container
        return container
    if not isinstance(raw, dict):
        raise PermissionContinuationCorruptError("permission continuation metadata is not an object")
    container = raw
    if container.get("schema") != PERMISSION_CONTINUATION_SCHEMA:
        raise PermissionContinuationCorruptError("permission continuation schema mismatch")
    if int(container.get("schema_version") or 0) != PERMISSION_CONTINUATION_VERSION:
        raise PermissionContinuationCorruptError("unsupported permission continuation version")
    for name in ("records", "by_request_id", "session_sequences"):
        if not isinstance(container.get(name), dict):
            raise PermissionContinuationCorruptError(
                f"permission continuation field {name} is not an object"
            )
    _reject_raw_arguments(container)
    seen_requests: set[str] = set()
    for continuation_id, value in container["records"].items():
        record = PermissionContinuationRecord.from_dict(_mapping(value))
        if continuation_id != record.continuation_id:
            raise PermissionContinuationCorruptError("continuation record key/id mismatch")
        if record.request_id in seen_requests:
            raise PermissionContinuationCorruptError("duplicate continuation request_id")
        seen_requests.add(record.request_id)
        if container["by_request_id"].get(record.request_id) != continuation_id:
            raise PermissionContinuationCorruptError("continuation request index mismatch")
    for request_id, continuation_id in container["by_request_id"].items():
        if continuation_id not in container["records"]:
            raise PermissionContinuationCorruptError(
                f"continuation request index points to missing record: {request_id}"
            )
    return container


def _touch_container(container: dict[str, Any], now: str) -> None:
    container["revision"] = int(container.get("revision") or 0) + 1
    container["updated_at"] = now


def _put_record(container: dict[str, Any], record: PermissionContinuationRecord) -> None:
    existing_id = str(container["by_request_id"].get(record.request_id) or "")
    if existing_id and existing_id != record.continuation_id:
        raise PermissionContinuationConflict("request_id is already owned by another continuation")
    container["records"][record.continuation_id] = record.to_dict()
    container["by_request_id"][record.request_id] = record.continuation_id
    container["session_sequences"][record.session_id] = max(
        int(container["session_sequences"].get(record.session_id) or 0),
        record.session_sequence,
    )


def _record_from_container(
    container: Mapping[str, Any],
    continuation_id: str,
) -> PermissionContinuationRecord:
    value = _mapping(container.get("records")).get(continuation_id)
    if not isinstance(value, Mapping):
        raise KeyError(continuation_id)
    return PermissionContinuationRecord.from_dict(value)


def _record_by_request(
    container: Mapping[str, Any],
    request_id: str,
) -> PermissionContinuationRecord:
    continuation_id = str(_mapping(container.get("by_request_id")).get(request_id) or "")
    if not continuation_id:
        raise KeyError(request_id)
    return _record_from_container(container, continuation_id)


def _expect_record_revision(record: PermissionContinuationRecord, expected: int) -> None:
    if int(expected) != record.revision:
        raise PermissionContinuationConflict(
            f"continuation revision conflict: expected {expected}, actual {record.revision}"
        )


def _authoritative_request(
    state: Mapping[str, Any],
    request_id: str,
) -> PermissionRequestRecord:
    requests = state.get("requests")
    value = requests.get(request_id) if isinstance(requests, Mapping) else None
    if not isinstance(value, Mapping):
        raise PermissionContinuationStateError(
            "authoritative permission request is missing from PermissionStateStore"
        )
    record = PermissionRequestRecord.from_dict(value)
    if record.request_id != request_id:
        raise PermissionContinuationCorruptError("permission request key/id mismatch")
    return record


def _approval_consumed_outcome_unknown(
    record: PermissionContinuationRecord,
    authoritative: PermissionRequestRecord,
    *,
    now: str,
) -> PermissionContinuationRecord:
    return replace(
        record,
        phase=PermissionContinuationPhase.FAILED,
        revision=record.revision + 1,
        failed_at=now,
        failure_code="approval_consumed_outcome_unknown",
        metadata={
            **record.metadata,
            "execution_outcome_unknown": True,
            "recovery_requires_different_action": True,
            "parked_permission_request_revision": record.permission_request_revision,
            "authoritative_permission_request_revision": authoritative.revision,
        },
    )


def _approval_execution_claimed(request: PermissionRequestRecord) -> bool:
    return bool(str(request.metadata.get("execution_claim_decision_id") or "").strip())


def _assert_request_records_equal(
    authoritative: PermissionRequestRecord,
    supplied: PermissionRequestRecord,
) -> None:
    if canonical_arguments_json(authoritative.to_dict()) != canonical_arguments_json(supplied.to_dict()):
        raise PermissionContinuationIdentityError(
            "supplied permission request is not the authoritative store record"
        )


def _assert_request_identity(
    record: PermissionContinuationRecord,
    request: PermissionRequestRecord,
) -> None:
    mismatches: list[str] = []
    for expected, actual, name in (
        (record.request_id, request.request_id, "request_id"),
        (record.session_id, request.session_id, "session_id"),
        (record.task_id, request.task_id, "task_id"),
        (record.run_id, request.run_id, "run_id"),
        (record.tool_use_id, request.tool_use_id, "tool_use_id"),
        (record.tool_identity, request.tool_identity, "tool_identity"),
        (record.scope.to_dict(), request.scope.to_dict(), "scope"),
        (record.expires_at, request.expires_at, "expires_at"),
    ):
        if expected != actual:
            mismatches.append(name)
    for expected, actual, name in (
        (record.arguments_digest, request.arguments_digest, "arguments_digest"),
        (record.request_fingerprint, request.request_fingerprint, "request_fingerprint"),
    ):
        if not hmac.compare_digest(expected, actual):
            mismatches.append(name)
    if mismatches:
        raise PermissionContinuationIdentityError(
            "permission request does not match parked continuation: " + ", ".join(mismatches)
        )


def _assert_replay_identity(
    record: PermissionContinuationRecord,
    replay: PermissionContinuationReplay,
    *,
    source: str,
) -> None:
    mismatches: list[str] = []
    for expected, actual, name in (
        (record.session_id, replay.session_id, "session_id"),
        (record.task_id, replay.task_id, "task_id"),
        (record.run_id, replay.run_id, "run_id"),
        (record.tool_use_id, replay.tool_use_id, "tool_use_id"),
        (record.tool_identity.namespace, replay.tool_identity.namespace, "tool_namespace"),
        (record.tool_identity.name, replay.tool_identity.name, "tool_name"),
        (record.tool_identity.server_id, replay.tool_identity.server_id, "server_id"),
        (record.tool_identity.version, replay.tool_identity.version, "tool_version"),
        (record.tool_identity.schema_digest, replay.tool_identity.schema_digest, "tool_schema_digest"),
        (record.payload_locator, replay.payload_locator, "payload_locator"),
        (record.session_sequence, replay.session_sequence, "session_sequence"),
    ):
        if expected != actual:
            mismatches.append(name)
    for expected, actual, name in (
        (record.arguments_digest, replay.arguments_digest, "arguments_digest"),
        (record.request_fingerprint, replay.request_fingerprint, "request_fingerprint"),
    ):
        if not hmac.compare_digest(expected, actual):
            mismatches.append(name)
    if canonical_arguments_json(record.scope.to_dict()) != canonical_arguments_json(replay.scope.to_dict()):
        mismatches.append("scope")
    if mismatches:
        raise PermissionContinuationIdentityError(
            f"{source} replay does not match parked continuation: " + ", ".join(mismatches)
        )


def _assert_same_replay(
    presented: PermissionContinuationReplay,
    authoritative: PermissionContinuationReplay,
) -> None:
    if canonical_arguments_json(presented.to_dict()) != canonical_arguments_json(authoritative.to_dict()):
        # Source/metadata describe transport provenance, not tool identity.
        left = presented.to_dict()
        right = authoritative.to_dict()
        for item in (left, right):
            item.pop("source", None)
            item.pop("metadata", None)
        if canonical_arguments_json(left) != canonical_arguments_json(right):
            raise PermissionContinuationIdentityError(
                "presented replay differs from authoritative session payload"
            )


def _assert_same_immutable_identity(
    left: PermissionContinuationRecord,
    right: PermissionContinuationRecord,
) -> None:
    if not hmac.compare_digest(left.identity_digest, right.identity_digest):
        raise PermissionContinuationIdentityError(
            "request_id already has a different immutable continuation identity"
        )
    if left.continuation_id != right.continuation_id:
        # A caller retry may omit/recreate a local continuation id; request id
        # remains the durable idempotency key, so do not reject solely for it.
        return


def _record_identity_digest(record: PermissionContinuationRecord) -> str:
    payload = {
        "schema": "zyra.permission-continuation-identity.v1",
        "request_id": record.request_id,
        "session_id": record.session_id,
        "task_id": record.task_id,
        "run_id": record.run_id,
        "worker_request_id": record.worker_request_id,
        "tool_use_id": record.tool_use_id,
        "tool_identity": record.tool_identity.to_dict(),
        "arguments_digest": record.arguments_digest,
        "request_fingerprint": record.request_fingerprint,
        "scope": record.scope.to_dict(),
        "payload_locator": record.payload_locator,
        "session_sequence": record.session_sequence,
        "expires_at": record.expires_at,
    }
    encoded = canonical_arguments_json(payload).encode("utf-8")
    return f"sha256:zyra-permission-continuation-v1:{hashlib.sha256(encoded).hexdigest()}"


def _snapshot_checksum(snapshot: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(snapshot))
    payload.pop("checksum", None)
    return f"sha256:{hashlib.sha256(canonical_arguments_json(payload).encode('utf-8')).hexdigest()}"


def _validate_locator(value: str) -> None:
    locator = str(value)
    if not _LOCATOR_RE.fullmatch(locator):
        raise ValueError("payload_locator must be a bounded opaque URI-like locator")
    lowered = locator.casefold()
    if any(token in lowered for token in ("arguments=", "raw_arguments", "canonical_arguments")):
        raise ValueError("payload_locator cannot contain raw tool arguments")
    if locator.lstrip().startswith(("{", "[")):
        raise ValueError("payload_locator cannot embed a JSON payload")


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("continuation metadata must be a mapping")
    _reject_raw_arguments(value)
    return _json_clone(value)


def _json_clone(value: Any, *, path: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite continuation metadata at {path}")
        return value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise TypeError(f"non-string continuation metadata key at {path}")
            output[raw_key] = _json_clone(item, path=f"{path}.{raw_key}")
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_json_clone(item, path=f"{path}[]") for item in value]
    raise TypeError(f"non-JSON continuation metadata at {path}: {type(value).__name__}")


def _reject_raw_arguments(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).casefold().replace("-", "_")
            if key in _FORBIDDEN_METADATA_KEYS:
                raise ValueError(f"raw tool arguments are forbidden in continuation state at {path}.{raw_key}")
            _reject_raw_arguments(item, path=f"{path}.{raw_key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        for index, item in enumerate(value):
            _reject_raw_arguments(item, path=f"{path}[{index}]")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _require_aware_time(value: str, name: str) -> None:
    try:
        _parse_time(value)
    except ValueError as error:
        raise ValueError(f"{name} must be timezone-aware ISO-8601") from error


__all__ = [
    "PERMISSION_CONTINUATION_METADATA_KEY",
    "PERMISSION_CONTINUATION_SCHEMA",
    "PERMISSION_CONTINUATION_SNAPSHOT_SCHEMA",
    "PermissionContinuationAlreadyClaimed",
    "PermissionContinuationClaim",
    "PermissionContinuationConflict",
    "PermissionContinuationCorruptError",
    "PermissionContinuationDisabledError",
    "PermissionContinuationError",
    "PermissionContinuationIdentityError",
    "PermissionContinuationPayloadMissing",
    "PermissionContinuationPhase",
    "PermissionContinuationRecord",
    "PermissionContinuationReplay",
    "PermissionContinuationRuntime",
    "PermissionContinuationStateError",
    "PermissionContinuationStore",
]
