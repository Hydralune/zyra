from __future__ import annotations

from dataclasses import replace

from zyra_core import PlanNode, TaskState
from zyra_orchestration.topology_policy import TopologyPolicyTriggerAdapter
from zyra_orchestration.topology_policy.registry import ResolutionPurpose
from zyra_symbolic import TopologyRouter

from tests.support.topology_composer import (
    custody,
    environment_and_catalog,
    policy,
    policy_input,
    request,
)


def test_normal_trigger_uses_activation_ready_strongest_default(tmp_path):
    graph_custody = custody(tmp_path, suffix="normal-strongest")
    current = graph_custody.current("graph-normal-strongest")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
        readiness_stage="activation_ready",
    )
    policy_value = policy(graph_custody=graph_custody)
    validation_request = request(
        policy_value=policy_value,
        input_snapshot=input_snapshot,
        current_graph=current,
        catalog=catalog,
        purpose=ResolutionPurpose.NORMAL,
    )

    result = policy_value.execute(validation_request)

    assert result.used_baseline is False
    assert result.committed is True
    assert result.topology_result is not None
    assert result.topology_result.mode == "default"
    assert graph_custody.current(current.graph_id).revision == current.revision + 1
    assert (
        result.policy_result.execution_receipt.actual_profile_id
        == "phase2_strongest_v1"
    )


def test_normal_trigger_rejects_non_activation_ready_input(tmp_path):
    graph_custody = custody(tmp_path, suffix="normal-readiness-reject")
    current = graph_custody.current("graph-normal-readiness-reject")
    environment, catalog = environment_and_catalog()
    activation_input = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
        readiness_stage="activation_ready",
    )
    rejected_input = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
        readiness_stage="implementation_validated",
    )
    policy_value = policy(graph_custody=graph_custody)
    normal_request = replace(
        request(
            policy_value=policy_value,
            input_snapshot=activation_input,
            current_graph=current,
            catalog=catalog,
            purpose=ResolutionPurpose.NORMAL,
        ),
        policy_input=rejected_input,
    )

    result = policy_value.execute(normal_request)

    assert result.used_baseline is True
    assert result.committed is False
    assert result.policy_result.execution_receipt.degraded is True
    assert result.policy_result.execution_receipt.degraded_reason == (
        "input_readiness_drift"
    )
    assert graph_custody.current(current.graph_id).revision == current.revision


def test_active_default_runs_full_composer_and_custody_commit(tmp_path):
    events = []
    graph_custody = custody(tmp_path, suffix="validation-commit")
    current = graph_custody.current("graph-validation-commit")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
    )
    policy_value = policy(
        graph_custody=graph_custody,
        events=events,
    )
    validation_request = request(
        policy_value=policy_value,
        input_snapshot=input_snapshot,
        current_graph=current,
        catalog=catalog,
        purpose=ResolutionPurpose.NORMAL,
    )

    result = policy_value.execute(validation_request)

    assert result.used_baseline is False
    assert result.committed is True
    assert result.topology_result is not None
    topology = result.topology_result
    assert topology.mode == "default"
    assert topology.graph_revision_before == 0
    assert topology.graph_revision_after == 1
    assert topology.permission_result == "permission_and_placement_allowed"
    assert tuple(layer.mechanism_id for layer in topology.composition.layers) == (
        "arg_designer",
        "card",
        "agentprune",
    )
    assert all(layer.affected_commit for layer in topology.composition.layers)
    assert topology.outcome is not None
    assert topology.outcome.causal_refs
    assert topology.memory_causal_refs == ("memory-continuity-r1",)
    assert len(topology.scheduler_causal_refs) == 4
    assert "memory-continuity-r1" in topology.outcome.causal_refs
    assert "GraphStateCustody.validate_graph" in (
        topology.outcome.causal_refs
    )
    assert topology.outcome.verifier_result == "graph_commit_verified"
    assert graph_custody.current(current.graph_id).revision == 1
    assert any(
        event.payload.get("schema")
        == "zyra.topology-composer-decision/v1"
        for event in events
    )


def test_existing_topology_router_trigger_reaches_composer_without_owning_route(
    tmp_path,
):
    graph_custody = custody(tmp_path, suffix="router-trigger")
    current = graph_custody.current("graph-router-trigger")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
    )
    policy_value = policy(graph_custody=graph_custody)
    validation_request = request(
        policy_value=policy_value,
        input_snapshot=input_snapshot,
        current_graph=current,
        catalog=catalog,
        purpose=ResolutionPurpose.NORMAL,
    )
    adapter = TopologyPolicyTriggerAdapter(
        policy_value,
        request_builder=lambda state, node, event: validation_request,
    )
    root = PlanNode(
        node_id="root-router-trigger",
        title="Execute and verify artifact",
        description="Use the existing symbolic topology trigger.",
        metadata={"stage": "execute"},
    )
    state = TaskState(
        run_id=input_snapshot.run_id,
        task_id=input_snapshot.task_id,
        user_goal="Execute and verify a code artifact.",
        root_node_id=root.node_id,
        plan_nodes={root.node_id: root},
    )

    decision, event = TopologyRouter(
        topology_policy_trigger=adapter,
    ).route(state, node=root)

    embedded = event.payload["topology_policy"]
    assert embedded["committed"] is True
    assert embedded["used_baseline"] is False
    assert decision.metadata["topology_policy_profile"] == (
        "phase2_strongest_v1"
    )
    assert decision.metadata["topology_policy_committed"] == "true"
    assert state.metadata["last_topology_route"]["topology_policy"] == embedded
    assert graph_custody.current(current.graph_id).revision == 1
    assert root.assigned_worker_id


def test_diagnostic_composition_is_read_only_and_attributed_to_baseline(
    tmp_path,
):
    graph_custody = custody(tmp_path, suffix="diagnostic-read-only")
    current = graph_custody.current("graph-diagnostic-read-only")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
    )
    policy_value = policy(graph_custody=graph_custody)
    diagnostic_request = replace(
        request(
            policy_value=policy_value,
            input_snapshot=input_snapshot,
            current_graph=current,
            catalog=catalog,
        ),
        purpose=ResolutionPurpose.DIAGNOSTIC,
        version="phase2_diagnostic_v1",
        validation_manifest=None,
    )

    result = policy_value.execute(diagnostic_request)

    assert result.used_baseline is True
    assert result.policy_result.diagnostic_receipt is not None
    assert result.topology_result is not None
    assert result.topology_result.mode == "diagnostic"
    assert result.topology_result.projection is None
    assert result.topology_result.composition.proposal is None
    assert (
        result.topology_result.composition.degraded_reason
        == "diagnostic_canonical_mutation_forbidden"
    )
    assert graph_custody.current(current.graph_id).revision == 0
