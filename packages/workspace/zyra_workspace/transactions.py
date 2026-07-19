from __future__ import annotations

"""Workspace read/edit gateway and rollback-capable patch transactions."""

import hashlib
import json
import os
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .atomic import sha256_bytes
from .dirty_state import WorkspaceDirtyStateRuntime
from .errors import WorkspaceError, WorkspaceErrorCode, error_from_exception
from .integration_models import (
    ArtifactPublication,
    IntegrationOperation,
    IntegrationPhase,
    MutationKind,
    MutationPathResult,
    RecoverySeverity,
    WorkerWorkspaceReceipt,
    WorkspaceMutation,
    WorkspaceMutationPlan,
    WorkspaceReadEvidence,
    WorkspaceRecoveryInput,
    WorkspaceTransactionRecord,
)
from .integration_store import WorkspaceIntegrationStore
from .local_backend import WorkspaceAccessHandle
from .models import (
    WorkspaceKind,
    WorkspaceLifecycleState,
    WorkspaceOperation,
    WorkspaceReadMode,
    new_workspace_id,
    stable_digest,
    utc_now,
)


class ArtifactWriterPort(Protocol):
    def write_bytes(
        self,
        *,
        run_id: str,
        task_id: str,
        content: bytes,
        title: str,
        kind: Any,
        extension: str,
        producer_node_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


class WorkspaceManagerPort(Protocol):
    store: Any
    backend: Any
    ownership_store: Any
    integration_store: WorkspaceIntegrationStore

    def snapshot(self, workspace_id: str, *, causation_id: str = "", include_dirty_state: bool = True) -> Any: ...
    def restore(self, workspace_id: str, snapshot_id: str, *, causation_id: str = "") -> Any: ...
    def acquire_for_worker(
        self,
        *,
        task_id: str,
        session_id: str,
        worker_id: str,
    ) -> WorkspaceAccessHandle: ...
    def rotate_after_integration(
        self,
        workspace_id: str,
        *,
        worker_id: str,
        active_snapshot_id: str = "",
        causation_id: str = "",
        reason: str = "integration_commit",
    ) -> WorkspaceAccessHandle: ...
    def emit_integration_event(
        self,
        event_type: str,
        workspace_id: str,
        *,
        causation_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class GatewayReadResult:
    workspace_id: str
    logical_path: str
    content: bytes
    evidence: WorkspaceReadEvidence
    exists: bool
    mount_kind: WorkspaceKind = WorkspaceKind.TASK

    def text(self, encoding: str = "utf-8") -> str:
        return self.content.decode(encoding)

    def to_public_dict(self, *, include_content: bool = True, encoding: str = "utf-8") -> dict[str, Any]:
        result: dict[str, Any] = {
            "workspace_id": self.workspace_id,
            "logical_path": self.logical_path,
            "exists": self.exists,
            "mount_kind": self.mount_kind.value,
            "content_bytes": len(self.content),
            "read_evidence": self.evidence.to_dict(),
            "physical_location_redacted": True,
        }
        if include_content:
            result["content"] = self.content.decode(encoding)
            result["encoding"] = encoding
        return result


@dataclass(frozen=True, slots=True)
class GatewayMutationResult:
    transaction: WorkspaceTransactionRecord
    receipt: WorkerWorkspaceReceipt
    access: WorkspaceAccessHandle
    artifact_refs: tuple[Any, ...] = ()
    publications: tuple[ArtifactPublication, ...] = ()
    idempotent_replay: bool = False

    @property
    def ok(self) -> bool:
        return self.transaction.ok and self.receipt.ok

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "transaction": self.transaction.to_dict(),
            "receipt": self.receipt.to_dict(),
            "workspace_access": self.access.to_public_dict(),
            "artifact_refs": [
                {
                    "artifact_id": str(getattr(item, "artifact_id", "")),
                    "uri": f"artifact://{getattr(item, 'artifact_id', '')}",
                    "kind": str(getattr(item, "kind", "")),
                }
                for item in self.artifact_refs
            ],
            "publications": [item.to_dict() for item in self.publications],
            "idempotent_replay": self.idempotent_replay,
            "physical_location_redacted": True,
        }


class WorkspacePatchTransactionRuntime:
    """Apply a plan through the 05A owners with snapshot rollback.

    The integration journal is written before the first filesystem mutation.
    A committed before-snapshot is retained as the undo reference.  All
    participating readers/writers use the shared per-workspace coordination
    lock.  A failure after the first mutation restores that snapshot and
    rotates the owner epoch; a failed restore emits a durable recovery input.
    """

    def __init__(
        self,
        manager: WorkspaceManagerPort,
        *,
        artifact_store: ArtifactWriterPort | None = None,
        disabled: bool = False,
    ) -> None:
        self.manager = manager
        self.store = manager.integration_store
        self.artifact_store = artifact_store
        self.disabled = bool(disabled)

    def execute(
        self,
        access: WorkspaceAccessHandle,
        plan: WorkspaceMutationPlan,
        *,
        run_id: str,
        task_id: str,
        node_id: str = "",
    ) -> GatewayMutationResult:
        self._require_enabled(plan.workspace_id)
        if access.workspace_id != plan.workspace_id:
            raise WorkspaceError(
                WorkspaceErrorCode.BINDING_STALE,
                "Workspace mutation plan does not match its access capability.",
                workspace_id=plan.workspace_id,
                operation="execute_workspace_patch",
                expected=access.workspace_id,
                actual=plan.workspace_id,
            )
        fingerprint = str(plan.metadata.get("request_digest") or plan.digest)
        idempotency_key = plan.idempotency_key or plan.transaction_id
        claim, created = self.store.claim_idempotency(
            namespace="workspace-patch",
            key=idempotency_key,
            fingerprint=fingerprint,
            workspace_id=plan.workspace_id,
            result_ref=plan.transaction_id,
        )
        if not created and str(claim.get("result_ref") or ""):
            existing = self.store.require_transaction(str(claim["result_ref"]))
            if existing.ok:
                receipt_values = self.store.list_receipts(
                    plan.workspace_id,
                    transaction_id=existing.transaction_id,
                )
                if not receipt_values:
                    raise WorkspaceError(
                        WorkspaceErrorCode.STORE_CORRUPT,
                        "Committed workspace transaction has no worker receipt.",
                        workspace_id=plan.workspace_id,
                        operation="replay_workspace_patch",
                    )
                receipt = _receipt_from_dict(receipt_values[-1])
                latest_binding = self.manager.store.require_binding(plan.workspace_id)
                latest_access = self.manager.acquire_for_worker(
                    task_id=latest_binding.task_id,
                    session_id=latest_binding.session_id,
                    worker_id=plan.worker_id,
                )
                return GatewayMutationResult(
                    transaction=existing,
                    receipt=receipt,
                    access=latest_access,
                    idempotent_replay=True,
                )
            raise WorkspaceError(
                (
                    WorkspaceErrorCode.OPERATION_IN_PROGRESS
                    if not existing.terminal
                    else WorkspaceErrorCode.IDEMPOTENCY_CONFLICT
                ),
                "Workspace patch idempotency key is already bound to an uncommitted operation.",
                workspace_id=plan.workspace_id,
                operation="replay_workspace_patch",
                metadata={
                    "transaction_id": existing.transaction_id,
                    "phase": existing.phase.value,
                },
            )
        with self.store.workspace_locks.acquire_many((plan.workspace_id,)):
            binding = self._validate_capability(access, plan)
            record = WorkspaceTransactionRecord(
                transaction_id=plan.transaction_id,
                workspace_id=plan.workspace_id,
                operation=IntegrationOperation.PATCH,
                phase=IntegrationPhase.REQUESTED,
                owner_epoch_before=binding.owner_epoch,
                owner_epoch_after=0,
                binding_revision_before=binding.binding_revision,
                binding_revision_after=0,
                lease_id_before=binding.lease_id,
                lease_id_after="",
                plan_digest=plan.digest,
                idempotency_key=idempotency_key,
                metadata={
                    **dict(plan.metadata),
                    "state_owner": "WorkspaceManagerRuntime/WorkspacePatchTransactionRuntime",
                    "read_set_count": len(plan.evidence),
                    "write_set_count": len(plan.mutations),
                    "rollback_contract": "committed_snapshot",
                },
            )
            record = self.store.create_transaction(record)
            if record.ok:
                receipts = self.store.list_receipts(plan.workspace_id, transaction_id=record.transaction_id)
                if not receipts:
                    raise WorkspaceError(
                        WorkspaceErrorCode.STORE_CORRUPT,
                        "Committed workspace transaction is missing a receipt.",
                        workspace_id=plan.workspace_id,
                        operation="replay_workspace_patch",
                    )
                return GatewayMutationResult(
                    transaction=record,
                    receipt=_receipt_from_dict(receipts[-1]),
                    access=access,
                    idempotent_replay=True,
                )
            snapshot_id = ""
            mutation_started = False
            ownership_before: tuple[Any, ...] = ()
            path_results: list[MutationPathResult] = []
            artifact_refs: list[Any] = []
            publications: list[ArtifactPublication] = []
            try:
                validating = record.advance(IntegrationPhase.VALIDATING)
                record = self.store.update_transaction(validating, expected_revision=record.revision)
                self._preflight(access, plan)
                snapshot = self.manager.snapshot(
                    plan.workspace_id,
                    causation_id=plan.causation_id or plan.transaction_id,
                    include_dirty_state=True,
                )
                snapshot_id = str(snapshot.snapshot_id)
                staging = record.advance(
                    IntegrationPhase.STAGING,
                    snapshot_id=snapshot_id,
                    metadata={
                        **dict(record.metadata),
                        "undo_snapshot_committed": True,
                        "undo_snapshot_id": snapshot_id,
                    },
                )
                record = self.store.update_transaction(staging, expected_revision=record.revision)
                self._preflight(access, plan, allow_binding_revision_advance=True)
                ownership_before = tuple(self.manager.ownership_store.list(plan.workspace_id))
                applying = record.advance(IntegrationPhase.APPLYING)
                record = self.store.update_transaction(applying, expected_revision=record.revision)
                for mutation in plan.mutations:
                    mutation_started = True
                    result = self._apply_one(access, plan, mutation)
                    path_results.append(result)
                    if plan.publish_artifact and mutation.mutates_bytes:
                        publication, artifact = self._publish_mutation(
                            plan=plan,
                            mutation=mutation,
                            run_id=run_id,
                            task_id=task_id,
                            node_id=node_id,
                        )
                        publications.append(self.store.put_publication(publication))
                        artifact_refs.append(artifact)
                verifying = record.advance(
                    IntegrationPhase.VERIFYING,
                    path_results=tuple(path_results),
                    artifact_refs=tuple(
                        f"artifact://{getattr(item, 'artifact_id', '')}" for item in artifact_refs
                    ),
                )
                record = self.store.update_transaction(verifying, expected_revision=record.revision)
                self._verify_results(access, plan, path_results)
                committing = record.advance(IntegrationPhase.COMMITTING)
                record = self.store.update_transaction(committing, expected_revision=record.revision)
                next_access = self.manager.rotate_after_integration(
                    plan.workspace_id,
                    worker_id=plan.worker_id,
                    active_snapshot_id=snapshot_id,
                    causation_id=plan.causation_id or plan.transaction_id,
                    reason="patch_transaction_committed",
                )
                latest = self.manager.store.require_binding(plan.workspace_id)
                committed = record.advance(
                    IntegrationPhase.COMMITTED,
                    owner_epoch_after=latest.owner_epoch,
                    binding_revision_after=latest.binding_revision,
                    lease_id_after=latest.lease_id,
                    completed_at=utc_now(),
                    message="workspace patch transaction committed",
                )
                record = self.store.update_transaction(committed, expected_revision=record.revision)
                receipt = self.store.append_receipt(
                    WorkerWorkspaceReceipt(
                        receipt_id=new_workspace_id("workspace-receipt"),
                        workspace_id=plan.workspace_id,
                        worker_id=plan.worker_id,
                        operation=IntegrationOperation.PATCH,
                        ok=True,
                        owner_epoch=latest.owner_epoch,
                        binding_revision=latest.binding_revision,
                        lease_id=latest.lease_id,
                        transaction_id=plan.transaction_id,
                        logical_paths=tuple(item.logical_path for item in plan.mutations),
                        artifact_refs=tuple(
                            f"artifact://{getattr(item, 'artifact_id', '')}" for item in artifact_refs
                        ),
                        summary="workspace patch committed through lease and read-precondition gateway",
                        metadata={
                            "snapshot_id": snapshot_id,
                            "path_result_count": len(path_results),
                            "downstream_consumers": ["M1-05C", "M1-07C"],
                        },
                    )
                )
                self.store.complete_idempotency(
                    namespace="workspace-patch",
                    key=idempotency_key,
                    fingerprint=fingerprint,
                    result_ref=record.transaction_id,
                )
                self.manager.emit_integration_event(
                    "workspace.patch.committed",
                    plan.workspace_id,
                    causation_id=plan.causation_id,
                    metadata={
                        "transaction_id": plan.transaction_id,
                        "receipt_id": receipt.receipt_id,
                        "snapshot_id": snapshot_id,
                        "path_count": len(path_results),
                    },
                )
                return GatewayMutationResult(
                    transaction=record,
                    receipt=receipt,
                    access=next_access,
                    artifact_refs=tuple(artifact_refs),
                    publications=tuple(publications),
                )
            except Exception as error:
                return self._rollback_or_raise(
                    access=access,
                    plan=plan,
                    record=record,
                    snapshot_id=snapshot_id,
                    mutation_started=mutation_started,
                    path_results=tuple(path_results),
                    ownership_before=ownership_before,
                    error=error,
                )

    def _validate_capability(
        self,
        access: WorkspaceAccessHandle,
        plan: WorkspaceMutationPlan,
        *,
        allow_binding_revision_advance: bool = False,
    ) -> Any:
        binding = self.manager.store.require_binding(plan.workspace_id)
        if binding.lifecycle_state is not WorkspaceLifecycleState.OPEN:
            raise WorkspaceError(
                WorkspaceErrorCode.WRITE_FROZEN,
                "Workspace patch requires an open binding.",
                workspace_id=plan.workspace_id,
                operation="validate_workspace_patch",
                actual=binding.lifecycle_state.value,
            )
        if (
            binding.owner_epoch != plan.owner_epoch
            or binding.owner_epoch != access.owner_epoch
            or (
                binding.binding_revision != plan.binding_revision
                and not (
                    allow_binding_revision_advance
                    and binding.binding_revision > plan.binding_revision
                )
            )
            or binding.lease_id != plan.lease_id
            or binding.lease_id != access.lease_id
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.OWNER_EPOCH_STALE,
                "Workspace patch capability is stale.",
                workspace_id=plan.workspace_id,
                operation="validate_workspace_patch",
                expected={
                    "owner_epoch": binding.owner_epoch,
                    "binding_revision": binding.binding_revision,
                    "lease_id": binding.lease_id,
                },
                actual={
                    "owner_epoch": plan.owner_epoch,
                    "binding_revision": plan.binding_revision,
                    "lease_id": plan.lease_id,
                },
            )
        lease = self.manager.store.get_lease(access.lease_id)
        if lease is None:
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_NOT_FOUND,
                "Workspace patch capability references a missing lease.",
                workspace_id=plan.workspace_id,
                operation="validate_workspace_patch",
            )
        self.manager.backend.validate_lease(
            binding,
            lease,
            fence_token=access.fence_token,
            operation=WorkspaceOperation.WRITE,
        )
        if access.worker_id != plan.worker_id:
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_OWNER_MISMATCH,
                "Workspace patch worker does not own the active lease.",
                workspace_id=plan.workspace_id,
                operation="validate_workspace_patch",
                expected=access.worker_id,
                actual=plan.worker_id,
            )
        return binding

    def _preflight(
        self,
        access: WorkspaceAccessHandle,
        plan: WorkspaceMutationPlan,
        *,
        allow_binding_revision_advance: bool = False,
    ) -> None:
        binding = self._validate_capability(
            access,
            plan,
            allow_binding_revision_advance=allow_binding_revision_advance,
        )
        evidence_by_id = plan.evidence_by_id()
        seen_paths: set[str] = set()
        for mutation in plan.mutations:
            canonical, _parts = self.manager.backend.path_policy.validate_logical_path(
                mutation.logical_path,
                quota=binding.quota,
            )
            collision_key = canonical.casefold()
            if collision_key in seen_paths:
                raise WorkspaceError(
                    WorkspaceErrorCode.INVALID_ARGUMENT,
                    "Workspace patch contains case-colliding paths.",
                    workspace_id=plan.workspace_id,
                    operation="preflight_workspace_patch",
                    path=canonical,
                )
            seen_paths.add(collision_key)
            if mutation.read_evidence_id:
                evidence = evidence_by_id[mutation.read_evidence_id]
                self._validate_evidence(access, mutation.logical_path, evidence)
            elif mutation.kind in {
                MutationKind.REPLACE_TEXT,
                MutationKind.DELETE_FILE,
            }:
                raise WorkspaceError(
                    WorkspaceErrorCode.READ_REQUIRED,
                    "Destructive workspace mutation requires full-read evidence.",
                    workspace_id=plan.workspace_id,
                    operation="preflight_workspace_patch",
                    path=mutation.logical_path,
                )
        total_new_bytes = sum(len(item.content) for item in plan.mutations if item.mutates_bytes)
        if total_new_bytes > binding.quota.max_bytes:
            raise WorkspaceError(
                WorkspaceErrorCode.QUOTA_EXCEEDED,
                "Workspace patch aggregate content exceeds the binding quota.",
                workspace_id=plan.workspace_id,
                operation="preflight_workspace_patch",
                expected=binding.quota.max_bytes,
                actual=total_new_bytes,
            )

    def _validate_evidence(
        self,
        access: WorkspaceAccessHandle,
        logical_path: str,
        evidence: WorkspaceReadEvidence,
    ) -> None:
        if evidence.workspace_id != access.workspace_id or evidence.logical_path != logical_path:
            raise WorkspaceError(
                WorkspaceErrorCode.READ_REQUIRED,
                "Workspace read evidence belongs to a different target.",
                workspace_id=access.workspace_id,
                operation="validate_workspace_read_evidence",
                path=logical_path,
            )
        if evidence.owner_epoch != access.owner_epoch or evidence.lease_id != access.lease_id:
            raise WorkspaceError(
                WorkspaceErrorCode.READ_EPOCH_STALE,
                "Workspace read evidence belongs to a stale lease epoch.",
                workspace_id=access.workspace_id,
                operation="validate_workspace_read_evidence",
                path=logical_path,
            )
        if not evidence.complete:
            raise WorkspaceError(
                WorkspaceErrorCode.READ_INCOMPLETE,
                "Workspace mutation requires complete read evidence.",
                workspace_id=access.workspace_id,
                operation="validate_workspace_read_evidence",
                path=logical_path,
            )
        current = self.manager.backend.read(
            access,
            mount_kind=WorkspaceKind.TASK,
            path=logical_path,
            mode=WorkspaceReadMode.FULL,
        )
        current_hash = current.record.base_hash or sha256_bytes(current.content)
        if current_hash != evidence.content_hash:
            raise WorkspaceError(
                WorkspaceErrorCode.BASE_HASH_STALE,
                "Workspace file changed after the read evidence was issued.",
                workspace_id=access.workspace_id,
                operation="validate_workspace_read_evidence",
                path=logical_path,
                expected=evidence.content_hash,
                actual=current_hash,
            )
        if current.record.file_identity != evidence.file_identity:
            raise WorkspaceError(
                WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                "Workspace file identity changed after the read evidence was issued.",
                workspace_id=access.workspace_id,
                operation="validate_workspace_read_evidence",
                path=logical_path,
                expected=evidence.file_identity,
                actual=current.record.file_identity,
            )

    def _apply_one(
        self,
        access: WorkspaceAccessHandle,
        plan: WorkspaceMutationPlan,
        mutation: WorkspaceMutation,
    ) -> MutationPathResult:
        read = self.manager.backend.read(
            access,
            mount_kind=WorkspaceKind.TASK,
            path=mutation.logical_path,
            mode=WorkspaceReadMode.FULL,
        )
        before = bytes(read.content)
        before_hash = read.record.base_hash or sha256_bytes(before)
        existed = read.record.file_identity != "absent"
        if mutation.expected_absent and existed:
            raise WorkspaceError(
                WorkspaceErrorCode.BASE_HASH_STALE,
                "Workspace mutation expected an absent path.",
                workspace_id=plan.workspace_id,
                operation="apply_workspace_patch",
                path=mutation.logical_path,
            )
        if mutation.kind is MutationKind.WRITE_TEXT:
            content = bytes(mutation.content)
            result = self.manager.backend.write_after_read(
                access,
                mount_kind=WorkspaceKind.TASK,
                path=mutation.logical_path,
                content=content,
                service="CodeWorkerRuntime",
            )
            disposition = "created" if not existed else "replaced"
        elif mutation.kind is MutationKind.WRITE_BYTES:
            content = bytes(mutation.content)
            result = self.manager.backend.write_after_read(
                access,
                mount_kind=WorkspaceKind.TASK,
                path=mutation.logical_path,
                content=content,
                service="CodeWorkerRuntime",
            )
            disposition = "created" if not existed else "replaced"
        elif mutation.kind is MutationKind.REPLACE_TEXT:
            if not existed:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_FOUND,
                    "Workspace edit target does not exist.",
                    workspace_id=plan.workspace_id,
                    operation="apply_workspace_patch",
                    path=mutation.logical_path,
                )
            text = before.decode(mutation.encoding)
            occurrences = text.count(mutation.old_text)
            if occurrences == 0:
                raise WorkspaceError(
                    WorkspaceErrorCode.BASE_HASH_STALE,
                    "Exact workspace edit text was not found.",
                    workspace_id=plan.workspace_id,
                    operation="apply_workspace_patch",
                    path=mutation.logical_path,
                    actual={"matches": 0},
                )
            if occurrences > 1 and not mutation.replace_all:
                raise WorkspaceError(
                    WorkspaceErrorCode.INVALID_ARGUMENT,
                    "Workspace edit is ambiguous without replace_all.",
                    workspace_id=plan.workspace_id,
                    operation="apply_workspace_patch",
                    path=mutation.logical_path,
                    actual={"matches": occurrences},
                )
            edited = (
                text.replace(mutation.old_text, mutation.new_text)
                if mutation.replace_all
                else text.replace(mutation.old_text, mutation.new_text, 1)
            )
            content = edited.encode(mutation.encoding)
            result = self.manager.backend.write_after_read(
                access,
                mount_kind=WorkspaceKind.TASK,
                path=mutation.logical_path,
                content=content,
                service="CodeWorkerRuntime",
            )
            disposition = f"edited:{occurrences if mutation.replace_all else 1}"
        elif mutation.kind is MutationKind.DELETE_FILE:
            if not existed:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_FOUND,
                    "Workspace delete target does not exist.",
                    workspace_id=plan.workspace_id,
                    operation="apply_workspace_patch",
                    path=mutation.logical_path,
                )
            precondition = self.manager.backend.file_state.precondition_for_latest(
                workspace_id=plan.workspace_id,
                path=read.record.path,
                owner_epoch=access.owner_epoch,
                lease_id=access.lease_id,
                require_full_read=True,
            )
            deleted = self.manager.backend.delete(
                access,
                mount_kind=WorkspaceKind.TASK,
                path=mutation.logical_path,
                precondition=precondition,
                recursive=False,
            )
            content = b""
            result = None
            disposition = f"deleted:{deleted.removed_files}"
        else:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace patch mutation kind is not supported by the transactional gateway.",
                workspace_id=plan.workspace_id,
                operation="apply_workspace_patch",
                path=mutation.logical_path,
                actual=mutation.kind.value,
            )
        dirty_runtime = WorkspaceDirtyStateRuntime(
            workspace_id=plan.workspace_id,
            workspace_root=self.manager.backend.mount_root(
                self.manager.store.require_binding(plan.workspace_id),
                WorkspaceKind.TASK,
            ),
            ownership_store=self.manager.ownership_store,
            path_policy=self.manager.backend.path_policy,
        )
        ownership_claimed = True
        try:
            dirty_runtime.claim_agent_write(
                mutation.logical_path,
                worker_id=plan.worker_id,
                operation_id=plan.transaction_id,
                base_hash=before_hash,
            )
        except WorkspaceError as ownership_error:
            if (
                ownership_error.code is WorkspaceErrorCode.DIRTY_STATE_CONFLICT
                and mutation.metadata.get("allow_user_baseline_overlay") is True
            ):
                # The child delta was computed from the immutable user-dirty
                # baseline.  Keep the user's ownership claim instead of
                # silently taking it over; the transaction receipt still
                # records the agent overlay.
                ownership_claimed = False
            else:
                raise
        after_hash = result.content_hash if result is not None else sha256_bytes(content)
        return MutationPathResult(
            logical_path=mutation.logical_path,
            kind=mutation.kind,
            before_hash=before_hash,
            after_hash=after_hash,
            bytes_before=len(before),
            bytes_after=len(content),
            disposition=disposition,
            ownership_claimed=ownership_claimed,
        )

    def _verify_results(
        self,
        access: WorkspaceAccessHandle,
        plan: WorkspaceMutationPlan,
        results: Sequence[MutationPathResult],
    ) -> None:
        result_by_path = {item.logical_path: item for item in results}
        for mutation in plan.mutations:
            expected = result_by_path[mutation.logical_path]
            observed = self.manager.backend.read(
                access,
                mount_kind=WorkspaceKind.TASK,
                path=mutation.logical_path,
                mode=WorkspaceReadMode.FULL,
            )
            if mutation.kind is MutationKind.DELETE_FILE:
                if observed.record.file_identity != "absent":
                    raise WorkspaceError(
                        WorkspaceErrorCode.RESTORE_CONFLICT,
                        "Deleted workspace path reappeared before transaction commit.",
                        workspace_id=plan.workspace_id,
                        operation="verify_workspace_patch",
                        path=mutation.logical_path,
                    )
                continue
            observed_hash = observed.record.base_hash or sha256_bytes(observed.content)
            if observed_hash != expected.after_hash:
                raise WorkspaceError(
                    WorkspaceErrorCode.BASE_HASH_STALE,
                    "Workspace patch verification observed unexpected bytes.",
                    workspace_id=plan.workspace_id,
                    operation="verify_workspace_patch",
                    path=mutation.logical_path,
                    expected=expected.after_hash,
                    actual=observed_hash,
                )

    def _publish_mutation(
        self,
        *,
        plan: WorkspaceMutationPlan,
        mutation: WorkspaceMutation,
        run_id: str,
        task_id: str,
        node_id: str,
    ) -> tuple[ArtifactPublication, Any]:
        if self.artifact_store is None:
            raise WorkspaceError(
                WorkspaceErrorCode.BACKEND_UNAVAILABLE,
                "Workspace artifact publication is required but its owner is unavailable.",
                workspace_id=plan.workspace_id,
                operation="publish_workspace_patch_artifact",
            )
        try:
            from zyra_core import ArtifactKind
        except ImportError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.BACKEND_UNAVAILABLE,
                "Artifact kind contract is unavailable.",
                workspace_id=plan.workspace_id,
                operation="publish_workspace_patch_artifact",
            ) from error
        content = mutation.content
        if mutation.kind is MutationKind.REPLACE_TEXT:
            latest = self.manager.store.get_read_record(
                plan.workspace_id,
                f"task/{mutation.logical_path}",
            )
            content_hash = latest.base_hash if latest is not None else mutation.content_digest()
            binding = self.manager.store.require_binding(plan.workspace_id)
            root = self.manager.backend.mount_root(binding, WorkspaceKind.TASK)
            canonical = self.manager.backend.path_policy.resolve(
                root=root,
                logical_path=mutation.logical_path,
                operation=WorkspaceOperation.READ,
                mount=next(
                    item for item in self.manager.store.get_mounts(plan.workspace_id)
                    if item.kind is WorkspaceKind.TASK
                ),
                require_exists=True,
            )
            content = canonical.physical_path.read_bytes()
        else:
            content_hash = sha256_bytes(content)
        extension = Path(mutation.logical_path).suffix or ".bin"
        artifact = self.artifact_store.write_bytes(
            run_id=run_id,
            task_id=task_id,
            content=bytes(content),
            title=f"Workspace patch {mutation.logical_path}",
            kind=ArtifactKind.CODE if extension in {".py", ".js", ".ts", ".tsx", ".go", ".rs"} else ArtifactKind.FILE,
            extension=extension,
            producer_node_id=node_id or None,
            metadata={
                "workspace_id": plan.workspace_id,
                "transaction_id": plan.transaction_id,
                "logical_path": mutation.logical_path,
                "physical_source_persisted": False,
            },
        )
        publication = ArtifactPublication(
            publication_id=new_workspace_id("publication"),
            workspace_id=plan.workspace_id,
            transaction_id=plan.transaction_id,
            logical_path=mutation.logical_path,
            artifact_id=str(artifact.artifact_id),
            artifact_uri=f"artifact://{artifact.artifact_id}",
            content_hash=content_hash,
            bytes_published=len(content),
            metadata={"artifact_byte_owner": "LocalArtifactStore"},
        )
        return publication, artifact

    def _rollback_or_raise(
        self,
        *,
        access: WorkspaceAccessHandle,
        plan: WorkspaceMutationPlan,
        record: WorkspaceTransactionRecord,
        snapshot_id: str,
        mutation_started: bool,
        path_results: Sequence[MutationPathResult],
        ownership_before: Sequence[Any],
        error: Exception,
    ) -> GatewayMutationResult:
        normalized = error_from_exception(
            error,
            workspace_id=plan.workspace_id,
            operation="execute_workspace_patch",
        )
        latest_record = self.store.require_transaction(record.transaction_id)
        if snapshot_id and mutation_started:
            rolling = latest_record.advance(
                IntegrationPhase.ROLLING_BACK,
                error_code=normalized.code.value,
                error_type=type(error).__name__,
                message="workspace patch failed; restoring committed undo snapshot",
            )
            latest_record = self.store.update_transaction(rolling, expected_revision=latest_record.revision)
            try:
                snapshot = self.manager.store.get_snapshot(snapshot_id) or self.manager.snapshot_runtime.load(snapshot_id)
                binding_before_rollback = self.manager.store.require_binding(plan.workspace_id)
                task_root = self.manager.backend.mount_root(
                    binding_before_rollback,
                    WorkspaceKind.TASK,
                )
                expected_live_hashes = {
                    item.logical_path: (
                        None if item.kind is MutationKind.DELETE_FILE else item.after_hash
                    )
                    for item in path_results
                }
                restored_paths, conflict_paths = self.manager.snapshot_runtime.restore_write_set(
                    snapshot=snapshot,
                    workspace_id=plan.workspace_id,
                    owner_epoch=binding_before_rollback.owner_epoch,
                    target_root=task_root,
                    candidate_paths=tuple(item.logical_path for item in plan.mutations),
                    expected_live_hashes=expected_live_hashes,
                )
                self.manager.backend.file_state.invalidate(plan.workspace_id)
                next_access = self.manager.rotate_after_integration(
                    plan.workspace_id,
                    worker_id=plan.worker_id,
                    active_snapshot_id=snapshot_id,
                    causation_id=plan.causation_id or plan.transaction_id,
                    reason="patch_write_set_rolled_back",
                )
                del next_access
                self.manager.ownership_store.replace_workspace(
                    plan.workspace_id,
                    ownership_before,
                )
                binding = self.manager.store.require_binding(plan.workspace_id)
                if conflict_paths:
                    raise WorkspaceError(
                        WorkspaceErrorCode.RESTORE_CONFLICT,
                        "Workspace rollback retained concurrent changes for recovery.",
                        workspace_id=plan.workspace_id,
                        operation="rollback_workspace_patch",
                        metadata={
                            "conflict_paths": list(conflict_paths),
                            "restored_paths": list(restored_paths),
                        },
                    )
                rolled_back = latest_record.advance(
                    IntegrationPhase.ROLLED_BACK,
                    owner_epoch_after=binding.owner_epoch,
                    binding_revision_after=binding.binding_revision,
                    lease_id_after=binding.lease_id,
                    completed_at=utc_now(),
                    error_code=normalized.code.value,
                    error_type=type(error).__name__,
                    metadata={
                        **dict(latest_record.metadata),
                        "rollback_mode": "write_set",
                        "restored_paths": list(restored_paths),
                        "concurrent_paths_preserved": True,
                    },
                    message="workspace patch write-set rolled back; unrelated user changes were preserved",
                )
                latest_record = self.store.update_transaction(
                    rolled_back,
                    expected_revision=latest_record.revision,
                )
                receipt = self.store.append_receipt(
                    WorkerWorkspaceReceipt(
                        receipt_id=new_workspace_id("workspace-receipt"),
                        workspace_id=plan.workspace_id,
                        worker_id=plan.worker_id,
                        operation=IntegrationOperation.PATCH,
                        ok=False,
                        owner_epoch=binding.owner_epoch,
                        binding_revision=binding.binding_revision,
                        lease_id=binding.lease_id,
                        transaction_id=plan.transaction_id,
                        logical_paths=tuple(item.logical_path for item in plan.mutations),
                        error_code=normalized.code.value,
                        summary="workspace patch failed and was rolled back",
                        metadata={
                            "snapshot_id": snapshot_id,
                            "rolled_back": True,
                            "rollback_mode": "write_set",
                            "restored_paths": list(restored_paths),
                            "concurrent_paths_preserved": True,
                        },
                    )
                )
                self.manager.emit_integration_event(
                    "workspace.patch.rolled_back",
                    plan.workspace_id,
                    causation_id=plan.causation_id,
                    metadata={
                        "transaction_id": plan.transaction_id,
                        "receipt_id": receipt.receipt_id,
                        "error_code": normalized.code.value,
                    },
                )
            except Exception as restore_error:
                recovery = self._recovery_input(
                    plan=plan,
                    record=latest_record,
                    snapshot_id=snapshot_id,
                    error=restore_error,
                    original_error=normalized,
                )
                failed = self.store.require_transaction(record.transaction_id).advance(
                    IntegrationPhase.QUARANTINED,
                    completed_at=utc_now(),
                    error_code=normalized.code.value,
                    error_type=type(error).__name__,
                    recovery_input_id=recovery.recovery_input_id,
                    message="workspace patch and rollback both failed; recovery input emitted",
                )
                self.store.update_transaction(failed, expected_revision=failed.revision - 1)
                self.manager.emit_integration_event(
                    "workspace.backend_unavailable_candidate",
                    plan.workspace_id,
                    causation_id=plan.causation_id,
                    metadata={
                        "transaction_id": plan.transaction_id,
                        "recovery_input_id": recovery.recovery_input_id,
                        "error_code": normalized.code.value,
                        "restore_error_type": type(restore_error).__name__,
                    },
                )
        else:
            failed = latest_record.advance(
                IntegrationPhase.FAILED,
                completed_at=utc_now(),
                error_code=normalized.code.value,
                error_type=type(error).__name__,
                message="workspace patch rejected before canonical mutation",
            )
            self.store.update_transaction(failed, expected_revision=latest_record.revision)
            binding = self.manager.store.require_binding(plan.workspace_id)
            self.store.append_receipt(
                WorkerWorkspaceReceipt(
                    receipt_id=new_workspace_id("workspace-receipt"),
                    workspace_id=plan.workspace_id,
                    worker_id=plan.worker_id,
                    operation=IntegrationOperation.PATCH,
                    ok=False,
                    owner_epoch=binding.owner_epoch,
                    binding_revision=binding.binding_revision,
                    lease_id=binding.lease_id,
                    transaction_id=plan.transaction_id,
                    logical_paths=tuple(item.logical_path for item in plan.mutations),
                    error_code=normalized.code.value,
                    summary="workspace patch rejected before filesystem mutation",
                    metadata={"mutation_started": False},
                )
            )
        raise normalized from error

    def _recovery_input(
        self,
        *,
        plan: WorkspaceMutationPlan,
        record: WorkspaceTransactionRecord,
        snapshot_id: str,
        error: Exception,
        original_error: WorkspaceError,
    ) -> WorkspaceRecoveryInput:
        binding = self.manager.store.require_binding(plan.workspace_id)
        recovery = WorkspaceRecoveryInput(
            recovery_input_id=new_workspace_id("workspace-recovery-input"),
            workspace_id=plan.workspace_id,
            operation=IntegrationOperation.PATCH,
            severity=RecoverySeverity.BACKEND_UNAVAILABLE_CANDIDATE,
            reason_code="patch_rollback_failed",
            owner_epoch=binding.owner_epoch,
            binding_revision=binding.binding_revision,
            transaction_id=record.transaction_id,
            snapshot_id=snapshot_id,
            retryable=True,
            evidence_refs=(
                f"workspace-transaction://{record.transaction_id}",
                f"workspace-snapshot://{snapshot_id}",
            ),
            recommended_actions=(
                "verify_snapshot",
                "fence_workspace_writes",
                "restore_last_safe_snapshot",
                "replan_backend_placement",
            ),
            metadata={
                "original_error_code": original_error.code.value,
                "rollback_error_type": type(error).__name__,
                "downstream_consumers": ["M1-05C", "M1-07C"],
            },
        )
        return self.store.put_recovery_input(recovery)

    def _require_enabled(self, workspace_id: str) -> None:
        if self.disabled:
            raise WorkspaceError(
                WorkspaceErrorCode.DISABLED,
                "Workspace patch transaction runtime is disabled; raw file fallback is forbidden.",
                workspace_id=workspace_id,
                operation="workspace_patch_transaction",
            )


