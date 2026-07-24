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


def _real_fault_recovery() -> tuple[Any, list[EventRecord]]:
    state = create_task_state(
        "Execute a heterogeneous worker task and recover after node timeout."
    )
    runtime_events = list(run_task_graph(state))
    failure = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.FAILURE_INJECTED,
        node_id=state.root_node_id,
        payload={"raw": "BrowserWorker node timeout and failure"},
    )
    recovery_events = list(apply_failure_injection(state, failure))
    events = [*runtime_events, *recovery_events]
    assert any(event.event_type == EventType.NODE_FAILED for event in events)
    assert any(event.event_type == EventType.RECOVERY_PLANNED for event in events)
    assert sum(
        event.event_type == EventType.RESOURCE_DECISION for event in events
    ) >= 2
    return state, events


def _selected(events: list[EventRecord]) -> list[EventRecord]:
    output: list[EventRecord] = []
    for event in events:
        if event.event_type in {
            EventType.TOPOLOGY_ROUTE,
            EventType.RESOURCE_DECISION,
            EventType.NODE_FAILED,
            EventType.RECOVERY_PLANNED,
        }:
            output.append(event)
            continue
        if event.event_type != EventType.NODE_UPDATED:
            continue
        node = event.payload.get("node", {})
        if (
            node.get("metadata", {}).get("stage") == "execute"
            and node.get("status") in {"running", "completed"}
        ):
            output.append(event)
    return output


def test_real_scheduler_fault_recovery_reaches_cross_view_trace(
    tmp_path: Path,
) -> None:
    state, events = _real_fault_recovery()
    selected = _selected(events)
    payload = {
        "task_id": state.task_id,
        "run_id": state.run_id,
        "events": [_event_json(event) for event in selected],
    }
    input_path = tmp_path / "causal-trace-input.json"
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
                / "causal-trace-integration-probe.ts"
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

    assert projected["adaptedEventCount"] == len(selected)
    assert projected["sourceEventIds"] == [event.event_id for event in selected]
    assert {
        "placement",
        "provider",
        "provider-retry",
        "fault",
        "recovery",
        "restore",
    } <= set(projected["semantics"])
    assert {"CodeWorkerRuntime", "BrowserWorker"} <= set(
        projected["workerIds"]
    )
    assert projected["providerIds"]
    assert projected["failureIds"]
    assert projected["recoveryIds"]
    assert projected["criticalPath"]["eventIds"]
    assert projected["criticalPath"]["workerIds"]
    assert projected["diagnostics"]["ready"] is True
    assert projected["diagnostics"]["lag"] == 0
    assert projected["hasTimelineTarget"] is True
    assert {"trace", "timeline", "topology"} <= set(
        projected["navigationViews"]
    )
    assert projected["disabledCode"] == "trace_projection_disabled"
    assert projected["disabledAudit"]["disabled"] is True
    assert projected["disabledAudit"]["projectionCount"] == 0
