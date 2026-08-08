from __future__ import annotations

from dataclasses import replace

import pytest

from zyra_orchestration.graph_custody import (
    DynamicTopologyRuntime,
    GraphEdge,
    GraphNode,
    NodeExecutionState,
)
from zyra_orchestration.topology_policy import (
    PolicyDecisionDisposition,
    TopologyOperationKind,
)
from zyra_orchestration.topology_policy.pruning import (
    AgentPruneContractError,
)
from zyra_orchestration.topology_policy.composer import TopologyLayerSwitches

from tests.support.topology_composer import (
    custody,
    environment_and_catalog,
    layer_results,
    policy,
    policy_input,
    request,
)


@pytest.mark.parametrize(
    "switches,expected_reason",
    (
        (
            TopologyLayerSwitches(arg_enabled=False),
            "arg_disabled",
        ),
        (
            TopologyLayerSwitches(card_enabled=False),
            "card_disabled",
        ),
        (
            TopologyLayerSwitches(agentprune_enabled=False),
            "agentprune_disabled",
        ),
        (
            TopologyLayerSwitches(policy_enabled=False),
            "phase2_topology_policy_disabled",
        ),
    ),
)
def test_required_layer_disable_forces_explicit_baseline(
    tmp_path,
    switches,
    expected_reason,
):
    suffix = expected_reason.replace("_", "-")
    graph_custody = custody(tmp_path, suffix=suffix)
    current = graph_custody.current(f"graph-{suffix}")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
    )
    policy_value = policy(graph_custody=graph_custody)
    request_value = replace(
        request(
            policy_value=policy_value,
            input_snapshot=input_snapshot,
            current_graph=current,
            catalog=catalog,
        ),
        switches=switches,
    )

    result = policy_value.execute(request_value)

    assert result.used_baseline is True
    assert result.policy_result.execution_receipt.degraded is True
    assert expected_reason in (
        result.policy_result.execution_receipt.degraded_reason,
        (
            ""
            if result.topology_result is None
            else result.topology_result.degraded_reason
        ),
    )
    assert graph_custody.current(current.graph_id).revision == 0


def test_permission_rejection_cannot_commit_and_is_not_silent(tmp_path):
    graph_custody = custody(tmp_path, suffix="permission-reject")
    current = graph_custody.current("graph-permission-reject")
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
    composition = policy_value.composer_runtime.composer.compose(
        policy_input=input_snapshot,
        current_graph=current,
        arg_result=arg_result,
        card_result=card_result,
        pruning_result=pruning_result,
    )
    assert composition.proposal is not None
    first, *rest = composition.proposal.operations
    denied = replace(
        composition.proposal,
        operations=(
            replace(first, required_permissions=("graph.admin",)),
            *rest,
        ),
    )

    projection = policy_value.composer_runtime.projector.execute(
        input_snapshot,
        denied,
        decision_id="decision-permission-reject",
        execution_mode="validation",
    )

    receipt = projection.receipt
    permission = next(
        item
        for item in receipt.constraint_results
        if item.constraint_id == "permission_privacy_placement"
    )
    assert permission.passed is False
    assert receipt.fallback_reason == permission.reason_code
    assert receipt.fallback_profile == "phase1_deterministic_baseline"
    assert projection.commit is None
    assert graph_custody.current(current.graph_id).revision == 0


def test_unknown_capability_is_rejected_by_composite_registry_gate(tmp_path):
    graph_custody = custody(tmp_path, suffix="unknown-capability")
    current = graph_custody.current("graph-unknown-capability")
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
    composition = policy_value.composer_runtime.composer.compose(
        policy_input=input_snapshot,
        current_graph=current,
        arg_result=arg_result,
        card_result=card_result,
        pruning_result=pruning_result,
    )
    proposal = composition.proposal
    node_index = next(
        index
        for index, item in enumerate(proposal.operations)
        if item.kind is TopologyOperationKind.ADD_NODE
    )
    operations = list(proposal.operations)
    node = operations[node_index]
    operations[node_index] = replace(
        node,
        value={
            **dict(node.value),
            "capabilities": ["unknown-privileged-capability"],
        },
    )
    mutated = replace(proposal, operations=tuple(operations))

    projection = policy_value.composer_runtime.projector.execute(
        input_snapshot,
        mutated,
        decision_id="decision-unknown-capability",
        execution_mode="validation",
    )

    registry = next(
        item
        for item in projection.receipt.constraint_results
        if item.constraint_id == "registry"
    )
    assert registry.passed is False
    assert registry.reason_code == "unknown_role_or_capability"
    assert projection.commit is None


def test_mixed_layer_snapshot_is_rejected_before_projection(tmp_path):
    graph_custody = custody(tmp_path, suffix="mixed-snapshot")
    current = graph_custody.current("graph-mixed-snapshot")
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
    card_result = replace(
        card_result,
        correction_proposal=replace(
            card_result.correction_proposal,
            input_snapshot_digest="0" * 64,
        ),
    )

    composition = policy_value.composer_runtime.composer.compose(
        policy_input=input_snapshot,
        current_graph=current,
        arg_result=arg_result,
        card_result=card_result,
        pruning_result=pruning_result,
    )

    assert composition.degraded is True
    assert composition.degraded_reason == "composer_mixed_snapshot"
    assert composition.replan_required is True


