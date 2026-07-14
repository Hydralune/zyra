from __future__ import annotations

"""Physical child workspaces and deterministic three-way merge."""

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .dirty_state import WorkspaceDirtyStateRuntime
from .errors import WorkspaceError, WorkspaceErrorCode, error_from_exception
from .integration_models import (
    IntegrationOperation,
    IsolationMergeResult,
    IsolationRecord,
    IsolationState,
    MergeConflict,
    MergeDisposition,
    MutationKind,
    RecoverySeverity,
    WorkspaceMutation,
    WorkspaceRecoveryInput,
    classify_merge_disposition,
)
from .integration_store import WorkspaceIntegrationStore
from .local_backend import WorkspaceAccessHandle
from .models import WorkspaceKind, WorkspaceOperation, new_workspace_id, stable_digest, utc_now
from .transactions import GatewayMutationResult, WorkspaceEditPort
from .tree_state import (
    TreeScanLimits,
    copy_workspace_tree,
    read_tree_file,
    scan_workspace_tree,
)


@dataclass(frozen=True, slots=True)
class IsolationAccess:
    isolation_id: str
    parent_workspace_id: str
    child_workspace_id: str
    child_task_id: str
    child_worker_id: str
    owner_epoch: int
    binding_revision: int
    lease_id: str
    opaque_location_ref: str
    expires_at: str
    _child_access: WorkspaceAccessHandle = field(repr=False, compare=False)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "isolation_id": self.isolation_id,
            "parent_workspace_id": self.parent_workspace_id,
            "child_workspace_id": self.child_workspace_id,
            "child_task_id": self.child_task_id,
            "child_worker_id": self.child_worker_id,
            "owner_epoch": self.owner_epoch,
            "binding_revision": self.binding_revision,
            "lease_id": self.lease_id,
            "opaque_location_ref": self.opaque_location_ref,
            "expires_at": self.expires_at,
            "physical_location_redacted": True,
        }


@dataclass(frozen=True, slots=True)
class IsolatedShellResult:
    isolation: IsolationRecord
    merge: IsolationMergeResult | None
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    command_digest: str
    child_receipt_ref: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.merge is not None and self.merge.ok

    def to_public_dict(self, *, max_inline_chars: int = 12000) -> dict[str, Any]:
        return {
            "isolation": self.isolation.to_dict(),
            "merge": self.merge.to_dict() if self.merge is not None else None,
            "returncode": self.returncode,
            "stdout": self.stdout[:max_inline_chars],
            "stderr": self.stderr[:max_inline_chars],
            "stdout_truncated": len(self.stdout) > max_inline_chars,
            "stderr_truncated": len(self.stderr) > max_inline_chars,
            "timed_out": self.timed_out,
            "command_digest": self.command_digest,
            "child_receipt_ref": self.child_receipt_ref,
            "physical_location_redacted": True,
        }


