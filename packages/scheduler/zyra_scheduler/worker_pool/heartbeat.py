from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from .errors import WorkerPoolError, WorkerPoolErrorCode
from .leases import WorkerLeaseManager
from .lifecycle import WorkerLifecycleRuntime
from .models import (
    HealthAssessment,
    HealthDisposition,
    LeaseState,
    WorkerHealthStatus,
    WorkerHeartbeat,
    WorkerLifecycleState,
    parse_utc,
    utc_iso,
    utc_now,
)
from .store import WorkerPoolStore


@dataclass(frozen=True, slots=True)
class HeartbeatPolicy:
    healthy_after_seconds: float = 10.0
    stale_after_seconds: float = 20.0
    lost_after_seconds: float = 40.0
    unrecoverable_after_seconds: float = 180.0
    expire_leases_when_lost: bool = True

    def __post_init__(self) -> None:
        values = (
            self.healthy_after_seconds,
            self.stale_after_seconds,
            self.lost_after_seconds,
            self.unrecoverable_after_seconds,
        )
        if any(value <= 0 for value in values):
            raise ValueError("heartbeat thresholds must be positive")
        if list(values) != sorted(values):
            raise ValueError("heartbeat thresholds must be monotonically increasing")


@dataclass(frozen=True, slots=True)
class HeartbeatSweepResult:
    assessments: tuple[HealthAssessment, ...]
    expired_lease_ids: tuple[str, ...]
    recovery_task_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessments": [item.to_dict() for item in self.assessments],
            "expired_lease_ids": list(self.expired_lease_ids),
            "recovery_task_ids": list(self.recovery_task_ids),
        }