def test_agentprune_budget_overrun_forces_explicit_baseline(tmp_path):
    graph_custody = custody(tmp_path, suffix="budget-overrun")
    current = graph_custody.current("graph-budget-overrun")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
        max_communication_bytes=100,
    )
    policy_value = policy(graph_custody=graph_custody)

    result = policy_value.execute(
        request(
            policy_value=policy_value,
            input_snapshot=input_snapshot,
            current_graph=current,
            catalog=catalog,
        )
    )

    assert result.used_baseline is True
    assert result.policy_result.execution_receipt.degraded_reason == (
        "agentprune_budget_exceeds_policy_input"
    )
    assert graph_custody.current(current.graph_id).revision == 0


def test_composite_fanout_overrun_is_rejected_by_projector(tmp_path):
    graph_custody = custody(tmp_path, suffix="fanout-overrun")
    current = graph_custody.current("graph-fanout-overrun")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
        max_fan_out=1,
    )
    policy_value = policy(graph_custody=graph_custody)

    result = policy_value.execute(
        request(
            policy_value=policy_value,
            input_snapshot=input_snapshot,
            current_graph=current,
            catalog=catalog,
        )
    )

    assert result.used_baseline is True
    assert result.topology_result is not None
    fanout = next(
        item
        for item in (
            result.topology_result.projection.receipt.constraint_results
        )
        if item.constraint_id == "fanout"
    )
    assert fanout.passed is False
    assert fanout.reason_code == "fanout_exceeded"
    assert graph_custody.current(current.graph_id).revision == 0


def test_terminal_execution_history_does_not_exhaust_active_fanout(tmp_path):
    graph_custody = custody(tmp_path, suffix="terminal-fanout-history")
    topology = DynamicTopologyRuntime(graph_custody)
    graph_id = "graph-terminal-fanout-history"
    root = GraphNode(
        node_id="historical-root",
        role="planner",
        capabilities=("planning",),
        state=NodeExecutionState.SUCCEEDED,
    )
    assert topology.add_node(
        graph_id,
        root,
        actor_id="test",
        causation_id="terminal-history-root",
    ).receipt.committed
    for index in range(6):
        terminal = GraphNode(
            node_id=f"historical-terminal-{index}",
            role="worker",
            capabilities=("execution",),
            state=NodeExecutionState.SUCCEEDED,
        )
        assert topology.add_node(
            graph_id,
            terminal,
            actor_id="test",
            causation_id=f"terminal-history-node-{index}",
        ).receipt.committed
        assert topology.add_edge(
            graph_id,
            GraphEdge(
                edge_id=f"historical-edge-{index}",
                source_node_id=root.node_id,
                target_node_id=terminal.node_id,
                relation="completed_runtime_branch",
            ),
            actor_id="test",
            causation_id=f"terminal-history-edge-{index}",
        ).receipt.committed

    current = graph_custody.current(graph_id)
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
        max_fan_out=4,
    )
    policy_value = policy(graph_custody=graph_custody)

    result = policy_value.execute(
        request(
            policy_value=policy_value,
            input_snapshot=input_snapshot,
            current_graph=current,
            catalog=catalog,
        )
    )

    assert result.used_baseline is False
    assert result.topology_result is not None
    fanout = next(
        item
        for item in result.topology_result.projection.receipt.constraint_results
        if item.constraint_id == "fanout"
    )
    assert fanout.passed is True
    assert fanout.details["non_policy_edge_count"] >= 6


def test_identical_stale_composite_replays_without_duplicate_commit(tmp_path):
    graph_custody = custody(tmp_path, suffix="stale-composite")
    current = graph_custody.current("graph-stale-composite")
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
    assert first.committed is True

    replay = policy_value.execute(request_value)

    assert replay.used_baseline is False
    assert replay.topology_result is not None
    assert replay.topology_result.committed is False
    assert (
        replay.topology_result.projection.receipt.disposition
        is PolicyDecisionDisposition.REPLAY
    )
    assert replay.topology_result.graph_revision_after == 1
    assert graph_custody.current(current.graph_id).revision == 1


def test_changed_stale_composite_is_rejected_not_replayed(tmp_path):
    graph_custody = custody(tmp_path, suffix="changed-stale")
    current = graph_custody.current("graph-changed-stale")
    environment, catalog = environment_and_catalog()
    first_input = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
    )
    policy_value = policy(graph_custody=graph_custody)
    first_request = request(
        policy_value=policy_value,
        input_snapshot=first_input,
        current_graph=current,
        catalog=catalog,
    )
    assert policy_value.execute(first_request).committed is True
    stale_changed_input = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
        requirement_revision="requirement-r2",
        unresolved_obligations=("recover continuity", "re-verify artifact"),
    )
    stale_changed_request = request(
        policy_value=policy_value,
        input_snapshot=stale_changed_input,
        current_graph=current,
        catalog=catalog,
        trigger_kind="requirement_change",
    )

    result = policy_value.execute(stale_changed_request)

    assert result.used_baseline is True
    assert result.topology_result is not None
    assert (
        result.topology_result.projection.receipt.fallback_reason
        == "stale_or_mismatched_snapshot"
    )
    assert graph_custody.current(current.graph_id).revision == 1


