from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType, create_task_state
from zyra_orchestration import run_task_graph
from zyra_symbolic import apply_failure_injection


ROOT = Path(__file__).resolve().parents[2]


def _event_json(event: EventRecord) -> dict[str, Any]:
    value = asdict(event)
    value["event_type"] = str(event.event_type)
    return value


def _real_worker_fault_recovery_events() -> tuple[Any, list[EventRecord]]:
    state = create_task_state(
        "Run a long worker task and recover after a real injected node failure."
    )
    runtime_events = list(run_task_graph(state))
    failure_event = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.FAILURE_INJECTED,
        node_id=state.root_node_id,
        payload={
            "raw": "node=execute BrowserWorker timeout and node failure",
        },
    )
    recovery_events = list(apply_failure_injection(state, failure_event))
    events = [*runtime_events, *recovery_events]
    execute_updates = [
        event
        for event in runtime_events
        if event.event_type == EventType.NODE_UPDATED
        and event.payload.get("node", {}).get("metadata", {}).get("stage")
        == "execute"
    ]
    assert {
        event.payload["node"]["status"] for event in execute_updates
    } >= {"running", "completed"}
    assert sum(
        event.event_type == EventType.RESOURCE_DECISION for event in events
    ) >= 2
    assert any(event.event_type == EventType.NODE_FAILED for event in events)
    assert any(
        event.event_type == EventType.RECOVERY_PLANNED for event in events
    )
    return state, events


def _projection_source_events(
    events: list[EventRecord],
) -> list[EventRecord]:
    selected: list[EventRecord] = []
    for event in events:
        if event.event_type == EventType.RESOURCE_DECISION:
            payload = event.payload
            decision = payload.get("resource_decision") or payload.get("decision") or {}
            plan = payload.get("recovery_plan") or {}
            if not any(
                str(value or "")
                for value in (
                    payload.get("selected_worker"),
                    decision.get("selected_worker"),
                    decision.get("selected"),
                    plan.get("selected_worker"),
                )
            ):
                continue
            selected.append(event)
            continue
        if event.event_type in {
            EventType.TOPOLOGY_ROUTE,
            EventType.NODE_FAILED,
            EventType.RECOVERY_PLANNED,
        }:
            selected.append(event)
            continue
        if event.event_type != EventType.NODE_UPDATED:
            continue
        node = event.payload.get("node", {})
        if (
            node.get("metadata", {}).get("stage") == "execute"
            and node.get("status") in {"running", "completed"}
        ):
            selected.append(event)
    return selected


def test_real_m1_worker_fault_and_recovery_drive_causal_timeline(
    tmp_path: Path,
) -> None:
    state, events = _real_worker_fault_recovery_events()
    selected = _projection_source_events(events)
    payload = {
        "task_id": state.task_id,
        "run_id": state.run_id,
        "events": [_event_json(event) for event in selected],
    }
    input_path = tmp_path / "worker-causal-timeline-input.json"
    input_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(ROOT / "node_modules" / ".bin" / "bun.exe"),
            str(
                ROOT
                / "apps"
                / "web"
                / "test"
                / "worker-causal-timeline-probe.ts"
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

    selected_ids = [event.event_id for event in selected]
    assert projected["adaptedEventCount"] == len(selected_ids)
    assert projected["sourceEventIds"] == selected_ids
    assert "worker.running" in projected["eventTypes"]
    assert "worker.completed" in projected["eventTypes"]
    assert "watchdog.worker.failed" in projected["eventTypes"]
    assert "worker.lease.replaced" in projected["eventTypes"]
    assert "recovery.started" in projected["eventTypes"]
    assert {"running", "completed", "failed", "recovering"} <= set(
        projected["phases"]
    )
    assert {"CodeWorkerRuntime", "BrowserWorker"} <= set(
        projected["workerIds"]
    )
    assert len(projected["workerEpochs"]) >= 2
    assert projected["failureRows"], json.dumps(
        projected["rows"],
        ensure_ascii=False,
        indent=2,
    )
    assert projected["failureRows"][0]["workerId"] == "CodeWorkerRuntime"
    assert projected["recoveryRows"]
    assert projected["recoveryRows"][0]["recoveryId"]
    assert projected["recoveryRows"][0]["failureId"]
    assert projected["recoveryChains"]
    assert projected["recoveryChains"][0]["attempts"] >= 1
    assert projected["recoveryChains"][0]["failureId"]
    assert projected["graph"]["edgeCount"] >= len(selected) - 1
    assert projected["graph"]["explicitEdgeCount"] >= len(selected) - 2
    assert projected["graph"]["failureRecoveryEdgeCount"] >= 1
    assert projected["criticalPath"]["eventIds"]
    assert projected["criticalPath"]["workerIds"]
    assert projected["diagnostics"]["ready"] is True
    assert projected["diagnostics"]["lag"] == 0
    assert projected["disabledErrorCode"] == "timeline_projection_disabled"
    assert projected["disabledAudit"]["disabled"] is True
    assert projected["disabledAudit"]["projectionCount"] == 0
