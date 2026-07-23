from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .application import WorkerPoolFoundationRuntime
from .errors import WorkerPoolError, WorkerPoolErrorCode
from .integration_models import (
    AdmissionPhase,
    ControlCommand,
    ControlKind,
    ControlPhase,
    PhysicalDispatchBinding,
)
from .integration_store import WorkerPoolIntegrationRepository
from .models import (
    CancellationRequest,
    LeaseState,
    WakeupRecord,
    WakeupState,
    WorkerLifecycleState,
    stable_digest,
)


class ProjectionControlPort(Protocol):
    def park(self, task_id: str, *, reason: str) -> Mapping[str, Any]: ...
    def revive(self, task_id: str) -> Mapping[str, Any]: ...
    def cancel(self, task_id: str, *, reason: str) -> Mapping[str, Any]: ...


class ExecutionCancellationPort(Protocol):
    def cancel(self, binding: PhysicalDispatchBinding, *, reason: str) -> bool: ...


class WakeExecutionPort(Protocol):
    def is_session_active(self, worker_id: str) -> bool: ...
    def dispatch(self, wakeup: WakeupRecord) -> Mapping[str, Any] | bool: ...


@dataclass(frozen=True, slots=True)
class ControlDispatchReport:
    claimed: tuple[ControlCommand, ...]
    applied: tuple[ControlCommand, ...]
    failed: tuple[ControlCommand, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimed": [item.to_dict() for item in self.claimed],
            "applied": [item.to_dict() for item in self.applied],
            "failed": [item.to_dict() for item in self.failed],
        }