class WorkerHeartbeatRuntime:
    def __init__(
        self,
        store: WorkerPoolStore,
        lifecycle: WorkerLifecycleRuntime,
        leases: WorkerLeaseManager,
        *,
        policy: HeartbeatPolicy | None = None,
        recovery_sink: Callable[[HealthAssessment], Any] | None = None,
    ) -> None:
        self.store = store
        self.lifecycle = lifecycle
        self.leases = leases
        self.policy = policy or HeartbeatPolicy()
        self.recovery_sink = recovery_sink

    def observe(self, heartbeat: WorkerHeartbeat) -> WorkerHeartbeat:
        worker = self.store.require_worker(heartbeat.worker_id)
        if heartbeat.worker_generation != worker.generation:
            raise WorkerPoolError(
                WorkerPoolErrorCode.LEASE_FENCED,
                "heartbeat belongs to a fenced worker generation",
                operation="observe_worker_heartbeat",
                worker_id=heartbeat.worker_id,
                metadata={
                    "stored_generation": worker.generation,
                    "heartbeat_generation": heartbeat.worker_generation,
                },
            )
        if heartbeat.manifest_digest != worker.manifest_digest:
            raise WorkerPoolError(
                WorkerPoolErrorCode.CAPABILITY_MISMATCH,
                "heartbeat manifest digest differs from registered worker manifest",
                operation="observe_worker_heartbeat",
                worker_id=heartbeat.worker_id,
            )
        if worker.process_identity and heartbeat.process_identity != worker.process_identity:
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTESTATION_REJECTED,
                "heartbeat process identity differs from capability attestation",
                operation="observe_worker_heartbeat",
                worker_id=heartbeat.worker_id,
            )
        stored_active = {
            lease.lease_id
            for lease in self.store.list_leases(
                worker_id=heartbeat.worker_id,
                states=(LeaseState.ACTIVE, LeaseState.DRAINING),
            )
        }
        reported_active = set(heartbeat.active_lease_ids)
        unexpected = reported_active - stored_active
        if unexpected:
            raise WorkerPoolError(
                WorkerPoolErrorCode.LEASE_FENCED,
                "heartbeat reports lease ids that are not active in canonical storage",
                operation="observe_worker_heartbeat",
                worker_id=heartbeat.worker_id,
                metadata={"unexpected_lease_ids": sorted(unexpected)},
            )
        updated_worker = worker.advance(
            worker.state,
            last_heartbeat_at=heartbeat.observed_at,
            failure_code="",
            failure_reason="",
        )
        return self.store.save_heartbeat(heartbeat, worker=updated_worker)

    def assess(self, worker_id: str, *, now: datetime | None = None) -> HealthAssessment:
        current = now or utc_now()
        worker = self.store.require_worker(worker_id)
        active_leases = self.store.list_leases(
            worker_id=worker_id,
            states=(LeaseState.ACTIVE, LeaseState.DRAINING),
        )
        if worker.state is WorkerLifecycleState.FAILED:
            return HealthAssessment(
                worker_id=worker_id,
                status=WorkerHealthStatus.UNRECOVERABLE,
                disposition=HealthDisposition.REPLAN,
                reason=worker.failure_reason or "worker entered failed state",
                active_lease_ids=tuple(lease.lease_id for lease in active_leases),
                recoverable=False,
            )
        observed_at = worker.last_heartbeat_at or worker.started_at or worker.registered_at
        stale_seconds = max(0.0, (current - parse_utc(observed_at)).total_seconds())
        status, disposition, recoverable = self._classify(stale_seconds, bool(active_leases))
        reason = {
            WorkerHealthStatus.HEALTHY: "worker heartbeat is current",
            WorkerHealthStatus.DEGRADED: "worker heartbeat is delayed",
            WorkerHealthStatus.STALE: "worker heartbeat exceeded the stale threshold",
            WorkerHealthStatus.LOST: "worker heartbeat exceeded the lost threshold",
            WorkerHealthStatus.UNRECOVERABLE: "worker remained unavailable beyond recovery threshold",
        }[status]
        telemetry = self.store.latest_telemetry(worker_id)
        metadata: dict[str, Any] = {}
        if telemetry is not None:
            metadata["telemetry_sequence"] = telemetry.sequence
            metadata["queue_depth"] = telemetry.queue_depth
            metadata["overcommitted"] = telemetry.overcommitted
            if telemetry.overcommitted and status in {WorkerHealthStatus.HEALTHY, WorkerHealthStatus.DEGRADED}:
                status = WorkerHealthStatus.DEGRADED
                disposition = HealthDisposition.OBSERVE
                reason = "worker telemetry reports resource overcommitment"
        return HealthAssessment(
            worker_id=worker_id,
            status=status,
            disposition=disposition,
            reason=reason,
            stale_for_ms=int(stale_seconds * 1000),
            active_lease_ids=tuple(lease.lease_id for lease in active_leases),
            recoverable=recoverable,
            metadata=metadata,
        )

    def sweep(self, *, now: datetime | None = None) -> HeartbeatSweepResult:
        current = now or utc_now()
        assessments: list[HealthAssessment] = []
        expired: list[str] = []
        recovery_tasks: list[str] = []
        for worker in self.store.list_workers():
            if worker.state is WorkerLifecycleState.STOPPED:
                continue
            assessment = self.assess(worker.worker_id, now=current)
            assessments.append(assessment)
            if assessment.status in {WorkerHealthStatus.LOST, WorkerHealthStatus.UNRECOVERABLE}:
                self.lifecycle.mark_lost(worker.worker_id, reason=assessment.reason)
                if self.policy.expire_leases_when_lost:
                    for lease_id in assessment.active_lease_ids:
                        lease, attempt = self.leases.expire(
                            lease_id,
                            reason=f"worker {worker.worker_id} lost: {assessment.reason}",
                        )
                        expired.append(lease.lease_id)
                        recovery_tasks.append(attempt.task_id)
            if self.recovery_sink is not None and assessment.disposition is not HealthDisposition.NONE:
                self.recovery_sink(assessment)
        return HeartbeatSweepResult(
            assessments=tuple(assessments),
            expired_lease_ids=tuple(sorted(set(expired))),
            recovery_task_ids=tuple(sorted(set(recovery_tasks))),
        )

    def _classify(
        self,
        stale_seconds: float,
        has_active_leases: bool,
    ) -> tuple[WorkerHealthStatus, HealthDisposition, bool]:
        if stale_seconds <= self.policy.healthy_after_seconds:
            return WorkerHealthStatus.HEALTHY, HealthDisposition.NONE, True
        if stale_seconds <= self.policy.stale_after_seconds:
            return WorkerHealthStatus.DEGRADED, HealthDisposition.OBSERVE, True
        if stale_seconds <= self.policy.lost_after_seconds:
            disposition = HealthDisposition.EXPIRE_LEASE if has_active_leases else HealthDisposition.OBSERVE
            return WorkerHealthStatus.STALE, disposition, True
        if stale_seconds <= self.policy.unrecoverable_after_seconds:
            disposition = HealthDisposition.REQUEUE_ATTEMPT if has_active_leases else HealthDisposition.REPLAN
            return WorkerHealthStatus.LOST, disposition, True
        return WorkerHealthStatus.UNRECOVERABLE, HealthDisposition.REPLAN, False
