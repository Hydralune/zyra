from __future__ import annotations

from zyra_evaluation.policy_benchmark import evaluate_operator_outcome
from zyra_orchestration.topology_policy import PolicyDecisionDisposition
from zyra_scheduler.worker_pool.models import AttemptState, LeaseState

from tests.integration.operator_placement_harness import (
    build_harness,
    execute_harness,
)
from tests.support.topology_composer import (
    custody,
    environment_and_catalog,
    layer_results,
    policy,
    policy_input,
    request,
)


def _topology_layer_digests(tmp_path):
    graph_custody = custody(tmp_path, suffix="strongest-deterministic")
    current = graph_custody.current("graph-strongest-deterministic")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
    )
    policy_value = policy(graph_custody=graph_custody)
    request_value = request(
        policy_value=policy_value,
        input_snapshot=input_snapshot,
        current_graph=current,
        catalog=catalog,
    )
    arg_result, card_result, pruning_result = layer_results(
        policy_value=policy_value,
        request_value=request_value,
    )
    return (
        arg_result.proposal.digest,
        card_result.correction.digest,
        pruning_result.mask.digest,
    )


def test_identical_snapshot_produces_identical_arg_card_agentprune_digests(
    tmp_path,
) -> None:
    first = _topology_layer_digests(tmp_path / "first")
    second = _topology_layer_digests(tmp_path / "second")

    assert first == second


def test_strongest_validation_projects_commits_and_replays_once(tmp_path) -> None:
    graph_custody = custody(tmp_path, suffix="strongest-commit")
    current = graph_custody.current("graph-strongest-commit")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
    )
    policy_value = policy(graph_custody=graph_custody)
    request_value = request(
        policy_value=policy_value,
        input_snapshot=input_snapshot,
        current_graph=current,
        catalog=catalog,
    )

    first = policy_value.execute(request_value)
    replay = policy_value.execute(request_value)

    assert first.used_baseline is False
    assert first.committed is True
    assert first.topology_result is not None
    assert first.topology_result.projection.commit is not None
    assert first.topology_result.projection.delta is not None
    assert first.topology_result.projection.delta.mutations
    assert replay.used_baseline is False
    assert replay.topology_result is not None
    assert replay.topology_result.projection.receipt.disposition is (
        PolicyDecisionDisposition.REPLAY
    )
    assert replay.topology_result.committed is False
    assert graph_custody.current(current.graph_id).revision == 1


def test_maas_scheduler_lease_tool_artifact_and_verifier_chain(tmp_path) -> None:
    harness = build_harness(tmp_path)

    result = execute_harness(harness)
    assessment = evaluate_operator_outcome(result, pool=harness.pool)

    assert result.mode == "validation"
    assert result.placement.route_mode == "operator_constrained"
    assert result.attempt_receipts
    assert assessment.passed is True
    assert assessment.execution_without_lease_count == 0
    assert result.attempt_receipts[-1].artifact_refs
    assert result.attempt_receipts[-1].verification_refs
    for receipt in result.attempt_receipts:
        lease = harness.pool.store.require_lease(receipt.lease_id)
        attempt = harness.pool.store.require_attempt(receipt.attempt_id)
        assert lease.state is LeaseState.RELEASED
        assert attempt.state is AttemptState.SUCCEEDED


def test_operator_fault_uses_recovery_owner_and_reroutes_without_duplicate(
    tmp_path,
) -> None:
    harness = build_harness(
        tmp_path,
        single_layer=True,
        b_first=True,
        fail_first_worker="worker-b",
    )

    result = execute_harness(harness)

    assert result.placement.selected_manifest_id == "worker-a"
    assert result.recovery_plan_refs
    assert result.attempt_receipts[0].worker_id == "worker-a"
    assert harness.call_port.side_effect_count == 1
    assert "reroute_worker" in (
        harness.task.metadata["last_recovery_plan"]["actions"]
    )
