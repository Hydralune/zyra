from __future__ import annotations

import hashlib
import os
import secrets
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from .dirty_state import DirtyOwnershipStore, WorkspaceDirtyStateRuntime
from .errors import WorkspaceError, WorkspaceErrorCode
from .integration_store import WorkspaceIntegrationStore
from .local_backend import LocalWorkspaceBackend, WorkspaceAccessHandle
from .models import (
    LocalWorkspaceLocation,
    RecoveryState,
    WorkspaceBackendKind,
    WorkspaceBinding,
    WorkspaceKind,
    WorkspaceLease,
    WorkspaceLeaseState,
    WorkspaceLifecycleState,
    WorkspaceOperation,
    WorkspaceOperationReceipt,
    WorkspaceQuota,
    WorkspaceRecoveryRecord,
    WorkspaceSnapshot,
    new_workspace_id,
    utc_now,
)
from .mounts import WorkspaceArtifactMount
from .quota import QuotaReservationStore, WorkspaceQuotaRuntime
from .snapshots import SnapshotRestoreResult, WorkspaceSnapshotRuntime
from .store import WorkspaceBindingStore


WorkspaceEventSink = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True, slots=True)
class WorkspaceManagerConfig:
    state_root: Path
    data_root: Path
    local_enabled: bool = True
    default_backend_id: str = "local-default"
    lease_ttl_seconds: int = 30 * 60
    reservation_ttl_seconds: int = 5 * 60
    max_receipts: int = 4096
    default_quota: WorkspaceQuota = field(default_factory=WorkspaceQuota)

    @classmethod
    def from_environment(
        cls,
        *,
        base_root: str | Path,
        environment: Mapping[str, str] | None = None,
    ) -> "WorkspaceManagerConfig":
        values = environment or os.environ
        base = Path(base_root).resolve()
        state_root = Path(values.get("ZYRA_WORKSPACE_STATE_ROOT") or (base / "workspace-state")).resolve()
        data_root = Path(values.get("ZYRA_WORKSPACE_DATA_ROOT") or (base / "workspaces")).resolve()
        enabled_value = str(values.get("ZYRA_LOCAL_WORKSPACE_ENABLED", "1")).strip().casefold()
        enabled = enabled_value not in {"0", "false", "no", "off", "disabled"}
        integration_disabled = str(
            values.get("ZYRA_WORKSPACE_INTEGRATION_DISABLED", "")
        ).strip().casefold() in {"1", "true", "yes", "on"}
        enabled = enabled and not integration_disabled
        return cls(
            state_root=state_root,
            data_root=data_root,
            local_enabled=enabled,
            default_backend_id=str(values.get("ZYRA_WORKSPACE_BACKEND_ID") or "local-default"),
            lease_ttl_seconds=max(30, int(values.get("ZYRA_WORKSPACE_LEASE_TTL_SECONDS") or 1800)),
            reservation_ttl_seconds=max(5, int(values.get("ZYRA_WORKSPACE_RESERVATION_TTL_SECONDS") or 300)),
            max_receipts=max(128, int(values.get("ZYRA_WORKSPACE_MAX_RECEIPTS") or 4096)),
        )


@dataclass(frozen=True, slots=True)
class WorkspacePublicProjection:
    workspace_id: str
    run_id: str
    task_id: str
    session_id: str
    backend_id: str
    backend_kind: WorkspaceBackendKind
    workspace_kind: WorkspaceKind
    lifecycle_state: WorkspaceLifecycleState
    owner_epoch: int
    binding_revision: int
    capability_revision: int
    lease_id: str
    active_snapshot_id: str
    quota: Mapping[str, Any]
    usage: Mapping[str, Any]
    mounts: tuple[Mapping[str, Any], ...]
    updated_at: str
    physical_location_redacted: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "backend_id": self.backend_id,
            "backend_kind": self.backend_kind.value,
            "workspace_kind": self.workspace_kind.value,
            "lifecycle_state": self.lifecycle_state.value,
            "owner_epoch": self.owner_epoch,
            "binding_revision": self.binding_revision,
            "capability_revision": self.capability_revision,
            "lease_id": self.lease_id,
            "active_snapshot_id": self.active_snapshot_id,
            "quota": dict(self.quota),
            "usage": dict(self.usage),
            "mounts": [dict(item) for item in self.mounts],
            "updated_at": self.updated_at,
            "physical_location_redacted": self.physical_location_redacted,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceTaskBindingResult:
    projection: WorkspacePublicProjection
    access: WorkspaceAccessHandle
    created: bool
    ready_barrier_passed: bool
    receipt: WorkspaceOperationReceipt

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.projection.to_dict(),
            "access": self.access.to_public_dict(),
            "created": self.created,
            "ready_barrier_passed": self.ready_barrier_passed,
            "receipt": self.receipt.to_dict(),
        }