class WorkspaceEditPort:
    """Per-worker capability consumed by ``ToolExecutor`` and BrowserWorker.

    The port keeps private access material in process memory.  Public methods
    return logical paths and opaque receipts only.  A successful transaction
    rotates the owner epoch and replaces the private capability, so subsequent
    tool calls in the same query loop remain valid while cached external
    capabilities become stale.
    """

    def __init__(
        self,
        manager: WorkspaceManagerPort,
        access: WorkspaceAccessHandle,
        *,
        worker_id: str,
        run_id: str,
        task_id: str,
        node_id: str = "",
        artifact_store: ArtifactWriterPort | None = None,
        disabled: bool = False,
    ) -> None:
        self.manager = manager
        self.worker_id = str(worker_id)
        self.run_id = str(run_id)
        self.task_id = str(task_id)
        self.node_id = str(node_id)
        self._access = access
        self._guard = threading.RLock()
        self._evidence: dict[str, WorkspaceReadEvidence] = {}
        self.runtime = WorkspacePatchTransactionRuntime(
            manager,
            artifact_store=artifact_store,
            disabled=disabled,
        )
        self.disabled = bool(disabled)

    @property
    def workspace_id(self) -> str:
        return self._access.workspace_id

    @property
    def access_projection(self) -> dict[str, Any]:
        return self._access.to_public_dict()

    def current_access(self) -> WorkspaceAccessHandle:
        return self._access

    def adopt_access(self, access: WorkspaceAccessHandle) -> None:
        """Adopt a manager-issued replacement capability after isolation merge."""

        if access.workspace_id != self.workspace_id or access.worker_id != self.worker_id:
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_OWNER_MISMATCH,
                "Workspace edit port cannot adopt a capability for another workspace or worker.",
                workspace_id=self.workspace_id,
                operation="adopt_workspace_access",
            )
        self._access = access
        self._evidence.clear()

    def read_bytes(
        self,
        logical_path: str,
        *,
        mount_kind: WorkspaceKind = WorkspaceKind.TASK,
        mode: WorkspaceReadMode = WorkspaceReadMode.FULL,
        start: int = 0,
        length: int | None = None,
    ) -> GatewayReadResult:
        self._require_enabled()
        with self._guard, self.manager.integration_store.workspace_locks.acquire_many((self.workspace_id,)):
            binding = self.manager.store.require_binding(self.workspace_id)
            self._assert_current(binding)
            result = self.manager.backend.read(
                self._access,
                mount_kind=mount_kind,
                path=logical_path,
                mode=mode,
                start=start,
                length=length,
            )
            record = result.record
            absent = record.file_identity == "absent"
            complete = bool(record.fully_read or absent)
            evidence = WorkspaceReadEvidence(
                workspace_id=self.workspace_id,
                logical_path=logical_path,
                owner_epoch=binding.owner_epoch,
                binding_revision=binding.binding_revision,
                lease_id=binding.lease_id,
                content_hash=record.base_hash or sha256_bytes(result.content),
                size=record.base_size,
                mtime_ns=record.base_mtime_ns,
                file_identity=record.file_identity,
                complete=complete,
                encoding=result.encoding,
            )
            self._evidence[evidence.evidence_id] = evidence
            self.manager.emit_integration_event(
                "workspace.file.read",
                self.workspace_id,
                metadata={
                    "worker_id": self.worker_id,
                    "logical_path": evidence.logical_path,
                    "evidence_id": evidence.evidence_id,
                    "complete": evidence.complete,
                    "content_bytes": len(result.content),
                },
            )
            return GatewayReadResult(
                workspace_id=self.workspace_id,
                logical_path=evidence.logical_path,
                content=result.content,
                evidence=evidence,
                exists=not absent,
                mount_kind=mount_kind,
            )

    def read_text(self, logical_path: str, *, encoding: str = "utf-8") -> GatewayReadResult:
        result = self.read_bytes(logical_path, mode=WorkspaceReadMode.FULL)
        result.content.decode(encoding)
        return result

    def write_text(
        self,
        logical_path: str,
        content: str,
        *,
        encoding: str = "utf-8",
        publish_artifact: bool = False,
        idempotency_key: str = "",
        causation_id: str = "",
    ) -> GatewayMutationResult:
        read = self.read_bytes(logical_path)
        mutation = WorkspaceMutation(
            mutation_id=new_workspace_id("mutation"),
            kind=MutationKind.WRITE_TEXT,
            logical_path=logical_path,
            content=str(content).encode(encoding),
            encoding=encoding,
            read_evidence_id=read.evidence.evidence_id,
            expected_absent=not read.exists,
        )
        return self.apply(
            (mutation,),
            evidence=(read.evidence,),
            publish_artifact=publish_artifact,
            idempotency_key=idempotency_key,
            causation_id=causation_id,
        )

    def write_bytes(
        self,
        logical_path: str,
        content: bytes,
        *,
        mount_kind: WorkspaceKind = WorkspaceKind.TASK,
        publish_artifact: bool = False,
        idempotency_key: str = "",
        causation_id: str = "",
    ) -> GatewayMutationResult:
        if mount_kind is not WorkspaceKind.TASK:
            return self._write_non_task_mount(
                logical_path,
                bytes(content),
                mount_kind=mount_kind,
                publish_artifact=publish_artifact,
                idempotency_key=idempotency_key,
                causation_id=causation_id,
            )
        read = self.read_bytes(logical_path)
        mutation = WorkspaceMutation(
            mutation_id=new_workspace_id("mutation"),
            kind=MutationKind.WRITE_BYTES,
            logical_path=logical_path,
            content=bytes(content),
            read_evidence_id=read.evidence.evidence_id,
            expected_absent=not read.exists,
        )
        return self.apply(
            (mutation,),
            evidence=(read.evidence,),
            publish_artifact=publish_artifact,
            idempotency_key=idempotency_key,
            causation_id=causation_id,
        )

    def edit_text(
        self,
        logical_path: str,
        *,
        old: str,
        new: str,
        replace_all: bool = False,
        encoding: str = "utf-8",
        publish_artifact: bool = False,
        idempotency_key: str = "",
        causation_id: str = "",
    ) -> GatewayMutationResult:
        read = self.read_text(logical_path, encoding=encoding)
        mutation = WorkspaceMutation(
            mutation_id=new_workspace_id("mutation"),
            kind=MutationKind.REPLACE_TEXT,
            logical_path=logical_path,
            encoding=encoding,
            old_text=str(old),
            new_text=str(new),
            replace_all=bool(replace_all),
            read_evidence_id=read.evidence.evidence_id,
        )
        return self.apply(
            (mutation,),
            evidence=(read.evidence,),
            publish_artifact=publish_artifact,
            idempotency_key=idempotency_key,
            causation_id=causation_id,
        )

    def delete_file(
        self,
        logical_path: str,
        *,
        idempotency_key: str = "",
        causation_id: str = "",
    ) -> GatewayMutationResult:
        read = self.read_bytes(logical_path)
        if not read.exists:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "Workspace delete target does not exist.",
                workspace_id=self.workspace_id,
                operation="delete_workspace_file",
                path=logical_path,
            )
        mutation = WorkspaceMutation(
            mutation_id=new_workspace_id("mutation"),
            kind=MutationKind.DELETE_FILE,
            logical_path=logical_path,
            read_evidence_id=read.evidence.evidence_id,
        )
        return self.apply(
            (mutation,),
            evidence=(read.evidence,),
            idempotency_key=idempotency_key,
            causation_id=causation_id,
        )

    def apply(
        self,
        mutations: Sequence[WorkspaceMutation],
        *,
        evidence: Sequence[WorkspaceReadEvidence] = (),
        publish_artifact: bool = False,
        idempotency_key: str = "",
        causation_id: str = "",
    ) -> GatewayMutationResult:
        self._require_enabled()
        with self._guard:
            binding = self.manager.store.require_binding(self.workspace_id)
            self._assert_current(binding)
            selected_evidence = tuple(evidence)
            if not selected_evidence:
                identifiers = {item.read_evidence_id for item in mutations if item.read_evidence_id}
                selected_evidence = tuple(self._evidence[item] for item in identifiers if item in self._evidence)
            transaction_id = new_workspace_id("workspace-txn")
            request_digest = stable_digest({
                "workspace_id": self.workspace_id,
                "worker_id": self.worker_id,
                "publish_artifact": publish_artifact,
                "mutations": [
                    {
                        "kind": item.kind.value,
                        "logical_path": item.logical_path,
                        "content_digest": item.content_digest(),
                        "encoding": item.encoding,
                        "mode": item.mode,
                        "metadata": dict(item.metadata),
                    }
                    for item in mutations
                ],
            })
            plan = WorkspaceMutationPlan(
                transaction_id=transaction_id,
                workspace_id=self.workspace_id,
                owner_epoch=binding.owner_epoch,
                binding_revision=binding.binding_revision,
                lease_id=binding.lease_id,
                worker_id=self.worker_id,
                mutations=tuple(mutations),
                evidence=selected_evidence,
                idempotency_key=idempotency_key or transaction_id,
                causation_id=causation_id,
                publish_artifact=publish_artifact,
                metadata={
                    "gateway": "WorkspaceEditPort",
                    "raw_path_fallback": False,
                    "request_digest": request_digest,
                },
            )
            result = self.runtime.execute(
                self._access,
                plan,
                run_id=self.run_id,
                task_id=self.task_id,
                node_id=self.node_id,
            )
            self._access = result.access
            self._evidence.clear()
            return result

    def _write_non_task_mount(
        self,
        logical_path: str,
        content: bytes,
        *,
        mount_kind: WorkspaceKind,
        publish_artifact: bool,
        idempotency_key: str,
        causation_id: str,
    ) -> GatewayMutationResult:
        if mount_kind not in {WorkspaceKind.DOWNLOAD, WorkspaceKind.TEMP}:
            raise WorkspaceError(
                WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                "Workspace edit port only permits task, download, or temp writes.",
                workspace_id=self.workspace_id,
                operation="write_workspace_mount",
                actual=mount_kind.value,
            )
        with self._guard, self.manager.integration_store.workspace_locks.acquire_many((self.workspace_id,)):
            binding = self.manager.store.require_binding(self.workspace_id)
            self._assert_current(binding)
            plan_digest = stable_digest({
                "mount_kind": mount_kind.value,
                "logical_path": logical_path,
                "content_hash": sha256_bytes(content),
            })
            replay_key = idempotency_key or new_workspace_id("mount-write")
            transaction_id = new_workspace_id("workspace-txn")
            store = self.manager.integration_store
            claim, created = store.claim_idempotency(
                namespace="workspace-mount-write",
                key=replay_key,
                fingerprint=plan_digest,
                workspace_id=self.workspace_id,
                result_ref=transaction_id,
            )
            if not created and str(claim.get("result_ref") or ""):
                existing = store.require_transaction(str(claim["result_ref"]))
                receipts = store.list_receipts(
                    self.workspace_id,
                    transaction_id=existing.transaction_id,
                )
                if not existing.ok or not receipts:
                    raise WorkspaceError(
                        (
                            WorkspaceErrorCode.OPERATION_IN_PROGRESS
                            if not existing.terminal
                            else WorkspaceErrorCode.IDEMPOTENCY_CONFLICT
                        ),
                        "Mount write idempotency key is already bound to an uncommitted operation.",
                        workspace_id=self.workspace_id,
                        operation="replay_workspace_mount_write",
                        metadata={
                            "transaction_id": existing.transaction_id,
                            "phase": existing.phase.value,
                            "receipt_present": bool(receipts),
                        },
                    )
                latest_binding = self.manager.store.require_binding(self.workspace_id)
                latest_access = self.manager.acquire_for_worker(
                    task_id=latest_binding.task_id,
                    session_id=latest_binding.session_id,
                    worker_id=self.worker_id,
                )
                self._access = latest_access
                return GatewayMutationResult(
                    transaction=existing,
                    receipt=_receipt_from_dict(receipts[-1]),
                    access=latest_access,
                    idempotent_replay=True,
                )
            read = self.manager.backend.read(
                self._access,
                mount_kind=mount_kind,
                path=logical_path,
                mode=WorkspaceReadMode.FULL,
            )
            record = WorkspaceTransactionRecord(
                transaction_id=transaction_id,
                workspace_id=self.workspace_id,
                operation=IntegrationOperation.WRITE,
                phase=IntegrationPhase.REQUESTED,
                owner_epoch_before=binding.owner_epoch,
                owner_epoch_after=0,
                binding_revision_before=binding.binding_revision,
                binding_revision_after=0,
                lease_id_before=binding.lease_id,
                lease_id_after="",
                plan_digest=plan_digest,
                idempotency_key=replay_key,
                message=f"{mount_kind.value} mount write journaled before mutation",
                metadata={
                    "mount_kind": mount_kind.value,
                    "artifact_publish_requested": publish_artifact,
                    "rollback_contract": "restore_prior_mount_bytes",
                },
            )
            record = store.create_transaction(record)
            try:
                applying = record.advance(IntegrationPhase.APPLYING)
                record = store.update_transaction(applying, expected_revision=record.revision)
                write = self.manager.backend.write_after_read(
                    self._access,
                    mount_kind=mount_kind,
                    path=logical_path,
                    content=bytes(content),
                    service=(
                        "browser-download"
                        if mount_kind is WorkspaceKind.DOWNLOAD
                        else self.worker_id
                    ),
                )
                next_access = self.manager.rotate_after_integration(
                    self.workspace_id,
                    worker_id=self.worker_id,
                    active_snapshot_id=binding.active_snapshot_id,
                    causation_id=causation_id,
                    reason=f"{mount_kind.value}_write_committed",
                )
                self._access = next_access
                latest = self.manager.store.require_binding(self.workspace_id)
                committed = record.advance(
                    IntegrationPhase.COMMITTED,
                    owner_epoch_after=latest.owner_epoch,
                    binding_revision_after=latest.binding_revision,
                    lease_id_after=latest.lease_id,
                    path_results=(
                        MutationPathResult(
                            logical_path=logical_path,
                            kind=MutationKind.WRITE_BYTES,
                            before_hash=read.record.base_hash or sha256_bytes(read.content),
                            after_hash=write.content_hash,
                            bytes_before=len(read.content),
                            bytes_after=len(content),
                            disposition=f"{mount_kind.value}_externalized",
                            ownership_claimed=False,
                        ),
                    ),
                    completed_at=utc_now(),
                    message=f"{mount_kind.value} mount write committed through workspace gateway",
                )
                record = store.update_transaction(committed, expected_revision=record.revision)
            except Exception as error:
                normalized = error_from_exception(
                    error,
                    workspace_id=self.workspace_id,
                    operation="write_workspace_mount",
                )
                latest_record = store.require_transaction(transaction_id)
                if not latest_record.terminal:
                    failed = latest_record.advance(
                        IntegrationPhase.FAILED,
                        completed_at=utc_now(),
                        error_code=normalized.code.value,
                        error_type=type(error).__name__,
                        message=f"{mount_kind.value} mount write failed",
                    )
                    store.update_transaction(failed, expected_revision=latest_record.revision)
                raise normalized from error
            receipt = store.append_receipt(
                WorkerWorkspaceReceipt(
                    receipt_id=new_workspace_id("workspace-receipt"),
                    workspace_id=self.workspace_id,
                    worker_id=self.worker_id,
                    operation=IntegrationOperation.WRITE,
                    ok=True,
                    owner_epoch=latest.owner_epoch,
                    binding_revision=latest.binding_revision,
                    lease_id=latest.lease_id,
                    transaction_id=transaction_id,
                    logical_paths=(logical_path,),
                    summary=f"{mount_kind.value} bytes committed through workspace gateway",
                    metadata={"mount_kind": mount_kind.value, "content_hash": write.content_hash},
                )
            )
            store.complete_idempotency(
                namespace="workspace-mount-write",
                key=replay_key,
                fingerprint=plan_digest,
                result_ref=record.transaction_id,
            )
            return GatewayMutationResult(
                transaction=record,
                receipt=receipt,
                access=next_access,
            )

    def _assert_current(self, binding: Any) -> None:
        if (
            binding.owner_epoch != self._access.owner_epoch
            or binding.lease_id != self._access.lease_id
            or binding.capability_revision != self._access.capability_revision
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.OWNER_EPOCH_STALE,
                "Workspace edit port capability is stale.",
                workspace_id=self.workspace_id,
                operation="workspace_edit_port",
                expected={
                    "owner_epoch": binding.owner_epoch,
                    "lease_id": binding.lease_id,
                    "capability_revision": binding.capability_revision,
                },
                actual={
                    "owner_epoch": self._access.owner_epoch,
                    "lease_id": self._access.lease_id,
                    "capability_revision": self._access.capability_revision,
                },
            )

    def _require_enabled(self) -> None:
        if self.disabled:
            raise WorkspaceError(
                WorkspaceErrorCode.DISABLED,
                "Workspace edit port is disabled; direct filesystem fallback is forbidden.",
                workspace_id=self.workspace_id,
                operation="workspace_edit_port",
            )


