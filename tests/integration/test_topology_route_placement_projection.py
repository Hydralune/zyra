from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType, create_task_state
from zyra_orchestration import ensure_default_graph
from zyra_orchestration.graph_custody import (
    DynamicTopologyRuntime,
    GraphEdge,
    GraphNode,
    GraphStateCustody,
    GraphStateStore,
)
from zyra_scheduler import ResourceScheduler
from zyra_symbolic import apply_requirement_change


ROOT = Path(__file__).resolve().parents[2]


def _decision_payload(decision: Any) -> dict[str, Any]:
    raw = asdict(decision)
    selected = {
        "candidate_id": raw["selected_manifest_id"],
        "worker_id": raw["selected_worker"],
        "worker_name": raw["selected_worker"],
        "manifest_id": raw["selected_manifest_id"],
        "backend": str(raw["selected_backend"]),
        "location": str(raw["selected_location"]),
        "rank": 1,
        "score": raw["score"],
        "accepted": True,
        "selected": True,
        "reasons": list(raw["reasons"]),
        "capabilities": list(raw["metadata"]["selected_capabilities"]),
        "privacy_class": raw["metadata"]["selected_privacy"],
    }
    alternatives = [
        {
            "candidate_id": item["worker_id"],
            "worker_id": item["runtime_worker"],
            "worker_name": item["runtime_worker"],
            "manifest_id": item["worker_id"],
            "backend": item["backend"],
            "location": item["location"],
            "rank": index + 2,
            "score": item["score"],
            "accepted": True,
            "selected": False,
            "reasons": list(item["reasons"]),
        }
        for index, item in enumerate(raw["alternatives"])
    ]
    return {
        "decision_id": raw["decision_id"],
        "resource_decision_id": raw["decision_id"],
        "node_id": "execute",
        "selected_manifest_id": raw["selected_manifest_id"],
        "selected_worker": raw["selected_worker"],
        "selected_backend": str(raw["selected_backend"]),
        "selected_location": str(raw["selected_location"]),
        "score": raw["score"],
        "reasons": list(raw["reasons"]),
        "model_split": raw["model_split"],
        "signals": {
            "task_profile": raw["signals"]["task_profile"],
            "contains_url": raw["signals"]["contains_url"],
            "privacy_mode": raw["signals"]["privacy_mode"],
            "required_tools": raw["signals"]["required_tools"],
        },
        "privacy_class": raw["metadata"]["selected_privacy"],
        "selected_capabilities": list(raw["metadata"]["selected_capabilities"]),
        "candidates": [selected, *alternatives],
        "alternatives": [selected, *alternatives],
        "accepted": True,
    }


def _graph_payload(
    path: Path,
    *,
    task_id: str,
    run_id: str,
    backend_route_ref: str,
    selected_worker: str,
) -> dict[str, Any]:
    store = GraphStateStore(path)
    store.initialize()
    custody = GraphStateCustody(store)
    graph_id = f"graph:{task_id}"
    custody.create(graph_id_value=graph_id, run_id=run_id)
    runtime = DynamicTopologyRuntime(custody)
    results = [
        runtime.add_node(
            graph_id,
            GraphNode(
                node_id="root",
                role="coordinator",
                capabilities=("planning", "routing"),
                metadata={"namespace": "root"},
            ),
            actor_id="projection-integration",
            causation_id=f"{task_id}:root",
        ),
        runtime.add_node(
            graph_id,
            GraphNode(
                node_id="execute",
                role=selected_worker,
                capabilities=("agent_task", "artifact_return"),
                dependencies=("root",),
                backend_route_ref=backend_route_ref,
                metadata={"namespace": "execution"},
            ),
            actor_id="projection-integration",
            causation_id=f"{task_id}:execute",
        ),
        runtime.add_edge(
            graph_id,
            GraphEdge(
                edge_id="edge:root:execute",
                source_node_id="root",
                target_node_id="execute",
                relation="handoff",
                required_capabilities=("agent_task",),
            ),
            actor_id="projection-integration",
            causation_id=f"{task_id}:edge",
        ),
    ]
    assert all(result.receipt.committed for result in results)
    snapshot = custody.current(graph_id)
    assert snapshot.node_map["execute"].backend_route_ref == backend_route_ref
    mutations = [
        mutation.to_dict()
        for result in results
        for mutation in result.delta.mutations
    ]
    return {
        "snapshot": snapshot.to_dict(),
        "mutations": mutations,
    }


