from __future__ import annotations

from pathlib import Path

import pytest

from zyra_orchestration.graph_custody import (
    DynamicTopologyRuntime,
    GraphCommitStatus,
    GraphConflictKind,
    GraphConflictStrategy,
    GraphDeltaBuilder,
    GraphEdge,
    GraphMutationRejected,
    GraphNode,
    GraphStateCustody,
    GraphStateStore,
)


def _custody(path: Path) -> tuple[GraphStateCustody, DynamicTopologyRuntime]:
    store = GraphStateStore(path)
    store.initialize()
    custody = GraphStateCustody(store)
    custody.create(graph_id_value="graph-dynamic-test", run_id="run-dynamic")
    return custody, DynamicTopologyRuntime(custody)


def test_runtime_can_add_replace_and_delete_nodes_edges_roles_and_capabilities(tmp_path: Path) -> None:
    custody, topology = _custody(tmp_path / "graph.sqlite3")
    first = topology.add_node(
        "graph-dynamic-test",
        GraphNode(node_id="node-a", role="planner", capabilities=("plan",)),
        actor_id="test",
        causation_id="cause-a",
    )
    assert first.receipt.committed
    second = topology.add_node(
        "graph-dynamic-test",
        GraphNode(node_id="node-b", role="worker", capabilities=("execute",), dependencies=("node-a",)),
        actor_id="test",
        causation_id="cause-b",
    )
    edge = topology.add_edge(
        "graph-dynamic-test",
        GraphEdge(
            edge_id="edge-a-b",
            source_node_id="node-a",
            target_node_id="node-b",
            relation="handoff",
        ),
        actor_id="test",
        causation_id="cause-edge",
    )
    changed_role = topology.set_role(
        "graph-dynamic-test",
        "node-b",
        "reviewer",
        actor_id="test",
        causation_id="cause-role",
    )
    changed_caps = topology.set_capabilities(
        "graph-dynamic-test",
        "node-b",
        ("execute", "verify"),
        actor_id="test",
        causation_id="cause-capabilities",
    )
    snapshot = custody.current("graph-dynamic-test")
    assert snapshot.node_map["node-b"].role == "reviewer"
    assert snapshot.node_map["node-b"].capabilities == ("execute", "verify")
    assert snapshot.revision == 5
    assert custody.store.integrity_report("graph-dynamic-test")["ok"] is True

    blocked = topology.remove_node(
        "graph-dynamic-test",
        "node-a",
        actor_id="test",
        causation_id="cause-remove-blocked",
    )
    assert blocked.receipt.status is GraphCommitStatus.CONFLICTED
    topology.remove_edge(
        "graph-dynamic-test",
        "edge-a-b",
        actor_id="test",
        causation_id="cause-remove-edge",
    )
    builder = custody.branch(
        "graph-dynamic-test",
        branch_id="remove-dependency",
        actor_id="test",
        causation_id="cause-remove-dependency",
    )
    builder.set_dependencies("node-b", ())
    assert custody.commit(builder.build()).receipt.committed
    removed = topology.remove_node(
        "graph-dynamic-test",
        "node-a",
        actor_id="test",
        causation_id="cause-remove-node",
    )
    assert "node-a" not in removed.snapshot.node_map


def test_branch_local_delta_is_immutable_and_write_conflict_is_deterministic(tmp_path: Path) -> None:
    custody, topology = _custody(tmp_path / "graph.sqlite3")
    topology.add_node(
        "graph-dynamic-test",
        GraphNode(node_id="node-a", role="worker", capabilities=("execute",)),
        actor_id="test",
        causation_id="bootstrap",
    )
    base = custody.current("graph-dynamic-test")
    first_builder = GraphDeltaBuilder(
        base,
        branch_id="branch-first",
        actor_id="agent-a",
        causation_id="cause-first",
    )
    first_builder.read_node("node-a")
    first = first_builder.set_role("node-a", "reviewer").build()
    second_builder = GraphDeltaBuilder(
        base,
        branch_id="branch-second",
        actor_id="agent-b",
        causation_id="cause-second",
    )
    second_builder.read_node("node-a")
    second = second_builder.set_capabilities("node-a", ("execute", "verify")).build()
    first_result = custody.commit(first)
    second_result = custody.commit(second, strategy=GraphConflictStrategy.SERIALIZE)
    assert first_result.receipt.status is GraphCommitStatus.COMMITTED
    assert second_result.receipt.status is GraphCommitStatus.CONFLICTED
    assert {item.kind for item in second_result.receipt.conflicts}.intersection(
        {GraphConflictKind.READ_WRITE, GraphConflictKind.WRITE_WRITE}
    )
    assert base.node_map["node-a"].role == "worker"
    assert custody.current("graph-dynamic-test").node_map["node-a"].role == "reviewer"


def test_disjoint_stale_branch_can_rebase_without_order_sensitive_reducer(tmp_path: Path) -> None:
    custody, topology = _custody(tmp_path / "graph.sqlite3")
    topology.add_node(
        "graph-dynamic-test",
        GraphNode(node_id="node-a", role="worker", capabilities=("a",)),
        actor_id="test",
        causation_id="bootstrap-a",
    )
    topology.add_node(
        "graph-dynamic-test",
        GraphNode(node_id="node-b", role="worker", capabilities=("b",)),
        actor_id="test",
        causation_id="bootstrap-b",
    )
    base = custody.current("graph-dynamic-test")
    one = GraphDeltaBuilder(
        base,
        branch_id="branch-a",
        actor_id="a",
        causation_id="change-a",
    ).set_role("node-a", "planner").build()
    two = GraphDeltaBuilder(
        base,
        branch_id="branch-b",
        actor_id="b",
        causation_id="change-b",
    ).set_role("node-b", "reviewer").build()
    assert custody.commit(one).receipt.committed
    rebased = custody.commit(two, strategy=GraphConflictStrategy.REBASE)
    assert rebased.receipt.committed
    current = custody.current("graph-dynamic-test")
    assert current.node_map["node-a"].role == "planner"
    assert current.node_map["node-b"].role == "reviewer"


def test_cycle_is_rejected_before_snapshot_commit(tmp_path: Path) -> None:
    custody, topology = _custody(tmp_path / "graph.sqlite3")
    topology.add_node(
        "graph-dynamic-test",
        GraphNode(node_id="node-a", role="worker", capabilities=("a",)),
        actor_id="test",
        causation_id="bootstrap-a",
    )
    topology.add_node(
        "graph-dynamic-test",
        GraphNode(node_id="node-b", role="worker", capabilities=("b",), dependencies=("node-a",)),
        actor_id="test",
        causation_id="bootstrap-b",
    )
    builder = custody.branch(
        "graph-dynamic-test",
        branch_id="cycle",
        actor_id="test",
        causation_id="cycle-cause",
    )
    builder.set_dependencies("node-a", ("node-b",))
    result = custody.commit(builder.build(), strategy=GraphConflictStrategy.REPLAN)
    assert result.receipt.status is GraphCommitStatus.REPLAN_REQUIRED
    assert GraphConflictKind.CYCLE_DETECTED in {item.kind for item in result.receipt.conflicts}
