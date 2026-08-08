from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

from .application import WorkerPoolFoundationRuntime
from .errors import WorkerPoolError, WorkerPoolErrorCode
from .integration_models import (
    AdmissionPhase,
    ControlKind,
    ControlPhase,
    PhysicalDispatchBinding,
)
from .integration_store import WorkerPoolIntegrationRepository
from .models import (
    AttemptState,
    LeaseState,
    PoolJournalRecord,
    WorkerHealthStatus,
    WorkerLifecycleState,
    new_pool_id,
    stable_digest,
    utc_iso,
)


class WorkerExecutionGateRuntime:
    """Fail-closed authorization at the last boundary before worker code runs.

    Admission decides where work should run.  This gate answers a narrower and
    time-sensitive question: whether the already-admitted physical attempt is
    still executable *now*.  It reads every value from the canonical pool store;
    the signed projection is only an untrusted correlation envelope.
    """

    PROJECTION_SCHEMA = "zyra.worker-pool-dispatch/v1"
    PENDING_CONTROL_PHASES = (ControlPhase.PENDING, ControlPhase.CLAIMED)
    RUNNABLE_BINDING_PHASES = (AdmissionPhase.DISPATCHED, AdmissionPhase.DRAINING)
    RUNNABLE_LEASE_STATES = (LeaseState.ACTIVE, LeaseState.DRAINING)
    RUNNABLE_WORKER_STATES = (WorkerLifecycleState.BUSY, WorkerLifecycleState.DRAINING)

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        repository: WorkerPoolIntegrationRepository,
    ) -> None:
        self.pool = pool
        self.repository = repository

    def authorize_projection(
        self,
        projection: Mapping[str, Any],
        *,
        expected_task_id: str,
        expected_run_id: str,
        expected_session_id: str = "",
        operation: str = "execute_worker_task",
    ) -> Mapping[str, Any]:
        self._assert_enabled(operation, task_id=expected_task_id)
        supplied = dict(projection)
        self._assert_projection_signature(supplied, operation=operation)
        binding_id = self._required_text(supplied, "integration_binding_id", operation)
        binding = self.repository.get_binding(binding_id)
        if binding is None:
            self._reject(
                "physical dispatch projection references a missing binding",
                operation=operation,
                task_id=expected_task_id,
                metadata={"binding_id": binding_id},
            )
        self._assert_projection_identity(
            supplied,
            binding,
            expected_task_id=expected_task_id,
            expected_run_id=expected_run_id,
            expected_session_id=expected_session_id,
            operation=operation,
        )
        return self._authorize_binding(binding, operation=operation, projection=supplied)

    def authorize_task(
        self,
        task_id: str,
        *,
        expected_run_id: str,
        expected_session_id: str = "",
        operation: str = "resume_worker_task",
    ) -> Mapping[str, Any]:
        self._assert_enabled(operation, task_id=task_id)
        binding = self.repository.latest_binding(task_id)
        if binding is None:
            self._reject(
                "logical task has no physical dispatch binding",
                operation=operation,
                task_id=task_id,
            )
        if binding.run_id != expected_run_id:
            self._reject(
                "physical dispatch run authority differs from the logical task",
                operation=operation,
                task_id=task_id,
                binding=binding,
                metadata={"expected_run_id": expected_run_id, "actual_run_id": binding.run_id},
            )
        if expected_session_id and binding.owner_session_id != expected_session_id:
            self._reject(
                "physical dispatch session authority differs from the caller",
                operation=operation,
                task_id=task_id,
                binding=binding,
                metadata={
                    "expected_session_id": expected_session_id,
                    "actual_session_id": binding.owner_session_id,
                },
            )
        return self._authorize_binding(binding, operation=operation, projection=None)

    def authorize_many(
        self,
        projections: Sequence[Mapping[str, Any]],
        *,
        expected_run_id: str,
        expected_session_id: str,
        operation: str = "execute_worker_fanout",
    ) -> tuple[Mapping[str, Any], ...]:
        if not projections:
            self._reject(
                "worker fanout has no physical dispatch projections",
                operation=operation,
            )
        seen_tasks: set[str] = set()
        seen_bindings: set[str] = set()
        authorizations: list[Mapping[str, Any]] = []
        for projection in projections:
            task_id = str(projection.get("task_id") or "")
            binding_id = str(projection.get("integration_binding_id") or "")
            if not task_id or task_id in seen_tasks:
                self._reject(
                    "worker fanout task identities must be non-empty and unique",
                    operation=operation,
                    task_id=task_id,
                )
            if not binding_id or binding_id in seen_bindings:
                self._reject(
                    "worker fanout binding identities must be non-empty and unique",
                    operation=operation,
                    task_id=task_id,
                    metadata={"binding_id": binding_id},
                )
            seen_tasks.add(task_id)
            seen_bindings.add(binding_id)
            authorizations.append(
                self.authorize_projection(
                    projection,
                    expected_task_id=task_id,
                    expected_run_id=expected_run_id,
                    expected_session_id=expected_session_id,
                    operation=operation,
                )
            )
        return tuple(authorizations)

    def _authorize_binding(
        self,
        binding: PhysicalDispatchBinding,
        *,
        operation: str,
        projection: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        current = self.repository.get_binding(binding.binding_id)
        if current is None:
            self._reject(
                "physical dispatch binding disappeared during authorization",
                operation=operation,
                binding=binding,
            )
        if current.digest != binding.digest:
            self._reject(
                "physical dispatch binding changed during authorization",
                operation=operation,
                binding=current,
                retryable=True,
                metadata={"observed_digest": binding.digest, "current_digest": current.digest},
            )
        if current.phase not in self.RUNNABLE_BINDING_PHASES:
            self._reject(
                f"physical dispatch phase {current.phase.value} is not executable",
                operation=operation,
                binding=current,
                code=(
                    WorkerPoolErrorCode.CANCELLED
                    if current.phase is AdmissionPhase.CANCELLED
                    else WorkerPoolErrorCode.EXECUTION_REJECTED
                ),
            )
        lease = self.pool.store.get_lease(current.lease_id)
        attempt = self.pool.store.get_attempt(current.attempt_id)
        worker = self.pool.store.get_worker(current.worker_id)
        manifest = self.pool.store.latest_manifest(current.worker_id)
        if lease is None or attempt is None or worker is None or manifest is None:
            missing = [
                name
                for name, value in (
                    ("lease", lease),
                    ("attempt", attempt),
                    ("worker", worker),
                    ("manifest", manifest),
                )
                if value is None
            ]
            self._reject(
                "physical dispatch canonical state is incomplete",
                operation=operation,
                binding=current,
                metadata={"missing": missing},
            )
        if lease.state not in self.RUNNABLE_LEASE_STATES or lease.expired_at():
            self._reject(
                "physical worker lease is terminal or expired before execution",
                operation=operation,
                binding=current,
                code=(
                    WorkerPoolErrorCode.LEASE_EXPIRED
                    if lease.expired_at()
                    else WorkerPoolErrorCode.LEASE_FENCED
                ),
                metadata={"lease_state": lease.state.value, "deadline_at": lease.deadline_at},
            )
        if attempt.state is not AttemptState.RUNNING:
            self._reject(
                "physical attempt is not running at the execution boundary",
                operation=operation,
                binding=current,
                metadata={"attempt_state": attempt.state.value},
            )
        if worker.state not in self.RUNNABLE_WORKER_STATES:
            self._reject(
                f"worker state {worker.state.value} cannot execute an admitted task",
                operation=operation,
                binding=current,
                metadata={"worker_state": worker.state.value},
            )
        self._assert_canonical_identity(current, lease, attempt, worker, manifest, operation)
        self._assert_no_blocking_control(current, operation=operation)
        health = self.pool.heartbeats.assess(current.worker_id)
        if health.status in {WorkerHealthStatus.LOST, WorkerHealthStatus.UNRECOVERABLE}:
            self._reject(
                f"worker {current.worker_id} health {health.status.value} "
                "blocks execution",
                operation=operation,
                binding=current,
                retryable=health.recoverable,
                metadata={"health": health.to_dict()},
            )
        if projection is not None:
            projected_state = str(projection.get("lease_state") or "")
            allowed_projected_states = {lease.state.value}
            if lease.state is LeaseState.DRAINING:
                allowed_projected_states.add(LeaseState.ACTIVE.value)
            if projected_state not in allowed_projected_states:
                self._reject(
                    "physical dispatch projection lease state is inconsistent",
                    operation=operation,
                    binding=current,
                    metadata={
                        "projected_lease_state": projected_state,
                        "canonical_lease_state": lease.state.value,
                    },
                )
        authorization_digest = stable_digest(
            {
                "binding_id": current.binding_id,
                "binding_version": current.version,
                "binding_digest": current.digest,
                "lease_id": lease.lease_id,
                "lease_version": lease.version,
                "lease_state": lease.state.value,
                "fence_epoch": lease.fence_epoch,
                "worker_generation": worker.generation,
                "manifest_digest": manifest.digest,
                "operation": operation,
            }
        )
        authorization_id = new_pool_id("execution_authorization")
        self.pool.store.append_journal_record(
            PoolJournalRecord(
                journal_id=authorization_id,
                aggregate_type="worker_execution_authorization",
                aggregate_id=authorization_id,
                operation="worker_execution_authorized",
                run_id=current.run_id,
                task_id=current.task_id,
                causation_id=current.binding_id,
                correlation_id=current.owner_session_id,
                payload={
                    "binding_id": current.binding_id,
                    "attempt_id": current.attempt_id,
                    "lease_id": current.lease_id,
                    "worker_id": current.worker_id,
                    "worker_generation": current.worker_generation,
                    "binding_version": current.version,
                    "lease_version": lease.version,
                    "fence_epoch": lease.fence_epoch,
                    "manifest_digest": manifest.digest,
                    "operation": operation,
                    "authorization_digest": authorization_digest,
                    "canonical_recheck": True,
                },
            )
        )
        return {
            "authorization_id": authorization_id,
            "authorization_digest": authorization_digest,
            "authorized_at": utc_iso(),
            "operation": operation,
            "task_id": current.task_id,
            "run_id": current.run_id,
            "binding_id": current.binding_id,
            "binding_version": current.version,
            "attempt_id": current.attempt_id,
            "lease_id": current.lease_id,
            "lease_version": lease.version,
            "lease_state": lease.state.value,
            "worker_id": current.worker_id,
            "worker_generation": worker.generation,
            "worker_state": worker.state.value,
            "fence_epoch": lease.fence_epoch,
            "manifest_digest": manifest.digest,
            "health": health.status.value,
            "canonical_owner": "python.WorkerPoolStore",
            "logical_owner": "typescript.E03AgentControlCoordinator",
            "commit_still_requires_fence": True,
        }

    def _assert_projection_signature(self, projection: Mapping[str, Any], *, operation: str) -> None:
        if projection.get("schema") != self.PROJECTION_SCHEMA:
            self._reject("unsupported physical dispatch projection schema", operation=operation)
        if projection.get("required") is not True:
            self._reject("physical dispatch projection is not marked required", operation=operation)
        if projection.get("canonical_owner") != "python.WorkerPoolStore":
            self._reject("physical dispatch projection names another canonical owner", operation=operation)
        digest = str(projection.get("projection_digest") or "")
        unsigned = dict(projection)
        unsigned.pop("projection_digest", None)
        if not digest or digest != stable_digest(unsigned):
            self._reject("physical dispatch projection digest is invalid", operation=operation)

    def _assert_projection_identity(
        self,
        projection: Mapping[str, Any],
        binding: PhysicalDispatchBinding,
        *,
        expected_task_id: str,
        expected_run_id: str,
        expected_session_id: str,
        operation: str,
    ) -> None:
        expected = {
            "task_id": binding.task_id,
            "attempt_id": binding.attempt_id,
            "attempt": binding.attempt_number,
            "lease_id": binding.lease_id,
            "worker_id": binding.worker_id,
            "backend_id": binding.backend_id,
            "fence_epoch": binding.fence_epoch,
            "manifest_digest": binding.manifest_digest,
            "integration_binding_id": binding.binding_id,
        }
        mismatches = {
            key: {"projected": projection.get(key), "canonical": value}
            for key, value in expected.items()
            if projection.get(key) != value
        }
        if mismatches:
            self._reject(
                "physical dispatch projection differs from canonical binding",
                operation=operation,
                binding=binding,
                metadata={"mismatches": mismatches},
            )
        if binding.task_id != expected_task_id or binding.run_id != expected_run_id:
            self._reject(
                "physical dispatch authority differs from the requested logical task",
                operation=operation,
                binding=binding,
                metadata={
                    "expected_task_id": expected_task_id,
                    "expected_run_id": expected_run_id,
                    "binding_task_id": binding.task_id,
                    "binding_run_id": binding.run_id,
                },
            )
        if expected_session_id and binding.owner_session_id != expected_session_id:
            self._reject(
                "physical dispatch owner session differs from the caller",
                operation=operation,
                binding=binding,
                metadata={
                    "expected_session_id": expected_session_id,
                    "binding_session_id": binding.owner_session_id,
                },
            )

    def _assert_canonical_identity(
        self,
        binding: PhysicalDispatchBinding,
        lease: Any,
        attempt: Any,
        worker: Any,
        manifest: Any,
        operation: str,
    ) -> None:
        mismatches: dict[str, Any] = {}
        for label, values in {
            "task": (binding.task_id, lease.task_id, attempt.task_id),
            "run": (binding.run_id, lease.run_id, attempt.run_id),
            "attempt": (binding.attempt_id, lease.attempt_id, attempt.attempt_id),
            "lease": (binding.lease_id, attempt.lease_id, lease.lease_id),
            "worker": (binding.worker_id, lease.worker_id, attempt.worker_id, worker.worker_id),
            "backend": (binding.backend_id, lease.backend_id, worker.backend_id),
        }.items():
            if len(set(values)) != 1:
                mismatches[label] = list(values)
        if binding.worker_generation != worker.generation:
            mismatches["worker_generation"] = [binding.worker_generation, worker.generation]
        if binding.fence_epoch != lease.fence_epoch:
            mismatches["fence_epoch"] = [binding.fence_epoch, lease.fence_epoch]
        if binding.manifest_digest != worker.manifest_digest or manifest.digest != worker.manifest_digest:
            mismatches["manifest_digest"] = [
                binding.manifest_digest,
                worker.manifest_digest,
                manifest.digest,
            ]
        if mismatches:
            self._reject(
                "physical dispatch canonical identities are inconsistent",
                operation=operation,
                binding=binding,
                metadata={"mismatches": mismatches},
            )

    def _assert_no_blocking_control(self, binding: PhysicalDispatchBinding, *, operation: str) -> None:
        task_controls = self.repository.list_controls(
            task_id=binding.task_id,
            phases=self.PENDING_CONTROL_PHASES,
        )
        worker_controls = self.repository.list_controls(
            worker_id=binding.worker_id,
            phases=self.PENDING_CONTROL_PHASES,
        )
        blockers = [
            item
            for item in (*task_controls, *worker_controls)
            if item.kind in {ControlKind.CANCEL, ControlKind.STOP, ControlKind.PARK}
            and (not item.binding_id or item.binding_id == binding.binding_id)
            and (not item.lease_id or item.lease_id == binding.lease_id)
        ]
        if blockers:
            self._reject(
                "pending durable control blocks worker execution",
                operation=operation,
                binding=binding,
                retryable=True,
                metadata={
                    "control_ids": [item.command_id for item in blockers],
                    "control_kinds": [item.kind.value for item in blockers],
                },
            )

    @staticmethod
    def _required_text(projection: Mapping[str, Any], key: str, operation: str) -> str:
        value = str(projection.get(key) or "")
        if value:
            return value
        raise WorkerPoolError(
            WorkerPoolErrorCode.EXECUTION_REJECTED,
            f"physical dispatch projection is missing {key}",
            operation=operation,
        )

    @staticmethod
    def _assert_enabled(operation: str, *, task_id: str) -> None:
        if os.getenv("ZYRA_WORKER_EXECUTION_GATE_DISABLED") == "1":
            raise WorkerPoolError(
                WorkerPoolErrorCode.EXECUTION_REJECTED,
                "canonical worker execution gate is disabled",
                operation=operation,
                task_id=task_id,
            )

    @staticmethod
    def _reject(
        message: str,
        *,
        operation: str,
        binding: PhysicalDispatchBinding | None = None,
        task_id: str = "",
        retryable: bool = False,
        code: WorkerPoolErrorCode = WorkerPoolErrorCode.EXECUTION_REJECTED,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        raise WorkerPoolError(
            code,
            message,
            operation=operation,
            retryable=retryable,
            task_id=task_id or (binding.task_id if binding else ""),
            attempt_id=binding.attempt_id if binding else "",
            lease_id=binding.lease_id if binding else "",
            worker_id=binding.worker_id if binding else "",
            metadata=metadata,
        )


__all__ = ["WorkerExecutionGateRuntime"]
