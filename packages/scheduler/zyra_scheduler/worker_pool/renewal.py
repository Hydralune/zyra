from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from .application import WorkerPoolFoundationRuntime
from .errors import LeaseFenced
from .integration_models import (
    AdmissionPolicy,
    LeaseRenewalRecord,
    PhysicalDispatchBinding,
    RenewalDisposition,
    RouteHealth,
)
from .integration_store import WorkerPoolIntegrationRepository
from .models import LeaseState, WorkerHealthStatus, parse_utc, stable_digest, utc_now


class RenewalBackendHealthPort(Protocol):
    def health(self, backend_id: str) -> RouteHealth: ...


@dataclass(frozen=True, slots=True)
class RenewalAssessment:
    lease_id: str
    due: bool
    terminal: bool
    renew_at: str
    deadline_at: str
    remaining_seconds: float
    route_health: RouteHealth
    worker_health: WorkerHealthStatus
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "due": self.due,
            "terminal": self.terminal,
            "renew_at": self.renew_at,
            "deadline_at": self.deadline_at,
            "remaining_seconds": self.remaining_seconds,
            "route_health": self.route_health.value,
            "worker_health": self.worker_health.value,
            "reason": self.reason,
        }


class LeaseRenewalRuntime:
    """Progress-coupled lease renewal for long-running physical attempts."""

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        repository: WorkerPoolIntegrationRepository,
        *,
        policy: AdmissionPolicy | None = None,
        backend_health: RenewalBackendHealthPort | None = None,
    ) -> None:
        self.pool = pool
        self.repository = repository
        self.policy = policy or AdmissionPolicy()
        self.backend_health = backend_health

    def assess(
        self,
        binding: PhysicalDispatchBinding,
        *,
        now: datetime | None = None,
    ) -> RenewalAssessment:
        current = now or utc_now()
        lease = self.pool.store.require_lease(binding.lease_id)
        route_health = self._route_health(binding.foreign_refs.backend_route.object_id)
        try:
            worker_health = self.pool.heartbeats.assess(binding.worker_id, now=current).status
        except Exception:
            worker_health = WorkerHealthStatus.UNRECOVERABLE
        if lease.terminal:
            return RenewalAssessment(
                lease_id=lease.lease_id,
                due=False,
                terminal=True,
                renew_at=lease.deadline_at,
                deadline_at=lease.deadline_at,
                remaining_seconds=max(0.0, (parse_utc(lease.deadline_at) - current).total_seconds()),
                route_health=route_health,
                worker_health=worker_health,
                reason=f"lease is {lease.state.value}",
            )
        started = parse_utc(lease.renewed_at or lease.acquired_at)
        deadline = parse_utc(lease.deadline_at)
        duration = max(0.001, (deadline - started).total_seconds())
        lead = max(self.policy.minimum_renewal_lead_seconds, duration * (1.0 - self.policy.renewal_fraction))
        renew_at = deadline.timestamp() - lead
        due = current.timestamp() >= renew_at
        reason = "renewal window reached" if due else "renewal window has not been reached"
        if not route_health.dispatchable:
            reason = f"backend route is {route_health.value}"
        elif worker_health in {WorkerHealthStatus.LOST, WorkerHealthStatus.UNRECOVERABLE}:
            reason = f"worker health is {worker_health.value}"
        return RenewalAssessment(
            lease_id=lease.lease_id,
            due=due,
            terminal=False,
            renew_at=datetime.fromtimestamp(renew_at, tz=deadline.tzinfo).isoformat(),
            deadline_at=lease.deadline_at,
            remaining_seconds=max(0.0, (deadline - current).total_seconds()),
            route_health=route_health,
            worker_health=worker_health,
            reason=reason,
        )

    def renew_if_due(
        self,
        binding: PhysicalDispatchBinding,
        *,
        fence_token: str,
        progress_sequence: int,
        ttl_seconds: float | None = None,
        now: datetime | None = None,
        force: bool = False,
    ) -> LeaseRenewalRecord:
        lease = self.pool.store.require_lease(binding.lease_id)
        assessment = self.assess(binding, now=now)
        disposition = RenewalDisposition.NOT_DUE
        reason = assessment.reason
        next_deadline = lease.deadline_at
        heartbeat = self.pool.store.latest_heartbeat(binding.worker_id)
        heartbeat_sequence = heartbeat.sequence if heartbeat else 0
        last_renewed_progress = max(
            (
                item.progress_sequence
                for item in self.repository.renewals_for_lease(binding.lease_id)
                if item.disposition is RenewalDisposition.RENEWED
            ),
            default=0,
        )
        if lease.terminal:
            disposition = RenewalDisposition.TERMINAL
        elif lease.expired_at(now):
            try:
                expired, _ = self.pool.leases.expire(
                    lease.lease_id,
                    reason="lease expired before renewal could commit",
                )
                next_deadline = expired.deadline_at
            finally:
                disposition = RenewalDisposition.EXPIRED
                reason = "lease deadline elapsed"
        elif assessment.worker_health in {WorkerHealthStatus.LOST, WorkerHealthStatus.UNRECOVERABLE}:
            disposition = RenewalDisposition.REJECTED_HEALTH
            reason = f"worker health is {assessment.worker_health.value}"
        elif not assessment.route_health.dispatchable:
            disposition = RenewalDisposition.REJECTED_ROUTE
            reason = f"backend route is {assessment.route_health.value}"
        elif lease.state is LeaseState.DRAINING and binding.phase.value != "draining":
            disposition = RenewalDisposition.REJECTED_DRAIN
            reason = "lease is draining without a matching integration drain phase"
        elif (assessment.due or force) and progress_sequence <= last_renewed_progress:
            disposition = RenewalDisposition.NOT_DUE
            reason = "lease renewal requires progress beyond the last renewed sequence"
        elif assessment.due or force:
            try:
                renewed = self.pool.leases.renew(
                    lease.lease_id,
                    worker_id=binding.worker_id,
                    fence_token=fence_token,
                    fence_epoch=binding.fence_epoch,
                    ttl_seconds=min(
                        ttl_seconds or self.policy.default_lease_ttl_seconds,
                        self.policy.maximum_lease_ttl_seconds,
                    ),
                )
                disposition = RenewalDisposition.RENEWED
                next_deadline = renewed.deadline_at
                reason = "lease renewed after verified progress and worker heartbeat"
            except LeaseFenced:
                disposition = RenewalDisposition.FENCED
                reason = "lease fence rejected renewal"
        renewal_id = "renewal:" + stable_digest(
            {
                "lease_id": lease.lease_id,
                "lease_version": lease.version,
                "progress_sequence": progress_sequence,
                "disposition": disposition.value,
                "prior_deadline": lease.deadline_at,
                "next_deadline": next_deadline,
            }
        )[:32]
        record = LeaseRenewalRecord(
            renewal_id=renewal_id,
            lease_id=lease.lease_id,
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            worker_id=lease.worker_id,
            fence_epoch=binding.fence_epoch,
            disposition=disposition,
            prior_deadline_at=lease.deadline_at,
            next_deadline_at=next_deadline,
            heartbeat_sequence=heartbeat_sequence,
            route_health=assessment.route_health,
            reason=reason,
            progress_sequence=progress_sequence,
            metadata={
                "assessment": assessment.to_dict(),
                "canonical_lease_owner": "WorkerPoolStore",
                "renewal_owner": "LeaseRenewalRuntime",
                "last_renewed_progress_sequence": last_renewed_progress,
            },
        )
        return self.repository.append_renewal(record)

    def tick(
        self,
        fence_tokens: Mapping[str, str],
        *,
        now: datetime | None = None,
    ) -> tuple[LeaseRenewalRecord, ...]:
        records: list[LeaseRenewalRecord] = []
        for binding in self.repository.list_bindings():
            if binding.terminal:
                continue
            token = str(fence_tokens.get(binding.lease_id) or "")
            if not token:
                continue
            assessment = self.assess(binding, now=now)
            if assessment.due or assessment.terminal or not assessment.route_health.dispatchable:
                records.append(
                    self.renew_if_due(
                        binding,
                        fence_token=token,
                        progress_sequence=binding.progress_sequence,
                        now=now,
                    )
                )
        return tuple(records)

    def _route_health(self, backend_id: str) -> RouteHealth:
        if self.backend_health is None:
            return RouteHealth.HEALTHY
        try:
            value = self.backend_health.health(backend_id)
            return value if isinstance(value, RouteHealth) else RouteHealth(str(value))
        except Exception:
            return RouteHealth.UNAVAILABLE


__all__ = ["LeaseRenewalRuntime", "RenewalAssessment", "RenewalBackendHealthPort"]