def _receipt_from_dict(value: Mapping[str, Any]) -> WorkerWorkspaceReceipt:
    return WorkerWorkspaceReceipt(
        receipt_id=value.get("receipt_id", ""),
        workspace_id=value.get("workspace_id", ""),
        worker_id=value.get("worker_id", ""),
        operation=IntegrationOperation(str(value.get("operation") or IntegrationOperation.PATCH.value)),
        ok=bool(value.get("ok", False)),
        owner_epoch=value.get("owner_epoch", 0),
        binding_revision=value.get("binding_revision", 0),
        lease_id=value.get("lease_id", ""),
        transaction_id=str(value.get("transaction_id") or ""),
        isolation_id=str(value.get("isolation_id") or ""),
        rebind_id=str(value.get("rebind_id") or ""),
        logical_paths=tuple(str(item) for item in value.get("logical_paths") or ()),
        artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
        error_code=str(value.get("error_code") or ""),
        summary=str(value.get("summary") or ""),
        created_at=str(value.get("created_at") or utc_now()),
        metadata=dict(value.get("metadata") or {}),
    )


__all__ = [
    "ArtifactWriterPort",
    "GatewayMutationResult",
    "GatewayReadResult",
    "WorkspaceEditPort",
    "WorkspaceManagerPort",
    "WorkspacePatchTransactionRuntime",
]
