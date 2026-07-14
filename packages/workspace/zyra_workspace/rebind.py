from __future__ import annotations

"""Atomic local-to-local workspace endpoint rebind."""

import os
import secrets
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .atomic import atomic_write_json, file_identity
from .dirty_state import WorkspaceDirtyStateRuntime
from .errors import WorkspaceError, WorkspaceErrorCode, error_from_exception
from .integration_models import (
    IntegrationOperation,
    LocalWorkspaceEndpoint,
    RebindState,
    RecoverySeverity,
    WorkerWorkspaceReceipt,
    WorkspaceRebindRecord,
    WorkspaceRecoveryInput,
    WorkspaceReferenceMigration,
)
from .integration_store import WorkspaceIntegrationStore
from .local_backend import WORKSPACE_MARKER_SCHEMA, WorkspaceAccessHandle
from .models import (
    LocalWorkspaceLocation,
    WorkspaceKind,
    WorkspaceLifecycleState,
    WorkspaceOperation,
    new_workspace_id,
    stable_digest,
    utc_now,
)
from .tree_state import TreeScanLimits, safe_remove_private_tree, scan_workspace_tree


@dataclass(frozen=True, slots=True)
class WorkspaceRebindResult:
    record: WorkspaceRebindRecord
    access: WorkspaceAccessHandle
    receipt: WorkerWorkspaceReceipt
    migration: WorkspaceReferenceMigration
    idempotent_replay: bool = False

    @property
    def ok(self) -> bool:
        return self.record.state is RebindState.COMMITTED and self.receipt.ok

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "rebind": self.record.to_dict(),
            "workspace_access": self.access.to_public_dict(),
            "receipt": self.receipt.to_dict(),
            "reference_migration": self.migration.to_dict(),
            "idempotent_replay": self.idempotent_replay,
            "physical_location_redacted": True,
        }


