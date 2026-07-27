from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import HandoffRejected
from .models import (
    DeploymentProfile,
    DispatchReceipt,
    Workload,
    digest,
    new_id,
    now_iso,
)
from .node_client import DeploymentNodeClient
from .state_store import DeploymentStateStore


class CanonicalCheckpointVerifier(Protocol):
    def __call__(
        self,
        *,
        task_id: str,
        run_id: str,
        checkpoint_ref: str,
    ) -> bool | Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HandoffReceipt:
    handoff_id: str
    workload_id: str
    source_profile: DeploymentProfile
    target_profile: DeploymentProfile
    source_attempt_id: str
    target_attempt_id: str
    source_checkpoint_ref: str
    imported_checkpoint_ref: str
    canonical_checkpoint_ref: str
    source_checksum: str
    import_checksum: str
    verified: bool
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.deployment-checkpoint-handoff-receipt/v1",
            "handoff_id": self.handoff_id,
            "workload_id": self.workload_id,
            "source_profile": self.source_profile.value,
            "target_profile": self.target_profile.value,
            "source_attempt_id": self.source_attempt_id,
            "target_attempt_id": self.target_attempt_id,
            "source_checkpoint_ref": self.source_checkpoint_ref,
            "imported_checkpoint_ref": self.imported_checkpoint_ref,
            "canonical_checkpoint_ref": self.canonical_checkpoint_ref,
            "source_checksum": self.source_checksum,
            "import_checksum": self.import_checksum,
            "verified": self.verified,
            "created_at": self.created_at,
            "canonical_checkpoint_owner": False,
            "fallback": False,
        }