def test_card_forbidden_edge_overrides_agentprune_keep(tmp_path):
    graph_custody = custody(tmp_path, suffix="card-precedence")
    current = graph_custody.current("graph-card-precedence")
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
    kept = next(item for item in pruning_result.mask.decisions if item.keep)
    correction = replace(
        card_result.correction,
        decisions=tuple(
            (
                replace(
                    item,
                    action="drop",
                    active_after=False,
                    reasons=(
                        *item.reasons,
                        "privacy constraint forbids the edge",
                    ),
                )
                if item.edge_id == kept.edge.edge_id
                else item
            )
            for item in card_result.correction.decisions
        ),
        effective_spatial_edge_ids=tuple(
            item
            for item in card_result.effective_spatial_edge_ids
            if item != kept.edge.edge_id
        ),
        effective_temporal_edge_ids=tuple(
            item
            for item in card_result.effective_temporal_edge_ids
            if item != kept.edge.edge_id
        ),
    )
    card_result = replace(card_result, correction=correction)

    composition = policy_value.composer_runtime.composer.compose(
        policy_input=input_snapshot,
        current_graph=current,
        arg_result=arg_result,
        card_result=card_result,
        pruning_result=pruning_result,
    )

    assert composition.degraded is False
    assert f"card_drop:{kept.edge.edge_id}" in (
        composition.projection_differences
    )
    assert all(
        not (
            operation.entity_id == kept.edge.edge_id
            and operation.kind
            in {
                TopologyOperationKind.ADD_EDGE,
                TopologyOperationKind.REPLACE_EDGE,
            }
        )
        for operation in composition.proposal.operations
    )


def test_agentprune_contract_cannot_materialize_protected_drop(tmp_path):
    graph_custody = custody(tmp_path, suffix="protected-drop")
    current = graph_custody.current("graph-protected-drop")
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
    selected = pruning_result.mask.decisions[0]
    with pytest.raises(
        AgentPruneContractError,
        match="hard constraints cannot be dropped",
    ) as captured:
        replace(
            selected,
            keep=False,
            action="drop",
            hard_constraints=("continuity_edge",),
            reasons=(*selected.reasons, "mutated protected drop"),
        )
    assert captured.value.code == "agentprune_protected_edge_dropped"


def test_run_level_churn_window_rejects_excess_commit(tmp_path):
    graph_custody = custody(tmp_path, suffix="churn-window")
    current = graph_custody.current("graph-churn-window")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
    )
    policy_value = policy(graph_custody=graph_custody)
    limit = (
        policy_value.composer_runtime.composer.config
        .maximum_commits_per_window
    )
    policy_value.composer_runtime._committed_signatures[
        input_snapshot.run_id
    ] = [
        (
            input_snapshot.header.created_at,
            f"before-{index}",
            f"after-{index}",
        )
        for index in range(limit)
    ]

    result = policy_value.composer_runtime._commit_guard(
        policy_input=input_snapshot,
        before_signature="before-next",
        after_signature="after-next",
    )

    assert result is not None
    assert result.passed is False
    assert result.reason_code == "composer_churn_window_limit"


def test_run_level_oscillation_guard_rejects_recent_signature(tmp_path):
    graph_custody = custody(tmp_path, suffix="oscillation")
    current = graph_custody.current("graph-oscillation")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
    )
    policy_value = policy(graph_custody=graph_custody)
    policy_value.composer_runtime._committed_signatures[
        input_snapshot.run_id
    ] = [
        (
            input_snapshot.header.created_at,
            "signature-a",
            "signature-b",
        )
    ]

    result = policy_value.composer_runtime._commit_guard(
        policy_input=input_snapshot,
        before_signature="signature-c",
        after_signature="signature-a",
    )

    assert result is not None
    assert result.passed is False
    assert result.reason_code == "topology_oscillation_detected"


def test_oscillation_history_survives_composer_runtime_restart(tmp_path):
    graph_custody = custody(tmp_path, suffix="oscillation-restart")
    initial = graph_custody.current("graph-oscillation-restart")
    environment, catalog = environment_and_catalog()
    initial_input = policy_input(
        graph=initial,
        environment=environment,
        catalog=catalog,
    )
    first_policy = policy(graph_custody=graph_custody)
    assert first_policy.execute(
        request(
            policy_value=first_policy,
            input_snapshot=initial_input,
            current_graph=initial,
            catalog=catalog,
        )
    ).committed is True
    current = graph_custody.current(initial.graph_id)
    restarted_policy = policy(graph_custody=graph_custody)
    current_input = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
        requirement_revision="requirement-r2",
    )

    result = restarted_policy.composer_runtime._commit_guard(
        policy_input=current_input,
        before_signature=current.signature,
        after_signature=initial.signature,
    )

    assert result is not None
    assert result.passed is False
    assert result.reason_code == "topology_oscillation_detected"
