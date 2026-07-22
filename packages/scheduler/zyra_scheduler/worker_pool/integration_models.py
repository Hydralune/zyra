from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .models import (
    CapabilityRequirement,
    ExecutionOutcome,
    ResourceVector,
    new_pool_id,
    require_token,
    stable_digest,
    utc_iso,
)


class DispatchMode(StrEnum):
    FOREGROUND = "foreground"
    BACKGROUND = "background"


class AdmissionPhase(StrEnum):
    REQUESTED = "requested"
    ADMITTED = "admitted"
    DISPATCHED = "dispatched"
    PARKED = "parked"
    DRAINING = "draining"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"
    SUPERSEDED = "superseded"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.CANCELLED,
            self.SUPERSEDED,
        }


class ControlKind(StrEnum):
    CANCEL = "cancel"
    DRAIN = "drain"
    WAKE = "wake"
    STOP = "stop"
    PARK = "park"
    REVIVE = "revive"


class ControlPhase(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    APPLIED = "applied"
    FAILED = "failed"
    SUPERSEDED = "superseded"

    @property
    def terminal(self) -> bool:
        return self in {self.APPLIED, self.FAILED, self.SUPERSEDED}


class RenewalDisposition(StrEnum):
    NOT_DUE = "not_due"
    RENEWED = "renewed"
    REJECTED_HEALTH = "rejected_health"
    REJECTED_ROUTE = "rejected_route"
    REJECTED_DRAIN = "rejected_drain"
    FENCED = "fenced"
    EXPIRED = "expired"
    TERMINAL = "terminal"


class RouteHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    QUARANTINED = "quarantined"
    DISABLED = "disabled"

    @property
    def dispatchable(self) -> bool:
        return self in {self.HEALTHY, self.DEGRADED}


class YieldKind(StrEnum):
    RESULT = "result"
    ARTIFACT = "artifact"
    HANDOFF = "handoff"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RecoveryDisposition(StrEnum):
    RESUME = "resume"
    REASSIGN = "reassign"
    REPLAN = "replan"
    CANCEL = "cancel"
    OBSERVE = "observe"


def _tokens(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({require_token(str(value), "token") for value in values if str(value).strip()}))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _enum(enum_type: type[StrEnum], value: Any, default: StrEnum) -> Any:
    try:
        return enum_type(str(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class ForeignStateRef:
    """Read-only identity for state whose writer belongs to another unit."""

    owner: str
    kind: str
    object_id: str
    revision: int = 0
    digest: str = ""
    required: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_token(self.owner, "foreign state owner")
        require_token(self.kind, "foreign state kind")
        if self.required:
            require_token(self.object_id, "foreign state object id")
        if self.revision < 0:
            raise ValueError("foreign state revision cannot be negative")
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "kind": self.kind,
            "object_id": self.object_id,
            "revision": self.revision,
            "digest": self.digest,
            "required": self.required,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "ForeignStateRef":
        data = _mapping(value)
        return cls(
            owner=str(data.get("owner") or "unknown"),
            kind=str(data.get("kind") or "unknown"),
            object_id=str(data.get("object_id") or ""),
            revision=int(data.get("revision") or 0),
            digest=str(data.get("digest") or ""),
            required=bool(data.get("required", True)),
            metadata=dict(_mapping(data.get("metadata"))),
        )


@dataclass(frozen=True, slots=True)
class DispatchForeignRefs:
    logical_task: ForeignStateRef
    backend_route: ForeignStateRef
    workspace: ForeignStateRef
    gateway: ForeignStateRef
    graph: ForeignStateRef
    checkpoint: ForeignStateRef | None = None
    memory_signal: ForeignStateRef | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_task": self.logical_task.to_dict(),
            "backend_route": self.backend_route.to_dict(),
            "workspace": self.workspace.to_dict(),
            "gateway": self.gateway.to_dict(),
            "graph": self.graph.to_dict(),
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "memory_signal": self.memory_signal.to_dict() if self.memory_signal else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DispatchForeignRefs":
        return cls(
            logical_task=ForeignStateRef.from_dict(_mapping(value.get("logical_task"))),
            backend_route=ForeignStateRef.from_dict(_mapping(value.get("backend_route"))),
            workspace=ForeignStateRef.from_dict(_mapping(value.get("workspace"))),
            gateway=ForeignStateRef.from_dict(_mapping(value.get("gateway"))),
            graph=ForeignStateRef.from_dict(_mapping(value.get("graph"))),
            checkpoint=(
                ForeignStateRef.from_dict(_mapping(value.get("checkpoint")))
                if value.get("checkpoint")
                else None
            ),
            memory_signal=(
                ForeignStateRef.from_dict(_mapping(value.get("memory_signal")))
                if value.get("memory_signal")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    maximum_active_per_session: int = 4
    maximum_active_per_run: int = 32
    maximum_active_per_worker: int = 16
    default_lease_ttl_seconds: float = 30.0
    maximum_lease_ttl_seconds: float = 3600.0
    renewal_fraction: float = 0.5
    minimum_renewal_lead_seconds: float = 2.0
    allow_degraded_routes: bool = True
    require_current_heartbeat: bool = True

    def __post_init__(self) -> None:
        for label, value in {
            "maximum_active_per_session": self.maximum_active_per_session,
            "maximum_active_per_run": self.maximum_active_per_run,
            "maximum_active_per_worker": self.maximum_active_per_worker,
        }.items():
            if value < 1:
                raise ValueError(f"{label} must be positive")
        if self.default_lease_ttl_seconds <= 0:
            raise ValueError("default lease ttl must be positive")
        if self.maximum_lease_ttl_seconds < self.default_lease_ttl_seconds:
            raise ValueError("maximum lease ttl cannot be below the default")
        if not 0.05 <= self.renewal_fraction <= 0.95:
            raise ValueError("renewal fraction must be in 0.05..0.95")

    def to_dict(self) -> dict[str, Any]:
        return {
            "maximum_active_per_session": self.maximum_active_per_session,
            "maximum_active_per_run": self.maximum_active_per_run,
            "maximum_active_per_worker": self.maximum_active_per_worker,
            "default_lease_ttl_seconds": self.default_lease_ttl_seconds,
            "maximum_lease_ttl_seconds": self.maximum_lease_ttl_seconds,
            "renewal_fraction": self.renewal_fraction,
            "minimum_renewal_lead_seconds": self.minimum_renewal_lead_seconds,
            "allow_degraded_routes": self.allow_degraded_routes,
            "require_current_heartbeat": self.require_current_heartbeat,
        }


@dataclass(frozen=True, slots=True)
class DispatchAdmissionRequest:
    task_id: str
    run_id: str
    owner_session_id: str
    logical_attempt: int
    requirement: CapabilityRequirement
    foreign_refs: DispatchForeignRefs
    execution_mode: DispatchMode = DispatchMode.FOREGROUND
    preferred_worker_ids: tuple[str, ...] = ()
    excluded_worker_ids: tuple[str, ...] = ()
    attempt_number: int | None = None
    lease_ttl_seconds: float | None = None
    edge_only: bool = False
    idempotency_key: str = ""
    causation_id: str = ""
    correlation_id: str = ""
    recovery_reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_token(self.task_id, "task id")
        require_token(self.run_id, "run id")
        require_token(self.owner_session_id, "owner session id")
        if self.logical_attempt < 1:
            raise ValueError("logical attempt must be positive")
        if self.attempt_number is not None and self.attempt_number < 1:
            raise ValueError("attempt number must be positive")
        if self.lease_ttl_seconds is not None and self.lease_ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        if self.foreign_refs.logical_task.object_id != self.task_id:
            raise ValueError("logical task ref must identify the admission task")
        object.__setattr__(self, "preferred_worker_ids", _tokens(self.preferred_worker_ids))
        object.__setattr__(self, "excluded_worker_ids", _tokens(self.excluded_worker_ids))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    @property
    def request_digest(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "owner_session_id": self.owner_session_id,
            "logical_attempt": self.logical_attempt,
            "requirement": self.requirement.to_dict(),
            "foreign_refs": self.foreign_refs.to_dict(),
            "execution_mode": self.execution_mode.value,
            "preferred_worker_ids": list(self.preferred_worker_ids),
            "excluded_worker_ids": list(self.excluded_worker_ids),
            "attempt_number": self.attempt_number,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "edge_only": self.edge_only,
            "idempotency_key": self.idempotency_key,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "recovery_reason": self.recovery_reason,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class CapacityObservation:
    worker_id: str
    accepted: bool
    score: float
    manifest_digest: str
    capacity: ResourceVector
    allocated: ResourceVector
    available: ResourceVector
    active_worker_leases: int
    active_session_leases: int
    active_run_leases: int
    route_health: RouteHealth
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_token(self.worker_id, "worker id")
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "accepted": self.accepted,
            "score": self.score,
            "manifest_digest": self.manifest_digest,
            "capacity": self.capacity.to_dict(),
            "allocated": self.allocated.to_dict(),
            "available": self.available.to_dict(),
            "active_worker_leases": self.active_worker_leases,
            "active_session_leases": self.active_session_leases,
            "active_run_leases": self.active_run_leases,
            "route_health": self.route_health.value,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class PhysicalDispatchBinding:
    binding_id: str
    task_id: str
    run_id: str
    owner_session_id: str
    logical_attempt: int
    attempt_id: str
    attempt_number: int
    lease_id: str
    worker_id: str
    worker_generation: int
    backend_id: str
    manifest_digest: str
    fence_epoch: int
    phase: AdmissionPhase
    execution_mode: DispatchMode
    foreign_refs: DispatchForeignRefs
    request_digest: str
    idempotency_key: str
    edge_only: bool = False
    progress_sequence: int = 0
    progress_digest: str = ""
    typed_yield_id: str = ""
    backend_dispatch_id: str = ""
    created_at: str = field(default_factory=utc_iso)
    updated_at: str = field(default_factory=utc_iso)
    terminal_at: str = ""
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)
    digest: str = ""

    def __post_init__(self) -> None:
        for label, value in {
            "binding id": self.binding_id,
            "task id": self.task_id,
            "run id": self.run_id,
            "owner session id": self.owner_session_id,
            "attempt id": self.attempt_id,
            "lease id": self.lease_id,
            "worker id": self.worker_id,
            "backend id": self.backend_id,
            "manifest digest": self.manifest_digest,
            "request digest": self.request_digest,
            "idempotency key": self.idempotency_key,
        }.items():
            require_token(value, label)
        if min(self.logical_attempt, self.attempt_number, self.worker_generation, self.fence_epoch, self.version) < 1:
            raise ValueError("dispatch binding revisions must be positive")
        if self.progress_sequence < 0:
            raise ValueError("progress sequence cannot be negative")
        object.__setattr__(self, "metadata", dict(self.metadata))
        expected = self.compute_digest()
        if self.digest and self.digest != expected:
            raise ValueError("physical dispatch binding digest mismatch")
        object.__setattr__(self, "digest", expected)

    @property
    def terminal(self) -> bool:
        return self.phase.terminal

    def compute_digest(self) -> str:
        return stable_digest(self.to_dict(include_digest=False))

    def advance(self, phase: AdmissionPhase | None = None, **changes: Any) -> "PhysicalDispatchBinding":
        payload = {**changes, "version": self.version + 1, "updated_at": utc_iso(), "digest": ""}
        if phase is not None:
            payload["phase"] = phase
            if phase.terminal:
                payload.setdefault("terminal_at", utc_iso())
        return replace(self, **payload)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "binding_id": self.binding_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "owner_session_id": self.owner_session_id,
            "logical_attempt": self.logical_attempt,
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "worker_generation": self.worker_generation,
            "backend_id": self.backend_id,
            "manifest_digest": self.manifest_digest,
            "fence_epoch": self.fence_epoch,
            "phase": self.phase.value,
            "execution_mode": self.execution_mode.value,
            "foreign_refs": self.foreign_refs.to_dict(),
            "request_digest": self.request_digest,
            "idempotency_key": self.idempotency_key,
            "edge_only": self.edge_only,
            "progress_sequence": self.progress_sequence,
            "progress_digest": self.progress_digest,
            "typed_yield_id": self.typed_yield_id,
            "backend_dispatch_id": self.backend_dispatch_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "terminal_at": self.terminal_at,
            "version": self.version,
            "metadata": dict(self.metadata),
        }
        if include_digest:
            result["digest"] = self.digest
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PhysicalDispatchBinding":
        return cls(
            binding_id=str(value.get("binding_id") or ""),
            task_id=str(value.get("task_id") or ""),
            run_id=str(value.get("run_id") or ""),
            owner_session_id=str(value.get("owner_session_id") or ""),
            logical_attempt=int(value.get("logical_attempt") or 1),
            attempt_id=str(value.get("attempt_id") or ""),
            attempt_number=int(value.get("attempt_number") or 1),
            lease_id=str(value.get("lease_id") or ""),
            worker_id=str(value.get("worker_id") or ""),
            worker_generation=int(value.get("worker_generation") or 1),
            backend_id=str(value.get("backend_id") or ""),
            manifest_digest=str(value.get("manifest_digest") or ""),
            fence_epoch=int(value.get("fence_epoch") or 1),
            phase=_enum(AdmissionPhase, value.get("phase"), AdmissionPhase.REQUESTED),
            execution_mode=_enum(DispatchMode, value.get("execution_mode"), DispatchMode.FOREGROUND),
            foreign_refs=DispatchForeignRefs.from_dict(_mapping(value.get("foreign_refs"))),
            request_digest=str(value.get("request_digest") or ""),
            idempotency_key=str(value.get("idempotency_key") or ""),
            edge_only=bool(value.get("edge_only", False)),
            progress_sequence=int(value.get("progress_sequence") or 0),
            progress_digest=str(value.get("progress_digest") or ""),
            typed_yield_id=str(value.get("typed_yield_id") or ""),
            backend_dispatch_id=str(value.get("backend_dispatch_id") or ""),
            created_at=str(value.get("created_at") or utc_iso()),
            updated_at=str(value.get("updated_at") or utc_iso()),
            terminal_at=str(value.get("terminal_at") or ""),
            version=int(value.get("version") or 1),
            metadata=dict(_mapping(value.get("metadata"))),
            digest=str(value.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class ControlCommand:
    command_id: str
    kind: ControlKind
    phase: ControlPhase
    actor_id: str
    reason: str
    idempotency_key: str
    task_id: str = ""
    run_id: str = ""
    worker_id: str = ""
    attempt_id: str = ""
    lease_id: str = ""
    binding_id: str = ""
    claim_owner: str = ""
    claim_deadline_at: str = ""
    attempts: int = 0
    error: str = ""
    effect: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_iso)
    updated_at: str = field(default_factory=utc_iso)
    completed_at: str = ""
    version: int = 1
    digest: str = ""

    def __post_init__(self) -> None:
        require_token(self.command_id, "command id")
        require_token(self.actor_id, "control actor")
        reason = str(self.reason or "").strip()
        if not reason:
            raise ValueError("control reason is required")
        if len(reason) > 2048:
            raise ValueError("control reason exceeds 2048 characters")
        object.__setattr__(self, "reason", reason)
        require_token(self.idempotency_key, "control idempotency key")
        if not self.task_id and not self.worker_id and not self.lease_id:
            raise ValueError("control command requires a task, worker, or lease target")
        if self.attempts < 0 or self.version < 1:
            raise ValueError("control command revisions are invalid")
        object.__setattr__(self, "effect", copy.deepcopy(dict(self.effect)))
        expected = stable_digest(self.to_dict(include_digest=False))
        if self.digest and self.digest != expected:
            raise ValueError("control command digest mismatch")
        object.__setattr__(self, "digest", expected)

    @property
    def terminal(self) -> bool:
        return self.phase.terminal

    def advance(self, phase: ControlPhase | None = None, **changes: Any) -> "ControlCommand":
        payload = {**changes, "updated_at": utc_iso(), "version": self.version + 1, "digest": ""}
        if phase is not None:
            payload["phase"] = phase
            if phase.terminal:
                payload.setdefault("completed_at", utc_iso())
        return replace(self, **payload)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "command_id": self.command_id,
            "kind": self.kind.value,
            "phase": self.phase.value,
            "actor_id": self.actor_id,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "worker_id": self.worker_id,
            "attempt_id": self.attempt_id,
            "lease_id": self.lease_id,
            "binding_id": self.binding_id,
            "claim_owner": self.claim_owner,
            "claim_deadline_at": self.claim_deadline_at,
            "attempts": self.attempts,
            "error": self.error,
            "effect": copy.deepcopy(dict(self.effect)),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "version": self.version,
        }
        if include_digest:
            result["digest"] = self.digest
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ControlCommand":
        return cls(
            command_id=str(value.get("command_id") or ""),
            kind=_enum(ControlKind, value.get("kind"), ControlKind.CANCEL),
            phase=_enum(ControlPhase, value.get("phase"), ControlPhase.PENDING),
            actor_id=str(value.get("actor_id") or "unknown"),
            reason=str(value.get("reason") or "unspecified control"),
            idempotency_key=str(value.get("idempotency_key") or ""),
            task_id=str(value.get("task_id") or ""),
            run_id=str(value.get("run_id") or ""),
            worker_id=str(value.get("worker_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            lease_id=str(value.get("lease_id") or ""),
            binding_id=str(value.get("binding_id") or ""),
            claim_owner=str(value.get("claim_owner") or ""),
            claim_deadline_at=str(value.get("claim_deadline_at") or ""),
            attempts=int(value.get("attempts") or 0),
            error=str(value.get("error") or ""),
            effect=dict(_mapping(value.get("effect"))),
            created_at=str(value.get("created_at") or utc_iso()),
            updated_at=str(value.get("updated_at") or utc_iso()),
            completed_at=str(value.get("completed_at") or ""),
            version=int(value.get("version") or 1),
            digest=str(value.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class LeaseRenewalRecord:
    renewal_id: str
    lease_id: str
    task_id: str
    attempt_id: str
    worker_id: str
    fence_epoch: int
    disposition: RenewalDisposition
    prior_deadline_at: str
    next_deadline_at: str
    heartbeat_sequence: int
    route_health: RouteHealth
    reason: str
    progress_sequence: int = 0
    observed_at: str = field(default_factory=utc_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    digest: str = ""

    def __post_init__(self) -> None:
        for label, value in {
            "renewal id": self.renewal_id,
            "lease id": self.lease_id,
            "task id": self.task_id,
            "attempt id": self.attempt_id,
            "worker id": self.worker_id,
        }.items():
            require_token(value, label)
        reason = str(self.reason or "").strip()
        if not reason:
            raise ValueError("renewal reason is required")
        if len(reason) > 2048:
            raise ValueError("renewal reason exceeds 2048 characters")
        object.__setattr__(self, "reason", reason)
        if min(self.fence_epoch, self.heartbeat_sequence) < 0 or self.progress_sequence < 0:
            raise ValueError("renewal counters cannot be negative")
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        expected = stable_digest(self.to_dict(include_digest=False))
        if self.digest and self.digest != expected:
            raise ValueError("lease renewal record digest mismatch")
        object.__setattr__(self, "digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "renewal_id": self.renewal_id,
            "lease_id": self.lease_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "worker_id": self.worker_id,
            "fence_epoch": self.fence_epoch,
            "disposition": self.disposition.value,
            "prior_deadline_at": self.prior_deadline_at,
            "next_deadline_at": self.next_deadline_at,
            "heartbeat_sequence": self.heartbeat_sequence,
            "route_health": self.route_health.value,
            "reason": self.reason,
            "progress_sequence": self.progress_sequence,
            "observed_at": self.observed_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }
        if include_digest:
            result["digest"] = self.digest
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LeaseRenewalRecord":
        return cls(
            renewal_id=str(value.get("renewal_id") or ""),
            lease_id=str(value.get("lease_id") or ""),
            task_id=str(value.get("task_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            worker_id=str(value.get("worker_id") or ""),
            fence_epoch=int(value.get("fence_epoch") or 0),
            disposition=_enum(RenewalDisposition, value.get("disposition"), RenewalDisposition.NOT_DUE),
            prior_deadline_at=str(value.get("prior_deadline_at") or ""),
            next_deadline_at=str(value.get("next_deadline_at") or ""),
            heartbeat_sequence=int(value.get("heartbeat_sequence") or 0),
            route_health=_enum(RouteHealth, value.get("route_health"), RouteHealth.UNAVAILABLE),
            reason=str(value.get("reason") or "renewal observation"),
            progress_sequence=int(value.get("progress_sequence") or 0),
            observed_at=str(value.get("observed_at") or utc_iso()),
            metadata=dict(_mapping(value.get("metadata"))),
            digest=str(value.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class TypedYieldReceipt:
    yield_id: str
    task_id: str
    run_id: str
    binding_id: str
    attempt_id: str
    lease_id: str
    kind: YieldKind
    sequence: int
    summary: str
    payload: Mapping[str, Any]
    artifact_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_iso)
    digest: str = ""

    def __post_init__(self) -> None:
        for label, value in {
            "yield id": self.yield_id,
            "task id": self.task_id,
            "run id": self.run_id,
            "binding id": self.binding_id,
            "attempt id": self.attempt_id,
            "lease id": self.lease_id,
        }.items():
            require_token(value, label)
        summary = str(self.summary or "").strip()
        if not summary:
            raise ValueError("yield summary is required")
        if len(summary) > 8192:
            raise ValueError("yield summary exceeds 8192 characters")
        object.__setattr__(self, "summary", summary)
        if self.sequence < 1:
            raise ValueError("typed yield sequence must be positive")
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))
        object.__setattr__(self, "artifact_refs", _tokens(self.artifact_refs))
        object.__setattr__(self, "event_refs", _tokens(self.event_refs))
        expected = stable_digest(self.to_dict(include_digest=False))
        if self.digest and self.digest != expected:
            raise ValueError("typed yield digest mismatch")
        object.__setattr__(self, "digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "yield_id": self.yield_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "binding_id": self.binding_id,
            "attempt_id": self.attempt_id,
            "lease_id": self.lease_id,
            "kind": self.kind.value,
            "sequence": self.sequence,
            "summary": self.summary,
            "payload": copy.deepcopy(dict(self.payload)),
            "artifact_refs": list(self.artifact_refs),
            "event_refs": list(self.event_refs),
            "created_at": self.created_at,
        }
        if include_digest:
            result["digest"] = self.digest
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TypedYieldReceipt":
        return cls(
            yield_id=str(value.get("yield_id") or ""),
            task_id=str(value.get("task_id") or ""),
            run_id=str(value.get("run_id") or ""),
            binding_id=str(value.get("binding_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            lease_id=str(value.get("lease_id") or ""),
            kind=_enum(YieldKind, value.get("kind"), YieldKind.RESULT),
            sequence=int(value.get("sequence") or 1),
            summary=str(value.get("summary") or ""),
            payload=dict(_mapping(value.get("payload"))),
            artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
            event_refs=tuple(str(item) for item in value.get("event_refs") or ()),
            created_at=str(value.get("created_at") or utc_iso()),
            digest=str(value.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class RecoveryEvidence:
    evidence_id: str
    task_id: str
    run_id: str
    attempt_id: str
    lease_id: str
    worker_id: str
    disposition: RecoveryDisposition
    reason: str
    signal_kind: str
    successor_attempt_id: str = ""
    successor_lease_id: str = ""
    graph_ref: Mapping[str, Any] = field(default_factory=dict)
    route_ref: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    digest: str = ""

    def __post_init__(self) -> None:
        for label, value in {
            "evidence id": self.evidence_id,
            "task id": self.task_id,
            "run id": self.run_id,
            "attempt id": self.attempt_id,
            "lease id": self.lease_id,
            "worker id": self.worker_id,
            "signal kind": self.signal_kind,
        }.items():
            require_token(value, label)
        reason = str(self.reason or "").strip()
        if not reason:
            raise ValueError("recovery reason is required")
        if len(reason) > 4096:
            raise ValueError("recovery reason exceeds 4096 characters")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "graph_ref", copy.deepcopy(dict(self.graph_ref)))
        object.__setattr__(self, "route_ref", copy.deepcopy(dict(self.route_ref)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        expected = stable_digest(self.to_dict(include_digest=False))
        if self.digest and self.digest != expected:
            raise ValueError("recovery evidence digest mismatch")
        object.__setattr__(self, "digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "evidence_id": self.evidence_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "disposition": self.disposition.value,
            "reason": self.reason,
            "signal_kind": self.signal_kind,
            "successor_attempt_id": self.successor_attempt_id,
            "successor_lease_id": self.successor_lease_id,
            "graph_ref": copy.deepcopy(dict(self.graph_ref)),
            "route_ref": copy.deepcopy(dict(self.route_ref)),
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }
        if include_digest:
            result["digest"] = self.digest
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryEvidence":
        return cls(
            evidence_id=str(value.get("evidence_id") or ""),
            task_id=str(value.get("task_id") or ""),
            run_id=str(value.get("run_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            lease_id=str(value.get("lease_id") or ""),
            worker_id=str(value.get("worker_id") or ""),
            disposition=_enum(RecoveryDisposition, value.get("disposition"), RecoveryDisposition.REPLAN),
            reason=str(value.get("reason") or ""),
            signal_kind=str(value.get("signal_kind") or "worker_failure"),
            successor_attempt_id=str(value.get("successor_attempt_id") or ""),
            successor_lease_id=str(value.get("successor_lease_id") or ""),
            graph_ref=dict(_mapping(value.get("graph_ref"))),
            route_ref=dict(_mapping(value.get("route_ref"))),
            created_at=str(value.get("created_at") or utc_iso()),
            metadata=dict(_mapping(value.get("metadata"))),
            digest=str(value.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class IntegrationCheckpoint:
    checkpoint_id: str
    run_id: str
    pool_revision: int
    graph_refs: tuple[Mapping[str, Any], ...]
    active_binding_ids: tuple[str, ...]
    active_lease_ids: tuple[str, ...]
    draining_worker_ids: tuple[str, ...]
    pending_control_ids: tuple[str, ...]
    pending_cancellation_ids: tuple[str, ...]
    foreign_checkpoint_refs: tuple[Mapping[str, Any], ...]
    journal_sequence: int
    created_at: str = field(default_factory=utc_iso)
    previous_checkpoint_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    digest: str = ""

    def __post_init__(self) -> None:
        require_token(self.checkpoint_id, "checkpoint id")
        require_token(self.run_id, "run id")
        if self.pool_revision < 0 or self.journal_sequence < 0:
            raise ValueError("checkpoint revisions cannot be negative")
        object.__setattr__(self, "graph_refs", tuple(copy.deepcopy(dict(item)) for item in self.graph_refs))
        object.__setattr__(self, "active_binding_ids", _tokens(self.active_binding_ids))
        object.__setattr__(self, "active_lease_ids", _tokens(self.active_lease_ids))
        object.__setattr__(self, "draining_worker_ids", _tokens(self.draining_worker_ids))
        object.__setattr__(self, "pending_control_ids", _tokens(self.pending_control_ids))
        object.__setattr__(self, "pending_cancellation_ids", _tokens(self.pending_cancellation_ids))
        object.__setattr__(self, "foreign_checkpoint_refs", tuple(copy.deepcopy(dict(item)) for item in self.foreign_checkpoint_refs))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        expected = stable_digest(self.to_dict(include_digest=False))
        if self.digest and self.digest != expected:
            raise ValueError("integration checkpoint digest mismatch")
        object.__setattr__(self, "digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "pool_revision": self.pool_revision,
            "graph_refs": [copy.deepcopy(dict(item)) for item in self.graph_refs],
            "active_binding_ids": list(self.active_binding_ids),
            "active_lease_ids": list(self.active_lease_ids),
            "draining_worker_ids": list(self.draining_worker_ids),
            "pending_control_ids": list(self.pending_control_ids),
            "pending_cancellation_ids": list(self.pending_cancellation_ids),
            "foreign_checkpoint_refs": [copy.deepcopy(dict(item)) for item in self.foreign_checkpoint_refs],
            "journal_sequence": self.journal_sequence,
            "created_at": self.created_at,
            "previous_checkpoint_id": self.previous_checkpoint_id,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }
        if include_digest:
            result["digest"] = self.digest
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IntegrationCheckpoint":
        return cls(
            checkpoint_id=str(value.get("checkpoint_id") or ""),
            run_id=str(value.get("run_id") or ""),
            pool_revision=int(value.get("pool_revision") or 0),
            graph_refs=tuple(dict(_mapping(item)) for item in value.get("graph_refs") or ()),
            active_binding_ids=tuple(str(item) for item in value.get("active_binding_ids") or ()),
            active_lease_ids=tuple(str(item) for item in value.get("active_lease_ids") or ()),
            draining_worker_ids=tuple(str(item) for item in value.get("draining_worker_ids") or ()),
            pending_control_ids=tuple(str(item) for item in value.get("pending_control_ids") or ()),
            pending_cancellation_ids=tuple(str(item) for item in value.get("pending_cancellation_ids") or ()),
            foreign_checkpoint_refs=tuple(
                dict(_mapping(item)) for item in value.get("foreign_checkpoint_refs") or ()
            ),
            journal_sequence=int(value.get("journal_sequence") or 0),
            created_at=str(value.get("created_at") or utc_iso()),
            previous_checkpoint_id=str(value.get("previous_checkpoint_id") or ""),
            metadata=dict(_mapping(value.get("metadata"))),
            digest=str(value.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class RestoreAudit:
    checkpoint_id: str
    run_id: str
    exact: bool
    active_bindings: tuple[PhysicalDispatchBinding, ...]
    pending_controls: tuple[ControlCommand, ...]
    draining_worker_ids: tuple[str, ...]
    graph_refs: tuple[Mapping[str, Any], ...]
    mismatches: tuple[str, ...]
    replay_digest: str
    restored_at: str = field(default_factory=utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "exact": self.exact,
            "active_bindings": [item.to_dict() for item in self.active_bindings],
            "pending_controls": [item.to_dict() for item in self.pending_controls],
            "draining_worker_ids": list(self.draining_worker_ids),
            "graph_refs": [dict(item) for item in self.graph_refs],
            "mismatches": list(self.mismatches),
            "replay_digest": self.replay_digest,
            "restored_at": self.restored_at,
        }


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    binding: PhysicalDispatchBinding
    candidates: tuple[CapacityObservation, ...]
    reused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "reused": self.reused,
        }


@dataclass(frozen=True, slots=True)
class ProgressReceipt:
    binding: PhysicalDispatchBinding
    renewal: LeaseRenewalRecord | None
    accepted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "renewal": self.renewal.to_dict() if self.renewal else None,
            "accepted": self.accepted,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class IntegrationOutcome:
    binding: PhysicalDispatchBinding
    outcome: ExecutionOutcome
    execution_receipt: Mapping[str, Any]
    typed_yield: TypedYieldReceipt | None
    recovery: RecoveryEvidence | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "outcome": self.outcome.value,
            "execution_receipt": dict(self.execution_receipt),
            "typed_yield": self.typed_yield.to_dict() if self.typed_yield else None,
            "recovery": self.recovery.to_dict() if self.recovery else None,
        }


def make_foreign_refs(
    *,
    task_id: str,
    task_revision: int,
    backend_route_id: str,
    workspace_ref: str,
    gateway_ref: str,
    graph_id: str,
    graph_revision: int,
    checkpoint_ref: str = "",
    memory_signal_ref: str = "",
) -> DispatchForeignRefs:
    return DispatchForeignRefs(
        logical_task=ForeignStateRef("typescript.AgentTaskRuntime", "logical_task", task_id, task_revision),
        backend_route=ForeignStateRef("M1-S05D.BackendRegistry", "backend_route", backend_route_id),
        workspace=ForeignStateRef("M1-S05A.WorkspaceManager", "workspace_binding", workspace_ref),
        gateway=ForeignStateRef("M1-S05B.SandboxGatewayRuntime", "gateway_route", gateway_ref),
        graph=ForeignStateRef("GraphStateCustody", "dynamic_graph", graph_id, graph_revision),
        checkpoint=(
            ForeignStateRef("logical-task-checkpoint", "checkpoint", checkpoint_ref)
            if checkpoint_ref
            else None
        ),
        memory_signal=(
            ForeignStateRef("M1-S06C.MemorySignalEmitter", "memory_signal", memory_signal_ref)
            if memory_signal_ref
            else None
        ),
    )


def new_binding_id(task_id: str, attempt_number: int) -> str:
    return new_pool_id(f"binding-{task_id}-{attempt_number}")


__all__ = [
    "AdmissionPhase",
    "AdmissionPolicy",
    "AdmissionResult",
    "CapacityObservation",
    "ControlCommand",
    "ControlKind",
    "ControlPhase",
    "DispatchAdmissionRequest",
    "DispatchForeignRefs",
    "DispatchMode",
    "ForeignStateRef",
    "IntegrationCheckpoint",
    "IntegrationOutcome",
    "LeaseRenewalRecord",
    "PhysicalDispatchBinding",
    "ProgressReceipt",
    "RecoveryDisposition",
    "RecoveryEvidence",
    "RenewalDisposition",
    "RestoreAudit",
    "RouteHealth",
    "TypedYieldReceipt",
    "YieldKind",
    "make_foreign_refs",
    "new_binding_id",
]
