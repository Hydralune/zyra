from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from zyra_evaluation.m1_hardening import (
    LongHorizonExecutionError,
    SealedLongHorizonRuntime,
)
from zyra_evaluation.m1_hardening.benchmark import BenchmarkAnalyzer
from zyra_evaluation.m1_hardening.evidence_graph import (
    CausalEdge,
    CausalEvidenceGraph,
    CausalNode,
)


def test_sealed_long_horizon_executes_two_real_tasks_and_passes_every_child_gate(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[2]
    receipt = SealedLongHorizonRuntime(
        project_root,
        artifact_root=tmp_path,
    ).execute(
        run_id="m1-sealed-behavior",
        actions_per_task=500,
    )

    assert receipt.accepted
    assert [item.action_count for item in receipt.tasks] == [500, 500]
    assert all(item.succeeded for item in receipt.tasks)
    assert receipt.metrics["effective_progress_rate"] == 1.0
    assert receipt.metrics["human_intervention_count"] == 0
    assert receipt.metrics["restart_count"] == 1
    assert receipt.metrics["recovery_count"] == 2
    assert receipt.metrics["final_constraint_satisfaction_rate"] == 1.0
    assert receipt.benchmark["effective_action_count"] >= 1_000
    assert receipt.benchmark["effective_transition_count"] >= 2_000
    assert receipt.benchmark["child_status"] == {
        "causal-evidence": "passed",
        "long-horizon-progress": "passed",
        "low-entropy": "passed",
        "sealed-autonomy": "passed",
    }
    assert Path(receipt.event_path).is_file()
    assert Path(receipt.state_path).is_file()
    assert Path(receipt.receipt_path).is_file()
    assert receipt.algorithm_materials["source_event_count"] >= 2_000
    assert receipt.algorithm_materials["selected_node_count"] >= 20
    assert receipt.algorithm_materials["selected_edge_count"] >= 10
    for key in ("graph_path", "mermaid_path", "analysis_path", "manifest_path"):
        assert Path(str(receipt.algorithm_materials[key])).is_file()

    events = SealedLongHorizonRuntime.load_events(receipt.event_path)
    snapshot = BenchmarkAnalyzer().analyze(events, run_id=receipt.run_id)
    assert len(snapshot.effective_actions) >= 1_000
    assert len(snapshot.effective_transitions) >= 2_000
    assert snapshot.duplicates == 0
    assert snapshot.requirement_changes >= 1
    assert snapshot.fault_recoveries >= 2
    assert snapshot.topology_mutations >= 1
    assert sum(snapshot.omp_effects.values()) >= 2

    with sqlite3.connect(receipt.state_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM action_results").fetchone()[0] == 1_000
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM topology_nodes WHERE state IN ('active', 'lost')"
        ).fetchone()[0] >= 3

    persisted = json.loads(Path(receipt.receipt_path).read_text(encoding="utf-8"))
    assert persisted["content_digest"] == receipt.content_digest
    assert persisted["benchmark"]["passed"] is True
    graph = json.loads(
        Path(receipt.algorithm_materials["graph_path"]).read_text(encoding="utf-8")
    )
    assert graph["source_event_count"] == len(events)
    assert {
        "requirement_changed",
        "topology_node_added",
        "worker_fault_detected",
        "recovery_reroute_completed",
        "permission_denied",
        "restore_checkpoint_verified",
    } <= {item["event_type"] for item in graph["nodes"]}
    assert any(item["relation"] == "causes" for item in graph["edges"])
    analysis = Path(receipt.algorithm_materials["analysis_path"]).read_text(
        encoding="utf-8"
    )
    assert "O(V + E)" in analysis
    assert "O(W log W)" in analysis


def test_long_horizon_refuses_to_dilute_the_frozen_action_floor(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 500 actions"):
        SealedLongHorizonRuntime(
            Path(__file__).resolve().parents[2],
            artifact_root=tmp_path,
        ).execute(run_id="m1-too-short", actions_per_task=499)


def test_causal_scc_analysis_is_iterative_for_more_than_two_thousand_transitions() -> None:
    graph = CausalEvidenceGraph()
    previous = ""
    for index in range(2_500):
        node_id = f"node-{index:04d}"
        graph.add_node(
            CausalNode(
                node_id=node_id,
                kind="artifact_committed",
                semantic_family="artifact",
                task_id="long-task",
                run_id="long-run",
                sequence=index + 1,
                revision_before=index,
                revision_after=index + 1,
                payload_digest=f"{index:064x}"[-64:],
                canonical=True,
            )
        )
        if previous:
            graph.add_edge(
                CausalEdge(
                    source_id=previous,
                    target_id=node_id,
                    relation="sequence",
                    explicit=False,
                )
            )
        previous = node_id

    components = graph.strongly_connected_components()
    assert len(components) == 2_500
    assert all(len(item) == 1 for item in components)


def test_topology_is_not_misclassified_as_log_noise() -> None:
    event = {
        "event_id": "event-topology",
        "run_id": "run-topology",
        "task_id": "task-topology",
        "action_id": "action-topology",
        "event_type": "topology_node_added",
        "sequence": 1,
        "before_revision": 0,
        "after_revision": 1,
        "before": {"nodes": 1},
        "after": {"nodes": 2},
        "causation_id": "task-root",
        "payload": {
            "semantic_family": "topology",
            "semantic_key": "topology.node.new",
            "action_id": "action-topology",
            "before_revision": 0,
            "after_revision": 1,
            "before": {"nodes": 1},
            "after": {"nodes": 2},
            "causation_id": "task-root",
        },
    }
    snapshot = BenchmarkAnalyzer().analyze((event,), run_id="run-topology")
    assert len(snapshot.effective_transitions) == 1
    assert snapshot.topology_mutations == 1