class CheckpointHandoffRuntime:
    def __init__(
        self,
        store: DeploymentStateStore,
        *,
        canonical_verifier: CanonicalCheckpointVerifier | None = None,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.canonical_verifier = canonical_verifier
        self.enabled = enabled

    def handoff(
        self,
        *,
        workload: Workload,
        source_receipt: DispatchReceipt,
        target_attempt_id: str,
        source_client: DeploymentNodeClient,
        target_client: DeploymentNodeClient,
        target_profile: DeploymentProfile,
    ) -> HandoffReceipt:
        if not self.enabled:
            raise HandoffRejected(
                "deployment_checkpoint_handoff_disabled",
                "deployment checkpoint handoff is disabled",
                operation="handoff",
                profile=target_profile.value,
            )
        if source_receipt.workload_id != workload.workload_id:
            raise HandoffRejected(
                "deployment_handoff_workload_mismatch",
                "source receipt does not belong to the workload",
                operation="handoff",
                profile=target_profile.value,
            )
        if not source_receipt.checkpoint_ref:
            raise HandoffRejected(
                "deployment_handoff_checkpoint_missing",
                "source attempt did not produce a checkpoint",
                operation="handoff",
                profile=target_profile.value,
            )
        exported = source_client.export_checkpoint(source_receipt.checkpoint_ref)
        checkpoint = exported.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            raise HandoffRejected(
                "deployment_handoff_export_invalid",
                "source node returned an invalid checkpoint export",
                operation="handoff",
                profile=source_receipt.profile.value,
            )
        source_checksum = str(checkpoint.get("checksum") or "")
        semantic = {key: value for key, value in checkpoint.items() if key != "checksum"}
        if not source_checksum or digest(semantic) != source_checksum:
            raise HandoffRejected(
                "deployment_handoff_export_checksum_invalid",
                "source node checkpoint export checksum is invalid",
                operation="handoff",
                profile=source_receipt.profile.value,
            )
        canonical_ref = workload.checkpoint_ref or source_receipt.checkpoint_ref
        canonical_ready = self._verify_canonical(
            workload,
            checkpoint_ref=canonical_ref,
        )
        if not canonical_ready:
            raise HandoffRejected(
                "deployment_handoff_canonical_checkpoint_rejected",
                "canonical checkpoint owner did not accept the handoff reference",
                operation="handoff",
                profile=target_profile.value,
                details={"canonical_checkpoint_ref": canonical_ref},
            )
        staged_ref = new_id("deployment_handoff")
        self.store.save_checkpoint(
            checkpoint_ref=staged_ref,
            workload_id=workload.workload_id,
            task_id=workload.task_id,
            run_id=workload.run_id,
            source_profile=source_receipt.profile,
            source_attempt_id=source_receipt.attempt_id,
            canonical_checkpoint_ref=canonical_ref,
            payload={
                "source_node_id": source_receipt.node_id,
                "source_checkpoint_ref": source_receipt.checkpoint_ref,
                "source_checksum": source_checksum,
                "target_profile": target_profile.value,
                "target_attempt_id": target_attempt_id,
            },
            committed=True,
        )
        imported = target_client.import_checkpoint(
            checkpoint,
            source_node_id=source_receipt.node_id,
        )
        imported_checkpoint = imported.get("checkpoint")
        if not isinstance(imported_checkpoint, Mapping):
            raise HandoffRejected(
                "deployment_handoff_import_invalid",
                "target node returned an invalid checkpoint import",
                operation="handoff",
                profile=target_profile.value,
            )
        import_checksum = str(imported_checkpoint.get("checksum") or "")
        imported_semantic = {
            key: value
            for key, value in imported_checkpoint.items()
            if key != "checksum"
        }
        if not import_checksum or digest(imported_semantic) != import_checksum:
            raise HandoffRejected(
                "deployment_handoff_import_checksum_invalid",
                "target node checkpoint import checksum is invalid",
                operation="handoff",
                profile=target_profile.value,
            )
        consumed = self.store.consume_checkpoint(
            staged_ref,
            attempt_id=target_attempt_id,
        )
        if consumed.get("consumed_by_attempt_id") != target_attempt_id:
            raise HandoffRejected(
                "deployment_handoff_consumption_invalid",
                "deployment handoff was not consumed by the target attempt",
                operation="handoff",
                profile=target_profile.value,
            )
        receipt = HandoffReceipt(
            handoff_id=staged_ref,
            workload_id=workload.workload_id,
            source_profile=source_receipt.profile,
            target_profile=target_profile,
            source_attempt_id=source_receipt.attempt_id,
            target_attempt_id=target_attempt_id,
            source_checkpoint_ref=source_receipt.checkpoint_ref,
            imported_checkpoint_ref=str(
                imported_checkpoint.get("checkpoint_ref") or ""
            ),
            canonical_checkpoint_ref=canonical_ref,
            source_checksum=source_checksum,
            import_checksum=import_checksum,
            verified=True,
            created_at=now_iso(),
        )
        self.store.append_event(
            "deployment.checkpoint_handoff_completed",
            receipt.to_dict(),
            component_id=source_receipt.node_id,
            profile=target_profile.value,
            task_id=workload.task_id,
            run_id=workload.run_id,
            causation_id=source_receipt.attempt_id,
            correlation_id=target_attempt_id,
        )
        return receipt

    def _verify_canonical(
        self,
        workload: Workload,
        *,
        checkpoint_ref: str,
    ) -> bool:
        if self.canonical_verifier is None:
            # A deployment-node checkpoint is a derived continuity record. If
            # there is no task checkpoint ref, the derived record can move but
            # cannot claim canonical exact-resume custody.
            return not workload.checkpoint_ref or bool(checkpoint_ref)
        result = self.canonical_verifier(
            task_id=workload.task_id,
            run_id=workload.run_id,
            checkpoint_ref=checkpoint_ref,
        )
        if isinstance(result, Mapping):
            return (
                result.get("ready") is True
                or result.get("accepted") is True
                or result.get("verified") is True
            )
        return result is True


__all__ = [
    "CanonicalCheckpointVerifier",
    "CheckpointHandoffRuntime",
    "HandoffReceipt",
]
