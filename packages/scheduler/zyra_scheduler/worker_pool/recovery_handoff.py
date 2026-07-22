from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from .application import WorkerPoolFoundationRuntime
from .integration_models import (
    ControlKind,
    ControlPhase,
    RenewalDisposition,
)
from .integration_store import WorkerPoolIntegrationRepository
from .models import LeaseState, WorkerHealthStatus, stable_digest, utc_iso


class RecoverySignalKind(StrEnum):
    HEARTBEAT_STALE = "heartbeat_stale"
    WORKER_LOST = "worker_lost"
    LEASE_EXPIRED = "lease_expired"
    LEASE_FENCED = "lease_fenced"
    ROUTE_UNAVAILABLE = "route_unavailable"
    RENEWAL_REJECTED = "renewal_rejected"
    CANCEL_PENDING = "cancel_pending"
    DRAIN_ACTIVE = "drain_active"
    CHECKPOINT_DIVERGED = "checkpoint_diverged"


@dataclass(frozen=True, slots=True)
class WorkerLifecycleEvidence:
    evidence_id: str
    signal_kind: RecoverySignalKind
    task_id: str
    run_id: str
    attempt_id: str
    lease_id: str
    worker_id: str
    backend_id: str
    severity: str
    recoverable: bool
    recommended_disposition: str
    reason: str
    graph_ref: Mapping[str, Any]
    workspace_ref: Mapping[str, Any]
    gateway_ref: Mapping[str, Any]
    route_ref: Mapping[str, Any]
    observed_at: str = field(default_factory=utc_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph_ref", copy.deepcopy(dict(self.graph_ref)))
        object.__setattr__(self, "workspace_ref", copy.deepcopy(dict(self.workspace_ref)))
        object.__setattr__(self, "gateway_ref", copy.deepcopy(dict(self.gateway_ref)))
        object.__setattr__(self, "route_ref", copy.deepcopy(dict(self.route_ref)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        expected = stable_digest(self.to_dict(include_digest=False))
        if self.digest and self.digest != expected:
            raise ValueError("worker lifecycle evidence digest mismatch")
        object.__setattr__(self, "digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "evidence_id": self.evidence_id,
            "signal_kind": self.signal_kind.value,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "backend_id": self.backend_id,
            "severity": self.severity,
            "recoverable": self.recoverable,
            "recommended_disposition": self.recommended_disposition,
            "reason": self.reason,
            "graph_ref": copy.deepcopy(dict(self.graph_ref)),
            "workspace_ref": copy.deepcopy(dict(self.workspace_ref)),
            "gateway_ref": copy.deepcopy(dict(self.gateway_ref)),
            "route_ref": copy.deepcopy(dict(self.route_ref)),
            "observed_at": self.observed_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }
        if include_digest:
            result["digest"] = self.digest
        return result


@dataclass(frozen=True, slots=True)
class RecoveryHandoff:
    evidence: tuple[WorkerLifecycleEvidence, ...]
    active_binding_ids: tuple[str, ...]
    pending_control_ids: tuple[str, ...]
    draining_worker_ids: tuple[str, ...]
    canonical_pool_revision: int
    generated_at: str = field(default_factory=utc_iso)

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "schema": "zyra.worker-recovery-handoff/v1",
            "evidence": [item.to_dict() for item in self.evidence],
            "active_binding_ids": list(self.active_binding_ids),
            "pending_control_ids": list(self.pending_control_ids),
            "draining_worker_ids": list(self.draining_worker_ids),
            "canonical_pool_revision": self.canonical_pool_revision,
            "generated_at": self.generated_at,
            "producer": "M1-S07A-02.WorkerRecoveryHandoffRuntime",
            "consumer": "M1-S07C.RecoveryPlanner",
            "decision_owner_transferred": False,
        }
        if include_digest:
            result["digest"] = self.digest
        return result


class WorkerRecoveryHandoffRuntime:
    """Produces durable-state evidence for 07C without deciding its plan."""

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        repository: WorkerPoolIntegrationRepository,
    ) -> None:
        self.pool = pool
        self.repository = repository

    def build(self, *, run_id: str = "", task_id: str = "") -> RecoveryHandoff:
        bindings = self.repository.list_bindings(run_id=run_id, task_id=task_id)
        controls = self.repository.list_controls(
            task_id=task_id,
            phases=(ControlPhase.PENDING, ControlPhase.CLAIMED),
        )
        evidence: list[WorkerLifecycleEvidence] = []
        bindings_by_attempt = {item.attempt_id: item for item in bindings}
        for persisted in self.repository.list_recovery(task_id=task_id):
            if run_id and persisted.run_id != run_id:
                continue
            binding = bindings_by_attempt.get(persisted.attempt_id)
            if binding is None:
                continue
            kind = {
                "worker_lost": RecoverySignalKind.WORKER_LOST,
                "worker_unrecoverable": RecoverySignalKind.WORKER_LOST,
                "worker_heartbeat_stale": RecoverySignalKind.HEARTBEAT_STALE,
                "worker_heartbeat_degraded": RecoverySignalKind.HEARTBEAT_STALE,
                "route_unavailable": RecoverySignalKind.ROUTE_UNAVAILABLE,
                "lease_expired": RecoverySignalKind.LEASE_EXPIRED,
                "lease_fenced": RecoverySignalKind.LEASE_FENCED,
            }.get(persisted.signal_kind, RecoverySignalKind.RENEWAL_REJECTED)
            evidence.append(self._evidence(
                binding,
                kind,
                severity=(
                    "critical"
                    if kind in {
                        RecoverySignalKind.WORKER_LOST,
                        RecoverySignalKind.LEASE_EXPIRED,
                        RecoverySignalKind.LEASE_FENCED,
                    }
                    else "warning"
                ),
                recoverable=persisted.disposition.value not in {"cancel", "replan"},
                disposition=persisted.disposition.value,
                reason=persisted.reason,
                metadata={
                    "persisted_recovery": persisted.to_dict(),
                    "canonical_state_owner": "python.WorkerPoolIntegrationRepository",
                },
            ))
        for binding in bindings:
            lease = self.pool.store.get_lease(binding.lease_id)
            if lease is None:
                continue
            try:
                health = self.pool.heartbeats.assess(binding.worker_id)
            except Exception:
                health = None
            if health is not None and health.status is not WorkerHealthStatus.HEALTHY:
                kind = (
                    RecoverySignalKind.WORKER_LOST
                    if health.status in {WorkerHealthStatus.LOST, WorkerHealthStatus.UNRECOVERABLE}
                    else RecoverySignalKind.HEARTBEAT_STALE
                )
                evidence.append(self._evidence(
                    binding,
                    kind,
                    severity="critical" if kind is RecoverySignalKind.WORKER_LOST else "warning",
                    recoverable=health.recoverable,
                    disposition=health.disposition.value,
                    reason=health.reason,
                    metadata=health.to_dict(),
                ))
            if lease.state is LeaseState.EXPIRED:
                evidence.append(self._evidence(
                    binding,
                    RecoverySignalKind.LEASE_EXPIRED,
                    severity="critical",
                    recoverable=True,
                    disposition="reassign",
                    reason=str(lease.metadata.get("expiry_reason") or "physical lease expired"),
                    metadata={"lease_state": lease.state.value, "fence_epoch": lease.fence_epoch},
                ))
            elif lease.state is LeaseState.FENCED:
                evidence.append(self._evidence(
                    binding,
                    RecoverySignalKind.LEASE_FENCED,
                    severity="critical",
                    recoverable=True,
                    disposition="reassign",
                    reason=str(lease.metadata.get("fence_reason") or "physical lease fenced"),
                    metadata={"lease_state": lease.state.value, "fence_epoch": lease.fence_epoch},
                ))
            renewals = self.repository.renewals_for_lease(binding.lease_id)
            rejected = next(
                (
                    item
                    for item in reversed(renewals)
                    if item.disposition in {
                        RenewalDisposition.REJECTED_HEALTH,
                        RenewalDisposition.REJECTED_ROUTE,
                        RenewalDisposition.FENCED,
                        RenewalDisposition.EXPIRED,
                    }
                ),
                None,
            )
            if rejected is not None:
                kind = (
                    RecoverySignalKind.ROUTE_UNAVAILABLE
                    if rejected.disposition is RenewalDisposition.REJECTED_ROUTE
                    else RecoverySignalKind.RENEWAL_REJECTED
                )
                evidence.append(self._evidence(
                    binding,
                    kind,
                    severity="critical" if rejected.disposition in {RenewalDisposition.FENCED, RenewalDisposition.EXPIRED} else "warning",
                    recoverable=rejected.disposition is not RenewalDisposition.FENCED,
                    disposition="reassign" if rejected.disposition is not RenewalDisposition.REJECTED_HEALTH else "observe",
                    reason=rejected.reason,
                    metadata=rejected.to_dict(),
                ))
        for command in controls:
            binding = self.repository.get_binding(command.binding_id) if command.binding_id else self.repository.latest_binding(command.task_id)
            if binding is None:
                continue
            kind = (
                RecoverySignalKind.CANCEL_PENDING
                if command.kind is ControlKind.CANCEL
                else RecoverySignalKind.DRAIN_ACTIVE
            )
            evidence.append(self._evidence(
                binding,
                kind,
                severity="warning",
                recoverable=True,
                disposition="cancel" if kind is RecoverySignalKind.CANCEL_PENDING else "observe",
                reason=command.reason,
                metadata={"control": command.to_dict()},
            ))
        unique: dict[str, WorkerLifecycleEvidence] = {}
        for item in evidence:
            unique[item.evidence_id] = item
        ordered = tuple(sorted(unique.values(), key=lambda item: (item.task_id, item.attempt_id, item.signal_kind.value)))
        draining = tuple(
            worker.worker_id
            for worker in self.pool.store.list_workers(states=(WorkerLifecycleState.DRAINING,))
        )
        return RecoveryHandoff(
            evidence=ordered,
            active_binding_ids=tuple(item.binding_id for item in bindings if not item.terminal),
            pending_control_ids=tuple(item.command_id for item in controls),
            draining_worker_ids=draining,
            canonical_pool_revision=self.pool.store.revision,
        )

    def _evidence(
        self,
        binding: Any,
        signal_kind: RecoverySignalKind,
        *,
        severity: str,
        recoverable: bool,
        disposition: str,
        reason: str,
        metadata: Mapping[str, Any],
    ) -> WorkerLifecycleEvidence:
        identity = {
            "signal_kind": signal_kind.value,
            "task_id": binding.task_id,
            "attempt_id": binding.attempt_id,
            "lease_id": binding.lease_id,
            "reason": reason,
        }
        return WorkerLifecycleEvidence(
            evidence_id="worker-evidence:" + stable_digest(identity)[:32],
            signal_kind=signal_kind,
            task_id=binding.task_id,
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
            lease_id=binding.lease_id,
            worker_id=binding.worker_id,
            backend_id=binding.backend_id,
            severity=severity,
            recoverable=recoverable,
            recommended_disposition=disposition,
            reason=reason,
            graph_ref=binding.foreign_refs.graph.to_dict(),
            workspace_ref=binding.foreign_refs.workspace.to_dict(),
            gateway_ref=binding.foreign_refs.gateway.to_dict(),
            route_ref=binding.foreign_refs.backend_route.to_dict(),
            metadata={
                **dict(metadata),
                "binding_phase": binding.phase.value,
                "logical_task_owner": "typescript.AgentTaskRuntime",
                "recovery_decision_owner": "M1-S07C.RecoveryPlanner",
            },
        )


from .models import WorkerLifecycleState


__all__ = [
    "RecoveryHandoff",
    "RecoverySignalKind",
    "WorkerLifecycleEvidence",
    "WorkerRecoveryHandoffRuntime",
]
