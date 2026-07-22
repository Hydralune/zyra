from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from ..backend_registry import (
    BackendHealthStatus,
    BackendRegistry,
    BackendRegistryStore,
    ensure_default_backends,
)
from ..backend_registry.models import now_timestamp
from .application import WorkerPoolFoundationRuntime
from .errors import StoreConflict
from .heartbeat import HeartbeatSweepResult
from .integration_models import (
    AdmissionPhase,
    PhysicalDispatchBinding,
    RecoveryDisposition,
    RecoveryEvidence,
    RouteHealth,
)
from .integration_store import WorkerPoolIntegrationRepository
from .models import (
    HealthAssessment,
    HealthDisposition,
    WorkerHealthStatus,
    stable_digest,
)


_ACTIVE_BINDING_PHASES = (
    AdmissionPhase.ADMITTED,
    AdmissionPhase.DISPATCHED,
    AdmissionPhase.PARKED,
    AdmissionPhase.DRAINING,
)


@dataclass(frozen=True, slots=True)
class WorkerRouteHealthSignal:
    """A transition-oriented health signal shared with 05D and 07C.

    The signal contains identities and observations only.  BackendRegistry and
    the recovery planner retain custody of their own state and decisions.
    """

    signal_id: str
    route_id: str
    status: RouteHealth
    disposition: str
    reason: str
    worker_ids: tuple[str, ...]
    active_lease_ids: tuple[str, ...]
    task_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    worker_generations: Mapping[str, int]
    source_statuses: Mapping[str, str]
    observed_at: str
    consumers: tuple[str, ...] = (
        "M1-S05D.BackendRegistry",
        "M1-S07C.RecoveryPlanner",
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)
    digest: str = ""

    def __post_init__(self) -> None:
        if not self.signal_id or not self.route_id:
            raise ValueError("worker route health signal requires signal and route ids")
        object.__setattr__(self, "worker_ids", tuple(sorted(set(self.worker_ids))))
        object.__setattr__(self, "active_lease_ids", tuple(sorted(set(self.active_lease_ids))))
        object.__setattr__(self, "task_ids", tuple(sorted(set(self.task_ids))))
        object.__setattr__(self, "run_ids", tuple(sorted(set(self.run_ids))))
        object.__setattr__(self, "worker_generations", dict(sorted(self.worker_generations.items())))
        object.__setattr__(self, "source_statuses", dict(sorted(self.source_statuses.items())))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        expected = stable_digest(self.to_dict(include_digest=False))
        if self.digest and self.digest != expected:
            raise ValueError("worker route health signal digest mismatch")
        object.__setattr__(self, "digest", expected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.worker-route-health/v1",
            "signal_id": self.signal_id,
            "route_id": self.route_id,
            "status": self.status.value,
            "disposition": self.disposition,
            "reason": self.reason,
            "worker_ids": list(self.worker_ids),
            "active_lease_ids": list(self.active_lease_ids),
            "task_ids": list(self.task_ids),
            "run_ids": list(self.run_ids),
            "worker_generations": dict(self.worker_generations),
            "source_statuses": dict(self.source_statuses),
            "observed_at": self.observed_at,
            "consumers": list(self.consumers),
            "metadata": copy.deepcopy(dict(self.metadata)),
        }
        if include_digest:
            value["digest"] = self.digest
        return value


@dataclass(frozen=True, slots=True)
class BackendHealthMutationReceipt:
    route_id: str
    accepted: bool
    changed: bool
    status: RouteHealth
    revision: int
    signal_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "accepted": self.accepted,
            "changed": self.changed,
            "status": self.status.value,
            "revision": self.revision,
            "signal_id": self.signal_id,
            "reason": self.reason,
        }


class WorkerRouteHealthSink(Protocol):
    def health(self, backend_id: str) -> RouteHealth: ...

    def record_worker_health(
        self,
        signal: WorkerRouteHealthSignal,
    ) -> BackendHealthMutationReceipt: ...