class WorkspaceRebindRuntime:
    """Move a local binding between typed local endpoints.

    The source binding stays canonical until one binding-store CAS commits the
    target location and rotates epoch/lease/fence.  Pre-CAS failures remove the
    target and reopen the source.  Post-CAS cleanup failures keep the target
    canonical and emit a recovery input instead of rolling back to a stale
    execution reference.
    """

    def __init__(
        self,
        manager: Any,
        *,
        disabled: bool = False,
        scan_limits: TreeScanLimits | None = None,
    ) -> None:
        self.manager = manager
        self.store: WorkspaceIntegrationStore = manager.integration_store
        self.disabled = bool(disabled)
        self.scan_limits = scan_limits or TreeScanLimits()
        self._ensure_default_endpoint()

    def register_endpoint(
        self,
        endpoint_id: str,
        *,
        relative_root: str,
        root_token: str = "workspace-data",
        capability_revision: int = 1,
        enabled: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> LocalWorkspaceEndpoint:
        self._require_enabled("")
        endpoint = LocalWorkspaceEndpoint(
            endpoint_id=endpoint_id,
            root_token=root_token,
            relative_root=relative_root,
            capability_revision=capability_revision,
            enabled=enabled,
            metadata={
                **dict(metadata or {}),
                "backend_kind": "local",
                "physical_location_public": False,
            },
        )
        root = self._endpoint_root(endpoint)
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise WorkspaceError(
                WorkspaceErrorCode.SYMLINK_ESCAPE,
                "Local workspace endpoint root cannot be a symlink.",
                operation="register_workspace_endpoint",
            )
        return self.store.put_endpoint(endpoint)

    def rebind(
        self,
        access: WorkspaceAccessHandle,
        *,
        target_endpoint_id: str,
        worker_id: str,
        idempotency_key: str = "",
        causation_id: str = "",
        artifact_refs: tuple[str, ...] = (),
        event_refs: tuple[str, ...] = (),
    ) -> WorkspaceRebindResult:
        self._require_enabled(access.workspace_id)
        binding = self.manager.store.require_binding(access.workspace_id)
        self._validate_access(access, binding)
        source_endpoint_id = str(binding.metadata.get("endpoint_id") or self.manager.config.default_backend_id)
        target_endpoint = self.store.require_endpoint(target_endpoint_id)
        if source_endpoint_id == target_endpoint.endpoint_id:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace is already bound to the requested local endpoint.",
                workspace_id=binding.workspace_id,
                operation="rebind_workspace",
                actual=target_endpoint.endpoint_id,
            )
        fingerprint = stable_digest(
            {
                "workspace_id": binding.workspace_id,
                "source_endpoint_id": source_endpoint_id,
                "target_endpoint_id": target_endpoint.endpoint_id,
                "owner_epoch": binding.owner_epoch,
                "binding_revision": binding.binding_revision,
                "lease_id": binding.lease_id,
                "artifact_refs": list(artifact_refs),
                "event_refs": list(event_refs),
            }
        )
        key = idempotency_key or new_workspace_id("rebind-key")
        claim, created = self.store.claim_idempotency(
            namespace="workspace-rebind",
            key=key,
            fingerprint=fingerprint,
            workspace_id=binding.workspace_id,
        )
        if not created and str(claim.get("result_ref") or ""):
            record = self.store.require_rebind(str(claim["result_ref"]))
            if record.state is not RebindState.COMMITTED:
                raise WorkspaceError(
                    WorkspaceErrorCode.OPERATION_IN_PROGRESS,
                    "Workspace rebind idempotency record is not committed.",
                    workspace_id=binding.workspace_id,
                    operation="rebind_workspace",
                    actual=record.state.value,
                )
            return self._replay(record, worker_id=worker_id)
        rebind_id = new_workspace_id("rebind")
        record = WorkspaceRebindRecord(
            rebind_id=rebind_id,
            workspace_id=binding.workspace_id,
            source_endpoint_id=source_endpoint_id,
            target_endpoint_id=target_endpoint.endpoint_id,
            state=RebindState.REQUESTED,
            owner_epoch_before=binding.owner_epoch,
            owner_epoch_after=0,
            binding_revision_before=binding.binding_revision,
            binding_revision_after=0,
            lease_id_before=binding.lease_id,
            lease_id_after="",
            idempotency_key=key,
            metadata={
                "state_owner": "WorkspaceManagerRuntime/WorkspaceRebindRuntime",
                "binding_cas_commit_point": True,
                "source_physical_location_public": False,
                "target_physical_location_public": False,
            },
        )
        record = self.store.create_rebind(record)
        target_root: Path | None = None
        committed = False
        source_root: Path | None = None
        snapshot_id = ""
        with self.store.workspace_locks.acquire_many((binding.workspace_id,)):
            try:
                current = self.manager.store.require_binding(binding.workspace_id)
                self._validate_access(access, current)
                source_root = self.manager.backend.physical_root(current)
                frozen = self.manager.freeze_for_integration(
                    current.workspace_id,
                    reason="rebind_in_progress",
                    causation_id=causation_id or rebind_id,
                )
                record = self.store.update_rebind(
                    record.advance(
                        RebindState.FROZEN,
                        metadata={
                            **dict(record.metadata),
                            "source_root_identity": file_identity(source_root),
                        },
                    ),
                    expected_revision=record.revision,
                )
                snapshot = self.manager.snapshot(
                    frozen.workspace_id,
                    causation_id=causation_id or rebind_id,
                    include_dirty_state=True,
                )
                snapshot_id = snapshot.snapshot_id
                record = self.store.update_rebind(
                    record.advance(RebindState.SNAPSHOTTED, snapshot_id=snapshot_id),
                    expected_revision=record.revision,
                )
                refreshed = self.manager.store.require_binding(current.workspace_id)
                task_root = self.manager.backend.mount_root(refreshed, WorkspaceKind.TASK)
                dirty_runtime = WorkspaceDirtyStateRuntime(
                    workspace_id=refreshed.workspace_id,
                    workspace_root=task_root,
                    ownership_store=self.manager.ownership_store,
                    path_policy=self.manager.backend.path_policy,
                )
                baseline = dirty_runtime.capture_baseline(
                    owner_epoch=refreshed.owner_epoch,
                    snapshot_id=snapshot_id,
                )
                record = self.store.update_rebind(
                    record.advance(
                        RebindState.DIRTY_VALIDATED,
                        baseline_id=baseline.baseline_id,
                        metadata={
                            **dict(record.metadata),
                            "dirty_path_count": len(baseline.dirty_paths),
                            "nested_repository_count": len(baseline.repository_refs),
                            "dirty_owner_check": "passed",
                        },
                    ),
                    expected_revision=record.revision,
                )
                target_location = self._target_location(target_endpoint, refreshed.workspace_id)
                target_root = self._location_root(target_location)
                if target_root.exists():
                    if any(target_root.iterdir()):
                        raise WorkspaceError(
                            WorkspaceErrorCode.ALREADY_EXISTS,
                            "Workspace rebind target already contains data.",
                            workspace_id=refreshed.workspace_id,
                            operation="rebind_workspace",
                        )
                else:
                    target_root.mkdir(parents=True)
                provisional = replace(
                    refreshed,
                    location=target_location,
                    capability_revision=target_endpoint.capability_revision,
                    metadata={
                        **dict(refreshed.metadata),
                        "endpoint_id": target_endpoint.endpoint_id,
                        "rebind_id": rebind_id,
                    },
                )
                self._provision_target(provisional, target_root)
                target_task_root = self.manager.backend.mount_root(
                    provisional,
                    WorkspaceKind.TASK,
                )
                restore = self.manager.snapshot_runtime.restore(
                    snapshot=snapshot,
                    workspace_id=refreshed.workspace_id,
                    owner_epoch=refreshed.owner_epoch,
                    target_root=target_task_root,
                    preserve_displaced=False,
                )
                if restore.snapshot.snapshot_id != snapshot.snapshot_id or restore.cleanup_pending:
                    raise WorkspaceError(
                        WorkspaceErrorCode.RESTORE_FAILED,
                        "Workspace rebind target snapshot did not reach a clean restored state.",
                        workspace_id=refreshed.workspace_id,
                        operation="rebind_workspace",
                    )
                record = self.store.update_rebind(
                    record.advance(RebindState.MATERIALIZED),
                    expected_revision=record.revision,
                )
                repository_roots = tuple(item.relative_root for item in baseline.repository_refs)
                source_manifest = scan_workspace_tree(
                    task_root,
                    workspace_id=refreshed.workspace_id,
                    owner_epoch=refreshed.owner_epoch,
                    repository_roots=repository_roots,
                    source="rebind_source",
                    limits=self.scan_limits,
                )
                target_manifest = scan_workspace_tree(
                    target_task_root,
                    workspace_id=refreshed.workspace_id,
                    owner_epoch=refreshed.owner_epoch,
                    repository_roots=repository_roots,
                    source="rebind_target",
                    limits=self.scan_limits,
                )
                self.store.put_manifest(source_manifest)
                self.store.put_manifest(target_manifest)
                if _content_projection(source_manifest) != _content_projection(target_manifest):
                    raise WorkspaceError(
                        WorkspaceErrorCode.RESTORE_CONFLICT,
                        "Workspace rebind target does not match the source manifest.",
                        workspace_id=refreshed.workspace_id,
                        operation="rebind_workspace",
                        expected=source_manifest.manifest_id,
                        actual=target_manifest.manifest_id,
                    )
                record = self.store.update_rebind(
                    record.advance(
                        RebindState.VERIFIED,
                        metadata={
                            **dict(record.metadata),
                            "source_manifest_id": source_manifest.manifest_id,
                            "target_manifest_id": target_manifest.manifest_id,
                            "content_verified": True,
                        },
                    ),
                    expected_revision=record.revision,
                )
                migration = WorkspaceReferenceMigration(
                    migration_id=new_workspace_id("workspace-ref-migration"),
                    workspace_id=refreshed.workspace_id,
                    source_endpoint_id=source_endpoint_id,
                    target_endpoint_id=target_endpoint.endpoint_id,
                    source_owner_epoch=refreshed.owner_epoch,
                    target_owner_epoch=refreshed.owner_epoch + 1,
                    snapshot_id=snapshot_id,
                    artifact_refs=tuple(artifact_refs),
                    event_refs=tuple(event_refs),
                )
                self.store.put_reference_migration(migration)
                record = self.store.update_rebind(
                    record.advance(
                        RebindState.COMMITTING,
                        migration_id=migration.migration_id,
                    ),
                    expected_revision=record.revision,
                )
                next_access = self.manager.commit_local_rebind(
                    refreshed.workspace_id,
                    expected_revision=refreshed.binding_revision,
                    expected_owner_epoch=refreshed.owner_epoch,
                    target_location=target_location,
                    target_endpoint_id=target_endpoint.endpoint_id,
                    target_backend_id=refreshed.backend_id,
                    target_capability_revision=target_endpoint.capability_revision,
                    worker_id=worker_id,
                    active_snapshot_id=snapshot_id,
                    rebind_id=rebind_id,
                    causation_id=causation_id,
                )
                committed = True
                latest = self.manager.store.require_binding(refreshed.workspace_id)
                record = self.store.update_rebind(
                    record.advance(
                        RebindState.COMMITTED,
                        owner_epoch_after=latest.owner_epoch,
                        binding_revision_after=latest.binding_revision,
                        lease_id_after=latest.lease_id,
                        completed_at=utc_now(),
                        message="workspace local endpoint rebind committed by binding CAS",
                    ),
                    expected_revision=record.revision,
                )
                receipt = self.store.append_receipt(
                    WorkerWorkspaceReceipt(
                        receipt_id=new_workspace_id("workspace-receipt"),
                        workspace_id=latest.workspace_id,
                        worker_id=worker_id,
                        operation=IntegrationOperation.REBIND,
                        ok=True,
                        owner_epoch=latest.owner_epoch,
                        binding_revision=latest.binding_revision,
                        lease_id=latest.lease_id,
                        rebind_id=rebind_id,
                        artifact_refs=tuple(artifact_refs),
                        summary="workspace rebound to local endpoint with stale capability fencing",
                        metadata={
                            "source_endpoint_id": source_endpoint_id,
                            "target_endpoint_id": target_endpoint.endpoint_id,
                            "snapshot_id": snapshot_id,
                            "migration_id": migration.migration_id,
                        },
                    )
                )
                self.store.complete_idempotency(
                    namespace="workspace-rebind",
                    key=key,
                    fingerprint=fingerprint,
                    result_ref=rebind_id,
                )
                self.manager.emit_integration_event(
                    "workspace.rebind.committed",
                    latest.workspace_id,
                    causation_id=causation_id,
                    metadata={
                        "rebind_id": rebind_id,
                        "source_endpoint_id": source_endpoint_id,
                        "target_endpoint_id": target_endpoint.endpoint_id,
                        "migration_id": migration.migration_id,
                        "receipt_id": receipt.receipt_id,
                    },
                )
                self._cleanup_old_root_after_commit(
                    record=record,
                    source_root=source_root,
                    target_root=target_root,
                )
                return WorkspaceRebindResult(
                    record=record,
                    access=next_access,
                    receipt=receipt,
                    migration=migration,
                )
            except Exception as error:
                self._handle_failure(
                    record=record,
                    error=error,
                    committed=committed,
                    source_root=source_root,
                    target_root=target_root,
                    snapshot_id=snapshot_id,
                    worker_id=worker_id,
                    causation_id=causation_id,
                )
                raise

    def recover_on_startup(self) -> tuple[WorkspaceRecoveryInput, ...]:
        recoveries: list[WorkspaceRecoveryInput] = []
        for rebind_id in self.store.interrupted_operations().get("rebinds", ()):
            record = self.store.require_rebind(rebind_id)
            binding = self.manager.store.require_binding(record.workspace_id)
            cas_committed = (
                binding.owner_epoch > record.owner_epoch_before
                and str(binding.metadata.get("rebind_id") or "") == record.rebind_id
            )
            severity = (
                RecoverySeverity.RETRYABLE
                if not cas_committed
                else RecoverySeverity.RECOVERY_INPUT
            )
            recovery = WorkspaceRecoveryInput(
                recovery_input_id=new_workspace_id("workspace-recovery-input"),
                workspace_id=record.workspace_id,
                operation=IntegrationOperation.REBIND,
                severity=severity,
                reason_code=(
                    "rebind_interrupted_before_cas"
                    if not cas_committed
                    else "rebind_interrupted_after_cas"
                ),
                owner_epoch=binding.owner_epoch,
                binding_revision=binding.binding_revision,
                rebind_id=record.rebind_id,
                snapshot_id=record.snapshot_id,
                retryable=True,
                evidence_refs=(f"workspace-rebind://{record.rebind_id}",),
                recommended_actions=(
                    ("resume_source", "cleanup_target")
                    if not cas_committed
                    else ("retain_target", "cleanup_source")
                ),
                metadata={
                    "rebind_state": record.state.value,
                    "binding_cas_committed": cas_committed,
                    "rollback_to_old_execution_ref": False,
                },
            )
            recoveries.append(self.store.put_recovery_input(recovery))
        return tuple(recoveries)

    def _handle_failure(
        self,
        *,
        record: WorkspaceRebindRecord,
        error: Exception,
        committed: bool,
        source_root: Path | None,
        target_root: Path | None,
        snapshot_id: str,
        worker_id: str,
        causation_id: str,
    ) -> None:
        normalized = error_from_exception(
            error,
            workspace_id=record.workspace_id,
            operation="rebind_workspace",
        )
        latest_record = self.store.require_rebind(record.rebind_id)
        rolling = latest_record.advance(
            RebindState.ROLLING_BACK,
            error_code=normalized.code.value,
            error_type=type(error).__name__,
            message=(
                "post-CAS rebind cleanup failed; target remains canonical"
                if committed
                else "pre-CAS rebind failed; removing target and reopening source"
            ),
        )
        latest_record = self.store.update_rebind(rolling, expected_revision=latest_record.revision)
        try:
            if not committed:
                if target_root is not None and target_root.exists():
                    safe_remove_private_tree(target_root, allowed_root=self.manager.config.data_root)
                access = self.manager.resume_after_integration_failure(
                    record.workspace_id,
                    worker_id=worker_id,
                    reason="rebind_rolled_back_before_cas",
                    causation_id=causation_id,
                )
                binding = self.manager.store.require_binding(record.workspace_id)
                rolled = latest_record.advance(
                    RebindState.ROLLED_BACK,
                    owner_epoch_after=binding.owner_epoch,
                    binding_revision_after=binding.binding_revision,
                    lease_id_after=binding.lease_id,
                    completed_at=utc_now(),
                    rollback_snapshot_id=snapshot_id,
                    message="pre-CAS rebind rolled back; source remains canonical",
                )
                self.store.update_rebind(rolled, expected_revision=latest_record.revision)
            else:
                binding = self.manager.store.require_binding(record.workspace_id)
                recovery = WorkspaceRecoveryInput(
                    recovery_input_id=new_workspace_id("workspace-recovery-input"),
                    workspace_id=record.workspace_id,
                    operation=IntegrationOperation.REBIND,
                    severity=RecoverySeverity.RECOVERY_INPUT,
                    reason_code="rebind_post_cas_cleanup_failed",
                    owner_epoch=binding.owner_epoch,
                    binding_revision=binding.binding_revision,
                    rebind_id=record.rebind_id,
                    snapshot_id=snapshot_id,
                    retryable=True,
                    evidence_refs=(f"workspace-rebind://{record.rebind_id}",),
                    recommended_actions=("retain_target", "cleanup_source"),
                    metadata={
                        "binding_cas_committed": True,
                        "target_remains_canonical": True,
                        "rollback_to_old_execution_ref": False,
                    },
                )
                recovery = self.store.put_recovery_input(recovery)
                failed = latest_record.advance(
                    RebindState.FAILED,
                    owner_epoch_after=binding.owner_epoch,
                    binding_revision_after=binding.binding_revision,
                    lease_id_after=binding.lease_id,
                    completed_at=utc_now(),
                    recovery_input_id=recovery.recovery_input_id,
                    message="target binding committed; source cleanup requires recovery",
                )
                self.store.update_rebind(failed, expected_revision=latest_record.revision)
        except Exception as rollback_error:
            binding = self.manager.store.require_binding(record.workspace_id)
            recovery = WorkspaceRecoveryInput(
                recovery_input_id=new_workspace_id("workspace-recovery-input"),
                workspace_id=record.workspace_id,
                operation=IntegrationOperation.REBIND,
                severity=RecoverySeverity.BACKEND_UNAVAILABLE_CANDIDATE,
                reason_code="rebind_rollback_failed",
                owner_epoch=binding.owner_epoch,
                binding_revision=binding.binding_revision,
                rebind_id=record.rebind_id,
                snapshot_id=snapshot_id,
                retryable=True,
                evidence_refs=(f"workspace-rebind://{record.rebind_id}",),
                recommended_actions=("inspect_binding", "verify_snapshot", "replan_backend_placement"),
                metadata={
                    "original_error_code": normalized.code.value,
                    "rollback_error_type": type(rollback_error).__name__,
                    "binding_cas_committed": committed,
                },
            )
            self.store.put_recovery_input(recovery)

    def _cleanup_old_root_after_commit(
        self,
        *,
        record: WorkspaceRebindRecord,
        source_root: Path,
        target_root: Path,
    ) -> None:
        if source_root == target_root:
            return
        try:
            quarantine = self.manager.config.data_root / ".rebind-retired" / record.rebind_id
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            if quarantine.exists():
                safe_remove_private_tree(quarantine, allowed_root=self.manager.config.data_root)
            os.replace(source_root, quarantine)
            safe_remove_private_tree(quarantine, allowed_root=self.manager.config.data_root)
        except Exception as error:
            binding = self.manager.store.require_binding(record.workspace_id)
            self.store.put_recovery_input(
                WorkspaceRecoveryInput(
                    recovery_input_id=new_workspace_id("workspace-recovery-input"),
                    workspace_id=record.workspace_id,
                    operation=IntegrationOperation.REBIND,
                    severity=RecoverySeverity.RETRYABLE,
                    reason_code="rebind_retired_source_cleanup_failed",
                    owner_epoch=binding.owner_epoch,
                    binding_revision=binding.binding_revision,
                    rebind_id=record.rebind_id,
                    snapshot_id=record.snapshot_id,
                    retryable=True,
                    evidence_refs=(f"workspace-rebind://{record.rebind_id}",),
                    recommended_actions=("retain_target", "cleanup_retired_source"),
                    metadata={
                        "error_type": type(error).__name__,
                        "target_remains_canonical": True,
                    },
                )
            )

    def _provision_target(self, binding: Any, target_root: Path) -> None:
        marker = {
            "schema": WORKSPACE_MARKER_SCHEMA,
            "workspace_id": binding.workspace_id,
            "backend_id": binding.backend_id,
            "backend_kind": binding.backend_kind.value,
            "location_token": binding.location.root_token,
            "workspace_kind": binding.workspace_kind.value,
            "owner_epoch": binding.owner_epoch,
            "capability_revision": binding.capability_revision,
            "created_at": utc_now(),
            "rebind_provisional": True,
        }
        atomic_write_json(target_root / ".zyra-workspace.json", marker)
        for mount in self.manager.store.get_mounts(binding.workspace_id):
            mount_root = target_root.joinpath(*Path(mount.relative_root).parts)
            mount_root.mkdir(parents=True, exist_ok=True)
            if mount_root.is_symlink():
                raise WorkspaceError(
                    WorkspaceErrorCode.SYMLINK_ESCAPE,
                    "Workspace rebind target mount cannot be a symlink.",
                    workspace_id=binding.workspace_id,
                    operation="provision_rebind_target",
                )

    def _ensure_default_endpoint(self) -> None:
        if self.disabled or self.store.disabled:
            return
        endpoint_id = self.manager.config.default_backend_id
        existing = self.store.get_endpoint(endpoint_id)
        if existing is not None:
            return
        self.register_endpoint(
            endpoint_id,
            relative_root="tasks",
            metadata={"default": True},
        )

    def _target_location(
        self,
        endpoint: LocalWorkspaceEndpoint,
        workspace_id: str,
    ) -> LocalWorkspaceLocation:
        relative = f"{endpoint.relative_root.strip('/')}/{workspace_id[:5]}/{workspace_id}"
        return LocalWorkspaceLocation(
            relative_root=relative,
            root_token=endpoint.root_token,
            platform=os.name,
        )

    def _endpoint_root(self, endpoint: LocalWorkspaceEndpoint) -> Path:
        candidate = self.manager.config.data_root.joinpath(*Path(endpoint.relative_root).parts).resolve()
        try:
            candidate.relative_to(self.manager.config.data_root)
        except ValueError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                "Local workspace endpoint escaped the configured data root.",
                operation="resolve_workspace_endpoint",
            ) from error
        return candidate

    def _location_root(self, location: LocalWorkspaceLocation) -> Path:
        candidate = self.manager.config.data_root.joinpath(*Path(location.relative_root).parts).resolve()
        try:
            candidate.relative_to(self.manager.config.data_root)
        except ValueError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                "Workspace rebind location escaped the configured data root.",
                operation="resolve_rebind_location",
            ) from error
        return candidate

    def _validate_access(self, access: WorkspaceAccessHandle, binding: Any) -> None:
        if (
            access.workspace_id != binding.workspace_id
            or access.owner_epoch != binding.owner_epoch
            or access.lease_id != binding.lease_id
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.OWNER_EPOCH_STALE,
                "Workspace rebind access is stale.",
                workspace_id=binding.workspace_id,
                operation="rebind_workspace",
            )
        lease = self.manager.store.get_lease(access.lease_id)
        if lease is None:
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_NOT_FOUND,
                "Workspace rebind access references a missing lease.",
                workspace_id=binding.workspace_id,
                operation="rebind_workspace",
            )
        self.manager.backend.validate_lease(
            binding,
            lease,
            fence_token=access.fence_token,
            operation=WorkspaceOperation.WRITE,
        )

    def _replay(self, record: WorkspaceRebindRecord, *, worker_id: str) -> WorkspaceRebindResult:
        access = self.manager.acquire_for_worker(
            task_id=self.manager.store.require_binding(record.workspace_id).task_id,
            session_id="",
            worker_id=worker_id,
        )
        receipts = self.store.list_receipts(record.workspace_id, rebind_id=record.rebind_id)
        migrations = self.store.list_reference_migrations(record.workspace_id)
        receipt_value = next(
            (item for item in reversed(receipts) if item.get("rebind_id") == record.rebind_id),
            None,
        )
        migration_value = next(
            (item for item in reversed(migrations) if item.get("migration_id") == record.migration_id),
            None,
        )
        if receipt_value is None or migration_value is None:
            raise WorkspaceError(
                WorkspaceErrorCode.STORE_CORRUPT,
                "Committed workspace rebind is missing its receipt or reference migration.",
                workspace_id=record.workspace_id,
                operation="replay_workspace_rebind",
            )
        from .transactions import _receipt_from_dict

        migration = WorkspaceReferenceMigration(
            migration_id=migration_value["migration_id"],
            workspace_id=migration_value["workspace_id"],
            source_endpoint_id=migration_value["source_endpoint_id"],
            target_endpoint_id=migration_value["target_endpoint_id"],
            source_owner_epoch=migration_value["source_owner_epoch"],
            target_owner_epoch=migration_value["target_owner_epoch"],
            snapshot_id=migration_value["snapshot_id"],
            artifact_refs=tuple(migration_value.get("artifact_refs") or ()),
            event_refs=tuple(migration_value.get("event_refs") or ()),
            completed_at=migration_value.get("completed_at", ""),
        )
        return WorkspaceRebindResult(
            record=record,
            access=access,
            receipt=_receipt_from_dict(receipt_value),
            migration=migration,
            idempotent_replay=True,
        )

    def _require_enabled(self, workspace_id: str) -> None:
        if self.disabled:
            raise WorkspaceError(
                WorkspaceErrorCode.DISABLED,
                "Workspace rebind runtime is disabled; binding fallback is forbidden.",
                workspace_id=workspace_id,
                operation="workspace_rebind_runtime",
            )


def _content_projection(manifest: Any) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (item.logical_path, item.kind.value, item.content_hash, item.size, item.repository_root)
        for item in manifest.entries
    )


__all__ = [
    "WorkspaceRebindResult",
    "WorkspaceRebindRuntime",
]
