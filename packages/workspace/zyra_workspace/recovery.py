from __future__ import annotations

"""Restart continuation for composite workspace operations."""

from typing import Any

from .errors import WorkspaceError, WorkspaceErrorCode, error_from_exception
from .integration_models import (
    IntegrationOperation,
    IntegrationPhase,
    RecoverySeverity,
    RebindState,
    WorkspaceRecoveryInput,
)
from .isolation import WorkspaceIsolationRuntime
from .models import new_workspace_id, utc_now
from .rebind import WorkspaceRebindRuntime


class WorkspaceOperationRecoveryRuntime:
    """Convert interrupted journal phases into deterministic local outcomes.

    Global recovery/replan remains owned by M1-07C.  This runtime only closes
    local rollback, child cleanup and rebind continuation, then emits typed
    inputs for anything requiring backend placement or broader replanning.
    """

    def __init__(self, manager: Any, *, disabled: bool = False) -> None:
        self.manager = manager
        self.store = manager.integration_store
        self.disabled = bool(disabled)

    def recover(self) -> tuple[WorkspaceRecoveryInput, ...]:
        if self.disabled or not self.manager.config.local_enabled:
            return ()
        recoveries: list[WorkspaceRecoveryInput] = []
        recoveries.extend(self._recover_transactions())
        recoveries.extend(WorkspaceIsolationRuntime(self.manager).recover_on_startup())
        recoveries.extend(WorkspaceRebindRuntime(self.manager).recover_on_startup())
        return tuple(_deduplicate_recoveries(recoveries))

    def _recover_transactions(self) -> tuple[WorkspaceRecoveryInput, ...]:
        recoveries: list[WorkspaceRecoveryInput] = []
        interrupted = self.store.interrupted_operations().get("transactions", ())
        for transaction_id in interrupted:
            record = self.store.require_transaction(transaction_id)
            binding = self.manager.store.require_binding(record.workspace_id)
            if record.phase in {
                IntegrationPhase.REQUESTED,
                IntegrationPhase.VALIDATING,
                IntegrationPhase.STAGING,
            } and not record.snapshot_id:
                failed = record.advance(
                    IntegrationPhase.FAILED,
                    owner_epoch_after=binding.owner_epoch,
                    binding_revision_after=binding.binding_revision,
                    lease_id_after=binding.lease_id,
                    completed_at=utc_now(),
                    error_code=WorkspaceErrorCode.OPERATION_IN_PROGRESS.value,
                    message="interrupted patch had not crossed the filesystem mutation boundary",
                )
                self.store.update_transaction(failed, expected_revision=record.revision)
                continue
            if record.snapshot_id:
                try:
                    self.manager.restore(
                        record.workspace_id,
                        record.snapshot_id,
                        causation_id=record.transaction_id,
                    )
                    latest = self.manager.store.require_binding(record.workspace_id)
                    current = self.store.require_transaction(record.transaction_id)
                    rolled = current.advance(
                        IntegrationPhase.ROLLED_BACK,
                        owner_epoch_after=latest.owner_epoch,
                        binding_revision_after=latest.binding_revision,
                        lease_id_after=latest.lease_id,
                        completed_at=utc_now(),
                        error_code=WorkspaceErrorCode.OPERATION_IN_PROGRESS.value,
                        message="interrupted patch restored its committed undo snapshot on restart",
                    )
                    self.store.update_transaction(rolled, expected_revision=current.revision)
                    continue
                except Exception as error:
                    normalized = error_from_exception(
                        error,
                        workspace_id=record.workspace_id,
                        operation="recover_workspace_transaction",
                    )
                    latest = self.manager.store.require_binding(record.workspace_id)
                    recovery = WorkspaceRecoveryInput(
                        recovery_input_id=new_workspace_id("workspace-recovery-input"),
                        workspace_id=record.workspace_id,
                        operation=IntegrationOperation.PATCH,
                        severity=RecoverySeverity.BACKEND_UNAVAILABLE_CANDIDATE,
                        reason_code="interrupted_patch_restore_failed",
                        owner_epoch=latest.owner_epoch,
                        binding_revision=latest.binding_revision,
                        transaction_id=record.transaction_id,
                        snapshot_id=record.snapshot_id,
                        retryable=True,
                        evidence_refs=(
                            f"workspace-transaction://{record.transaction_id}",
                            f"workspace-snapshot://{record.snapshot_id}",
                        ),
                        recommended_actions=(
                            "verify_snapshot",
                            "fence_workspace_writes",
                            "replan_backend_placement",
                        ),
                        metadata={
                            "error_code": normalized.code.value,
                            "downstream_consumers": ["M1-05C", "M1-07C"],
                        },
                    )
                    recovery = self.store.put_recovery_input(recovery)
                    recoveries.append(recovery)
                    current = self.store.require_transaction(record.transaction_id)
                    quarantined = current.advance(
                        IntegrationPhase.QUARANTINED,
                        owner_epoch_after=latest.owner_epoch,
                        binding_revision_after=latest.binding_revision,
                        lease_id_after=latest.lease_id,
                        completed_at=utc_now(),
                        error_code=normalized.code.value,
                        recovery_input_id=recovery.recovery_input_id,
                        message="interrupted patch rollback failed and was quarantined",
                    )
                    self.store.update_transaction(quarantined, expected_revision=current.revision)
                    continue
            recovery = WorkspaceRecoveryInput(
                recovery_input_id=new_workspace_id("workspace-recovery-input"),
                workspace_id=record.workspace_id,
                operation=IntegrationOperation.PATCH,
                severity=RecoverySeverity.RECOVERY_INPUT,
                reason_code="interrupted_patch_has_no_undo_snapshot",
                owner_epoch=binding.owner_epoch,
                binding_revision=binding.binding_revision,
                transaction_id=record.transaction_id,
                retryable=False,
                evidence_refs=(f"workspace-transaction://{record.transaction_id}",),
                recommended_actions=("quarantine_workspace", "inspect_transaction"),
                metadata={"transaction_phase": record.phase.value},
            )
            recoveries.append(self.store.put_recovery_input(recovery))
        return tuple(recoveries)


def _deduplicate_recoveries(
    values: list[WorkspaceRecoveryInput],
) -> tuple[WorkspaceRecoveryInput, ...]:
    observed: set[str] = set()
    result: list[WorkspaceRecoveryInput] = []
    for value in values:
        if value.recovery_input_id in observed:
            continue
        observed.add(value.recovery_input_id)
        result.append(value)
    return tuple(result)


__all__ = ["WorkspaceOperationRecoveryRuntime"]