class WorkspaceIsolationRuntime:
    """Own real child bindings while preserving the parent dirty baseline."""

    def __init__(
        self,
        manager: Any,
        *,
        artifact_store: Any | None = None,
        disabled: bool = False,
        isolation_ttl_seconds: int = 4 * 60 * 60,
        scan_limits: TreeScanLimits | None = None,
    ) -> None:
        self.manager = manager
        self.store: WorkspaceIntegrationStore = manager.integration_store
        self.artifact_store = artifact_store
        self.disabled = bool(disabled)
        self.isolation_ttl_seconds = max(60, int(isolation_ttl_seconds))
        self.scan_limits = scan_limits or TreeScanLimits()
        self._private_access: dict[str, WorkspaceAccessHandle] = {}

    def prepare(
        self,
        parent_access: WorkspaceAccessHandle,
        *,
        parent_worker_id: str,
        child_worker_id: str,
        idempotency_key: str = "",
        causation_id: str = "",
    ) -> IsolationAccess:
        self._require_enabled(parent_access.workspace_id)
        fingerprint = stable_digest(
            {
                "workspace_id": parent_access.workspace_id,
                "owner_epoch": parent_access.owner_epoch,
                "lease_id": parent_access.lease_id,
                "parent_worker_id": parent_worker_id,
                "child_worker_id": child_worker_id,
            }
        )
        key = idempotency_key or new_workspace_id("isolation-key")
        claim, created = self.store.claim_idempotency(
            namespace="workspace-isolation-prepare",
            key=key,
            fingerprint=fingerprint,
            workspace_id=parent_access.workspace_id,
        )
        if not created and str(claim.get("result_ref") or ""):
            record = self.store.require_isolation(str(claim["result_ref"]))
            return self._access_for_record(record)
        with self.store.workspace_locks.acquire_many((parent_access.workspace_id,)):
            parent = self.manager.store.require_binding(parent_access.workspace_id)
            self._validate_parent_access(parent_access, parent, operation="prepare_workspace_isolation")
            parent_root = self.manager.backend.mount_root(parent, WorkspaceKind.TASK)
            dirty_runtime = WorkspaceDirtyStateRuntime(
                workspace_id=parent.workspace_id,
                workspace_root=parent_root,
                ownership_store=self.manager.ownership_store,
                path_policy=self.manager.backend.path_policy,
            )
            snapshot = self.manager.snapshot(
                parent.workspace_id,
                causation_id=causation_id or key,
                include_dirty_state=True,
            )
            refreshed_parent = self.manager.store.require_binding(parent.workspace_id)
            baseline = dirty_runtime.capture_baseline(
                owner_epoch=refreshed_parent.owner_epoch,
                snapshot_id=snapshot.snapshot_id,
            )
            repository_roots = tuple(item.relative_root for item in baseline.repository_refs)
            parent_manifest = scan_workspace_tree(
                parent_root,
                workspace_id=parent.workspace_id,
                owner_epoch=refreshed_parent.owner_epoch,
                repository_roots=repository_roots,
                source="isolation_baseline",
                limits=self.scan_limits,
            )
            self.store.put_manifest(parent_manifest)
            isolation_id = new_workspace_id("isolation")
            child_task_id = _child_task_id(parent.task_id, isolation_id)
            child_binding_result = self.manager.create_for_task(
                run_id=parent.run_id,
                task_id=child_task_id,
                session_id=f"isolation-{isolation_id[-16:]}",
                worker_id=child_worker_id,
                quota=parent.quota,
                idempotency_key=f"workspace-isolation:{isolation_id}",
                causation_id=causation_id or isolation_id,
            )
            child_access = child_binding_result.access
            child_binding = self.manager.store.require_binding(child_access.workspace_id)
            child_root = self.manager.backend.mount_root(child_binding, WorkspaceKind.TASK)
            try:
                copy_workspace_tree(
                    parent_root,
                    child_root,
                    workspace_id=parent.workspace_id,
                    owner_epoch=refreshed_parent.owner_epoch,
                    repository_roots=repository_roots,
                    limits=self.scan_limits,
                    allow_existing_empty=True,
                )
                child_manifest = scan_workspace_tree(
                    child_root,
                    workspace_id=parent.workspace_id,
                    owner_epoch=refreshed_parent.owner_epoch,
                    repository_roots=repository_roots,
                    source="isolation_child_initial",
                    limits=self.scan_limits,
                )
                if _content_projection(parent_manifest) != _content_projection(child_manifest):
                    raise WorkspaceError(
                        WorkspaceErrorCode.RESTORE_CONFLICT,
                        "Isolated child did not reproduce the parent baseline.",
                        workspace_id=parent.workspace_id,
                        operation="prepare_workspace_isolation",
                    )
                self.store.put_manifest(child_manifest)
                opaque_location_ref = f"workspace-isolation://{isolation_id}/child"
                expires_at = (
                    datetime.now(UTC) + timedelta(seconds=self.isolation_ttl_seconds)
                ).isoformat()
                record = IsolationRecord(
                    isolation_id=isolation_id,
                    workspace_id=parent.workspace_id,
                    task_id=parent.task_id,
                    parent_worker_id=parent_worker_id,
                    child_worker_id=child_worker_id,
                    state=IsolationState.READY,
                    owner_epoch=refreshed_parent.owner_epoch,
                    binding_revision=refreshed_parent.binding_revision,
                    lease_id=refreshed_parent.lease_id,
                    baseline_manifest_id=parent_manifest.manifest_id,
                    baseline_snapshot_id=snapshot.snapshot_id,
                    opaque_location_ref=opaque_location_ref,
                    allowed_operations=(
                        IntegrationOperation.READ,
                        IntegrationOperation.WRITE,
                        IntegrationOperation.EDIT,
                        IntegrationOperation.DELETE,
                        IntegrationOperation.SHELL,
                    ),
                    child_manifest_id=child_manifest.manifest_id,
                    nested_repository_roots=repository_roots,
                    idempotency_key=key,
                    expires_at=expires_at,
                    metadata={
                        "child_workspace_id": child_binding.workspace_id,
                        "child_task_id": child_task_id,
                        "child_session_id": child_binding.session_id,
                        "child_owner_epoch": child_binding.owner_epoch,
                        "child_binding_revision": child_binding.binding_revision,
                        "copy_capability": "directory_copy",
                        "raw_parent_path_shared": False,
                        "state_owner": "WorkspaceManagerRuntime/WorkspaceIsolationRuntime",
                    },
                )
                self.store.create_isolation(record)
                self._private_access[isolation_id] = child_access
                self.store.complete_idempotency(
                    namespace="workspace-isolation-prepare",
                    key=key,
                    fingerprint=fingerprint,
                    result_ref=isolation_id,
                )
                self.manager.emit_integration_event(
                    "workspace.isolation.ready",
                    parent.workspace_id,
                    causation_id=causation_id,
                    metadata={
                        "isolation_id": isolation_id,
                        "child_workspace_id": child_binding.workspace_id,
                        "baseline_manifest_id": parent_manifest.manifest_id,
                        "baseline_snapshot_id": snapshot.snapshot_id,
                        "nested_repository_count": len(repository_roots),
                    },
                )
                return IsolationAccess(
                    isolation_id=isolation_id,
                    parent_workspace_id=parent.workspace_id,
                    child_workspace_id=child_binding.workspace_id,
                    child_task_id=child_task_id,
                    child_worker_id=child_worker_id,
                    owner_epoch=child_binding.owner_epoch,
                    binding_revision=child_binding.binding_revision,
                    lease_id=child_binding.lease_id,
                    opaque_location_ref=opaque_location_ref,
                    expires_at=expires_at,
                    _child_access=child_access,
                )
            except Exception:
                try:
                    self.manager.cleanup(
                        child_binding.workspace_id,
                        causation_id=causation_id or isolation_id,
                        archive=True,
                    )
                except Exception:
                    pass
                raise

    def child_edit_port(
        self,
        isolation_id: str,
        *,
        run_id: str,
        node_id: str = "",
    ) -> WorkspaceEditPort:
        record = self.store.require_isolation(isolation_id)
        if record.state not in {IsolationState.READY, IsolationState.RUNNING, IsolationState.CAPTURED}:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_TRANSITION,
                "Isolated child is not available for edits.",
                workspace_id=record.workspace_id,
                operation="open_isolated_child_edit_port",
                actual=record.state.value,
            )
        access = self._child_access(record)
        return WorkspaceEditPort(
            self.manager,
            access,
            worker_id=record.child_worker_id,
            run_id=run_id,
            task_id=str(record.metadata.get("child_task_id") or record.task_id),
            node_id=node_id,
            artifact_store=self.artifact_store,
        )

    def capture_child(self, isolation_id: str) -> IsolationRecord:
        record = self.store.require_isolation(isolation_id)
        if record.state is IsolationState.CAPTURED:
            return record
        if record.state not in {IsolationState.READY, IsolationState.RUNNING}:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_TRANSITION,
                "Isolated child cannot be captured in its current state.",
                workspace_id=record.workspace_id,
                operation="capture_isolated_workspace",
                actual=record.state.value,
            )
        child_binding = self._child_binding(record)
        child_root = self.manager.backend.mount_root(child_binding, WorkspaceKind.TASK)
        child_manifest = scan_workspace_tree(
            child_root,
            workspace_id=record.workspace_id,
            owner_epoch=record.owner_epoch,
            repository_roots=record.nested_repository_roots,
            source="isolation_child_capture",
            limits=self.scan_limits,
        )
        self.store.put_manifest(child_manifest)
        captured = record.advance(
            IsolationState.CAPTURED,
            child_manifest_id=child_manifest.manifest_id,
        )
        return self.store.update_isolation(captured, expected_revision=record.revision)

    def merge(
        self,
        parent_access: WorkspaceAccessHandle,
        isolation_id: str,
        *,
        idempotency_key: str = "",
        causation_id: str = "",
    ) -> IsolationMergeResult:
        self._require_enabled(parent_access.workspace_id)
        record = self.store.require_isolation(isolation_id)
        if record.workspace_id != parent_access.workspace_id:
            raise WorkspaceError(
                WorkspaceErrorCode.BINDING_STALE,
                "Workspace isolation belongs to a different parent binding.",
                workspace_id=parent_access.workspace_id,
                operation="merge_isolated_workspace",
            )
        if record.state is IsolationState.MERGED:
            return self._replay_merge(record)
        if record.state is IsolationState.CONFLICTED:
            return self._conflicted_result(record, idempotent_replay=True)
        fingerprint = stable_digest(
            {
                "isolation_id": isolation_id,
                "workspace_id": parent_access.workspace_id,
                "owner_epoch": parent_access.owner_epoch,
                "lease_id": parent_access.lease_id,
            }
        )
        key = idempotency_key or f"merge:{isolation_id}"
        claim, created = self.store.claim_idempotency(
            namespace="workspace-isolation-merge",
            key=key,
            fingerprint=fingerprint,
            workspace_id=record.workspace_id,
        )
        if not created and str(claim.get("result_ref") or ""):
            prior = self.store.require_isolation(str(claim["result_ref"]))
            return self._replay_merge(prior) if prior.state is IsolationState.MERGED else self._conflicted_result(prior, idempotent_replay=True)
        with self.store.workspace_locks.acquire_many((record.workspace_id,)):
            parent = self.manager.store.require_binding(record.workspace_id)
            self._validate_parent_access(parent_access, parent, operation="merge_isolated_workspace")
            if parent.owner_epoch != record.owner_epoch or parent.lease_id != record.lease_id:
                raise WorkspaceError(
                    WorkspaceErrorCode.OWNER_EPOCH_STALE,
                    "Parent workspace changed ownership after isolation preparation.",
                    workspace_id=record.workspace_id,
                    operation="merge_isolated_workspace",
                    expected={"owner_epoch": record.owner_epoch, "lease_id": record.lease_id},
                    actual={"owner_epoch": parent.owner_epoch, "lease_id": parent.lease_id},
                )
            record = self.capture_child(isolation_id)
            merging = record.advance(IsolationState.MERGING)
            record = self.store.update_isolation(merging, expected_revision=record.revision)
            baseline = self.store.require_manifest(record.baseline_manifest_id)
            child = self.store.require_manifest(record.child_manifest_id)
            parent_root = self.manager.backend.mount_root(parent, WorkspaceKind.TASK)
            parent_manifest = scan_workspace_tree(
                parent_root,
                workspace_id=record.workspace_id,
                owner_epoch=parent.owner_epoch,
                repository_roots=record.nested_repository_roots,
                source="isolation_parent_premerge",
                limits=self.scan_limits,
            )
            self.store.put_manifest(parent_manifest)
            mutations, conflicts, preserved = self._build_merge_plan(
                record,
                baseline=baseline,
                parent=parent_manifest,
                child=child,
            )
            if conflicts:
                self.store.put_conflicts(conflicts)
                conflicted = record.advance(
                    IsolationState.CONFLICTED,
                    parent_manifest_id=parent_manifest.manifest_id,
                    conflict_ids=tuple(item.conflict_id for item in conflicts),
                    error_code=WorkspaceErrorCode.DIRTY_STATE_CONFLICT.value,
                    message="isolated child merge has typed three-way conflicts; parent unchanged",
                    metadata={
                        **dict(record.metadata),
                        "parent_unchanged": True,
                        "child_preserved": True,
                        "conflict_count": len(conflicts),
                    },
                )
                record = self.store.update_isolation(conflicted, expected_revision=record.revision)
                receipt_ref = self._write_merge_receipt(
                    record,
                    ok=False,
                    transaction_id="",
                    applied_paths=(),
                    preserved_paths=preserved,
                    error_code=WorkspaceErrorCode.DIRTY_STATE_CONFLICT.value,
                )
                self.store.complete_idempotency(
                    namespace="workspace-isolation-merge",
                    key=key,
                    fingerprint=fingerprint,
                    result_ref=record.isolation_id,
                )
                self.manager.emit_integration_event(
                    "workspace.isolation.merge.conflicted",
                    record.workspace_id,
                    causation_id=causation_id,
                    metadata={
                        "isolation_id": record.isolation_id,
                        "conflict_ids": list(record.conflict_ids),
                        "receipt_ref": receipt_ref,
                        "parent_unchanged": True,
                    },
                )
                return IsolationMergeResult(
                    isolation_id=record.isolation_id,
                    workspace_id=record.workspace_id,
                    state=record.state,
                    transaction_id="",
                    applied_paths=(),
                    preserved_parent_paths=preserved,
                    conflicts=tuple(conflicts),
                    owner_epoch_before=parent.owner_epoch,
                    owner_epoch_after=parent.owner_epoch,
                    receipt_ref=receipt_ref,
                )
            if not mutations:
                transaction_id = new_workspace_id("workspace-txn-noop")
                next_access = parent_access
            else:
                parent_port = WorkspaceEditPort(
                    self.manager,
                    parent_access,
                    worker_id=parent_access.worker_id,
                    run_id=parent.run_id,
                    task_id=parent.task_id,
                    artifact_store=self.artifact_store,
                )
                evidence = []
                normalized_mutations: list[WorkspaceMutation] = []
                for mutation in mutations:
                    read = parent_port.read_bytes(mutation.logical_path)
                    evidence.append(read.evidence)
                    normalized_mutations.append(
                        WorkspaceMutation(
                            mutation_id=mutation.mutation_id,
                            kind=mutation.kind,
                            logical_path=mutation.logical_path,
                            content=mutation.content,
                            encoding=mutation.encoding,
                            old_text=mutation.old_text,
                            new_text=mutation.new_text,
                            replace_all=mutation.replace_all,
                            read_evidence_id=read.evidence.evidence_id,
                            expected_absent=not read.exists,
                            metadata={
                                **dict(mutation.metadata),
                                "allow_user_baseline_overlay": True,
                                "isolation_id": record.isolation_id,
                            },
                        )
                    )
                mutation_result = parent_port.apply(
                    tuple(normalized_mutations),
                    evidence=tuple(evidence),
                    publish_artifact=True,
                    idempotency_key=f"isolation-merge:{record.isolation_id}",
                    causation_id=causation_id or record.isolation_id,
                )
                transaction_id = mutation_result.transaction.transaction_id
                next_access = mutation_result.access
            latest_parent = self.manager.store.require_binding(record.workspace_id)
            merged = record.advance(
                IsolationState.MERGED,
                parent_manifest_id=parent_manifest.manifest_id,
                merge_transaction_id=transaction_id,
                message="isolated child changes merged through parent patch transaction",
                metadata={
                    **dict(record.metadata),
                    "applied_path_count": len(mutations),
                    "preserved_parent_path_count": len(preserved),
                    "nested_repository_outcomes": self._nested_outcomes(record, mutations, ()),
                    "parent_owner_epoch_after": latest_parent.owner_epoch,
                },
            )
            record = self.store.update_isolation(merged, expected_revision=record.revision)
            applied_paths = tuple(item.logical_path for item in mutations)
            receipt_ref = self._write_merge_receipt(
                record,
                ok=True,
                transaction_id=transaction_id,
                applied_paths=applied_paths,
                preserved_paths=preserved,
            )
            self.store.complete_idempotency(
                namespace="workspace-isolation-merge",
                key=key,
                fingerprint=fingerprint,
                result_ref=record.isolation_id,
            )
            self.manager.emit_integration_event(
                "workspace.isolation.merge.committed",
                record.workspace_id,
                causation_id=causation_id,
                metadata={
                    "isolation_id": record.isolation_id,
                    "transaction_id": transaction_id,
                    "receipt_ref": receipt_ref,
                    "applied_path_count": len(applied_paths),
                    "preserved_parent_path_count": len(preserved),
                },
            )
            self._cleanup_child_after_merge(record)
            return IsolationMergeResult(
                isolation_id=record.isolation_id,
                workspace_id=record.workspace_id,
                state=record.state,
                transaction_id=transaction_id,
                applied_paths=applied_paths,
                preserved_parent_paths=preserved,
                conflicts=(),
                owner_epoch_before=parent.owner_epoch,
                owner_epoch_after=latest_parent.owner_epoch,
                receipt_ref=receipt_ref,
            )

    def discard(self, isolation_id: str, *, causation_id: str = "") -> IsolationRecord:
        record = self.store.require_isolation(isolation_id)
        if record.state is IsolationState.DISCARDED:
            return record
        if record.state is IsolationState.MERGED:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_TRANSITION,
                "A merged workspace isolation cannot be discarded again.",
                workspace_id=record.workspace_id,
                operation="discard_workspace_isolation",
            )
        discarding = record.advance(IsolationState.DISCARDING)
        record = self.store.update_isolation(discarding, expected_revision=record.revision)
        try:
            child = self._child_binding(record)
            self.manager.cleanup(
                child.workspace_id,
                causation_id=causation_id or isolation_id,
                archive=True,
            )
            discarded = record.advance(
                IsolationState.DISCARDED,
                message="isolated child archived and discarded",
            )
            record = self.store.update_isolation(discarded, expected_revision=record.revision)
            self._private_access.pop(isolation_id, None)
            return record
        except Exception as error:
            normalized = error_from_exception(
                error,
                workspace_id=record.workspace_id,
                operation="discard_workspace_isolation",
            )
            quarantined = record.advance(
                IsolationState.QUARANTINED,
                error_code=normalized.code.value,
                message="isolated child cleanup requires recovery",
            )
            self.store.update_isolation(quarantined, expected_revision=record.revision)
            raise normalized from error

    def run_shell_and_merge(
        self,
        parent_access: WorkspaceAccessHandle,
        *,
        command: str,
        parent_worker_id: str,
        child_worker_id: str,
        timeout_seconds: int = 30,
        environment: Mapping[str, str] | None = None,
        causation_id: str = "",
    ) -> IsolatedShellResult:
        command_text = str(command or "").strip()
        if not command_text:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Isolated shell command is required.",
                workspace_id=parent_access.workspace_id,
                operation="run_isolated_workspace_shell",
            )
        isolation_access = self.prepare(
            parent_access,
            parent_worker_id=parent_worker_id,
            child_worker_id=child_worker_id,
            idempotency_key=f"shell-isolation:{causation_id or stable_digest(command_text)[:24]}",
            causation_id=causation_id,
        )
        record = self.store.require_isolation(isolation_access.isolation_id)
        running = record.advance(IsolationState.RUNNING)
        record = self.store.update_isolation(running, expected_revision=record.revision)
        child_binding = self._child_binding(record)
        child_root = self.manager.backend.mount_root(child_binding, WorkspaceKind.TASK)
        selected_environment = _bounded_shell_environment(environment)
        completed: subprocess.CompletedProcess[str] | None = None
        timed_out = False
        stdout = ""
        stderr = ""
        returncode = -1
        try:
            completed = subprocess.run(
                command_text,
                cwd=child_root,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1, int(timeout_seconds)),
                env=selected_environment,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = str(error.stdout or "")
            stderr = str(error.stderr or "")
            returncode = -1
        merge: IsolationMergeResult | None = None
        if not timed_out:
            merge = self.merge(
                parent_access,
                isolation_access.isolation_id,
                idempotency_key=f"shell-merge:{causation_id or isolation_access.isolation_id}",
                causation_id=causation_id,
            )
            record = self.store.require_isolation(isolation_access.isolation_id)
        else:
            interrupted = record.advance(
                IsolationState.INTERRUPTED,
                error_code="tool_timeout",
                message="isolated shell timed out; child retained for recovery",
            )
            record = self.store.update_isolation(interrupted, expected_revision=record.revision)
        return IsolatedShellResult(
            isolation=record,
            merge=merge,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            command_digest=stable_digest({"command": command_text}),
            child_receipt_ref=f"workspace-isolation://{record.isolation_id}",
        )

    def recover_on_startup(self) -> tuple[WorkspaceRecoveryInput, ...]:
        recoveries: list[WorkspaceRecoveryInput] = []
        interrupted = self.store.interrupted_operations().get("isolations", ())
        for isolation_id in interrupted:
            record = self.store.require_isolation(isolation_id)
            if record.state in {IsolationState.READY, IsolationState.CAPTURED}:
                try:
                    self._child_binding(record)
                    continue
                except WorkspaceError:
                    pass
            binding = self.manager.store.require_binding(record.workspace_id)
            recovery = WorkspaceRecoveryInput(
                recovery_input_id=new_workspace_id("workspace-recovery-input"),
                workspace_id=record.workspace_id,
                operation=IntegrationOperation.ISOLATION_MERGE,
                severity=RecoverySeverity.RECOVERY_INPUT,
                reason_code="interrupted_workspace_isolation",
                owner_epoch=binding.owner_epoch,
                binding_revision=binding.binding_revision,
                isolation_id=record.isolation_id,
                snapshot_id=record.baseline_snapshot_id,
                retryable=True,
                evidence_refs=(
                    f"workspace-isolation://{record.isolation_id}",
                    f"workspace-manifest://{record.baseline_manifest_id}",
                ),
                recommended_actions=("inspect_child", "retry_merge", "discard_child"),
                metadata={"isolation_state": record.state.value},
            )
            recoveries.append(self.store.put_recovery_input(recovery))
        return tuple(recoveries)

    def _build_merge_plan(
        self,
        record: IsolationRecord,
        *,
        baseline: Any,
        parent: Any,
        child: Any,
    ) -> tuple[tuple[WorkspaceMutation, ...], tuple[MergeConflict, ...], tuple[str, ...]]:
        baseline_by_path = baseline.by_path()
        parent_by_path = parent.by_path()
        child_by_path = child.by_path()
        child_binding = self._child_binding(record)
        child_root = self.manager.backend.mount_root(child_binding, WorkspaceKind.TASK)
        mutations: list[WorkspaceMutation] = []
        conflicts: list[MergeConflict] = []
        preserved: list[str] = []
        for path in sorted(set(baseline_by_path) | set(parent_by_path) | set(child_by_path)):
            before = baseline_by_path.get(path)
            current_parent = parent_by_path.get(path)
            current_child = child_by_path.get(path)
            disposition = classify_merge_disposition(before, current_parent, current_child)
            if disposition is MergeDisposition.UNCHANGED:
                continue
            if disposition is MergeDisposition.PARENT_ONLY:
                preserved.append(path)
                continue
            if disposition is MergeDisposition.IDENTICAL_CHANGE:
                preserved.append(path)
                continue
            if disposition in {
                MergeDisposition.CONFLICT,
                MergeDisposition.DELETE_MODIFY_CONFLICT,
                MergeDisposition.TYPE_CONFLICT,
            }:
                conflicts.append(
                    MergeConflict(
                        conflict_id=new_workspace_id("workspace-conflict"),
                        isolation_id=record.isolation_id,
                        workspace_id=record.workspace_id,
                        logical_path=path,
                        disposition=disposition,
                        baseline_hash=_entry_hash(before),
                        parent_hash=_entry_hash(current_parent),
                        child_hash=_entry_hash(current_child),
                        repository_root=(current_child or current_parent or before).repository_root,
                        metadata={
                            "parent_unchanged": True,
                            "child_preserved": True,
                            "downstream_consumers": ["M1-05C", "M1-07C"],
                        },
                    )
                )
                continue
            if current_child is None:
                if current_parent is not None and current_parent.kind.value == "file":
                    mutations.append(
                        WorkspaceMutation(
                            mutation_id=new_workspace_id("mutation"),
                            kind=MutationKind.DELETE_FILE,
                            logical_path=path,
                            metadata={"repository_root": (before or current_parent).repository_root},
                        )
                    )
                continue
            if current_child.kind.value == "directory":
                continue
            content = read_tree_file(
                child_root,
                current_child,
                max_bytes=self.scan_limits.max_single_file_bytes,
            )
            mutations.append(
                WorkspaceMutation(
                    mutation_id=new_workspace_id("mutation"),
                    kind=MutationKind.WRITE_BYTES,
                    logical_path=path,
                    content=content,
                    metadata={"repository_root": current_child.repository_root},
                )
            )
        return tuple(mutations), tuple(conflicts), tuple(preserved)

    def _nested_outcomes(
        self,
        record: IsolationRecord,
        mutations: Sequence[WorkspaceMutation],
        conflicts: Sequence[MergeConflict],
    ) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for root in record.nested_repository_roots:
            applied = [
                item.logical_path
                for item in mutations
                if str(item.metadata.get("repository_root") or ".") == root
            ]
            conflicted = [item.logical_path for item in conflicts if item.repository_root == root]
            outcomes.append(
                {
                    "repository_root": root,
                    "outcome": "conflicted" if conflicted else ("applied" if applied else "unchanged"),
                    "applied_paths": applied,
                    "conflicted_paths": conflicted,
                }
            )
        return outcomes

    def _write_merge_receipt(
        self,
        record: IsolationRecord,
        *,
        ok: bool,
        transaction_id: str,
        applied_paths: Sequence[str],
        preserved_paths: Sequence[str],
        error_code: str = "",
    ) -> str:
        binding = self.manager.store.require_binding(record.workspace_id)
        from .integration_models import WorkerWorkspaceReceipt

        receipt = self.store.append_receipt(
            WorkerWorkspaceReceipt(
                receipt_id=new_workspace_id("workspace-receipt"),
                workspace_id=record.workspace_id,
                worker_id=record.child_worker_id,
                operation=IntegrationOperation.ISOLATION_MERGE,
                ok=ok,
                owner_epoch=binding.owner_epoch,
                binding_revision=binding.binding_revision,
                lease_id=binding.lease_id,
                transaction_id=transaction_id,
                isolation_id=record.isolation_id,
                logical_paths=tuple(applied_paths),
                error_code=error_code,
                summary=(
                    "isolated child merged through parent patch transaction"
                    if ok
                    else "isolated child merge conflicted; parent unchanged"
                ),
                metadata={
                    "preserved_parent_paths": list(preserved_paths),
                    "conflict_ids": list(record.conflict_ids),
                    "nested_repository_outcomes": dict(record.metadata).get("nested_repository_outcomes", []),
                    "downstream_consumers": ["M1-05C", "M1-07C"],
                },
            )
        )
        return f"workspace-receipt://{receipt.receipt_id}"

    def _cleanup_child_after_merge(self, record: IsolationRecord) -> None:
        try:
            child = self._child_binding(record)
            self.manager.cleanup(
                child.workspace_id,
                causation_id=record.isolation_id,
                archive=True,
            )
            self._private_access.pop(record.isolation_id, None)
        except Exception as error:
            binding = self.manager.store.require_binding(record.workspace_id)
            recovery = WorkspaceRecoveryInput(
                recovery_input_id=new_workspace_id("workspace-recovery-input"),
                workspace_id=record.workspace_id,
                operation=IntegrationOperation.ISOLATION_DISCARD,
                severity=RecoverySeverity.RETRYABLE,
                reason_code="merged_child_cleanup_failed",
                owner_epoch=binding.owner_epoch,
                binding_revision=binding.binding_revision,
                isolation_id=record.isolation_id,
                snapshot_id=record.baseline_snapshot_id,
                retryable=True,
                evidence_refs=(f"workspace-isolation://{record.isolation_id}",),
                recommended_actions=("retry_child_cleanup",),
                metadata={"error_type": type(error).__name__},
            )
            self.store.put_recovery_input(recovery)

    def _replay_merge(self, record: IsolationRecord) -> IsolationMergeResult:
        receipts = self.store.list_receipts(record.workspace_id, isolation_id=record.isolation_id)
        receipt_ref = (
            f"workspace-receipt://{receipts[-1]['receipt_id']}"
            if receipts
            else f"workspace-isolation://{record.isolation_id}"
        )
        binding = self.manager.store.require_binding(record.workspace_id)
        transaction = self.store.get_transaction(record.merge_transaction_id) if record.merge_transaction_id else None
        applied = tuple(item.logical_path for item in transaction.path_results) if transaction is not None else ()
        return IsolationMergeResult(
            isolation_id=record.isolation_id,
            workspace_id=record.workspace_id,
            state=record.state,
            transaction_id=record.merge_transaction_id,
            applied_paths=applied,
            preserved_parent_paths=tuple(dict(record.metadata).get("preserved_parent_paths") or ()),
            conflicts=(),
            owner_epoch_before=record.owner_epoch,
            owner_epoch_after=binding.owner_epoch,
            receipt_ref=receipt_ref,
            idempotent_replay=True,
        )

    def _conflicted_result(self, record: IsolationRecord, *, idempotent_replay: bool) -> IsolationMergeResult:
        conflicts = self.store.list_conflicts(
            record.workspace_id,
            isolation_id=record.isolation_id,
        )
        receipts = self.store.list_receipts(record.workspace_id, isolation_id=record.isolation_id)
        receipt_ref = (
            f"workspace-receipt://{receipts[-1]['receipt_id']}"
            if receipts
            else f"workspace-isolation://{record.isolation_id}"
        )
        return IsolationMergeResult(
            isolation_id=record.isolation_id,
            workspace_id=record.workspace_id,
            state=record.state,
            transaction_id=record.merge_transaction_id,
            applied_paths=(),
            preserved_parent_paths=(),
            conflicts=conflicts,
            owner_epoch_before=record.owner_epoch,
            owner_epoch_after=record.owner_epoch,
            receipt_ref=receipt_ref,
            idempotent_replay=idempotent_replay,
        )

    def _access_for_record(self, record: IsolationRecord) -> IsolationAccess:
        child_access = self._child_access(record)
        child_binding = self.manager.store.require_binding(child_access.workspace_id)
        return IsolationAccess(
            isolation_id=record.isolation_id,
            parent_workspace_id=record.workspace_id,
            child_workspace_id=child_binding.workspace_id,
            child_task_id=str(record.metadata.get("child_task_id") or ""),
            child_worker_id=record.child_worker_id,
            owner_epoch=child_binding.owner_epoch,
            binding_revision=child_binding.binding_revision,
            lease_id=child_binding.lease_id,
            opaque_location_ref=record.opaque_location_ref,
            expires_at=record.expires_at,
            _child_access=child_access,
        )

    def _child_binding(self, record: IsolationRecord) -> Any:
        workspace_id = str(record.metadata.get("child_workspace_id") or "")
        if not workspace_id:
            raise WorkspaceError(
                WorkspaceErrorCode.STORE_CORRUPT,
                "Workspace isolation is missing its child binding reference.",
                workspace_id=record.workspace_id,
                operation="resolve_isolated_child",
            )
        return self.manager.store.require_binding(workspace_id)

    def _child_access(self, record: IsolationRecord) -> WorkspaceAccessHandle:
        cached = self._private_access.get(record.isolation_id)
        child_binding = self._child_binding(record)
        if cached is not None and cached.owner_epoch == child_binding.owner_epoch and cached.lease_id == child_binding.lease_id:
            return cached
        access = self.manager.acquire_for_worker(
            task_id=child_binding.task_id,
            session_id=child_binding.session_id,
            worker_id=record.child_worker_id,
        )
        self._private_access[record.isolation_id] = access
        return access

    def _validate_parent_access(self, access: WorkspaceAccessHandle, binding: Any, *, operation: str) -> None:
        if (
            access.workspace_id != binding.workspace_id
            or access.owner_epoch != binding.owner_epoch
            or access.lease_id != binding.lease_id
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.OWNER_EPOCH_STALE,
                "Parent workspace access is stale.",
                workspace_id=binding.workspace_id,
                operation=operation,
            )
        lease = self.manager.store.get_lease(access.lease_id)
        if lease is None:
            raise WorkspaceError(
                WorkspaceErrorCode.LEASE_NOT_FOUND,
                "Parent workspace access references a missing lease.",
                workspace_id=binding.workspace_id,
                operation=operation,
            )
        self.manager.backend.validate_lease(
            binding,
            lease,
            fence_token=access.fence_token,
            operation=WorkspaceOperation.WRITE,
        )

    def _require_enabled(self, workspace_id: str) -> None:
        if self.disabled:
            raise WorkspaceError(
                WorkspaceErrorCode.DISABLED,
                "Workspace isolation runtime is disabled; shared parent execution is forbidden.",
                workspace_id=workspace_id,
                operation="workspace_isolation_runtime",
            )


