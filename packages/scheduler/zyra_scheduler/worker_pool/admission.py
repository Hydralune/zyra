from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .application import WorkerPoolFoundationRuntime
from .errors import WorkerPoolError, WorkerPoolErrorCode
from .integration_models import (
    AdmissionPhase,
    AdmissionPolicy,
    AdmissionResult,
    CapacityObservation,
    DispatchAdmissionRequest,
    PhysicalDispatchBinding,
    RouteHealth,
    new_binding_id,
)
from .integration_store import WorkerPoolIntegrationRepository
from .models import (
    LeaseState,
    ResourceVector,
    TaskAttempt,
    WorkerCapabilityManifest,
    WorkerInstance,
    WorkerLease,
    WorkerLocation,
)


class LogicalTaskReadPort(Protocol):
    def exists(self, task_id: str, *, revision: int) -> bool: ...


class BackendHealthReadPort(Protocol):
    def health(self, backend_id: str) -> RouteHealth: ...


class ForeignRefReadPort(Protocol):
    def resolve(self, owner: str, kind: str, object_id: str, revision: int) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class AdmissionPorts:
    logical_tasks: LogicalTaskReadPort | None = None
    backend_health: BackendHealthReadPort | None = None
    foreign_refs: ForeignRefReadPort | None = None


