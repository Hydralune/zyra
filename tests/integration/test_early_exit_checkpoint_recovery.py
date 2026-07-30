from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from zyra_core import PlanNodeStatus
from zyra_scheduler import ExitDecision, minimum_operator_path
from zyra_scheduler.recovery_runtime import (
    CheckpointCommitRequest,
    CheckpointCommitRuntime,
    RecoveryPlanStore,
    RecoveryRefs,
    SideEffectFenceRuntime,
)

from tests.integration.test_adaptive_depth_verifier import (
    NOW,
    OBLIGATIONS,
    _execute,
    _harness,
    _policy_input,
)


def _bound_checkpoint_request(harness, result) -> CheckpointCommitRequest:
    return harness.gate.bind_checkpoint_request(
        CheckpointCommitRequest(
            refs=RecoveryRefs(
                run_id=harness.task.run_id,
                task_id=harness.task.task_id,
                session_id=harness.checkpoint.session_id,
            ),
            workflow_signature=harness.checkpoint.workflow_signature,
            graph_signature=harness.checkpoint.graph_signature,
            topology_signature=harness.checkpoint.topology_signature,
            owner_refs=dict(harness.checkpoint.owner_refs),
            version_refs=dict(harness.checkpoint.version_refs),
            committed_refs=tuple(harness.checkpoint.committed_refs),
            completed_step_ids=("adaptive-depth-exit-evaluated",),
            side_effect_fence_keys=tuple(
                harness.checkpoint.side_effect_fence_keys
            ),
            state_payload={"phase": "early-exit-checkpointed"},
            metadata={"requirement_revision": harness.policy_input.requirement_revision},
            expected_revision=harness.checkpoint.commit_revision,
        ),
        result.checkpoint_binding,
        result.final_decision,
    )


def _capture(harness, *, checkpoint, policy_input=None, proposal=None):
    selected_policy = policy_input or harness.policy_input
    selected_proposal = proposal or harness.proposal
    minimum_operators, minimum_verification = minimum_operator_path(
        selected_proposal
    )
    return harness.builder.capture(
        task=harness.task,
        policy_input=selected_policy,
        proposal=selected_proposal,
        checkpoint=checkpoint,
        continuity_receipt=harness.continuity,
        required_artifact_ids=("deliverable",),
        minimum_operator_refs=minimum_operators,
        minimum_verification_refs=minimum_verification,
        remaining_candidates=selected_proposal.candidates[2:],
        observed_at=NOW,
    )


def test_checkpoint_binds_decision_and_restore_revalidates_fresh_owner_state(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    result = _execute(harness, enabled=True)
    request = _bound_checkpoint_request(harness, result)
    bound, _, created = CheckpointCommitRuntime(
        harness.recovery_store
    ).commit(request)

    assert created is True
    assert bound.metadata["early_exit"]["restore_policy"] == (
        "revalidate_fresh_owner_state_never_reuse_verdict"
    )
    assert (
        bound.metadata["early_exit"]["binding"]["decision_digest"]
        == result.final_decision.digest
    )
    assert bound.version_refs["requirement_revision"] == (
        harness.policy_input.requirement_revision
    )

    restarted_store = RecoveryPlanStore(
        Path(harness.recovery_store.path)
    )
    restored = restarted_store.checkpoint_head(harness.task.task_id)
    assert restored is not None
    assert restored.content_digest == bound.content_digest

    current_snapshot = _capture(harness, checkpoint=restored)
    validation = harness.gate.revalidate_restored(
        restored,
        current_snapshot,
        evaluated_at=NOW,
    )

    assert validation.prior_verdict_reused is False
    assert validation.prior_decision_ref == result.final_decision.decision_id
    assert "eligibility_inputs_changed" in validation.invalidation_reasons
    assert validation.decision.decision is ExitDecision.EXIT
    assert (
        validation.decision.snapshot_digest
        == current_snapshot.digest
    )


def test_requirement_change_invalidates_restored_exit_and_forces_continue(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    result = _execute(harness, enabled=True)
    bound, _, _ = CheckpointCommitRuntime(
        harness.recovery_store
    ).commit(_bound_checkpoint_request(harness, result))
    restored = RecoveryPlanStore(
        Path(harness.recovery_store.path)
    ).checkpoint_head(harness.task.task_id)
    assert restored is not None
    assert restored.content_digest == bound.content_digest

    new_obligation = "review changed requirement"
    harness.task.constraints.requirements.append(new_obligation)
    harness.task.metadata["requirement_revision"] = "requirement-r2"
    harness.task.status = PlanNodeStatus.RUNNING
    for node in harness.task.plan_nodes.values():
        node.status = PlanNodeStatus.RUNNING
    policy_r2 = _policy_input(
        harness.task,
        requirement_revision="requirement-r2",
        obligations=(*OBLIGATIONS, new_obligation),
    )
    proposal_r2 = replace(
        harness.proposal,
        proposal_id="proposal-early-exit-r2",
        input_snapshot_digest=policy_r2.digest,
        requirement_revision=policy_r2.requirement_revision,
    )
    changed_snapshot = _capture(
        harness,
        checkpoint=restored,
        policy_input=policy_r2,
        proposal=proposal_r2,
    )
    validation = harness.gate.revalidate_restored(
        restored,
        changed_snapshot,
        evaluated_at=NOW,
    )

    assert validation.prior_verdict_reused is False
    assert validation.decision.decision is ExitDecision.CONTINUE
    assert {
        "requirement_revision_changed",
        "policy_input_changed",
        "operator_proposal_changed",
        "eligibility_inputs_changed",
    }.issubset(set(validation.invalidation_reasons))
    assert {
        "critical_obligations_resolved",
        "checkpoint_requirement_current",
        "memory_continuity_passed",
        "final_verifier_passed",
    }.issubset(set(validation.decision.failed_conditions))


def test_canonical_pending_side_effect_owner_forces_continue(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    completed = _execute(harness, enabled=True)
    pending_fence, created = SideEffectFenceRuntime(
        harness.recovery_store
    ).reserve(
        run_id=harness.task.run_id,
        task_id=harness.task.task_id,
        operation="publish-deliverable",
        request={"artifact": "deliverable"},
        idempotency_key="publish-deliverable-pending",
    )
    assert created is True
    # The pending fence is deliberately not added to the older checkpoint.
    # Snapshot construction must query the canonical owner scope so a stale
    # checkpoint cannot hide an in-flight side effect.
    snapshot = _capture(harness, checkpoint=harness.checkpoint)
    decision = harness.gate.evaluate(snapshot, evaluated_at=NOW)

    assert completed.final_decision.decision is ExitDecision.EXIT
    assert snapshot.side_effect_pending_ids == (pending_fence.fence_key,)
    assert decision.decision is ExitDecision.CONTINUE
    assert "side_effects_settled" in decision.failed_conditions
