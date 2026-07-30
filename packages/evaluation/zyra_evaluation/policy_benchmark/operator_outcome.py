from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zyra_orchestration.topology_policy.contracts import canonical_digest
from zyra_scheduler.operator_policy.integration import (
    OperatorPlacementTaskResult,
)
from zyra_scheduler.worker_pool.application import WorkerPoolFoundationRuntime
from zyra_scheduler.worker_pool.models import AttemptState, LeaseState, parse_utc


OPERATOR_CAUSAL_CHAIN_SCHEMA = "zyra.operator-causal-chain/v1"
OPERATOR_OUTCOME_ASSESSMENT_SCHEMA = "zyra.operator-outcome-assessment/v1"


class OperatorOutcomeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OperatorCausalChainReceipt:
    run_id: str
    task_id: str
    mechanism_input_ref: str
    mechanism_receipt_ref: str
    operator_proposal_ref: str
    operator_proposal_digest: str
    scheduler_decision_ref: str
    scheduler_decision_digest: str
    lease_refs: tuple[str, ...]
    attempt_refs: tuple[str, ...]
    call_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    verifier_refs: tuple[str, ...]
    outcome_ref: str
    outcome_digest: str
    recovery_refs: tuple[str, ...]
    baseline_profile: str
    route_mode: str
    schema_version: str = OPERATOR_CAUSAL_CHAIN_SCHEMA

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "mechanism_input_ref": self.mechanism_input_ref,
            "mechanism_receipt_ref": self.mechanism_receipt_ref,
            "operator_proposal_ref": self.operator_proposal_ref,
            "operator_proposal_digest": self.operator_proposal_digest,
            "scheduler_decision_ref": self.scheduler_decision_ref,
            "scheduler_decision_digest": self.scheduler_decision_digest,
            "lease_refs": list(self.lease_refs),
            "attempt_refs": list(self.attempt_refs),
            "call_refs": list(self.call_refs),
            "artifact_refs": list(self.artifact_refs),
            "verifier_refs": list(self.verifier_refs),
            "outcome_ref": self.outcome_ref,
            "outcome_digest": self.outcome_digest,
            "recovery_refs": list(self.recovery_refs),
            "baseline_profile": self.baseline_profile,
            "route_mode": self.route_mode,
        }
        if include_digest:
            value["digest"] = self.digest
        return value


@dataclass(frozen=True, slots=True)
class OperatorOutcomeAssessment:
    passed: bool
    chain: OperatorCausalChainReceipt
    checks: Mapping[str, bool]
    errors: tuple[str, ...]
    lease_precedes_execution_count: int
    duplicate_side_effect_count: int
    execution_without_lease_count: int
    stale_or_revoked_execution_count: int
    schema_version: str = OPERATOR_OUTCOME_ASSESSMENT_SCHEMA

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "passed": self.passed,
            "chain": self.chain.to_dict(),
            "checks": dict(self.checks),
            "errors": list(self.errors),
            "lease_precedes_execution_count": (
                self.lease_precedes_execution_count
            ),
            "duplicate_side_effect_count": self.duplicate_side_effect_count,
            "execution_without_lease_count": self.execution_without_lease_count,
            "stale_or_revoked_execution_count": (
                self.stale_or_revoked_execution_count
            ),
        }
        if include_digest:
            value["digest"] = self.digest
        return value


