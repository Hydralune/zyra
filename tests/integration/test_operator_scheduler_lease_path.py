from __future__ import annotations

from types import SimpleNamespace

from zyra_evaluation.policy_benchmark import evaluate_operator_outcome
from zyra_scheduler import ResourceScheduler
from zyra_scheduler.worker_pool.models import AttemptState, LeaseState, parse_utc

from tests.integration.operator_placement_harness import (
    build_harness,
    execute_harness,
)


def test_candidate_set_changes_scheduler_placement_and_lease_precedes_call(
    tmp_path,
) -> None:
    harness = build_harness(tmp_path)
    baseline = ResourceScheduler(
        harness.scheduler.worker_pool
    ).decide(harness.task)

    result = execute_harness(harness)
    assessment = evaluate_operator_outcome(result, pool=harness.pool)

    assert baseline.selected_manifest_id == "worker-a"
    assert result.placement.selected_manifest_id == "worker-b"
    assert result.placement.route_mode == "operator_constrained"
    assert result.placement.selected_operator_refs == (
        "tool:produce-tool@1",
        "worker:worker-b@1",
        "tool:optional-three@1",
        "tool:optional-four@1",
    )
    assert result.adaptive_result is not None
    assert result.adaptive_result.exited_early is True
    assert len(result.attempt_receipts) == 2
    assert harness.call_port.prepare_states == [
        (LeaseState.ACTIVE.value, AttemptState.RUNNING.value),
        (LeaseState.ACTIVE.value, AttemptState.RUNNING.value),
    ]
    for receipt in result.attempt_receipts:
        assert (
            parse_utc(receipt.lease_acquired_at)
            <= parse_utc(receipt.attempt_started_at)
            <= parse_utc(receipt.call_started_at)
            <= parse_utc(receipt.call_finished_at)
        )
        lease = harness.pool.store.require_lease(receipt.lease_id)
        attempt = harness.pool.store.require_attempt(receipt.attempt_id)
        assert lease.state is LeaseState.RELEASED
        assert attempt.state is AttemptState.SUCCEEDED
        assert lease.run_id == harness.task.run_id
        assert lease.task_id == harness.task.task_id
        assert lease.attempt_id == receipt.attempt_id
        assert (
            lease.metadata["placement_decision_id"]
            == receipt.placement_decision_id
        )
        assert (
            lease.metadata["candidate_set_digest"]
            == receipt.candidate_set_digest
        )
    assert assessment.passed is True
    assert assessment.execution_without_lease_count == 0
    assert assessment.stale_or_revoked_execution_count == 0
    assert result.placement.decision_id in (
        result.adaptive_result.policy_outcome.causal_refs
    )


def test_operator_attempt_replay_does_not_repeat_side_effect(tmp_path) -> None:
    harness = build_harness(tmp_path)
    first = execute_harness(harness)
    side_effects = harness.call_port.side_effect_count
    lease_count = len(harness.pool.store.list_leases())

    replay = execute_harness(harness)
    prior = first.attempt_receipts[0]
    found = harness.runtime._prior_attempt(  # noqa: SLF001 - invariant probe.
        harness.task.task_id,
        operator_idempotency_key=prior.operator_idempotency_key,
    )

    assert found is not None
    assert found.replayed is True
    assert found.completion_receipt_ref == prior.completion_receipt_ref
    assert replay.attempt_receipts
    assert all(item.replayed for item in replay.attempt_receipts)
    assert harness.call_port.side_effect_count == side_effects
    assert len(harness.pool.store.list_leases()) == lease_count
    assert evaluate_operator_outcome(replay, pool=harness.pool).passed is True


def test_selector_disabled_uses_explicit_baseline_with_real_lease(tmp_path) -> None:
    harness = build_harness(tmp_path, single_layer=True)
    disabled = SimpleNamespace(
        proposal=None,
        scheduler_input=None,
        degraded=True,
        degraded_reason="maas_selector_disabled",
    )

    result = harness.runtime.execute_task(
        task=harness.task,
        policy_input=harness.policy,
        selector_result=disabled,
        catalog=harness.catalog,
        adaptive_depth=None,
        eligibility_port=None,
    )

    assert result.mode == "baseline"
    assert result.baseline_profile == "phase1_resource_scheduler_baseline"
    assert result.placement.route_mode == "baseline"
    assert result.placement.degraded_reason == "maas_selector_disabled"
    assert len(result.attempt_receipts) == 1
    receipt = result.attempt_receipts[0]
    assert receipt.operator_ref.startswith("baseline:")
    assert (
        parse_utc(receipt.lease_acquired_at)
        <= parse_utc(receipt.attempt_started_at)
        <= parse_utc(receipt.call_started_at)
    )
    assert receipt.artifact_refs
    assert receipt.verification_refs
    assert evaluate_operator_outcome(result, pool=harness.pool).passed is True
