from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
for relative in (
    "packages/core",
    "packages/runtime",
    "packages/workers",
    "packages/workspace",
    "packages/skills",
    "packages/integrations",
    "packages/orchestration",
):
    package_path = str(ROOT / relative)
    if package_path not in sys.path:
        sys.path.insert(0, package_path)

from zyra_runtime.runtime_events import (  # noqa: E402
    CodeWorkerRuntimeEventIngress,
    RuntimeEventContractError,
    RuntimeEventQuery,
    RuntimeEventSpineBridge,
    WorkerIngressIdentity,
)
from zyra_runtime.workers import WorkerRequest  # noqa: E402
from zyra_workers import CodeWorkerRuntime  # noqa: E402


def _bridge(tmp_path: Path) -> RuntimeEventSpineBridge:
    return RuntimeEventSpineBridge.create(
        database_path=tmp_path / "runtime-events.sqlite3",
        artifact_root=tmp_path / "runtime-event-artifacts",
        workspace_root=ROOT,
    )


def test_codeworker_ingress_preserves_causality_and_live_only_boundaries(
    tmp_path: Path,
) -> None:
    bridge = _bridge(tmp_path)
    ingress = CodeWorkerRuntimeEventIngress(
        bridge,
        WorkerIngressIdentity(
            run_id="run-ingress",
            task_id="task-ingress",
            session_id="session-ingress",
            worker_request_id="request-ingress",
            node_id="worker-node",
        ),
    )
    try:
        admitted = ingress.admit_query(sequence=0)
        turn = ingress.emit_payload(
            {"phase": "turn_start", "sequence": 1, "turn_index": 0}
        )
        called = ingress.emit_payload(
            {
                "phase": "tool_call_started",
                "sequence": 2,
                "tool_call_id": "call-1",
                "tool_name": "file_read",
                "arguments": {"path": "README.md"},
            }
        )
        succeeded = ingress.emit_payload(
            {
                "phase": "tool_call_completed",
                "sequence": 3,
                "tool_name": "file_read",
                "tool_result": {
                    "tool_call_id": "call-1",
                    "tool_name": "file_read",
                    "ok": True,
                    "output": "bounded result",
                },
            }
        )
        live = ingress.emit_payload(
            {"phase": "message_delta", "sequence": 4, "delta": "token"}
        )
        compact = ingress.emit_payload(
            {
                "phase": "context_compacted",
                "sequence": 5,
                "before_tokens": 9000,
                "after_tokens": 2200,
            }
        )

        assert admitted.event_type == "runtime.query.admitted"
        assert turn[0].event_type == "runtime.turn.started"
        assert called[0].event_type == "runtime.tool.called"
        assert succeeded[0].event_type == "runtime.tool.succeeded"
        assert live[0].live_only is True
        assert live[0].event_id is None
        assert [item.event_type for item in compact] == [
            "runtime.compact.started",
            "runtime.compact.completed",
        ]

        page = bridge.query(RuntimeEventQuery(task_id="task-ingress", limit=100))
        event_types = [event.event_type for event in page.events]
        assert event_types == [
            "runtime.query.admitted",
            "runtime.turn.started",
            "runtime.tool.called",
            "runtime.tool.succeeded",
            "runtime.compact.started",
            "runtime.compact.completed",
        ]
        terminal = next(
            event for event in page.events if event.event_type == "runtime.tool.succeeded"
        )
        tool_call = next(
            event for event in page.events if event.event_type == "runtime.tool.called"
        )
        assert terminal.causation_id == tool_call.event_id
        assert bridge.get_task_view("task-ingress")["toolCallsSettled"] == 1

        with pytest.raises(RuntimeEventContractError, match="not monotonic"):
            ingress.emit_payload({"phase": "turn_end", "sequence": 5})
    finally:
        bridge.close()


def test_default_codeworker_runtime_reaches_canonical_spine_without_log_parsing(
    tmp_path: Path,
) -> None:
    bridge = _bridge(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    runtime = CodeWorkerRuntime(
        project_root=ROOT,
        workspace_root=workspace,
        artifact_root=tmp_path / "worker-artifacts",
        runtime_services={"runtime_event_bridge": bridge},
    )
    request = WorkerRequest(
        run_id="run-live-worker",
        task_id="task-live-worker",
        worker_name="CodeWorkerRuntime",
        constraints={
            "query_turns": [],
            "permission_mode": "sealed",
            "session_id": "session-live-worker",
        },
    )
    try:
        result = runtime.run(request)
        assert result.worker_result.ok is True

        page = bridge.query(RuntimeEventQuery(task_id="task-live-worker", limit=200))
        event_types = {event.event_type for event in page.events}
        assert "runtime.query.admitted" in event_types
        assert "runtime.compact.restore" in event_types
        assert all(
            (event.canonical or {}).get("provenance", {}).get("normalizedFrom")
            != "stdout"
            for event in page.events
        )
        view = bridge.get_task_view("task-live-worker")
        assert view is not None
        assert view["taskId"] == "task-live-worker"
        assert view["lastGlobalSequence"] == page.high_watermark
    finally:
        bridge.close()
