from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from .dispatch import DeploymentDispatchRuntime
from .errors import DispatchRejected, HandoffRejected, PlacementRejected
from .handoff import CheckpointHandoffRuntime, HandoffReceipt
from .models import (
    DeploymentProfile,
    DispatchReceipt,
    DispatchStatus,
    NodeObservation,
    PlacementDecision,
    Workload,
    new_id,
    now_iso,
)
from .node_client import DeploymentNodeClient
from .placement import PlacementContext, PlacementPolicyRuntime
from .state_store import DeploymentStateStore


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    recovery_id: str
    workload_id: str
    failed_attempt_id: str
    failure_code: str
    source_profile: DeploymentProfile
    target_profile: DeploymentProfile
    decision: PlacementDecision
    handoff: HandoffReceipt | None
    receipt: DispatchReceipt
    degraded: bool
    created_at: str
    handoff_failure: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.deployment-recovery-outcome/v1",
            "recovery_id": self.recovery_id,
            "workload_id": self.workload_id,
            "failed_attempt_id": self.failed_attempt_id,
            "failure_code": self.failure_code,
            "source_profile": self.source_profile.value,
            "target_profile": self.target_profile.value,
            "decision": self.decision.to_dict(),
            "handoff": self.handoff.to_dict() if self.handoff else None,
            "receipt": self.receipt.to_dict(),
            "degraded": self.degraded,
            "created_at": self.created_at,
            "handoff_failure": (
                dict(self.handoff_failure) if self.handoff_failure else None
            ),
            "fallback": False,
        }


