from __future__ import annotations

from zyra_orchestration.topology_policy import TopologyOperationKind

from tests.support.topology_composer import (
    custody,
    environment_and_catalog,
    policy,
    policy_input,
    request,
)


def _committed_roles(tmp_path, *, suffix: str, phase: str, obligations):
    graph_custody = custody(tmp_path, suffix=suffix)
    current = graph_custody.current(f"graph-{suffix}")
    environment, catalog = environment_and_catalog()
    input_snapshot = policy_input(
        graph=current,
        environment=environment,
        catalog=catalog,
        phase=phase,
        unresolved_obligations=obligations,
    )
    policy_value = policy(graph_custody=graph_custody)
    result = policy_value.execute(
        request(
            policy_value=policy_value,
            input_snapshot=input_snapshot,
            current_graph=current,
            catalog=catalog,
            trigger_kind="phase_change",
        )
    )
    assert result.committed is True
    head = graph_custody.current(current.graph_id)
    return {node.role for node in head.nodes}, {
        edge.edge_id for edge in head.edges
    }


def test_phase_change_produces_different_role_node_edge_topology(tmp_path):
    execution_roles, execution_edges = _committed_roles(
        tmp_path,
        suffix="phase-execution",
        phase="execution",
        obligations=("execute artifact", "verify artifact"),
    )
    recovery_roles, recovery_edges = _committed_roles(
        tmp_path,
        suffix="phase-recovery",
        phase="recovery",
        obligations=("recover continuity", "verify repaired artifact"),
    )

    assert "execution_role" in execution_roles
    assert "recovery_role" in recovery_roles
    assert execution_roles != recovery_roles
    assert execution_edges != recovery_edges


def test_fault_and_requirement_change_proposes_dynamic_mutation_then_fails_closed(
    tmp_path,
):
    graph_custody = custody(tmp_path, suffix="fault-change")
    initial = graph_custody.current("graph-fault-change")
    initial_environment, initial_catalog = environment_and_catalog()
    policy_value = policy(graph_custody=graph_custody)
    initial_input = policy_input(
        graph=initial,
        environment=initial_environment,
        catalog=initial_catalog,
        phase="execution",
        unresolved_obligations=("execute artifact", "verify artifact"),
    )
    initial_result = policy_value.execute(
        request(
            policy_value=policy_value,
            input_snapshot=initial_input,
            current_graph=initial,
            catalog=initial_catalog,
        )
    )
    assert initial_result.committed is True
    before_change = graph_custody.current(initial.graph_id)

    changed_environment, changed_catalog = environment_and_catalog(
        faulted_worker_id="worker-execution",
        requirement_change=True,
    )
    changed_input = policy_input(
        graph=before_change,
        environment=changed_environment,
        catalog=changed_catalog,
        phase="recovery",
        requirement_revision="requirement-r2",
        unresolved_obligations=(
            "recover continuity after execution worker fault",
            "verify repaired artifact",
        ),
    )
    changed_request = request(
        policy_value=policy_value,
        input_snapshot=changed_input,
        current_graph=before_change,
        catalog=changed_catalog,
        trigger_kind="recovery",
    )

    changed_result = policy_value.execute(changed_request)

    assert changed_result.used_baseline is True
    assert changed_result.topology_result is not None
    topology = changed_result.topology_result
    assert topology.trigger_kind == "recovery"
    assert topology.recovery_causal_refs == ("receipt-recovery-topology",)
    assert topology.composition.proposal is not None
    operation_kinds = {
        operation.kind
        for operation in topology.composition.proposal.operations
    }
    assert TopologyOperationKind.REPLACE_NODE in operation_kinds
    assert TopologyOperationKind.REMOVE_EDGE in operation_kinds
    assert topology.projection.receipt.fallback_reason == (
        "graph_constraint_rejected"
    )
    graph_constraint = next(
        item
        for item in topology.projection.receipt.constraint_results
        if item.constraint_id == "canonical_graph_validation"
    )
    assert graph_constraint.passed is False
    assert "cycle" in graph_constraint.message
    assert graph_custody.current(initial.graph_id).revision == 1