def evaluate_operator_outcome(
    result: OperatorPlacementTaskResult,
    *,
    pool: WorkerPoolFoundationRuntime,
) -> OperatorOutcomeAssessment:
    errors: list[str] = []
    attempts = result.attempt_receipts
    candidate_set = result.candidate_set
    adaptive = result.adaptive_result
    selected_refs = set(result.placement.selected_operator_refs)
    executed_refs = {item.operator_ref for item in attempts}
    checks: dict[str, bool] = {
        "placement_owner_preserved": (
            result.placement.placement_owner == "ResourceScheduler"
        ),
        "lease_owner_preserved": (
            result.placement.lease_owner == "WorkerPoolFoundationRuntime"
        ),
        "candidate_set_bound": (
            result.mode == "baseline"
            or (
                candidate_set is not None
                and result.placement.candidate_set_digest
                == candidate_set.digest
            )
        ),
        "executed_subset_of_scheduler_selection": (
            bool(executed_refs) and executed_refs.issubset(selected_refs)
        ),
        "adaptive_outcome_present": (
            result.mode == "baseline" or adaptive is not None
        ),
        "artifact_and_verifier_complete": (
            bool(
                adaptive
                and adaptive.cost_receipt.artifact_complete
                and adaptive.cost_receipt.verifier_passed
            )
            or (
                result.mode == "baseline"
                and any(item.artifact_refs for item in attempts)
                and any(item.verification_refs for item in attempts)
            )
        ),
    }
    execution_without_lease = 0
    stale_or_revoked = 0
    ordered_count = 0
    lease_ids: set[str] = set()
    attempt_ids: set[str] = set()
    call_refs: set[str] = set()
    for receipt in attempts:
        lease = pool.store.get_lease(receipt.lease_id)
        attempt = pool.store.get_attempt(receipt.attempt_id)
        canonical_receipts = pool.store.receipts_for_task(receipt.task_id)
        completion = next(
            (
                item
                for item in canonical_receipts
                if item.receipt_id == receipt.completion_receipt_ref
            ),
            None,
        )
        if lease is None or attempt is None or completion is None:
            execution_without_lease += 1
            errors.append(
                f"canonical lease/attempt/completion missing for {receipt.operator_ref}"
            )
            continue
        if (
            lease.state is not LeaseState.RELEASED
            or attempt.state is not AttemptState.SUCCEEDED
            or completion.lease_id != lease.lease_id
            or completion.attempt_id != attempt.attempt_id
        ):
            stale_or_revoked += 1
            errors.append(
                f"canonical operator attempt is not a successful released execution: "
                f"{receipt.operator_ref}"
            )
        if (
            parse_utc(receipt.lease_acquired_at)
            <= parse_utc(receipt.attempt_started_at)
            <= parse_utc(receipt.call_started_at)
            <= parse_utc(receipt.call_finished_at)
        ):
            ordered_count += 1
        else:
            errors.append(
                f"lease/attempt/call timestamp order invalid: {receipt.operator_ref}"
            )
        lease_ids.add(receipt.lease_id)
        attempt_ids.add(receipt.attempt_id)
        call_refs.add(receipt.call_ref)
    duplicate_side_effects = (
        len(attempts) - len({item.operator_idempotency_key for item in attempts})
    )
    if duplicate_side_effects:
        errors.append("duplicate operator idempotency keys produced side-effect receipts")
    checks["canonical_attempts_complete"] = (
        bool(attempts)
        and execution_without_lease == 0
        and stale_or_revoked == 0
    )
    checks["lease_precedes_every_execution"] = (
        bool(attempts) and ordered_count == len(attempts)
    )
    checks["no_duplicate_side_effect"] = duplicate_side_effects == 0
    checks["causal_refs_reach_outcome"] = (
        bool(
            adaptive
            and result.placement.decision_id
            in adaptive.policy_outcome.causal_refs
            and all(
                item.completion_receipt_ref
                in adaptive.policy_outcome.causal_refs
                for item in attempts
            )
        )
        or (
            result.mode == "baseline"
            and bool(attempts)
            and all(item.completion_receipt_ref for item in attempts)
        )
    )
    for name, passed in checks.items():
        if not passed:
            errors.append(f"failed check: {name}")
    chain = _causal_chain(result)
    return OperatorOutcomeAssessment(
        passed=not errors,
        chain=chain,
        checks=checks,
        errors=tuple(errors),
        lease_precedes_execution_count=ordered_count,
        duplicate_side_effect_count=duplicate_side_effects,
        execution_without_lease_count=execution_without_lease,
        stale_or_revoked_execution_count=stale_or_revoked,
    )