class BackendRegistryHealthAdapter:
    """Writes worker-derived route health through the canonical 05D store.

    This adapter does not mirror BackendRegistry state in the worker-pool DB.
    It records the source transition digest in 05D metadata so repeated sweeps
    are idempotent and so a healthy worker observation only clears a failure
    that this bridge previously owned.
    """

    OWNER = "M1-S05D.BackendRegistryStore"

    def __init__(self, store_path: str | Path) -> None:
        self.store_path = Path(store_path)
        store = BackendRegistryStore(self.store_path)
        try:
            ensure_default_backends(BackendRegistry(store))
        finally:
            store.close()

    def health(self, backend_id: str) -> RouteHealth:
        store = BackendRegistryStore(self.store_path)
        try:
            return _route_health(BackendRegistry(store).health(backend_id).status)
        except Exception:
            return RouteHealth.UNAVAILABLE
        finally:
            store.close()

    def record_worker_health(
        self,
        signal: WorkerRouteHealthSignal,
    ) -> BackendHealthMutationReceipt:
        store = BackendRegistryStore(self.store_path)
        try:
            return self._record_worker_health(store, signal)
        finally:
            store.close()

    def _record_worker_health(
        self,
        store: BackendRegistryStore,
        signal: WorkerRouteHealthSignal,
    ) -> BackendHealthMutationReceipt:
        definition = store.get_definition(signal.route_id)
        current = store.get_health(signal.route_id)
        if definition is None or current is None:
            return BackendHealthMutationReceipt(
                route_id=signal.route_id,
                accepted=False,
                changed=False,
                status=RouteHealth.UNAVAILABLE,
                revision=0,
                signal_id=signal.signal_id,
                reason="backend route is not registered in canonical 05D state",
            )
        if not definition.enabled or current.status is BackendHealthStatus.DISABLED:
            return BackendHealthMutationReceipt(
                route_id=signal.route_id,
                accepted=False,
                changed=False,
                status=RouteHealth.DISABLED,
                revision=current.revision,
                signal_id=signal.signal_id,
                reason="backend definition is disabled; worker health cannot override policy",
            )
        metadata = dict(current.metadata)
        if metadata.get("worker_pool_signal_digest") == signal.digest:
            return BackendHealthMutationReceipt(
                route_id=signal.route_id,
                accepted=True,
                changed=False,
                status=_route_health(current.status),
                revision=current.revision,
                signal_id=signal.signal_id,
                reason="worker health transition was already applied",
            )
        bridge_owned = bool(metadata.get("worker_pool_bridge_owned"))
        target = _backend_health(signal.status)
        if target is BackendHealthStatus.HEALTHY and not bridge_owned:
            return BackendHealthMutationReceipt(
                route_id=signal.route_id,
                accepted=True,
                changed=False,
                status=_route_health(current.status),
                revision=current.revision,
                signal_id=signal.signal_id,
                reason="healthy observation did not overwrite health owned by another 05D source",
            )
        if current.status is BackendHealthStatus.QUARANTINED and not bridge_owned:
            return BackendHealthMutationReceipt(
                route_id=signal.route_id,
                accepted=True,
                changed=False,
                status=RouteHealth.QUARANTINED,
                revision=current.revision,
                signal_id=signal.signal_id,
                reason="worker health did not release a quarantine owned by another 05D source",
            )
        checked_at = now_timestamp()
        failed = target in {BackendHealthStatus.DEGRADED, BackendHealthStatus.UNAVAILABLE}
        next_metadata = {
            **metadata,
            "worker_pool_bridge_owned": target is not BackendHealthStatus.HEALTHY,
            "worker_pool_signal_id": signal.signal_id,
            "worker_pool_signal_digest": signal.digest,
            "worker_pool_workers": list(signal.worker_ids),
            "worker_pool_consumers": list(signal.consumers),
            "worker_pool_source_statuses": dict(signal.source_statuses),
        }
        updated = replace(
            current,
            status=target,
            revision=current.revision + 1,
            consecutive_failures=(current.consecutive_failures + 1 if failed else 0),
            failure_count=(current.failure_count + 1 if failed else current.failure_count),
            success_count=(current.success_count + 1 if target is BackendHealthStatus.HEALTHY else current.success_count),
            reason=signal.reason[:1_000],
            last_checked_at=checked_at,
            last_success_at=(checked_at if target is BackendHealthStatus.HEALTHY else current.last_success_at),
            last_failure_at=(checked_at if failed else current.last_failure_at),
            quarantine_until=(None if bridge_owned else current.quarantine_until),
            metadata=next_metadata,
        )
        store.put_health(updated)
        return BackendHealthMutationReceipt(
            route_id=signal.route_id,
            accepted=True,
            changed=True,
            status=signal.status,
            revision=updated.revision,
            signal_id=signal.signal_id,
            reason="canonical 05D backend health updated from worker lifecycle aggregate",
        )

    def close(self) -> None:
        # Calls are short-lived and close their own canonical 05D connection.
        return None