class DeploymentRecoveryRuntime:
    def __init__(
        self,
        *,
        store: DeploymentStateStore,
        placement: PlacementPolicyRuntime,
        dispatch: DeploymentDispatchRuntime,
        handoff: CheckpointHandoffRuntime,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.placement = placement
        self.dispatch = dispatch
        self.handoff = handoff
        self.enabled = enabled

    def recover(
        self,
        *,
        workload: Workload,
        failed_receipt: DispatchReceipt,
        observations: Mapping[DeploymentProfile, NodeObservation],
        clients: Mapping[DeploymentProfile, DeploymentNodeClient],
        source_client: DeploymentNodeClient | None = None,
        allow_degraded: bool = True,
        unavailable_profiles: frozenset[DeploymentProfile] = frozenset(),
    ) -> RecoveryOutcome:
        if not self.enabled:
            raise DispatchRejected(
                "deployment_recovery_disabled",
                "deployment recovery runtime is disabled",
                operation="recover",
                profile=failed_receipt.profile.value,
            )
        if failed_receipt.workload_id != workload.workload_id:
            raise DispatchRejected(
                "deployment_recovery_workload_mismatch",
                "failed receipt does not belong to the recovery workload",
                operation="recover",
                profile=failed_receipt.profile.value,
            )
        excluded = frozenset({failed_receipt.profile})
        context = PlacementContext(
            observations=observations,
            unavailable_profiles=unavailable_profiles,
            excluded_profiles=excluded,
            allow_degraded=allow_degraded,
            recovery=True,
        )
        recovery_workload = self._degraded_workload_if_needed(
            workload,
            failed_profile=failed_receipt.profile,
        )
        decision = self.placement.decide(recovery_workload, context)
        target_client = clients.get(decision.selected_profile)
        if target_client is None:
            raise PlacementRejected(
                "deployment_recovery_client_missing",
                "selected recovery profile has no authenticated node client",
                operation="recover",
                profile=decision.selected_profile.value,
            )
        target_attempt_id = new_id("attempt")
        handoff_receipt: HandoffReceipt | None = None
        handoff_failure: Mapping[str, Any] | None = None
        checkpoint_ref = ""
        if failed_receipt.checkpoint_ref and source_client is not None:
            try:
                handoff_receipt = self.handoff.handoff(
                    workload=workload,
                    source_receipt=failed_receipt,
                    target_attempt_id=target_attempt_id,
                    source_client=source_client,
                    target_client=target_client,
                    target_profile=decision.selected_profile,
                )
                checkpoint_ref = handoff_receipt.imported_checkpoint_ref
            except HandoffRejected as error:
                handoff_failure = error.to_dict()
                if workload.checkpoint_ref:
                    raise
                # A dispatch that failed before producing usable output may
                # have no exportable node checkpoint. Recovery can restart a
                # deterministic workload from its immutable input, but this is
                # explicitly recorded as degraded rather than exact resume.
                handoff_receipt = None
        recovered_workload = replace(
            recovery_workload,
            idempotency_key=digest_recovery_idempotency(
                recovery_workload,
                predecessor_attempt_id=failed_receipt.attempt_id,
                target_profile=decision.selected_profile,
            ),
            checkpoint_ref=checkpoint_ref or workload.checkpoint_ref,
        )
        receipt = self.dispatch.dispatch(
            recovered_workload,
            decision,
            target_client,
            predecessor_attempt_id=failed_receipt.attempt_id,
            checkpoint_ref=recovered_workload.checkpoint_ref,
            attempt_id=target_attempt_id,
        )
        degraded = (
            recovery_workload != workload
            or handoff_receipt is None
            or receipt.status is not DispatchStatus.SUCCEEDED
        )
        outcome = RecoveryOutcome(
            recovery_id=new_id("deployment_recovery"),
            workload_id=workload.workload_id,
            failed_attempt_id=failed_receipt.attempt_id,
            failure_code=failed_receipt.failure_code or "deployment_attempt_failed",
            source_profile=failed_receipt.profile,
            target_profile=decision.selected_profile,
            decision=decision,
            handoff=handoff_receipt,
            receipt=receipt,
            degraded=degraded,
            created_at=now_iso(),
            handoff_failure=handoff_failure,
        )
        self.store.append_event(
            "deployment.recovery_completed",
            outcome.to_dict(),
            component_id=receipt.node_id,
            profile=receipt.profile.value,
            task_id=workload.task_id,
            run_id=workload.run_id,
            causation_id=failed_receipt.attempt_id,
            correlation_id=receipt.attempt_id,
        )
        return outcome

    @staticmethod
    def _degraded_workload_if_needed(
        workload: Workload,
        *,
        failed_profile: DeploymentProfile,
    ) -> Workload:
        operation = workload.operation
        payload = dict(workload.payload)
        required = tuple(
            item
            for item in workload.required_capabilities
            if item
            not in {
                "provider-dispatch",
                f"{failed_profile.value}-compute",
            }
        )
        if "deterministic-transform" not in required:
            required = (*required, "deterministic-transform")
        provider_degraded = (
            workload.provider_required
            and failed_profile is DeploymentProfile.CLOUD
        )
        if operation == "provider-capability" and provider_degraded:
            operation = "hash-manifest"
            payload = {
                "entries": {
                    "provider": workload.preferred_provider,
                    "model": workload.preferred_model,
                    "degraded": True,
                    "reason": "cloud-provider-unavailable",
                }
            }
            required = ("deterministic-transform",)
        return replace(
            workload,
            operation=operation,
            payload=payload,
            provider_required=False,
            preferred_provider="",
            preferred_model="",
            required_capabilities=required,
            complexity=min(workload.complexity, 6),
            latency_sla_ms=max(workload.latency_sla_ms, 1500),
        )


def digest_recovery_idempotency(
    workload: Workload,
    *,
    predecessor_attempt_id: str,
    target_profile: DeploymentProfile,
) -> str:
    from .models import digest

    return digest(
        {
            "kind": "deployment-recovery",
            "workload_digest": workload.workload_digest,
            "predecessor_attempt_id": predecessor_attempt_id,
            "target_profile": target_profile.value,
        }
    )


__all__ = [
    "DeploymentRecoveryRuntime",
    "RecoveryOutcome",
    "digest_recovery_idempotency",
]