class WorkerCapacityRuntime:
    """Computes admission pressure from canonical leases, never projections."""

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        repository: WorkerPoolIntegrationRepository,
        *,
        policy: AdmissionPolicy | None = None,
        backend_health: BackendHealthReadPort | None = None,
    ) -> None:
        self.pool = pool
        self.repository = repository
        self.policy = policy or AdmissionPolicy()
        self.backend_health = backend_health

    def observe(self, request: DispatchAdmissionRequest) -> tuple[CapacityObservation, ...]:
        base = self.pool.leases.rank_candidates(
            request.requirement,
            preferred_worker_ids=request.preferred_worker_ids,
            excluded_worker_ids=request.excluded_worker_ids,
        )
        active = self.pool.store.list_leases(states=(LeaseState.ACTIVE, LeaseState.DRAINING))
        session_count = sum(1 for lease in active if lease.owner_session_id == request.owner_session_id)
        run_count = sum(1 for lease in active if lease.run_id == request.run_id)
        output: list[CapacityObservation] = []
        for candidate in base:
            worker = self.pool.store.require_worker(candidate.worker_id)
            manifest = self.pool.store.latest_manifest(worker.worker_id)
            allocated = self.pool.leases.allocated_resources(worker.worker_id)
            capacity = manifest.resource_capacity if manifest else ResourceVector()
            available = (
                capacity.minus(allocated)
                if manifest is not None and capacity.fits(allocated)
                else ResourceVector()
            )
            worker_count = sum(1 for lease in active if lease.worker_id == worker.worker_id)
            # The selected worker's backend id identifies the physical
            # executor.  05D owns health for the scheduler route carried as a
            # foreign reference, so admission must not query the physical
            # manifest id as though it were a BackendRegistry route.
            route_health = self._health(request.foreign_refs.backend_route.object_id)
            reasons = list(candidate.reasons)
            accepted = candidate.accepted
            score = candidate.score
            if session_count >= self.policy.maximum_active_per_session:
                accepted = False
                reasons.append("owner session reached its active physical lease limit")
            if run_count >= self.policy.maximum_active_per_run:
                accepted = False
                reasons.append("run reached its active physical lease limit")
            if worker_count >= self.policy.maximum_active_per_worker:
                accepted = False
                reasons.append("worker reached its integration admission limit")
            if not route_health.dispatchable:
                accepted = False
                reasons.append(f"backend route is {route_health.value}")
            elif route_health is RouteHealth.DEGRADED:
                if not self.policy.allow_degraded_routes:
                    accepted = False
                    reasons.append("policy rejects degraded backend routes")
                else:
                    score -= 150.0
                    reasons.append("degraded backend route penalty applied")
            if request.edge_only and worker.location is not WorkerLocation.EDGE:
                accepted = False
                reasons.append("edge-only request forbids local or cloud fallback")
            if request.requirement.locations and worker.location not in request.requirement.locations:
                accepted = False
                reasons.append("worker location is outside the request location fence")
            output.append(
                CapacityObservation(
                    worker_id=worker.worker_id,
                    accepted=accepted,
                    score=round(score, 6),
                    manifest_digest=manifest.digest if manifest else "missing",
                    capacity=capacity,
                    allocated=allocated,
                    available=available,
                    active_worker_leases=worker_count,
                    active_session_leases=session_count,
                    active_run_leases=run_count,
                    route_health=route_health,
                    reasons=tuple(dict.fromkeys(reasons)),
                )
            )
        output.sort(key=lambda item: (-int(item.accepted), -item.score, item.worker_id))
        return tuple(output)

    def assert_available(self, request: DispatchAdmissionRequest) -> tuple[CapacityObservation, ...]:
        observations = self.observe(request)
        if not any(item.accepted for item in observations):
            raise WorkerPoolError(
                WorkerPoolErrorCode.RESOURCE_EXHAUSTED,
                "integration admission found no worker with capacity and a healthy route",
                operation="integration_capacity_admission",
                task_id=request.task_id,
                retryable=True,
                metadata={
                    "edge_only": request.edge_only,
                    "candidates": [item.to_dict() for item in observations],
                },
            )
        return observations

    def projection(self, *, run_id: str = "", owner_session_id: str = "") -> Mapping[str, Any]:
        leases = self.pool.store.list_leases(states=(LeaseState.ACTIVE, LeaseState.DRAINING))
        if run_id:
            leases = tuple(item for item in leases if item.run_id == run_id)
        if owner_session_id:
            leases = tuple(item for item in leases if item.owner_session_id == owner_session_id)
        by_worker: dict[str, list[Any]] = {}
        by_session: dict[str, int] = {}
        by_run: dict[str, int] = {}
        for lease in leases:
            by_worker.setdefault(lease.worker_id, []).append(lease)
            by_session[lease.owner_session_id] = by_session.get(lease.owner_session_id, 0) + 1
            by_run[lease.run_id] = by_run.get(lease.run_id, 0) + 1
        workers: list[Mapping[str, Any]] = []
        for worker in self.pool.store.list_workers():
            manifest = self.pool.store.latest_manifest(worker.worker_id)
            allocated = self.pool.leases.allocated_resources(worker.worker_id)
            capacity = manifest.resource_capacity if manifest else ResourceVector()
            available = capacity.minus(allocated) if manifest and capacity.fits(allocated) else ResourceVector()
            route_ids = tuple(sorted({
                binding.foreign_refs.backend_route.object_id
                for binding in self.repository.list_bindings(worker_id=worker.worker_id)
                if not binding.terminal and binding.foreign_refs.backend_route.object_id
            }))
            route_health = {
                route_id: self._health(route_id).value
                for route_id in route_ids
            }
            workers.append(
                {
                    "worker_id": worker.worker_id,
                    "state": worker.state.value,
                    "backend_id": worker.backend_id,
                    "location": worker.location.value,
                    "route_health": route_health,
                    "route_ids": list(route_ids),
                    "physical_backend_health_owned_here": False,
                    "capacity": capacity.to_dict(),
                    "allocated": allocated.to_dict(),
                    "available": available.to_dict(),
                    "active_lease_count": len(by_worker.get(worker.worker_id, ())),
                    "accepting_leases": worker.accepting_leases,
                }
            )
        return {
            "policy": self.policy.to_dict(),
            "workers": workers,
            "active_by_session": dict(sorted(by_session.items())),
            "active_by_run": dict(sorted(by_run.items())),
            "active_lease_count": len(leases),
            "canonical_capacity_owner": "WorkerPoolStore.worker_leases",
        }

    def _health(self, backend_id: str) -> RouteHealth:
        if self.backend_health is None:
            return RouteHealth.HEALTHY
        try:
            value = self.backend_health.health(backend_id)
            return value if isinstance(value, RouteHealth) else RouteHealth(str(value))
        except Exception:
            return RouteHealth.UNAVAILABLE