@dataclass(frozen=True, slots=True)
class WorkerHealthBridgeReport:
    sweep: HeartbeatSweepResult
    signals: tuple[WorkerRouteHealthSignal, ...]
    backend_receipts: tuple[BackendHealthMutationReceipt, ...]
    recovery_evidence: tuple[RecoveryEvidence, ...]
    lost_binding_ids: tuple[str, ...]
    failures: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.worker-health-bridge-report/v1",
            "sweep": self.sweep.to_dict(),
            "signals": [item.to_dict() for item in self.signals],
            "backend_receipts": [item.to_dict() for item in self.backend_receipts],
            "recovery_evidence": [item.to_dict() for item in self.recovery_evidence],
            "lost_binding_ids": list(self.lost_binding_ids),
            "failures": [copy.deepcopy(dict(item)) for item in self.failures],
            "state_custody": {
                "worker_lifecycle": "python.WorkerPoolStore",
                "backend_health": "python.BackendRegistryStore",
                "recovery_evidence": "python.WorkerPoolIntegrationRepository",
                "second_health_store": False,
            },
        }


class WorkerHealthBridgeRuntime:
    """Turns worker heartbeat transitions into durable route/recovery effects."""

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        repository: WorkerPoolIntegrationRepository,
        *,
        backend_health: WorkerRouteHealthSink | None = None,
    ) -> None:
        self.pool = pool
        self.repository = repository
        self.backend_health = backend_health
        self._last_report: WorkerHealthBridgeReport | None = None

    @property
    def last_report(self) -> WorkerHealthBridgeReport | None:
        return self._last_report

    def sweep(self, *, now: datetime | None = None) -> WorkerHealthBridgeReport:
        all_bindings = self.repository.list_bindings()
        bindings = tuple(item for item in all_bindings if item.phase in _ACTIVE_BINDING_PHASES)
        # LOST is intentionally non-terminal: its durable route correlation is
        # required to clear a bridge-owned 05D failure after the worker is
        # explicitly restarted and emits a current heartbeat.
        route_bindings = tuple(item for item in all_bindings if not item.terminal)
        sweep = self.pool.heartbeats.sweep(now=now)
        assessments = {item.worker_id: item for item in sweep.assessments}
        failures: list[Mapping[str, Any]] = []
        lost_bindings: list[str] = []
        evidence: list[RecoveryEvidence] = []
        for binding in bindings:
            assessment = assessments.get(binding.worker_id)
            if assessment is None or assessment.status is WorkerHealthStatus.HEALTHY:
                continue
            if assessment.status in {WorkerHealthStatus.LOST, WorkerHealthStatus.UNRECOVERABLE}:
                if self._mark_binding_lost(binding, assessment):
                    lost_bindings.append(binding.binding_id)
            try:
                evidence.append(self._persist_recovery(binding, assessment))
            except Exception as error:
                failures.append({
                    "operation": "persist_recovery_evidence",
                    "binding_id": binding.binding_id,
                    "worker_id": binding.worker_id,
                    "error": f"{type(error).__name__}: {error}",
                })
        signals = self._aggregate_route_signals(route_bindings, assessments)
        receipts: list[BackendHealthMutationReceipt] = []
        if self.backend_health is not None:
            for signal in signals:
                try:
                    receipt = self.backend_health.record_worker_health(signal)
                    receipts.append(receipt)
                    if not receipt.accepted:
                        failures.append({
                            "operation": "record_backend_health",
                            "route_id": signal.route_id,
                            "signal_id": signal.signal_id,
                            "error": receipt.reason,
                        })
                except Exception as error:
                    failures.append({
                        "operation": "record_backend_health",
                        "route_id": signal.route_id,
                        "signal_id": signal.signal_id,
                        "error": f"{type(error).__name__}: {error}",
                    })
        report = WorkerHealthBridgeReport(
            sweep=sweep,
            signals=signals,
            backend_receipts=tuple(receipts),
            recovery_evidence=tuple(evidence),
            lost_binding_ids=tuple(sorted(set(lost_bindings))),
            failures=tuple(failures),
        )
        self._last_report = report
        return report

    def projection(self) -> Mapping[str, Any]:
        if self._last_report is None:
            return {
                "schema": "zyra.worker-health-bridge-report/v1",
                "sweep_performed": False,
                "backend_health_connected": self.backend_health is not None,
                "second_health_store": False,
            }
        return self._last_report.to_dict()

    def _mark_binding_lost(
        self,
        binding: PhysicalDispatchBinding,
        assessment: HealthAssessment,
    ) -> bool:
        current = self.repository.get_binding(binding.binding_id)
        if current is None or current.phase not in _ACTIVE_BINDING_PHASES:
            return False
        updated = current.advance(
            AdmissionPhase.LOST,
            metadata={
                **dict(current.metadata),
                "worker_health_status": assessment.status.value,
                "worker_health_disposition": assessment.disposition.value,
                "worker_health_reason": assessment.reason,
            },
        )
        try:
            self.repository.update_binding(
                updated,
                expected_version=current.version,
                operation="dispatch_worker_lost",
                payload={
                    "worker_health_status": assessment.status.value,
                    "worker_health_disposition": assessment.disposition.value,
                },
            )
            return True
        except StoreConflict:
            latest = self.repository.get_binding(binding.binding_id)
            return latest is not None and latest.phase is AdmissionPhase.LOST

    def _persist_recovery(
        self,
        binding: PhysicalDispatchBinding,
        assessment: HealthAssessment,
    ) -> RecoveryEvidence:
        signal_kind = {
            WorkerHealthStatus.DEGRADED: "worker_heartbeat_degraded",
            WorkerHealthStatus.STALE: "worker_heartbeat_stale",
            WorkerHealthStatus.LOST: "worker_lost",
            WorkerHealthStatus.UNRECOVERABLE: "worker_unrecoverable",
        }.get(assessment.status, "worker_health_transition")
        evidence_id = "recovery_" + stable_digest({
            "binding_id": binding.binding_id,
            "worker_generation": binding.worker_generation,
            "signal_kind": signal_kind,
        })[:32]
        prior = next(
            (
                item
                for item in self.repository.list_recovery(task_id=binding.task_id)
                if item.evidence_id == evidence_id
            ),
            None,
        )
        if prior is not None:
            return prior
        disposition = {
            HealthDisposition.NONE: RecoveryDisposition.OBSERVE,
            HealthDisposition.OBSERVE: RecoveryDisposition.OBSERVE,
            HealthDisposition.EXPIRE_LEASE: RecoveryDisposition.REASSIGN,
            HealthDisposition.REQUEUE_ATTEMPT: RecoveryDisposition.REASSIGN,
            HealthDisposition.REPLAN: RecoveryDisposition.REPLAN,
        }[assessment.disposition]
        route_id = binding.foreign_refs.backend_route.object_id
        return self.repository.append_recovery(
            RecoveryEvidence(
                evidence_id=evidence_id,
                task_id=binding.task_id,
                run_id=binding.run_id,
                attempt_id=binding.attempt_id,
                lease_id=binding.lease_id,
                worker_id=binding.worker_id,
                disposition=disposition,
                reason=assessment.reason,
                signal_kind=signal_kind,
                graph_ref=binding.foreign_refs.graph.to_dict(),
                route_ref=binding.foreign_refs.backend_route.to_dict(),
                metadata={
                    "binding_id": binding.binding_id,
                    "worker_generation": binding.worker_generation,
                    "worker_health_status": assessment.status.value,
                    "worker_health_disposition": assessment.disposition.value,
                    "stale_for_ms": assessment.stale_for_ms,
                    "route_id": route_id,
                    "consumers": ["M1-S07C.RecoveryPlanner"],
                    "canonical_state_owner": "python.WorkerPoolIntegrationRepository",
                },
            )
        )

    def _aggregate_route_signals(
        self,
        bindings: Sequence[PhysicalDispatchBinding],
        assessments: Mapping[str, HealthAssessment],
    ) -> tuple[WorkerRouteHealthSignal, ...]:
        grouped: dict[str, list[PhysicalDispatchBinding]] = {}
        for binding in bindings:
            route_id = binding.foreign_refs.backend_route.object_id
            if route_id:
                grouped.setdefault(route_id, []).append(binding)
        output: list[WorkerRouteHealthSignal] = []
        for route_id, route_bindings in sorted(grouped.items()):
            route_assessments = {
                binding.worker_id: assessments[binding.worker_id]
                for binding in route_bindings
                if binding.worker_id in assessments
            }
            if not route_assessments:
                continue
            status = _aggregate_health(tuple(route_assessments.values()))
            worker_ids = tuple(sorted(route_assessments))
            source_statuses = {
                worker_id: route_assessments[worker_id].status.value
                for worker_id in worker_ids
            }
            generations = {
                worker_id: max(
                    binding.worker_generation
                    for binding in route_bindings
                    if binding.worker_id == worker_id
                )
                for worker_id in worker_ids
            }
            transition_body = {
                "route_id": route_id,
                "status": status.value,
                "worker_generations": generations,
                "source_statuses": source_statuses,
                "active_lease_ids": sorted(
                    lease_id
                    for item in route_assessments.values()
                    for lease_id in item.active_lease_ids
                ),
            }
            signal_id = "worker_health_" + stable_digest(transition_body)[:32]
            worst = max(
                route_assessments.values(),
                key=lambda item: _health_weight(item.status),
            )
            output.append(
                WorkerRouteHealthSignal(
                    signal_id=signal_id,
                    route_id=route_id,
                    status=status,
                    disposition=worst.disposition.value,
                    reason=_aggregate_reason(status, route_assessments),
                    worker_ids=worker_ids,
                    active_lease_ids=tuple(transition_body["active_lease_ids"]),
                    task_ids=tuple(binding.task_id for binding in route_bindings),
                    run_ids=tuple(binding.run_id for binding in route_bindings),
                    worker_generations=generations,
                    source_statuses=source_statuses,
                    observed_at=max(binding.updated_at for binding in route_bindings),
                    metadata={
                        "binding_ids": sorted(binding.binding_id for binding in route_bindings),
                        "aggregation": "best-live-worker-wins; unavailable only when every bound worker is lost",
                        "state_owner": "python.WorkerHeartbeatRuntime",
                    },
                )
            )
        return tuple(output)


