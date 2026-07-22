from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from zyra_orchestration.graph_custody import GraphStateCustody

from .application import StartupRecoveryReport, WorkerPoolFoundationRuntime
from .errors import WorkerPoolError, WorkerPoolErrorCode
from .integration_models import (
    AdmissionPhase,
    ControlKind,
    ControlPhase,
    IntegrationCheckpoint,
    RestoreAudit,
)
from .integration_store import WorkerPoolIntegrationRepository
from .models import LeaseState, WorkerLifecycleState, stable_digest


@dataclass(frozen=True, slots=True)
class StartupIntegrationRecovery:
    foundation: StartupRecoveryReport
    restore: RestoreAudit | None
    recovered_control_ids: tuple[str, ...]
    process_registry_restored: bool
    process_registry_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "foundation": self.foundation.to_dict(),
            "restore": self.restore.to_dict() if self.restore else None,
            "recovered_control_ids": list(self.recovered_control_ids),
            "process_registry_restored": self.process_registry_restored,
            "process_registry_source": self.process_registry_source,
        }


class ExactWorkerCheckpointRuntime:
    """Cross-owner checkpoint correlation without copying foreign state.

    The checkpoint contains identities, revisions and digests.  Logical task,
    graph, workspace, gateway and backend stores remain independently writable;
    restore validates their current heads and never overwrites them.
    """

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        repository: WorkerPoolIntegrationRepository,
        graph_custody: GraphStateCustody,
    ) -> None:
        self.pool = pool
        self.repository = repository
        self.graph_custody = graph_custody

    def create(
        self,
        *,
        run_id: str,
        graph_ids: Sequence[str],
        foreign_checkpoint_refs: Sequence[Mapping[str, Any]] = (),
        previous_checkpoint_id: str = "",
        checkpoint_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> IntegrationCheckpoint:
        if os.getenv("ZYRA_DYNAMIC_GRAPH_COMMIT_DISABLED") == "1":
            raise WorkerPoolError(
                WorkerPoolErrorCode.DYNAMIC_GRAPH_COMMIT_DISABLED,
                "dynamic graph checkpoint module is disabled",
                operation="create_worker_integration_checkpoint",
            )
        bindings = self.repository.list_bindings(
            run_id=run_id,
            phases=(
                AdmissionPhase.ADMITTED,
                AdmissionPhase.DISPATCHED,
                AdmissionPhase.PARKED,
                AdmissionPhase.DRAINING,
                AdmissionPhase.LOST,
            ),
        )
        active_lease_ids = tuple(
            lease.lease_id
            for lease in self.pool.store.list_leases(
                states=(LeaseState.ACTIVE, LeaseState.DRAINING),
            )
            if lease.run_id == run_id
        )
        tracked_worker_ids = {
            item.worker_id for item in bindings
        }.union(
            lease.worker_id
            for lease in self.pool.store.list_leases(
                states=(LeaseState.ACTIVE, LeaseState.DRAINING),
            )
            if lease.run_id == run_id
        )
        tracked_task_ids = {item.task_id for item in bindings}
        draining_workers = tuple(
            worker.worker_id
            for worker in self.pool.store.list_workers(states=(WorkerLifecycleState.DRAINING,))
            if worker.worker_id in tracked_worker_ids
        )
        pending_controls = self.repository.list_controls(
            phases=(ControlPhase.PENDING, ControlPhase.CLAIMED),
        )
        pending_controls = tuple(
            item
            for item in pending_controls
            if (
                item.run_id == run_id
                or item.task_id in tracked_task_ids
                or item.worker_id in tracked_worker_ids
            )
        )
        graph_refs: list[Mapping[str, Any]] = []
        for graph_id in sorted(set(graph_ids)):
            snapshot = self.graph_custody.current(graph_id)
            graph_refs.append(
                {
                    "owner": "GraphStateCustody",
                    "graph_id": snapshot.graph_id,
                    "run_id": snapshot.run_id,
                    "revision": snapshot.revision,
                    "signature": snapshot.signature,
                    "parent_revision": snapshot.parent_revision,
                }
            )
        journal_sequence = self.pool.store.journal_head_sequence()
        payload = {
            "run_id": run_id,
            "pool_revision": self.pool.store.revision,
            "graph_refs": graph_refs,
            "active_binding_ids": [item.binding_id for item in bindings],
            "active_lease_ids": list(active_lease_ids),
            "draining_worker_ids": list(draining_workers),
            "pending_control_ids": [item.command_id for item in pending_controls],
            "pending_cancellation_ids": [
                item.command_id for item in pending_controls if item.kind is ControlKind.CANCEL
            ],
            "foreign_checkpoint_refs": [copy.deepcopy(dict(item)) for item in foreign_checkpoint_refs],
            "journal_sequence": journal_sequence,
            "previous_checkpoint_id": previous_checkpoint_id,
        }
        selected_id = checkpoint_id or "worker-checkpoint:" + stable_digest(payload)[:32]
        checkpoint = IntegrationCheckpoint(
            checkpoint_id=selected_id,
            run_id=run_id,
            pool_revision=self.pool.store.revision,
            graph_refs=tuple(graph_refs),
            active_binding_ids=tuple(item.binding_id for item in bindings),
            active_lease_ids=active_lease_ids,
            draining_worker_ids=draining_workers,
            pending_control_ids=tuple(item.command_id for item in pending_controls),
            pending_cancellation_ids=tuple(
                item.command_id for item in pending_controls if item.kind is ControlKind.CANCEL
            ),
            foreign_checkpoint_refs=tuple(copy.deepcopy(dict(item)) for item in foreign_checkpoint_refs),
            journal_sequence=journal_sequence,
            previous_checkpoint_id=previous_checkpoint_id,
            metadata={
                **dict(metadata or {}),
                "binding_digests": {
                    item.binding_id: item.digest for item in bindings
                },
                "control_digests": {
                    item.command_id: item.digest for item in pending_controls
                },
                "lease_fingerprints": {
                    lease.lease_id: stable_digest({
                        "attempt_id": lease.attempt_id,
                        "worker_id": lease.worker_id,
                        "state": lease.state.value,
                        "version": lease.version,
                        "fence_epoch": lease.fence_epoch,
                        "deadline_at": lease.deadline_at,
                    })
                    for lease in self.pool.store.list_leases(
                        states=(LeaseState.ACTIVE, LeaseState.DRAINING),
                    )
                    if lease.run_id == run_id
                },
                "logical_task_copies": 0,
                "graph_state_copies": 0,
                "workspace_state_copies": 0,
                "route_state_copies": 0,
                "process_local_registry_included": False,
                "canonical_pool_owner": "WorkerPoolStore",
            },
        )
        return self.repository.save_checkpoint(checkpoint)

    def restore(self, checkpoint_id: str, *, strict: bool = True) -> RestoreAudit:
        if os.getenv("ZYRA_DYNAMIC_GRAPH_COMMIT_DISABLED") == "1":
            raise WorkerPoolError(
                WorkerPoolErrorCode.DYNAMIC_GRAPH_COMMIT_DISABLED,
                "dynamic graph restore module is disabled",
                operation="restore_worker_integration_checkpoint",
            )
        checkpoint = self.repository.get_checkpoint(checkpoint_id)
        if checkpoint is None:
            raise KeyError(checkpoint_id)
        mismatches: list[str] = []
        bindings = tuple(
            item
            for binding_id in checkpoint.active_binding_ids
            if (item := self.repository.get_binding(binding_id)) is not None
        )
        found_binding_ids = {item.binding_id for item in bindings}
        for binding_id in checkpoint.active_binding_ids:
            if binding_id not in found_binding_ids:
                mismatches.append(f"active binding missing: {binding_id}")
        for binding in bindings:
            lease = self.pool.store.get_lease(binding.lease_id)
            attempt = self.pool.store.get_attempt(binding.attempt_id)
            worker = self.pool.store.get_worker(binding.worker_id)
            if lease is None:
                mismatches.append(f"binding {binding.binding_id} lease missing: {binding.lease_id}")
                continue
            if attempt is None:
                mismatches.append(f"binding {binding.binding_id} attempt missing: {binding.attempt_id}")
            if worker is None:
                mismatches.append(f"binding {binding.binding_id} worker missing: {binding.worker_id}")
            if lease.attempt_id != binding.attempt_id:
                mismatches.append(f"binding {binding.binding_id} attempt identity diverged")
            if lease.worker_id != binding.worker_id:
                mismatches.append(f"binding {binding.binding_id} worker identity diverged")
            if lease.fence_epoch != binding.fence_epoch:
                mismatches.append(f"binding {binding.binding_id} fence epoch diverged")
            expected_binding_digest = str(
                dict(checkpoint.metadata.get("binding_digests") or {}).get(binding.binding_id)
                or ""
            )
            if expected_binding_digest and binding.digest != expected_binding_digest:
                mismatches.append(f"binding payload diverged: {binding.binding_id}")
        current_active_lease_ids = {
            lease.lease_id
            for lease in self.pool.store.list_leases(states=(LeaseState.ACTIVE, LeaseState.DRAINING))
            if lease.run_id == checkpoint.run_id
        }
        expected_active_lease_ids = set(checkpoint.active_lease_ids)
        if current_active_lease_ids != expected_active_lease_ids:
            mismatches.append(
                "active lease set diverged: "
                f"expected={sorted(expected_active_lease_ids)} current={sorted(current_active_lease_ids)}"
            )
        expected_lease_fingerprints = dict(
            checkpoint.metadata.get("lease_fingerprints") or {}
        )
        for lease_id, expected_digest in expected_lease_fingerprints.items():
            lease = self.pool.store.get_lease(str(lease_id))
            if lease is None:
                continue
            actual_digest = stable_digest({
                "attempt_id": lease.attempt_id,
                "worker_id": lease.worker_id,
                "state": lease.state.value,
                "version": lease.version,
                "fence_epoch": lease.fence_epoch,
                "deadline_at": lease.deadline_at,
            })
            if str(expected_digest) != actual_digest:
                mismatches.append(f"active lease payload diverged: {lease_id}")
        tracked_worker_ids = {item.worker_id for item in bindings}
        tracked_task_ids = {item.task_id for item in bindings}
        current_draining = {
            worker.worker_id
            for worker in self.pool.store.list_workers(states=(WorkerLifecycleState.DRAINING,))
            if worker.worker_id in tracked_worker_ids
        }
        if current_draining != set(checkpoint.draining_worker_ids):
            mismatches.append(
                "draining worker set diverged: "
                f"expected={sorted(checkpoint.draining_worker_ids)} current={sorted(current_draining)}"
            )
        controls = tuple(
            item
            for command_id in checkpoint.pending_control_ids
            if (item := self.repository.get_control(command_id)) is not None
        )
        found_controls = {item.command_id for item in controls}
        for command_id in checkpoint.pending_control_ids:
            if command_id not in found_controls:
                mismatches.append(f"pending control missing: {command_id}")
        expected_control_digests = dict(
            checkpoint.metadata.get("control_digests") or {}
        )
        for control in controls:
            expected_digest = str(expected_control_digests.get(control.command_id) or "")
            if expected_digest and control.digest != expected_digest:
                mismatches.append(f"pending control payload diverged: {control.command_id}")
        current_pending = {
            item.command_id
            for item in self.repository.list_controls(
                phases=(ControlPhase.PENDING, ControlPhase.CLAIMED),
            )
            if (
                (item.run_id and item.run_id == checkpoint.run_id)
                or (item.task_id and item.task_id in tracked_task_ids)
                or (item.worker_id and item.worker_id in tracked_worker_ids)
            )
        }
        if current_pending != set(checkpoint.pending_control_ids):
            mismatches.append(
                "pending control set diverged: "
                f"expected={sorted(checkpoint.pending_control_ids)} current={sorted(current_pending)}"
            )
        graph_refs: list[Mapping[str, Any]] = []
        for expected in checkpoint.graph_refs:
            graph_id = str(expected.get("graph_id") or "")
            try:
                current = self.graph_custody.current(graph_id)
            except KeyError:
                mismatches.append(f"dynamic graph missing: {graph_id}")
                continue
            actual = {
                "owner": "GraphStateCustody",
                "graph_id": current.graph_id,
                "run_id": current.run_id,
                "revision": current.revision,
                "signature": current.signature,
                "parent_revision": current.parent_revision,
            }
            graph_refs.append(actual)
            if int(expected.get("revision") or 0) != current.revision:
                mismatches.append(f"dynamic graph revision diverged: {graph_id}")
            if str(expected.get("signature") or "") != current.signature:
                mismatches.append(f"dynamic graph signature diverged: {graph_id}")
        replay_digest = self.replay_digest(
            bindings=bindings,
            controls=controls,
            draining_worker_ids=tuple(sorted(current_draining)),
            graph_refs=tuple(graph_refs),
        )
        audit = RestoreAudit(
            checkpoint_id=checkpoint.checkpoint_id,
            run_id=checkpoint.run_id,
            exact=not mismatches,
            active_bindings=bindings,
            pending_controls=controls,
            draining_worker_ids=tuple(sorted(current_draining)),
            graph_refs=tuple(graph_refs),
            mismatches=tuple(mismatches),
            replay_digest=replay_digest,
        )
        if strict and mismatches:
            raise WorkerPoolError(
                WorkerPoolErrorCode.STORE_CONFLICT,
                "integration checkpoint cannot be restored exactly",
                operation="restore_worker_integration_checkpoint",
                metadata=audit.to_dict(),
            )
        return audit

    def replay_digest(
        self,
        *,
        bindings: Iterable[Any],
        controls: Iterable[Any],
        draining_worker_ids: Iterable[str],
        graph_refs: Iterable[Mapping[str, Any]],
    ) -> str:
        """Order-independent digest used by randomized completion tests."""

        payload = {
            "bindings": sorted(
                (copy.deepcopy(item.to_dict()) for item in bindings),
                key=lambda item: str(item.get("binding_id") or ""),
            ),
            "controls": sorted(
                (copy.deepcopy(item.to_dict()) for item in controls),
                key=lambda item: str(item.get("command_id") or ""),
            ),
            "draining_worker_ids": sorted(set(str(item) for item in draining_worker_ids)),
            "graph_refs": sorted(
                (copy.deepcopy(dict(item)) for item in graph_refs),
                key=lambda item: str(item.get("graph_id") or ""),
            ),
        }
        return stable_digest(payload)

    def startup_recover(self, *, run_id: str = "") -> StartupIntegrationRecovery:
        foundation = self.pool.recover_startup()
        checkpoint = self.repository.latest_checkpoint(run_id) if run_id else None
        restore = self.restore(checkpoint.checkpoint_id, strict=False) if checkpoint else None
        pending = self.repository.list_controls(phases=(ControlPhase.PENDING, ControlPhase.CLAIMED))
        return StartupIntegrationRecovery(
            foundation=foundation,
            restore=restore,
            recovered_control_ids=tuple(item.command_id for item in pending),
            process_registry_restored=False,
            process_registry_source="canonical lease/binding/control projections must rehydrate OMP process state",
        )


__all__ = ["ExactWorkerCheckpointRuntime", "StartupIntegrationRecovery"]