class WorkerAdmissionRuntime:
    """Binds a logical 03D task to one fenced physical attempt and route."""

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        repository: WorkerPoolIntegrationRepository,
        *,
        policy: AdmissionPolicy | None = None,
        ports: AdmissionPorts | None = None,
    ) -> None:
        self.pool = pool
        self.repository = repository
        self.policy = policy or AdmissionPolicy()
        self.ports = ports or AdmissionPorts()
        self.capacity = WorkerCapacityRuntime(
            pool,
            repository,
            policy=self.policy,
            backend_health=self.ports.backend_health,
        )

    def admit(self, request: DispatchAdmissionRequest) -> AdmissionResult:
        if os.getenv("ZYRA_WORKER_POOL_INTEGRATION_DISABLED") == "1":
            raise WorkerPoolError(
                WorkerPoolErrorCode.EXECUTION_REJECTED,
                "worker-pool integration admission is disabled",
                operation="integration_admit",
                task_id=request.task_id,
            )
        if os.getenv("ZYRA_WORKER_LEASE_STORE_DISABLED") == "1":
            raise WorkerPoolError(
                WorkerPoolErrorCode.EXECUTION_REJECTED,
                "canonical worker lease store is disabled",
                operation="integration_admit",
                task_id=request.task_id,
                metadata={"failure_owner": "WorkerPoolStore"},
            )
        self._validate_foreign_state(request)
        prior = next(
            (
                item
                for item in self.repository.list_bindings(task_id=request.task_id)
                if item.idempotency_key == request.idempotency_key
            ),
            None,
        )
        if prior is not None:
            if prior.request_digest != request.request_digest:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.IDEMPOTENCY_CONFLICT,
                    "admission idempotency key belongs to another request",
                    operation="integration_admit",
                    task_id=request.task_id,
                )
            return AdmissionResult(binding=prior, candidates=self.capacity.observe(request), reused=True)
        candidates = self.capacity.assert_available(request)
        accepted_ids = {item.worker_id for item in candidates if item.accepted}
        rejected_ids = {item.worker_id for item in candidates if not item.accepted}
        preferred = tuple(
            item.worker_id
            for item in candidates
            if item.accepted
        )
        excluded = tuple(sorted(set(request.excluded_worker_ids).union(rejected_ids)))
        if request.edge_only:
            locations = set(request.requirement.locations)
            if locations and locations != {WorkerLocation.EDGE}:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.CAPABILITY_MISMATCH,
                    "edge-only admission cannot request local or cloud locations",
                    operation="integration_admit",
                    task_id=request.task_id,
                )
            if not accepted_ids:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.RESOURCE_EXHAUSTED,
                    "edge-only admission has no edge worker and cannot fall back locally",
                    operation="integration_admit",
                    task_id=request.task_id,
                )
        committed_binding: PhysicalDispatchBinding | None = None

        def commit_binding(
            connection: Any,
            attempt: TaskAttempt,
            lease: WorkerLease,
            worker: WorkerInstance,
            manifest: WorkerCapabilityManifest,
        ) -> None:
            nonlocal committed_binding
            binding = PhysicalDispatchBinding(
                binding_id=new_binding_id(request.task_id, attempt.attempt_number),
                task_id=request.task_id,
                run_id=request.run_id,
                owner_session_id=request.owner_session_id,
                logical_attempt=request.logical_attempt,
                attempt_id=attempt.attempt_id,
                attempt_number=attempt.attempt_number,
                lease_id=lease.lease_id,
                worker_id=worker.worker_id,
                worker_generation=worker.generation,
                backend_id=lease.backend_id,
                manifest_digest=manifest.digest,
                fence_epoch=lease.fence_epoch,
                phase=AdmissionPhase.ADMITTED,
                execution_mode=request.execution_mode,
                foreign_refs=request.foreign_refs,
                request_digest=request.request_digest,
                idempotency_key=request.idempotency_key,
                edge_only=request.edge_only,
                metadata={
                    **dict(request.metadata),
                    "capacity_observations": [item.to_dict() for item in candidates],
                    "canonical_attempt_owner": "WorkerLeaseManager",
                    "canonical_lease_owner": "WorkerPoolStore",
                    "logical_task_read_only": True,
                    "atomic_admission": True,
                },
            )
            committed_binding = self.repository.insert_binding(
                binding,
                request=request.to_dict(),
                connection=connection,
            )

        acquisition = self.pool.acquire_task(
            task_id=request.task_id,
            run_id=request.run_id,
            owner_session_id=request.owner_session_id,
            requirement=request.requirement,
            attempt_number=request.attempt_number,
            preferred_worker_ids=preferred,
            excluded_worker_ids=excluded,
            ttl_seconds=min(
                request.lease_ttl_seconds or self.policy.default_lease_ttl_seconds,
                self.policy.maximum_lease_ttl_seconds,
            ),
            idempotency_key=f"integration-lease:{request.idempotency_key}",
            recovery_reason=request.recovery_reason,
            metadata={
                **dict(request.metadata),
                "integration_admission": True,
                "logical_attempt": request.logical_attempt,
                "execution_mode": request.execution_mode.value,
                "foreign_refs": request.foreign_refs.to_dict(),
                "edge_only": request.edge_only,
            },
            admission_guard=self._quota_guard(request),
            commit_hook=commit_binding,
        )
        if committed_binding is None:
            raise WorkerPoolError(
                WorkerPoolErrorCode.STORE_CORRUPTION,
                "canonical lease committed without its integration binding",
                operation="integration_atomic_admit",
                task_id=request.task_id,
                attempt_id=acquisition.attempt.attempt_id,
                lease_id=acquisition.lease.lease_id,
            )
        return AdmissionResult(binding=committed_binding, candidates=candidates)

    def _quota_guard(self, request: DispatchAdmissionRequest):
        """Recheck integration quotas inside the canonical lease transaction."""

        active_states = (LeaseState.ACTIVE.value, LeaseState.DRAINING.value)

        def guard(connection: Any, worker: Any) -> None:
            checks = (
                (
                    "owner session",
                    "owner_session_id",
                    request.owner_session_id,
                    self.policy.maximum_active_per_session,
                ),
                ("run", "run_id", request.run_id, self.policy.maximum_active_per_run),
                (
                    "worker",
                    "worker_id",
                    worker.worker_id,
                    self.policy.maximum_active_per_worker,
                ),
            )
            for label, column, value, maximum in checks:
                row = connection.execute(
                    f"SELECT COUNT(*) AS count FROM worker_leases "
                    f"WHERE {column}=? AND state IN (?,?)",  # noqa: S608
                    (value, *active_states),
                ).fetchone()
                count = int(row["count"] if row is not None else 0)
                if count >= maximum:
                    raise WorkerPoolError(
                        WorkerPoolErrorCode.RESOURCE_EXHAUSTED,
                        f"{label} reached its atomic integration lease limit",
                        operation="integration_capacity_commit_guard",
                        task_id=request.task_id,
                        worker_id=worker.worker_id,
                        retryable=True,
                        metadata={
                            "scope": label,
                            "current": count,
                            "maximum": maximum,
                            "canonical_state_owner": "WorkerPoolStore.worker_leases",
                        },
                    )

        return guard

    def _validate_foreign_state(self, request: DispatchAdmissionRequest) -> None:
        logical = request.foreign_refs.logical_task
        if logical.owner != "typescript.AgentTaskRuntime":
            raise WorkerPoolError(
                WorkerPoolErrorCode.LOGICAL_TASK_CONFLICT,
                "logical task reference is not owned by the 03D TypeScript runtime",
                operation="validate_integration_foreign_state",
                task_id=request.task_id,
            )
        if self.ports.logical_tasks is not None and not self.ports.logical_tasks.exists(
            request.task_id,
            revision=logical.revision,
        ):
            raise WorkerPoolError(
                WorkerPoolErrorCode.EXECUTION_REJECTED,
                "logical task store did not resolve the requested revision",
                operation="validate_integration_foreign_state",
                task_id=request.task_id,
            )
        if self.ports.foreign_refs is None:
            return
        for ref in (
            request.foreign_refs.backend_route,
            request.foreign_refs.workspace,
            request.foreign_refs.gateway,
            request.foreign_refs.graph,
            request.foreign_refs.checkpoint,
            request.foreign_refs.memory_signal,
        ):
            if ref is None or not ref.required:
                continue
            resolved = self.ports.foreign_refs.resolve(ref.owner, ref.kind, ref.object_id, ref.revision)
            if resolved is None:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.EXECUTION_REJECTED,
                    f"required foreign state is unavailable: {ref.owner}/{ref.kind}/{ref.object_id}",
                    operation="validate_integration_foreign_state",
                    task_id=request.task_id,
                )
            if ref.digest and str(resolved.get("digest") or "") != ref.digest:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.STORE_CONFLICT,
                    f"foreign state digest changed: {ref.kind}/{ref.object_id}",
                    operation="validate_integration_foreign_state",
                    task_id=request.task_id,
                )


__all__ = [
    "AdmissionPorts",
    "BackendHealthReadPort",
    "ForeignRefReadPort",
    "LogicalTaskReadPort",
    "WorkerAdmissionRuntime",
    "WorkerCapacityRuntime",
]