def _aggregate_health(assessments: Sequence[HealthAssessment]) -> RouteHealth:
    statuses = {item.status for item in assessments}
    if WorkerHealthStatus.HEALTHY in statuses:
        return RouteHealth.HEALTHY
    if WorkerHealthStatus.DEGRADED in statuses or WorkerHealthStatus.STALE in statuses:
        return RouteHealth.DEGRADED
    return RouteHealth.UNAVAILABLE


def _aggregate_reason(
    status: RouteHealth,
    assessments: Mapping[str, HealthAssessment],
) -> str:
    counts: dict[str, int] = {}
    for assessment in assessments.values():
        counts[assessment.status.value] = counts.get(assessment.status.value, 0) + 1
    summary = ", ".join(f"{name}={counts[name]}" for name in sorted(counts))
    return f"worker-pool route aggregate is {status.value}: {summary}"


def _health_weight(status: WorkerHealthStatus) -> int:
    return {
        WorkerHealthStatus.HEALTHY: 0,
        WorkerHealthStatus.DEGRADED: 1,
        WorkerHealthStatus.STALE: 2,
        WorkerHealthStatus.LOST: 3,
        WorkerHealthStatus.UNRECOVERABLE: 4,
    }[status]


def _route_health(status: BackendHealthStatus) -> RouteHealth:
    return {
        BackendHealthStatus.HEALTHY: RouteHealth.HEALTHY,
        BackendHealthStatus.DEGRADED: RouteHealth.DEGRADED,
        BackendHealthStatus.UNAVAILABLE: RouteHealth.UNAVAILABLE,
        BackendHealthStatus.QUARANTINED: RouteHealth.QUARANTINED,
        BackendHealthStatus.DISABLED: RouteHealth.DISABLED,
    }[status]


def _backend_health(status: RouteHealth) -> BackendHealthStatus:
    return {
        RouteHealth.HEALTHY: BackendHealthStatus.HEALTHY,
        RouteHealth.DEGRADED: BackendHealthStatus.DEGRADED,
        RouteHealth.UNAVAILABLE: BackendHealthStatus.UNAVAILABLE,
        RouteHealth.QUARANTINED: BackendHealthStatus.QUARANTINED,
        RouteHealth.DISABLED: BackendHealthStatus.DISABLED,
    }[status]


__all__ = [
    "BackendHealthMutationReceipt",
    "BackendRegistryHealthAdapter",
    "WorkerHealthBridgeReport",
    "WorkerHealthBridgeRuntime",
    "WorkerRouteHealthSignal",
    "WorkerRouteHealthSink",
]