class WorkspaceManagerRuntime:
    """Canonical task-to-workspace lifecycle, fencing, snapshot, restore, and cleanup owner."""

    def __init__(
        self,
        config: WorkspaceManagerConfig,
        *,
        event_sink: WorkspaceEventSink | None = None,
    ) -> None:
        self.config = config
        self.event_sink = event_sink
        self.store = WorkspaceBindingStore(
            config.state_root / "bindings",
            disabled=not config.local_enabled,
            max_receipts=config.max_receipts,
        )
        self.integration_store = WorkspaceIntegrationStore(
            config.state_root / "integration",
            disabled=not config.local_enabled,
            max_receipts_per_workspace=config.max_receipts,
        )
        self.reservation_store = QuotaReservationStore(
            config.state_root / "quota-reservations.json",
            disabled=not config.local_enabled,
        )
        self.quota_runtime = WorkspaceQuotaRuntime(
            self.store,
            self.reservation_store,
            reservation_ttl_seconds=config.reservation_ttl_seconds,
        )
        self.backend = LocalWorkspaceBackend(
            backend_id=config.default_backend_id,
            data_root=config.data_root,
            binding_store=self.store,
            quota_runtime=self.quota_runtime,
            enabled=config.local_enabled,
            coordination_locks=self.integration_store.workspace_locks,
        )
        self.snapshot_runtime = WorkspaceSnapshotRuntime(
            config.state_root / "snapshots",
            path_policy=self.backend.path_policy,
        )
        self.ownership_store = DirtyOwnershipStore(config.state_root / "dirty-ownership.json")
        self.mount_policy = WorkspaceArtifactMount()
        self._tokens: dict[str, str] = {}
        self._guard = threading.RLock()

    def create_for_task(
        self,
        *,
        run_id: str,
        task_id: str,
        session_id: str,
        worker_id: str = "task-runtime",
        quota: WorkspaceQuota | None = None,
        idempotency_key: str = "",
        causation_id: str = "",
    ) -> WorkspaceTaskBindingResult:
        # The existence check and durable create must be one task-scoped
        # operation.  Otherwise two callers without an idempotency key can
        # both observe absence and create competing canonical workspaces.
        with self.integration_store.workspace_locks.acquire_many((f"task:{task_id}",)):
            return self._create_for_task_coordinated(
                run_id=run_id,
                task_id=task_id,
                session_id=session_id,
                worker_id=worker_id,
                quota=quota,
                idempotency_key=idempotency_key,
                causation_id=causation_id,
            )

    def _create_for_task_coordinated(
        self,
        *,
        run_id: str,
        task_id: str,
        session_id: str,
        worker_id: str,
        quota: WorkspaceQuota | None,
        idempotency_key: str,
        causation_id: str,
    ) -> WorkspaceTaskBindingResult:
        self._require_enabled()
        existing = self.store.find_binding(task_id=task_id, session_id=session_id, workspace_kind="task")
        if existing is not None:
            access = self.acquire_for_worker(task_id=task_id, session_id=session_id, worker_id=worker_id)
            latest = self.store.require_binding(access.workspace_id)
            receipt = self._receipt(
                binding=latest,
                operation=WorkspaceOperation.OPEN,
                ok=True,
                state_before=existing.lifecycle_state.value,
                state_after=latest.lifecycle_state.value,
                causation_id=causation_id,
                idempotency_key=idempotency_key,
                message="existing workspace binding opened",
            )
            return WorkspaceTaskBindingResult(
                projection=self.project(latest.workspace_id),
                access=access,
                created=False,
                ready_barrier_passed=True,
                receipt=receipt,
            )
        selected_quota = quota or self.config.default_quota
        workspace_id = new_workspace_id("ws")
        lease_id = new_workspace_id("lease")
        fence_token = secrets.token_urlsafe(32)
        location = LocalWorkspaceLocation(
            relative_root=f"tasks/{workspace_id[:5]}/{workspace_id}",
            root_token="workspace-data",
            platform=os.name,
        )
        requested = WorkspaceBinding(
            workspace_id=workspace_id,
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            backend_id=self.backend.backend_id,
            backend_kind=WorkspaceBackendKind.LOCAL,
            workspace_kind=WorkspaceKind.TASK,
            location=location,
            capability_revision=self.backend.capabilities.revision,
            lease_id=lease_id,
            owner_epoch=1,
            fence_token_hash=_fence_hash(fence_token),
            lifecycle_state=WorkspaceLifecycleState.REQUESTED,
            quota=selected_quota,
            metadata={
                "canonical_owner": "WorkspaceManagerRuntime",
                "physical_location_public": False,
                "artifact_byte_owner": "LocalArtifactStore",
                "endpoint_id": self.config.default_backend_id,
            },
        )
        started_at = utc_now()
        created = self.store.create_binding(requested, idempotency_key=idempotency_key)
        mounts = self.mount_policy.build_default_layout(
            workspace_id=workspace_id,
            owner_epoch=created.owner_epoch,
            task_quota=selected_quota,
        )
        self.store.put_mounts(workspace_id, mounts.mounts)
        creating = self._transition(created, WorkspaceLifecycleState.CREATING)
        self._emit("workspace.creating", creating, causation_id=causation_id)
        try:
            self.backend.provision(creating, mounts.mounts)
            lease = self._new_lease(
                creating,
                worker_id=worker_id,
                lease_id=lease_id,
                fence_token=fence_token,
            )
            self.store.put_lease(lease)
            usage = self.backend.scan_usage(creating)
            self.quota_runtime.reconcile_usage(workspace_id, usage)
            ready = self._transition(creating, WorkspaceLifecycleState.READY)
            self._emit("workspace.ready", ready, causation_id=causation_id)
            opened = self._transition(ready, WorkspaceLifecycleState.OPEN)
            self._tokens[lease_id] = fence_token
            access = self.backend.access_handle(opened, lease, fence_token=fence_token)
            receipt = self._receipt(
                binding=opened,
                operation=WorkspaceOperation.CREATE,
                ok=True,
                started_at=started_at,
                state_before=WorkspaceLifecycleState.REQUESTED.value,
                state_after=WorkspaceLifecycleState.OPEN.value,
                causation_id=causation_id,
                idempotency_key=idempotency_key,
                message="workspace created and passed ready barrier",
                metadata={"mount_count": len(mounts.mounts), "ready_barrier": True},
            )
            self._emit("workspace.opened", opened, receipt=receipt.to_dict(), causation_id=causation_id)
            return WorkspaceTaskBindingResult(
                projection=self.project(workspace_id),
                access=access,
                created=True,
                ready_barrier_passed=True,
                receipt=receipt,
            )
        except Exception as error:
            latest = self.store.require_binding(workspace_id)
            try:
                failed = self._transition(
                    latest,
                    WorkspaceLifecycleState.RECOVERY_REQUIRED,
                    write_frozen_reason="workspace_create_failed",
                )
            except WorkspaceError:
                failed = latest
            self._receipt(
                binding=failed,
                operation=WorkspaceOperation.CREATE,
                ok=False,
                started_at=started_at,
                state_before=WorkspaceLifecycleState.REQUESTED.value,
                state_after=failed.lifecycle_state.value,
                causation_id=causation_id,
                idempotency_key=idempotency_key,
                error=error,
                message="workspace creation failed before ready barrier",
            )
            raise

    def acquire_for_worker(
        self,
        *,
        task_id: str,
        session_id: str = "",
        worker_id: str,
        operations: tuple[WorkspaceOperation, ...] | None = None,
    ) -> WorkspaceAccessHandle:
        binding = self.store.find_binding(task_id=task_id, session_id=session_id, workspace_kind="task")
        lock_keys = (f"task:{task_id}",) if binding is None else (f"task:{task_id}", binding.workspace_id)
        with self.integration_store.workspace_locks.acquire_many(lock_keys):
            return self._acquire_for_worker_unlocked(
                task_id=task_id,
                session_id=session_id,
                worker_id=worker_id,
                operations=operations,
            )

    def _acquire_for_worker_unlocked(
        self,
        *,
        task_id: str,
        session_id: str = "",
        worker_id: str,
        operations: tuple[WorkspaceOperation, ...] | None = None,
    ) -> WorkspaceAccessHandle:
        self._require_enabled()
        with self._guard:
            binding = self.store.find_binding(task_id=task_id, session_id=session_id, workspace_kind="task")
            if binding is None:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_FOUND,
                    "The task does not have a workspace binding.",
                    operation="acquire_worker_workspace",
                    metadata={"task_id": task_id},
                )
            current_lease = self.store.get_lease(binding.lease_id)
            token = self._tokens.get(binding.lease_id, "")
            if (
                current_lease is not None
                and current_lease.state is WorkspaceLeaseState.ACTIVE
                and current_lease.worker_id == worker_id
                and token
                and (operations is None or current_lease.operations == operations)
            ):
                return self.backend.access_handle(binding, current_lease, fence_token=token)
            return self._transfer_lease(binding, worker_id=worker_id, operations=operations)

    def project(self, workspace_id: str) -> WorkspacePublicProjection:
        binding = self.store.require_binding(workspace_id)
        usage = self.store.get_usage(workspace_id)
        mounts = self.store.get_mounts(workspace_id)
        return WorkspacePublicProjection(
            workspace_id=binding.workspace_id,
            run_id=binding.run_id,
            task_id=binding.task_id,
            session_id=binding.session_id,
            backend_id=binding.backend_id,
            backend_kind=binding.backend_kind,
            workspace_kind=binding.workspace_kind,
            lifecycle_state=binding.lifecycle_state,
            owner_epoch=binding.owner_epoch,
            binding_revision=binding.binding_revision,
            capability_revision=binding.capability_revision,
            lease_id=binding.lease_id,
            active_snapshot_id=binding.active_snapshot_id,
            quota=binding.quota.to_dict(),
            usage=usage.to_dict(),
            mounts=tuple(
                {
                    "mount_id": item.mount_id,
                    "kind": item.kind.value,
                    "access": item.access.value,
                    "quota": item.quota.to_dict(),
                    "physical_location_redacted": True,
                }
                for item in mounts
            ),
            updated_at=binding.updated_at,
        )

    def list_projections(
        self,
        *,
        run_id: str = "",
        task_id: str = "",
        include_deleted: bool = False,
    ) -> tuple[WorkspacePublicProjection, ...]:
        return tuple(
            self.project(item.workspace_id)
            for item in self.store.list_bindings(
                run_id=run_id,
                task_id=task_id,
                include_deleted=include_deleted,
            )
        )

    def internal_task_root(self, handle: WorkspaceAccessHandle) -> Path:
        binding = self.store.require_binding(handle.workspace_id)
        lease = self.store.get_lease(handle.lease_id)
        if lease is None:
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_NOT_FOUND,
                "Worker workspace handoff references a missing lease.",
                workspace_id=handle.workspace_id,
                operation="worker_handoff",
            )
        self.backend.validate_lease(binding, lease, fence_token=handle.fence_token, operation=WorkspaceOperation.READ)
        return self.backend.mount_root(binding, WorkspaceKind.TASK)

    def snapshot(
        self,
        workspace_id: str,
        *,
        causation_id: str = "",
        include_dirty_state: bool = True,
    ) -> WorkspaceSnapshot:
        with self.integration_store.workspace_locks.acquire_many((workspace_id,)):
            return self._snapshot_unlocked(
                workspace_id,
                causation_id=causation_id,
                include_dirty_state=include_dirty_state,
            )

    def _snapshot_unlocked(
        self,
        workspace_id: str,
        *,
        causation_id: str = "",
        include_dirty_state: bool = True,
    ) -> WorkspaceSnapshot:
        self._require_enabled()
        started_at = utc_now()
        binding = self.store.require_binding(workspace_id)
        previous_state = binding.lifecycle_state
        if previous_state not in {
            WorkspaceLifecycleState.OPEN,
            WorkspaceLifecycleState.READY,
            WorkspaceLifecycleState.WRITE_FROZEN,
        }:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_TRANSITION,
                "Workspace snapshots require a ready or open binding.",
                workspace_id=workspace_id,
                operation="snapshot_workspace",
                actual=previous_state.value,
            )
        frozen = self._transition(
            binding,
            WorkspaceLifecycleState.SNAPSHOTTING,
            write_frozen_reason="snapshot_in_progress",
        )
        try:
            task_root = self.backend.mount_root(frozen, WorkspaceKind.TASK)
            dirty_paths = ()
            repositories = ()
            baseline_id = ""
            if include_dirty_state:
                dirty_runtime = WorkspaceDirtyStateRuntime(
                    workspace_id=workspace_id,
                    workspace_root=task_root,
                    ownership_store=self.ownership_store,
                    path_policy=self.backend.path_policy,
                )
                dirty = dirty_runtime.scan()
                dirty_paths = dirty.paths
                repositories = dirty.repository_refs
            snapshot = self.snapshot_runtime.create(
                workspace_id=workspace_id,
                owner_epoch=frozen.owner_epoch,
                lease_id=frozen.lease_id,
                source_root=task_root,
                parent_snapshot_id=binding.active_snapshot_id,
                baseline_id=baseline_id,
                dirty_paths=dirty_paths,
                repository_refs=repositories,
                metadata={"workspace_kind": binding.workspace_kind.value},
            )
            self.store.put_snapshot(snapshot)
            after_state = (
                WorkspaceLifecycleState.WRITE_FROZEN
                if previous_state is WorkspaceLifecycleState.WRITE_FROZEN
                else WorkspaceLifecycleState.OPEN
            )
            opened = self._transition(
                frozen,
                after_state,
                active_snapshot_id=snapshot.snapshot_id,
                write_frozen_reason=(binding.write_frozen_reason if after_state is WorkspaceLifecycleState.WRITE_FROZEN else ""),
            )
            receipt = self._receipt(
                binding=opened,
                operation=WorkspaceOperation.SNAPSHOT,
                ok=True,
                started_at=started_at,
                state_before=previous_state.value,
                state_after=opened.lifecycle_state.value,
                snapshot_id=snapshot.snapshot_id,
                causation_id=causation_id,
                bytes_changed=snapshot.total_bytes,
                message="workspace snapshot committed",
            )
            self._emit("workspace.snapshot.committed", opened, receipt=receipt.to_dict(), causation_id=causation_id)
            return snapshot
        except Exception as error:
            latest = self.store.require_binding(workspace_id)
            if latest.lifecycle_state is WorkspaceLifecycleState.SNAPSHOTTING:
                self._transition(latest, previous_state, write_frozen_reason="")
            self._receipt(
                binding=self.store.require_binding(workspace_id),
                operation=WorkspaceOperation.SNAPSHOT,
                ok=False,
                started_at=started_at,
                state_before=previous_state.value,
                state_after=self.store.require_binding(workspace_id).lifecycle_state.value,
                causation_id=causation_id,
                error=error,
                message="workspace snapshot failed",
            )
            raise

    def restore(
        self,
        workspace_id: str,
        snapshot_id: str,
        *,
        causation_id: str = "",
    ) -> SnapshotRestoreResult:
        with self.integration_store.workspace_locks.acquire_many((workspace_id,)):
            return self._restore_unlocked(
                workspace_id,
                snapshot_id,
                causation_id=causation_id,
            )

    def _restore_unlocked(
        self,
        workspace_id: str,
        snapshot_id: str,
        *,
        causation_id: str = "",
    ) -> SnapshotRestoreResult:
        self._require_enabled()
        started_at = utc_now()
        binding = self.store.require_binding(workspace_id)
        snapshot = self.store.get_snapshot(snapshot_id) or self.snapshot_runtime.load(snapshot_id)
        if snapshot.workspace_id != workspace_id:
            raise WorkspaceError(
                WorkspaceErrorCode.SNAPSHOT_OWNER_MISMATCH,
                "The restore snapshot belongs to a different workspace.",
                workspace_id=workspace_id,
                operation="restore_workspace",
            )
        previous_state = binding.lifecycle_state
        restoring = self._transition(
            binding,
            WorkspaceLifecycleState.RESTORING,
            write_frozen_reason="restore_in_progress",
        )
        self.store.revoke_workspace_leases(workspace_id, state_value=WorkspaceLeaseState.REVOKED)
        self._tokens.pop(restoring.lease_id, None)
        try:
            task_root = self.backend.mount_root(restoring, WorkspaceKind.TASK)
            result = self.snapshot_runtime.restore(
                snapshot=snapshot,
                workspace_id=workspace_id,
                owner_epoch=restoring.owner_epoch,
                target_root=task_root,
                preserve_displaced=False,
            )
            self.backend.file_state.invalidate(workspace_id)
            opened = self._rotate_after_mutation(
                restoring,
                worker_id="restore-runtime",
                active_snapshot_id=snapshot_id,
            )
            usage = self.backend.scan_usage(opened[0])
            self.quota_runtime.reconcile_usage(workspace_id, usage)
            receipt = self._receipt(
                binding=opened[0],
                operation=WorkspaceOperation.RESTORE,
                ok=True,
                started_at=started_at,
                state_before=previous_state.value,
                state_after=WorkspaceLifecycleState.OPEN.value,
                snapshot_id=snapshot_id,
                causation_id=causation_id,
                bytes_changed=snapshot.total_bytes,
                message="workspace snapshot restored and owner epoch rotated",
            )
            self._emit("workspace.restore.committed", opened[0], receipt=receipt.to_dict(), causation_id=causation_id)
            return result
        except Exception as error:
            latest = self.store.require_binding(workspace_id)
            recovery = WorkspaceRecoveryRecord(
                recovery_id=new_workspace_id("recovery"),
                workspace_id=workspace_id,
                state=RecoveryState.FAILED,
                reason="snapshot_restore_failed",
                source_snapshot_id=snapshot_id,
                owner_epoch_before=binding.owner_epoch,
                owner_epoch_after=latest.owner_epoch,
                completed_at=utc_now(),
                outcome="recovery_required",
                retryable=True,
                metadata={"exception_type": type(error).__name__},
            )
            self.store.put_recovery(recovery)
            from .integration_models import (
                IntegrationOperation,
                RecoverySeverity,
                WorkspaceRecoveryInput,
            )

            recovery_input = self.integration_store.put_recovery_input(
                WorkspaceRecoveryInput(
                    recovery_input_id=new_workspace_id("workspace-recovery-input"),
                    workspace_id=workspace_id,
                    operation=IntegrationOperation.RESTORE,
                    severity=RecoverySeverity.BACKEND_UNAVAILABLE_CANDIDATE,
                    reason_code="snapshot_restore_failed",
                    owner_epoch=latest.owner_epoch,
                    binding_revision=latest.binding_revision,
                    snapshot_id=snapshot_id,
                    retryable=True,
                    evidence_refs=(
                        f"workspace-snapshot://{snapshot_id}",
                        f"workspace-recovery://{recovery.recovery_id}",
                    ),
                    recommended_actions=(
                        "verify_snapshot",
                        "fence_workspace_writes",
                        "replan_backend_placement",
                    ),
                    metadata={
                        "exception_type": type(error).__name__,
                        "downstream_consumers": ["M1-05C", "M1-07C"],
                    },
                )
            )
            failed = self._transition(
                latest,
                WorkspaceLifecycleState.RECOVERY_REQUIRED,
                recovery_record_id=recovery.recovery_id,
                write_frozen_reason="restore_failed",
            )
            self._receipt(
                binding=failed,
                operation=WorkspaceOperation.RESTORE,
                ok=False,
                started_at=started_at,
                state_before=previous_state.value,
                state_after=failed.lifecycle_state.value,
                snapshot_id=snapshot_id,
                causation_id=causation_id,
                error=error,
                message="workspace restore failed and requires recovery",
            )
            self._emit(
                "workspace.backend_unavailable_candidate",
                failed,
                causation_id=causation_id,
                metadata={
                    "snapshot_id": snapshot_id,
                    "recovery_input_id": recovery_input.recovery_input_id,
                    "reason_code": recovery_input.reason_code,
                },
            )
            raise

    def cleanup(
        self,
        workspace_id: str,
        *,
        causation_id: str = "",
        archive: bool = True,
    ) -> WorkspaceOperationReceipt:
        with self.integration_store.workspace_locks.acquire_many((workspace_id,)):
            return self._cleanup_unlocked(
                workspace_id,
                causation_id=causation_id,
                archive=archive,
            )

    def _cleanup_unlocked(
        self,
        workspace_id: str,
        *,
        causation_id: str = "",
        archive: bool = True,
    ) -> WorkspaceOperationReceipt:
        self._require_enabled()
        binding = self.store.require_binding(workspace_id)
        started_at = utc_now()
        snapshot_id = binding.active_snapshot_id
        if archive:
            snapshot_id = self.snapshot(workspace_id, causation_id=causation_id).snapshot_id
            binding = self.store.require_binding(workspace_id)
        if not snapshot_id:
            raise WorkspaceError(
                WorkspaceErrorCode.CLEANUP_CONFLICT,
                "Workspace cleanup requires archive-before-delete evidence.",
                workspace_id=workspace_id,
                operation="cleanup_workspace",
            )
        cleaning = self._transition(binding, WorkspaceLifecycleState.CLEANING, write_frozen_reason="cleanup_in_progress")
        self.store.revoke_workspace_leases(workspace_id, state_value=WorkspaceLeaseState.REVOKED)
        self._tokens.pop(cleaning.lease_id, None)
        try:
            quarantine_ref = self.backend.archive_and_cleanup_root(
                cleaning,
                archived_snapshot_id=snapshot_id,
            )
            deleted = self._transition(
                cleaning,
                WorkspaceLifecycleState.DELETED,
                write_frozen_reason="",
                metadata={**dict(cleaning.metadata), "cleanup_quarantine_ref": quarantine_ref},
            )
            receipt = self._receipt(
                binding=deleted,
                operation=WorkspaceOperation.CLEANUP,
                ok=True,
                started_at=started_at,
                state_before=binding.lifecycle_state.value,
                state_after=deleted.lifecycle_state.value,
                snapshot_id=snapshot_id,
                causation_id=causation_id,
                message="workspace archived and deleted",
            )
            self._emit("workspace.deleted", deleted, receipt=receipt.to_dict(), causation_id=causation_id)
            return receipt
        except Exception as error:
            latest = self.store.require_binding(workspace_id)
            failed = self._transition(
                latest,
                WorkspaceLifecycleState.RECOVERY_REQUIRED,
                write_frozen_reason="cleanup_failed",
            )
            self._receipt(
                binding=failed,
                operation=WorkspaceOperation.CLEANUP,
                ok=False,
                started_at=started_at,
                state_before=binding.lifecycle_state.value,
                state_after=failed.lifecycle_state.value,
                snapshot_id=snapshot_id,
                causation_id=causation_id,
                error=error,
                message="workspace cleanup failed",
            )
            raise

    def recover_on_startup(self) -> tuple[WorkspaceRecoveryRecord, ...]:
        if not self.config.local_enabled:
            return ()
        self.snapshot_runtime.prune_uncommitted()
        recoveries: list[WorkspaceRecoveryRecord] = []
        for binding in self.store.list_bindings(include_deleted=False):
            if (
                binding.lifecycle_state is WorkspaceLifecycleState.CLEANING
                and binding.active_snapshot_id
            ):
                try:
                    root = self.backend.physical_root(binding)
                    quarantine_ref = "already_absent_after_interrupted_cleanup"
                    if root.exists():
                        quarantine_ref = self.backend.archive_and_cleanup_root(
                            binding,
                            archived_snapshot_id=binding.active_snapshot_id,
                        )
                    deleted = self._transition(
                        binding,
                        WorkspaceLifecycleState.DELETED,
                        write_frozen_reason="",
                        metadata={
                            **dict(binding.metadata),
                            "cleanup_quarantine_ref": quarantine_ref,
                            "cleanup_resumed_on_startup": True,
                        },
                    )
                    recovery = WorkspaceRecoveryRecord(
                        recovery_id=new_workspace_id("recovery"),
                        workspace_id=binding.workspace_id,
                        state=RecoveryState.RESTORED,
                        reason="interrupted_cleanup_resumed",
                        source_snapshot_id=binding.active_snapshot_id,
                        owner_epoch_before=binding.owner_epoch,
                        owner_epoch_after=deleted.owner_epoch,
                        completed_at=utc_now(),
                        outcome="cleanup_completed",
                        retryable=False,
                        metadata={"quarantine_ref": quarantine_ref},
                    )
                    self.store.put_recovery(recovery)
                    self._emit(
                        "workspace.cleanup.recovered",
                        deleted,
                        metadata={"recovery_id": recovery.recovery_id},
                    )
                    recoveries.append(recovery)
                    continue
                except WorkspaceError:
                    # Fall through to the generic recovery-required record.
                    pass
            try:
                self.backend.open(binding)
                if binding.lifecycle_state in {
                    WorkspaceLifecycleState.CREATING,
                    WorkspaceLifecycleState.SNAPSHOTTING,
                    WorkspaceLifecycleState.RESTORING,
                    WorkspaceLifecycleState.CLEANING,
                }:
                    raise WorkspaceError(
                        WorkspaceErrorCode.OPERATION_IN_PROGRESS,
                        "An interrupted workspace phase requires explicit recovery.",
                        workspace_id=binding.workspace_id,
                        operation="startup_recovery",
                        actual=binding.lifecycle_state.value,
                    )
            except WorkspaceError as error:
                recovery = WorkspaceRecoveryRecord(
                    recovery_id=new_workspace_id("recovery"),
                    workspace_id=binding.workspace_id,
                    state=RecoveryState.DETECTED,
                    reason=error.detail.code.value,
                    source_snapshot_id=binding.active_snapshot_id,
                    owner_epoch_before=binding.owner_epoch,
                    owner_epoch_after=binding.owner_epoch,
                    outcome="manual_or_snapshot_recovery_required",
                    retryable=True,
                    metadata={"lifecycle_state": binding.lifecycle_state.value},
                )
                self.store.put_recovery(recovery)
                latest = self.store.require_binding(binding.workspace_id)
                if latest.lifecycle_state is not WorkspaceLifecycleState.RECOVERY_REQUIRED:
                    self._transition(
                        latest,
                        WorkspaceLifecycleState.RECOVERY_REQUIRED,
                        recovery_record_id=recovery.recovery_id,
                        write_frozen_reason="startup_recovery",
                    )
                recoveries.append(recovery)
        from .recovery import WorkspaceOperationRecoveryRuntime

        for recovery_input in WorkspaceOperationRecoveryRuntime(self).recover():
            self.emit_integration_event(
                "workspace.recovery_input",
                recovery_input.workspace_id,
                metadata={
                    "recovery_input_id": recovery_input.recovery_input_id,
                    "reason_code": recovery_input.reason_code,
                    "severity": recovery_input.severity.value,
                },
            )
        return tuple(recoveries)

    def freeze_for_integration(
        self,
        workspace_id: str,
        *,
        reason: str,
        causation_id: str = "",
    ) -> WorkspaceBinding:
        """Freeze writes under the shared workspace coordination guard."""

        self._require_enabled()
        reason_text = str(reason or "").strip()
        if not reason_text:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace integration freeze requires a reason.",
                workspace_id=workspace_id,
                operation="freeze_workspace_integration",
            )
        with self.integration_store.workspace_locks.acquire_many((workspace_id,)):
            binding = self.store.require_binding(workspace_id)
            if binding.lifecycle_state is WorkspaceLifecycleState.WRITE_FROZEN:
                if binding.write_frozen_reason != reason_text:
                    raise WorkspaceError(
                        WorkspaceErrorCode.OPERATION_IN_PROGRESS,
                        "Workspace is frozen by a different integration operation.",
                        workspace_id=workspace_id,
                        operation="freeze_workspace_integration",
                        expected=binding.write_frozen_reason,
                        actual=reason_text,
                    )
                return binding
            if binding.lifecycle_state is not WorkspaceLifecycleState.OPEN:
                raise WorkspaceError(
                    WorkspaceErrorCode.INVALID_TRANSITION,
                    "Workspace integration freeze requires an open binding.",
                    workspace_id=workspace_id,
                    operation="freeze_workspace_integration",
                    actual=binding.lifecycle_state.value,
                )
            frozen = self._transition(
                binding,
                WorkspaceLifecycleState.WRITE_FROZEN,
                write_frozen_reason=reason_text,
            )
            self._emit(
                "workspace.write.frozen",
                frozen,
                causation_id=causation_id,
                metadata={"reason": reason_text},
            )
            return frozen

    def resume_after_integration_failure(
        self,
        workspace_id: str,
        *,
        worker_id: str,
        reason: str,
        causation_id: str = "",
    ) -> WorkspaceAccessHandle:
        """Reopen the canonical source and rotate stale access after rollback."""

        self._require_enabled()
        with self.integration_store.workspace_locks.acquire_many((workspace_id,)):
            binding = self.store.require_binding(workspace_id)
            if binding.lifecycle_state is WorkspaceLifecycleState.WRITE_FROZEN:
                binding = self._transition(
                    binding,
                    WorkspaceLifecycleState.OPEN,
                    write_frozen_reason="",
                )
            elif binding.lifecycle_state is WorkspaceLifecycleState.RECOVERY_REQUIRED:
                binding = self._transition(
                    binding,
                    WorkspaceLifecycleState.OPEN,
                    write_frozen_reason="",
                    recovery_record_id="",
                )
            elif binding.lifecycle_state is not WorkspaceLifecycleState.OPEN:
                raise WorkspaceError(
                    WorkspaceErrorCode.INVALID_TRANSITION,
                    "Workspace cannot resume from its current lifecycle state.",
                    workspace_id=workspace_id,
                    operation="resume_workspace_integration",
                    actual=binding.lifecycle_state.value,
                )
            access = self._transfer_lease(binding, worker_id=worker_id, operations=None)
            self._emit(
                "workspace.integration.resumed",
                self.store.require_binding(workspace_id),
                causation_id=causation_id,
                metadata={"reason": str(reason or "integration_failure")},
            )
            return access

    def rotate_after_integration(
        self,
        workspace_id: str,
        *,
        worker_id: str,
        active_snapshot_id: str = "",
        causation_id: str = "",
        reason: str = "integration_commit",
    ) -> WorkspaceAccessHandle:
        """Fence stale handles after a composite mutation commits."""

        self._require_enabled()
        with self.integration_store.workspace_locks.acquire_many((workspace_id,)):
            binding = self.store.require_binding(workspace_id)
            if binding.lifecycle_state is not WorkspaceLifecycleState.OPEN:
                raise WorkspaceError(
                    WorkspaceErrorCode.INVALID_TRANSITION,
                    "Workspace epoch rotation requires an open binding.",
                    workspace_id=workspace_id,
                    operation="rotate_workspace_integration_epoch",
                    actual=binding.lifecycle_state.value,
                )
            old_leases = self.store.list_leases(workspace_id)
            updated, access = self._rotate_after_mutation(
                binding,
                worker_id=worker_id,
                active_snapshot_id=active_snapshot_id or binding.active_snapshot_id,
            )
            for lease in old_leases:
                if lease.lease_id == updated.lease_id:
                    continue
                if lease.state is WorkspaceLeaseState.ACTIVE:
                    self.store.update_lease(replace(lease, state=WorkspaceLeaseState.REVOKED))
                self._tokens.pop(lease.lease_id, None)
            self.backend.file_state.invalidate(workspace_id)
            self._emit(
                "workspace.integration.epoch.rotated",
                updated,
                causation_id=causation_id,
                metadata={"worker_id": worker_id, "reason": reason},
            )
            return access

    def commit_local_rebind(
        self,
        workspace_id: str,
        *,
        expected_revision: int,
        expected_owner_epoch: int,
        target_location: LocalWorkspaceLocation,
        target_endpoint_id: str,
        target_backend_id: str,
        target_capability_revision: int,
        worker_id: str,
        active_snapshot_id: str,
        rebind_id: str,
        causation_id: str = "",
    ) -> WorkspaceAccessHandle:
        """Commit the sole local rebind switch through one binding CAS."""

        self._require_enabled()
        with self.integration_store.workspace_locks.acquire_many((workspace_id,)):
            current = self.store.require_binding(workspace_id)
            if current.lifecycle_state is not WorkspaceLifecycleState.WRITE_FROZEN:
                raise WorkspaceError(
                    WorkspaceErrorCode.INVALID_TRANSITION,
                    "Workspace rebind commit requires a frozen source binding.",
                    workspace_id=workspace_id,
                    operation="commit_workspace_rebind",
                    actual=current.lifecycle_state.value,
                )
            new_lease_id = new_workspace_id("lease")
            fence_token = secrets.token_urlsafe(32)
            updated = self.store.compare_and_swap_binding(
                workspace_id,
                expected_revision=expected_revision,
                expected_owner_epoch=expected_owner_epoch,
                update=lambda binding: replace(
                    binding,
                    location=target_location,
                    backend_id=target_backend_id,
                    capability_revision=int(target_capability_revision),
                    owner_epoch=binding.owner_epoch + 1,
                    lease_id=new_lease_id,
                    fence_token_hash=_fence_hash(fence_token),
                    lifecycle_state=WorkspaceLifecycleState.OPEN,
                    active_snapshot_id=active_snapshot_id,
                    write_frozen_reason="",
                    binding_revision=binding.binding_revision + 1,
                    updated_at=utc_now(),
                    metadata={
                        **dict(binding.metadata),
                        "endpoint_id": str(target_endpoint_id),
                        "rebind_id": rebind_id,
                        "physical_location_public": False,
                    },
                ),
            )
            old_leases = self.store.list_leases(workspace_id)
            for lease in old_leases:
                if lease.state is WorkspaceLeaseState.ACTIVE:
                    self.store.update_lease(replace(lease, state=WorkspaceLeaseState.REVOKED))
                self._tokens.pop(lease.lease_id, None)
            lease = self._new_lease(
                updated,
                worker_id=worker_id,
                lease_id=new_lease_id,
                fence_token=fence_token,
            )
            self.store.put_lease(lease)
            self._tokens[new_lease_id] = fence_token
            self.backend.file_state.invalidate(workspace_id)
            usage = self.backend.scan_usage(updated)
            self.quota_runtime.reconcile_usage(workspace_id, usage)
            self._emit(
                "workspace.rebind.binding_switched",
                updated,
                causation_id=causation_id,
                metadata={"rebind_id": rebind_id, "worker_id": worker_id},
            )
            return self.backend.access_handle(updated, lease, fence_token=fence_token)

    def emit_integration_event(
        self,
        event_type: str,
        workspace_id: str,
        *,
        causation_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        binding = self.store.require_binding(workspace_id)
        self._emit(
            str(event_type),
            binding,
            causation_id=causation_id,
            metadata=metadata,
        )

    def health(self) -> dict[str, Any]:
        return {
            "ok": self.config.local_enabled and self.store.health().get("ok", False),
            "state_owner": "WorkspaceManagerRuntime+WorkspaceBindingStore",
            "default_backend": self.backend.health(),
            "store": self.store.health(),
            "integration_store": self.integration_store.health(),
            "binding_count": len(self.store.list_bindings(include_deleted=True)) if self.config.local_enabled else 0,
            "physical_paths_public": False,
            "fallback_backend": None,
        }

    def _transfer_lease(
        self,
        binding: WorkspaceBinding,
        *,
        worker_id: str,
        operations: tuple[WorkspaceOperation, ...] | None,
    ) -> WorkspaceAccessHandle:
        self.store.revoke_workspace_leases(binding.workspace_id, state_value=WorkspaceLeaseState.REVOKED)
        self._tokens.pop(binding.lease_id, None)
        new_lease_id = new_workspace_id("lease")
        fence_token = secrets.token_urlsafe(32)
        updated = self.store.compare_and_swap_binding(
            binding.workspace_id,
            expected_revision=binding.binding_revision,
            expected_owner_epoch=binding.owner_epoch,
            update=lambda current: replace(
                current,
                lease_id=new_lease_id,
                owner_epoch=current.owner_epoch + 1,
                fence_token_hash=_fence_hash(fence_token),
                lifecycle_state=WorkspaceLifecycleState.OPEN,
                binding_revision=current.binding_revision + 1,
                updated_at=utc_now(),
            ),
        )
        lease = self._new_lease(
            updated,
            worker_id=worker_id,
            lease_id=new_lease_id,
            fence_token=fence_token,
            operations=operations,
        )
        self.store.put_lease(lease)
        self._tokens[new_lease_id] = fence_token
        self.backend.file_state.invalidate(binding.workspace_id)
        self._emit("workspace.lease.transferred", updated, metadata={"worker_id": worker_id})
        return self.backend.access_handle(updated, lease, fence_token=fence_token)

    def _rotate_after_mutation(
        self,
        binding: WorkspaceBinding,
        *,
        worker_id: str,
        active_snapshot_id: str,
    ) -> tuple[WorkspaceBinding, WorkspaceAccessHandle]:
        new_lease_id = new_workspace_id("lease")
        fence_token = secrets.token_urlsafe(32)
        updated = self.store.compare_and_swap_binding(
            binding.workspace_id,
            expected_revision=binding.binding_revision,
            expected_owner_epoch=binding.owner_epoch,
            update=lambda current: replace(
                current,
                owner_epoch=current.owner_epoch + 1,
                lease_id=new_lease_id,
                fence_token_hash=_fence_hash(fence_token),
                lifecycle_state=WorkspaceLifecycleState.OPEN,
                active_snapshot_id=active_snapshot_id,
                write_frozen_reason="",
                binding_revision=current.binding_revision + 1,
                updated_at=utc_now(),
            ),
        )
        lease = self._new_lease(updated, worker_id=worker_id, lease_id=new_lease_id, fence_token=fence_token)
        self.store.put_lease(lease)
        self._tokens[new_lease_id] = fence_token
        return updated, self.backend.access_handle(updated, lease, fence_token=fence_token)

    def _new_lease(
        self,
        binding: WorkspaceBinding,
        *,
        worker_id: str,
        lease_id: str,
        fence_token: str,
        operations: tuple[WorkspaceOperation, ...] | None = None,
    ) -> WorkspaceLease:
        selected_operations = operations or (
            WorkspaceOperation.READ,
            WorkspaceOperation.WRITE,
            WorkspaceOperation.DELETE,
            WorkspaceOperation.LIST,
            WorkspaceOperation.SNAPSHOT,
            WorkspaceOperation.GIT_QUERY,
            WorkspaceOperation.QUOTA_QUERY,
        )
        return WorkspaceLease(
            lease_id=lease_id,
            workspace_id=binding.workspace_id,
            run_id=binding.run_id,
            task_id=binding.task_id,
            session_id=binding.session_id,
            worker_id=worker_id,
            owner_epoch=binding.owner_epoch,
            fence_token_hash=_fence_hash(fence_token),
            capability_revision=binding.capability_revision,
            expires_at=(datetime.now(UTC) + timedelta(seconds=self.config.lease_ttl_seconds)).isoformat(),
            operations=selected_operations,
            metadata={"physical_location_public": False},
        )

    def _transition(
        self,
        binding: WorkspaceBinding,
        state: WorkspaceLifecycleState,
        **updates: Any,
    ) -> WorkspaceBinding:
        _assert_transition(binding.lifecycle_state, state)
        return self.store.compare_and_swap_binding(
            binding.workspace_id,
            expected_revision=binding.binding_revision,
            expected_owner_epoch=binding.owner_epoch,
            update=lambda current: current.with_state(state, **updates),
        )

    def _receipt(
        self,
        *,
        binding: WorkspaceBinding,
        operation: WorkspaceOperation,
        ok: bool,
        state_before: str,
        state_after: str,
        started_at: str = "",
        causation_id: str = "",
        idempotency_key: str = "",
        snapshot_id: str = "",
        bytes_changed: int = 0,
        message: str = "",
        metadata: Mapping[str, Any] | None = None,
        error: Exception | None = None,
    ) -> WorkspaceOperationReceipt:
        error_code = ""
        if isinstance(error, WorkspaceError):
            error_code = error.detail.code.value
        elif error is not None:
            error_code = WorkspaceErrorCode.INTERNAL.value
        receipt = WorkspaceOperationReceipt(
            receipt_id=new_workspace_id("receipt"),
            workspace_id=binding.workspace_id,
            operation=operation,
            ok=ok,
            owner_epoch=binding.owner_epoch,
            binding_revision=binding.binding_revision,
            lease_id=binding.lease_id,
            started_at=started_at or utc_now(),
            completed_at=utc_now(),
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            snapshot_id=snapshot_id,
            bytes_changed=bytes_changed,
            state_before=state_before,
            state_after=state_after,
            error_code=error_code,
            message=message,
            metadata={**dict(metadata or {}), "state_owner": "WorkspaceManagerRuntime"},
        )
        return self.store.append_receipt(receipt)

    def _emit(
        self,
        event_type: str,
        binding: WorkspaceBinding,
        *,
        causation_id: str = "",
        receipt: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            event_type,
            {
                "workspace_id": binding.workspace_id,
                "run_id": binding.run_id,
                "task_id": binding.task_id,
                "session_id": binding.session_id,
                "backend_id": binding.backend_id,
                "lifecycle_state": binding.lifecycle_state.value,
                "owner_epoch": binding.owner_epoch,
                "binding_revision": binding.binding_revision,
                "causation_id": causation_id,
                "receipt": dict(receipt or {}),
                "metadata": dict(metadata or {}),
                "physical_location_redacted": True,
            },
        )

    def _require_enabled(self) -> None:
        if not self.config.local_enabled:
            raise WorkspaceError(
                WorkspaceErrorCode.DISABLED,
                "LocalWorkspace is disabled and no implicit global-directory fallback is permitted.",
                operation="workspace_manager",
            )


ALLOWED_TRANSITIONS: Mapping[WorkspaceLifecycleState, frozenset[WorkspaceLifecycleState]] = {
    WorkspaceLifecycleState.REQUESTED: frozenset(
        {WorkspaceLifecycleState.CREATING, WorkspaceLifecycleState.RECOVERY_REQUIRED}
    ),
    WorkspaceLifecycleState.CREATING: frozenset(
        {WorkspaceLifecycleState.READY, WorkspaceLifecycleState.RECOVERY_REQUIRED}
    ),
    WorkspaceLifecycleState.READY: frozenset(
        {WorkspaceLifecycleState.OPEN, WorkspaceLifecycleState.SNAPSHOTTING, WorkspaceLifecycleState.CLEANING}
    ),
    WorkspaceLifecycleState.OPEN: frozenset(
        {
            WorkspaceLifecycleState.SNAPSHOTTING,
            WorkspaceLifecycleState.RESTORING,
            WorkspaceLifecycleState.WRITE_FROZEN,
            WorkspaceLifecycleState.CLEANING,
            WorkspaceLifecycleState.CLOSED,
            WorkspaceLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    WorkspaceLifecycleState.WRITE_FROZEN: frozenset(
        {
            WorkspaceLifecycleState.OPEN,
            WorkspaceLifecycleState.SNAPSHOTTING,
            WorkspaceLifecycleState.RESTORING,
            WorkspaceLifecycleState.CLEANING,
            WorkspaceLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    WorkspaceLifecycleState.SNAPSHOTTING: frozenset(
        {
            WorkspaceLifecycleState.OPEN,
            WorkspaceLifecycleState.READY,
            WorkspaceLifecycleState.WRITE_FROZEN,
            WorkspaceLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    WorkspaceLifecycleState.RESTORING: frozenset(
        {WorkspaceLifecycleState.OPEN, WorkspaceLifecycleState.RECOVERY_REQUIRED}
    ),
    WorkspaceLifecycleState.CLEANING: frozenset(
        {WorkspaceLifecycleState.DELETED, WorkspaceLifecycleState.RECOVERY_REQUIRED}
    ),
    WorkspaceLifecycleState.CLOSED: frozenset(
        {WorkspaceLifecycleState.OPEN, WorkspaceLifecycleState.CLEANING, WorkspaceLifecycleState.RECOVERY_REQUIRED}
    ),
    WorkspaceLifecycleState.CORRUPT: frozenset(
        {WorkspaceLifecycleState.RECOVERY_REQUIRED, WorkspaceLifecycleState.CLEANING}
    ),
    WorkspaceLifecycleState.RECOVERY_REQUIRED: frozenset(
        {WorkspaceLifecycleState.RESTORING, WorkspaceLifecycleState.CLEANING, WorkspaceLifecycleState.OPEN}
    ),
    WorkspaceLifecycleState.DELETED: frozenset(),
}


def _assert_transition(before: WorkspaceLifecycleState, after: WorkspaceLifecycleState) -> None:
    if before == after:
        return
    if after not in ALLOWED_TRANSITIONS.get(before, frozenset()):
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_TRANSITION,
            "The requested workspace lifecycle transition is not allowed.",
            operation="workspace_transition",
            expected=sorted(item.value for item in ALLOWED_TRANSITIONS.get(before, frozenset())),
            actual={"before": before.value, "after": after.value},
        )


def _fence_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