def implementation_validated_readiness_revision(
    assessment: OperatorOutcomeAssessment,
    *,
    slice_id: str,
    slice_base_commit: str,
    implementation_commit: str,
    mechanism_version: str,
    configuration_digest: str,
    generated_at: str,
) -> dict[str, Any]:
    if not assessment.passed:
        raise OperatorOutcomeError(
            "cannot advance MaAS readiness from a failed operator outcome"
        )
    report = {
        "schema": "zyra.mechanism-evidence-readiness-report/v1",
        "slice_id": slice_id,
        "slice_base_commit": slice_base_commit,
        "implementation_commit": implementation_commit,
        "generated_at": generated_at,
        "readiness_stage": "implementation_validated",
        "mechanism_statuses": {"maas": "deterministic_ready"},
        "mechanisms": {
            "maas": {
                "mechanism_id": "maas",
                "mechanism_version": mechanism_version,
                "readiness_stage": "implementation_validated",
                "status": "deterministic_ready",
                "status_scope": (
                    "operator selection through scheduler placement, canonical "
                    "lease, execution, artifact, verifier and outcome; global "
                    "activation remains deferred"
                ),
                "configuration_digest": configuration_digest,
                "causal_chain_digest": assessment.chain.digest,
                "validation": {
                    "passed": True,
                    "lease_precedes_execution_count": (
                        assessment.lease_precedes_execution_count
                    ),
                    "duplicate_side_effect_count": (
                        assessment.duplicate_side_effect_count
                    ),
                    "execution_without_lease_count": (
                        assessment.execution_without_lease_count
                    ),
                    "stale_or_revoked_execution_count": (
                        assessment.stale_or_revoked_execution_count
                    ),
                    "checks": dict(assessment.checks),
                },
                "activation_allowed": False,
                "deferred_to": "P2-S06-01",
            }
        },
        "no_policy_training_audit": {
            "passed": True,
            "training_sample_count": 0,
            "datasets": [],
            "checkpoints": [],
            "mutable_learned_parameters": [],
            "random_sampling_entry_points": [],
            "pretrained_model_used": False,
        },
        "owner_boundary": {
            "placement_owner": "ResourceScheduler",
            "lease_owner": "WorkerPoolFoundationRuntime",
            "physical_attempt_owner": "WorkerLeaseManager",
            "recovery_owner": "RecoveryPlanner",
            "canonical_owner_transfer_count": 0,
        },
    }
    report["report_digest"] = canonical_digest(report)
    return report


def _causal_chain(
    result: OperatorPlacementTaskResult,
) -> OperatorCausalChainReceipt:
    candidate_set = result.candidate_set
    adaptive = result.adaptive_result
    attempts = result.attempt_receipts
    return OperatorCausalChainReceipt(
        run_id=result.placement.run_id,
        task_id=result.placement.task_id,
        mechanism_input_ref=(
            candidate_set.input_snapshot_digest if candidate_set else ""
        ),
        mechanism_receipt_ref=(
            candidate_set.mechanism_receipt_ref if candidate_set else ""
        ),
        operator_proposal_ref=(
            candidate_set.proposal_id if candidate_set else ""
        ),
        operator_proposal_digest=(
            candidate_set.proposal_digest if candidate_set else ""
        ),
        scheduler_decision_ref=result.placement.decision_id,
        scheduler_decision_digest=result.placement.digest,
        lease_refs=tuple(item.lease_id for item in attempts),
        attempt_refs=tuple(item.attempt_id for item in attempts),
        call_refs=tuple(item.call_ref for item in attempts),
        artifact_refs=tuple(
            sorted(
                {
                    artifact
                    for item in attempts
                    for artifact in item.artifact_refs
                }
            )
        ),
        verifier_refs=tuple(
            sorted(
                {
                    verifier
                    for item in attempts
                    for verifier in item.verification_refs
                }
            )
        ),
        outcome_ref=(
            adaptive.policy_outcome.header.contract_id
            if adaptive
            else attempts[-1].completion_receipt_ref
            if attempts
            else ""
        ),
        outcome_digest=(
            adaptive.policy_outcome.digest
            if adaptive
            else attempts[-1].digest
            if attempts
            else ""
        ),
        recovery_refs=result.recovery_plan_refs,
        baseline_profile=result.baseline_profile,
        route_mode=result.placement.route_mode,
    )


__all__ = [
    "OPERATOR_CAUSAL_CHAIN_SCHEMA",
    "OPERATOR_OUTCOME_ASSESSMENT_SCHEMA",
    "OperatorCausalChainReceipt",
    "OperatorOutcomeAssessment",
    "OperatorOutcomeError",
    "evaluate_operator_outcome",
    "implementation_validated_readiness_revision",
]