class WorkerControlRuntime:
    """Durable AgentScope-derived wake/cancel/drain dispatcher.

    Commands are persisted before effects, claimed with deadlines, and can be
    retried by another API process.  Physical mutations still go through the
    foundation lifecycle/lease/cancellation owners.
    """

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        repository: WorkerPoolIntegrationRepository,
        *,
        projection_control: ProjectionControlPort | None = None,
        execution_cancellation: ExecutionCancellationPort | None = None,
        wake_execution: WakeExecutionPort | None = None,
    ) -> None:
        self.pool = pool
        self.repository = repository
        self.projection_control = projection_control
        self.execution_cancellation = execution_cancellation
        self.wake_execution = wake_execution

    def submit(
        self,
        kind: ControlKind,
        *,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        task_id: str = "",
        run_id: str = "",
        worker_id: str = "",
        attempt_id: str = "",
        lease_id: str = "",
        binding_id: str = "",
    ) -> ControlCommand:
        if os.getenv("ZYRA_WORKER_CONTROL_DISABLED") == "1":
            raise WorkerPoolError(
                WorkerPoolErrorCode.EXECUTION_REJECTED,
                "durable worker control runtime is disabled",
                operation="submit_worker_control",
                task_id=task_id,
                worker_id=worker_id,
                lease_id=lease_id,
            )
        binding = self._resolve_binding(
            binding_id=binding_id,
            task_id=task_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
        )
        if binding is not None:
            task_id = task_id or binding.task_id
            run_id = run_id or binding.run_id
            worker_id = worker_id or binding.worker_id
            attempt_id = attempt_id or binding.attempt_id
            lease_id = lease_id or binding.lease_id
            binding_id = binding_id or binding.binding_id
        if kind in {ControlKind.DRAIN, ControlKind.WAKE, ControlKind.STOP} and not worker_id:
            raise ValueError(f"{kind.value} control requires a worker id")
        if kind in {ControlKind.PARK, ControlKind.REVIVE, ControlKind.CANCEL} and not task_id:
            raise ValueError(f"{kind.value} control requires a task id")
        command_id = "control:" + stable_digest(
            {
                "idempotency_key": idempotency_key,
                "kind": kind.value,
                "task_id": task_id,
                "worker_id": worker_id,
                "lease_id": lease_id,
            }
        )[:32]
        return self.repository.enqueue_control(
            ControlCommand(
                command_id=command_id,
                kind=kind,
                phase=ControlPhase.PENDING,
                actor_id=actor_id,
                reason=reason,
                idempotency_key=idempotency_key,
                task_id=task_id,
                run_id=run_id,
                worker_id=worker_id,
                attempt_id=attempt_id,
                lease_id=lease_id,
                binding_id=binding_id,
            )
        )

    def submit_cancel(
        self,
        *,
        task_id: str,
        run_id: str,
        reason: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ControlCommand:
        return self.submit(
            ControlKind.CANCEL,
            actor_id=actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            task_id=task_id,
            run_id=run_id,
        )

    def submit_drain(
        self,
        worker_id: str,
        *,
        reason: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ControlCommand:
        return self.submit(
            ControlKind.DRAIN,
            actor_id=actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
        )

    def submit_wake(
        self,
        worker_id: str,
        *,
        reason: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ControlCommand:
        return self.submit(
            ControlKind.WAKE,
            actor_id=actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
        )

    def submit_stop(
        self,
        worker_id: str,
        *,
        reason: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ControlCommand:
        return self.submit(
            ControlKind.STOP,
            actor_id=actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            worker_id=worker_id,
        )

    def submit_park(
        self,
        task_id: str,
        *,
        reason: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ControlCommand:
        return self.submit(
            ControlKind.PARK,
            actor_id=actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            task_id=task_id,
        )

    def submit_revive(
        self,
        task_id: str,
        *,
        reason: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ControlCommand:
        return self.submit(
            ControlKind.REVIVE,
            actor_id=actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            task_id=task_id,
        )

    def dispatch(
        self,
        *,
        claim_owner: str,
        limit: int = 32,
        claim_ttl_seconds: float = 30.0,
    ) -> ControlDispatchReport:
        claimed = self.repository.claim_controls(
            claim_owner,
            limit=limit,
            claim_ttl_seconds=claim_ttl_seconds,
        )
        applied: list[ControlCommand] = []
        failed: list[ControlCommand] = []
        for command in claimed:
            try:
                applied.append(self.apply(command, claim_owner=claim_owner))
            except Exception as error:
                current = self.repository.get_control(command.command_id) or command
                if current.terminal:
                    failed.append(current)
                    continue
                updated = current.advance(
                    ControlPhase.FAILED,
                    claim_owner=claim_owner,
                    claim_deadline_at="",
                    error=f"{type(error).__name__}: {error}",
                    effect={"changed": False, "failure_owner": self._failure_owner(command.kind)},
                )
                failed.append(
                    self.repository.update_control(
                        updated,
                        expected_version=current.version,
                        operation="control_failed",
                    )
                )
        return ControlDispatchReport(claimed=claimed, applied=tuple(applied), failed=tuple(failed))

    def apply(self, command: ControlCommand, *, claim_owner: str) -> ControlCommand:
        current = self.repository.get_control(command.command_id)
        if current is None:
            raise KeyError(command.command_id)
        if current.phase is ControlPhase.APPLIED:
            return current
        if current.phase is not ControlPhase.CLAIMED or current.claim_owner != claim_owner:
            raise WorkerPoolError(
                WorkerPoolErrorCode.OWNER_MISMATCH,
                "control command is not claimed by this dispatcher",
                operation="apply_worker_control",
                task_id=current.task_id,
                worker_id=current.worker_id,
                lease_id=current.lease_id,
            )
        effect = self._apply_effect(current)
        updated = current.advance(
            ControlPhase.APPLIED,
            claim_deadline_at="",
            error="",
            effect=effect,
        )
        return self.repository.update_control(
            updated,
            expected_version=current.version,
            operation="control_applied",
        )

    def submit_and_apply(
        self,
        kind: ControlKind,
        *,
        claim_owner: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        task_id: str = "",
        run_id: str = "",
        worker_id: str = "",
        attempt_id: str = "",
        lease_id: str = "",
        binding_id: str = "",
    ) -> ControlCommand:
        command = self.submit(
            kind,
            actor_id=actor_id,
            reason=reason,
            idempotency_key=idempotency_key,
            task_id=task_id,
            run_id=run_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            binding_id=binding_id,
        )
        if command.terminal:
            return command
        report = self.dispatch(claim_owner=claim_owner, limit=128)
        selected = next(
            (item for item in (*report.applied, *report.failed) if item.command_id == command.command_id),
            None,
        )
        return selected or self.repository.get_control(command.command_id) or command

    def recover_pending(self, *, claim_owner: str) -> ControlDispatchReport:
        return self.dispatch(claim_owner=claim_owner, limit=1000, claim_ttl_seconds=30.0)

    def _apply_effect(self, command: ControlCommand) -> Mapping[str, Any]:
        binding = self._resolve_binding(
            binding_id=command.binding_id,
            task_id=command.task_id,
            attempt_id=command.attempt_id,
            lease_id=command.lease_id,
        )
        if command.kind is ControlKind.CANCEL:
            effective_task_id = binding.task_id if binding is not None else command.task_id
            effective_run_id = (
                binding.run_id
                if binding is not None
                else command.run_id or "worker-control"
            )
            projection = None
            projection_error = ""
            execution_cancelled = False
            execution_cancel_error = ""
            # Fence the canonical lease before asking a remote execution target to
            # stop.  A slow or failed process/edge cancellation must never leave a
            # window where the old fence can still commit an artifact.
            receipt = self.pool.cancellation.cancel(
                CancellationRequest(
                    task_id=effective_task_id,
                    run_id=effective_run_id,
                    reason=command.reason,
                    actor_id=command.actor_id,
                    attempt_id=command.attempt_id,
                    lease_id=command.lease_id,
                    idempotency_key=command.idempotency_key,
                    request_id=command.command_id,
                )
            )
            if self.projection_control is not None:
                try:
                    projection = dict(
                        self.projection_control.cancel(
                            effective_task_id,
                            reason=command.reason,
                        )
                    )
                except Exception as error:
                    projection_error = f"{type(error).__name__}: {error}"
            if (
                binding is not None
                and binding.edge_only
                and self.execution_cancellation is not None
            ):
                try:
                    execution_cancelled = bool(
                        self.execution_cancellation.cancel(binding, reason=command.reason)
                    )
                except Exception as error:
                    execution_cancel_error = f"{type(error).__name__}: {error}"
            if binding is not None and not binding.terminal:
                self.repository.update_binding(
                    binding.advance(
                        AdmissionPhase.CANCELLED,
                        metadata={
                            **dict(binding.metadata),
                            "last_control_command_id": command.command_id,
                        },
                    ),
                    expected_version=binding.version,
                    operation="dispatch_cancelled",
                    payload={"control_command_id": command.command_id},
                )
            return {
                "changed": receipt.changed,
                "cancellation": receipt.to_dict(),
                "typescript_projection": projection,
                "typescript_projection_error": projection_error,
                "logical_task_owner": "typescript.AgentTaskRuntime",
                "physical_owner": "WorkerCancellationRuntime",
                "execution_cancelled": execution_cancelled,
                "execution_cancel_error": execution_cancel_error,
                "old_fence_blocks_late_commit": True,
            }
        if command.kind is ControlKind.DRAIN:
            worker = self.pool.lifecycle.begin_drain(command.worker_id, reason=command.reason)
            draining_leases: list[str] = []
            for lease in self.pool.store.list_leases(
                worker_id=command.worker_id,
                states=(LeaseState.ACTIVE,),
            ):
                self.pool.leases.begin_drain(lease.lease_id, reason=command.reason)
                draining_leases.append(lease.lease_id)
                related = self.repository.binding_for_lease(lease.lease_id)
                if related is not None and not related.terminal:
                    self.repository.update_binding(
                        related.advance(AdmissionPhase.DRAINING),
                        expected_version=related.version,
                        operation="dispatch_draining",
                        payload={"control_command_id": command.command_id},
                    )
            return {
                "changed": True,
                "worker": worker.to_dict(),
                "draining_lease_ids": draining_leases,
                "new_admission_blocked": not worker.accepting_leases,
            }
        if command.kind is ControlKind.WAKE:
            current_worker = self.pool.store.require_worker(command.worker_id)
            if current_worker.state is WorkerLifecycleState.DRAINING:
                active = self.pool.store.list_leases(
                    worker_id=command.worker_id,
                    states=(LeaseState.ACTIVE, LeaseState.DRAINING),
                )
                if active:
                    raise WorkerPoolError(
                        WorkerPoolErrorCode.INVALID_WORKER_TRANSITION,
                        "draining worker cannot wake before active leases settle",
                        operation="wake_worker_control",
                        worker_id=command.worker_id,
                        metadata={"active_lease_ids": [item.lease_id for item in active]},
                    )
                current_worker = self.pool.lifecycle.finish_drain(command.worker_id)
            recovered = self.pool.inbox.recover_wakeup_claims()
            queued = self.pool.store.list_wakeups(
                worker_id=command.worker_id,
                states=(WakeupState.QUEUED, WakeupState.REQUEUED),
            )
            if queued and (
                os.getenv("ZYRA_WORKER_WAKE_DISPATCH_DISABLED") == "1"
                or self.wake_execution is None
            ):
                raise WorkerPoolError(
                    WorkerPoolErrorCode.EXECUTION_REJECTED,
                    "durable wakeup has no enabled execution dispatch port",
                    operation="wake_worker_control",
                    worker_id=command.worker_id,
                    metadata={"queued_wakeup_ids": [item.wakeup_id for item in queued]},
                )
            if queued:
                wakeups = self.pool.inbox.dispatch_wakeups(
                    dispatcher_id=f"control:{command.command_id}",
                    worker_id=command.worker_id,
                    is_session_active=self.wake_execution.is_session_active,
                    on_wakeup=self.wake_execution.dispatch,
                    limit=100,
                )
                worker = self.pool.store.require_worker(command.worker_id)
            else:
                worker = self.pool.lifecycle.wake(command.worker_id, reason=command.reason)
                wakeups = ()
            return {
                "changed": True,
                "worker": worker.to_dict(),
                "dispatched_wakeup_ids": [item.wakeup_id for item in wakeups],
                "recovered_wakeup_ids": [item.wakeup_id for item in recovered],
                "wake_dispatch_port_used": bool(queued),
                "accepting_leases": worker.accepting_leases,
            }
        if command.kind is ControlKind.STOP:
            worker = self.pool.lifecycle.stop(command.worker_id, force=False, reason=command.reason)
            draining_leases: list[str] = []
            if worker.state is WorkerLifecycleState.DRAINING:
                for lease in self.pool.store.list_leases(
                    worker_id=command.worker_id,
                    states=(LeaseState.ACTIVE, LeaseState.DRAINING),
                ):
                    if lease.state is LeaseState.ACTIVE:
                        lease = self.pool.leases.begin_drain(lease.lease_id, reason=command.reason)
                    draining_leases.append(lease.lease_id)
                    related = self.repository.binding_for_lease(lease.lease_id)
                    if related is not None and related.phase is AdmissionPhase.DISPATCHED:
                        self.repository.update_binding(
                            related.advance(
                                AdmissionPhase.DRAINING,
                                metadata={
                                    **dict(related.metadata),
                                    "last_control_command_id": command.command_id,
                                    "stop_after_drain": True,
                                },
                            ),
                            expected_version=related.version,
                            operation="dispatch_stopping_after_drain",
                            payload={"control_command_id": command.command_id},
                        )
            return {
                "changed": True,
                "worker": worker.to_dict(),
                "draining_before_stop": bool(draining_leases),
                "draining_lease_ids": draining_leases,
                "stop_after_drain": bool(worker.metadata.get("stop_after_drain")),
                "new_admission_blocked": not worker.accepting_leases,
            }
        if command.kind is ControlKind.PARK:
            if binding is None:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.ATTEMPT_NOT_FOUND,
                    "park control has no active physical binding",
                    operation="park_physical_dispatch",
                    task_id=command.task_id,
                )
            if binding.phase is AdmissionPhase.PARKED:
                return {
                    "changed": False,
                    "binding": binding.to_dict(),
                    "typescript_projection": None,
                    "lease_retained": True,
                    "idempotent_replay": True,
                }
            if binding.phase not in {AdmissionPhase.DISPATCHED, AdmissionPhase.DRAINING}:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                    "park control requires a live dispatched physical binding",
                    operation="park_physical_dispatch",
                    task_id=command.task_id,
                    lease_id=binding.lease_id,
                    metadata={"binding_phase": binding.phase.value},
                )
            lease = self.pool.store.require_lease(binding.lease_id)
            if lease.terminal:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.LEASE_EXPIRED,
                    "dispatch lease became terminal before park",
                    operation="park_physical_dispatch",
                    task_id=command.task_id,
                    lease_id=lease.lease_id,
                )
            projection = None
            if self.projection_control is not None:
                projection = dict(self.projection_control.park(command.task_id, reason=command.reason))
            updated = self.repository.update_binding(
                binding.advance(
                    AdmissionPhase.PARKED,
                    metadata={
                        **dict(binding.metadata),
                        "last_control_command_id": command.command_id,
                    },
                ),
                expected_version=binding.version,
                operation="dispatch_parked",
                payload={"control_command_id": command.command_id},
            )
            return {
                "changed": True,
                "binding": updated.to_dict(),
                "typescript_projection": projection,
                "lease_retained": True,
            }
        if command.kind is ControlKind.REVIVE:
            if binding is not None and binding.phase in {
                AdmissionPhase.DISPATCHED,
                AdmissionPhase.DRAINING,
            } and str(binding.metadata.get("last_control_command_id") or "") == command.command_id:
                return {
                    "changed": False,
                    "binding": binding.to_dict(),
                    "typescript_projection": None,
                    "lease_retained": True,
                    "idempotent_replay": True,
                }
            if binding is None or binding.phase is not AdmissionPhase.PARKED:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                    "revive control requires a parked physical binding",
                    operation="revive_physical_dispatch",
                    task_id=command.task_id,
                )
            projection = None
            if self.projection_control is not None:
                projection = dict(self.projection_control.revive(command.task_id))
            lease = self.pool.store.require_lease(binding.lease_id)
            if lease.terminal:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.LEASE_EXPIRED,
                    "parked dispatch lease became terminal before revive",
                    operation="revive_physical_dispatch",
                    task_id=command.task_id,
                    lease_id=lease.lease_id,
                )
            target = AdmissionPhase.DRAINING if lease.state is LeaseState.DRAINING else AdmissionPhase.DISPATCHED
            updated = self.repository.update_binding(
                binding.advance(
                    target,
                    metadata={
                        **dict(binding.metadata),
                        "last_control_command_id": command.command_id,
                    },
                ),
                expected_version=binding.version,
                operation="dispatch_revived",
                payload={"control_command_id": command.command_id},
            )
            return {
                "changed": True,
                "binding": updated.to_dict(),
                "typescript_projection": projection,
                "lease_retained": True,
            }
        raise ValueError(f"unsupported control kind {command.kind.value}")

    def _resolve_binding(
        self,
        *,
        binding_id: str,
        task_id: str,
        attempt_id: str,
        lease_id: str,
    ) -> PhysicalDispatchBinding | None:
        if binding_id:
            return self.repository.get_binding(binding_id)
        if attempt_id:
            return self.repository.binding_for_attempt(attempt_id)
        if lease_id:
            return self.repository.binding_for_lease(lease_id)
        if task_id:
            return self.repository.latest_binding(task_id)
        return None

    @staticmethod
    def _failure_owner(kind: ControlKind) -> str:
        if kind is ControlKind.CANCEL:
            return "WorkerCancellationRuntime"
        if kind in {ControlKind.DRAIN, ControlKind.WAKE, ControlKind.STOP}:
            return "WorkerLifecycleRuntime"
        return "typescript.OmpWorkerDispatchRuntime"


__all__ = [
    "ControlDispatchReport",
    "ExecutionCancellationPort",
    "ProjectionControlPort",
    "WakeExecutionPort",
    "WorkerControlRuntime",
]