def _requirement_change(state: Any) -> dict[str, Any]:
    event = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.REQUIREMENT_CHANGE,
        node_id=state.root_node_id,
        payload={"raw": "Keep source code and credentials on the local device."},
    )
    emitted = apply_requirement_change(state, event)
    assert emitted
    change = dict(state.metadata["requirement_changes"][-1])
    assert change["affected_node_ids"]
    assert change["replan_node_id"] in state.plan_nodes
    return {
        **change,
        "change_id": change["event_id"],
        "requirement_text": change["text"],
        "local_replan": True,
    }


def test_real_scheduler_and_dynamic_topology_change_the_browser_projection(
    tmp_path: Path,
) -> None:
    scheduler = ResourceScheduler()
    code_state = create_task_state("Patch TypeScript code and run unit tests.")
    ensure_default_graph(code_state)
    code_node = next(
        node
        for node in code_state.plan_nodes.values()
        if node.metadata.get("stage") == "execute"
    )
    code_decision = scheduler.decide(code_state, node=code_node)

    browser_state = create_task_state(
        "Open https://example.com and extract DOM and screenshot evidence."
    )
    ensure_default_graph(browser_state)
    browser_node = next(
        node
        for node in browser_state.plan_nodes.values()
        if node.metadata.get("stage") == "execute"
    )
    browser_decision = scheduler.decide(browser_state, node=browser_node)

    assert code_decision.selected_manifest_id == "provider-code-worker"
    assert browser_decision.selected_manifest_id == "edge-browser-worker"
    assert code_decision.selected_worker != browser_decision.selected_worker
    assert code_decision.selected_location != browser_decision.selected_location

    payload = {
        "cases": {
            "code": {
                "task_id": code_state.task_id,
                "run_id": code_state.run_id,
                "graph": _graph_payload(
                    tmp_path / "code-graph.sqlite3",
                    task_id=code_state.task_id,
                    run_id=code_state.run_id,
                    backend_route_ref=code_decision.selected_manifest_id,
                    selected_worker=code_decision.selected_worker,
                ),
                "decision": _decision_payload(code_decision),
                "requirement_change": _requirement_change(code_state),
            },
            "browser": {
                "task_id": browser_state.task_id,
                "run_id": browser_state.run_id,
                "graph": _graph_payload(
                    tmp_path / "browser-graph.sqlite3",
                    task_id=browser_state.task_id,
                    run_id=browser_state.run_id,
                    backend_route_ref=browser_decision.selected_manifest_id,
                    selected_worker=browser_decision.selected_worker,
                ),
                "decision": _decision_payload(browser_decision),
            },
        }
    }
    input_path = tmp_path / "topology-projection-input.json"
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [
            str(ROOT / "node_modules" / ".bin" / "bun.exe"),
            str(
                ROOT
                / "apps"
                / "web"
                / "test"
                / "topology-projection-probe.ts"
            ),
            str(input_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    projected = json.loads(completed.stdout.strip().splitlines()[-1])
    code = projected["code"]
    browser = projected["browser"]

    assert code["selectedRoute"]["workerId"] == code_decision.selected_worker
    assert browser["selectedRoute"]["workerId"] == browser_decision.selected_worker
    assert code["selectedRoute"]["backendId"] == str(code_decision.selected_backend)
    assert browser["selectedRoute"]["backendId"] == str(
        browser_decision.selected_backend
    )
    assert code["selectedRoute"]["location"] == str(code_decision.selected_location)
    assert browser["selectedRoute"]["location"] == str(
        browser_decision.selected_location
    )
    assert code["executeBackendRouteRef"] == code_decision.selected_manifest_id
    assert (
        browser["executeBackendRouteRef"]
        == browser_decision.selected_manifest_id
    )
    assert code["executeBackendRouteRef"] != browser["executeBackendRouteRef"]
    assert code["graphRevision"] >= 3
    assert browser["graphRevision"] >= 3
    assert code["openWorldMutationCount"] >= 3
    assert browser["openWorldMutationCount"] >= 3
    assert code["selectedRoute"]["fixedCandidateSelection"] is True
    assert browser["selectedRoute"]["fixedCandidateSelection"] is True
    assert code["requirementChanges"][0]["localReplan"] is True
    assert code["requirementChanges"][0]["affectedNodeIds"]
    assert code["diagnostics"]["ready"] is True
    assert browser["diagnostics"]["ready"] is True