def _child_task_id(parent_task_id: str, isolation_id: str) -> str:
    digest = hashlib.sha256(f"{parent_task_id}:{isolation_id}".encode("utf-8")).hexdigest()[:20]
    prefix = "".join(character if character.isalnum() or character in "-_" else "-" for character in parent_task_id)
    prefix = prefix[:48].strip("-") or "task"
    return f"{prefix}-iso-{digest}"


def _entry_hash(entry: Any | None) -> str:
    if entry is None:
        return "absent"
    return str(entry.content_hash or entry.identity)


def _content_projection(manifest: Any) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (item.logical_path, item.kind.value, item.content_hash, item.size, item.repository_root)
        for item in manifest.entries
    )


def _bounded_shell_environment(values: Mapping[str, str] | None) -> dict[str, str]:
    selected_names = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "HOME",
        "USERPROFILE",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
    }
    environment = {name: value for name, value in os.environ.items() if name.upper() in selected_names}
    for name, value in dict(values or {}).items():
        normalized = str(name).upper()
        if normalized not in selected_names:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Isolated shell environment contains a non-allowlisted variable.",
                operation="build_isolated_shell_environment",
                metadata={"variable": str(name)},
            )
        environment[str(name)] = str(value)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


__all__ = [
    "IsolatedShellResult",
    "IsolationAccess",
    "WorkspaceIsolationRuntime",
]
