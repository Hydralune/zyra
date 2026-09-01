from __future__ import annotations

import json
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
from apps.api.zyra_api.event_stream_ingress import (  # noqa: E402
    EventIngressApiFacade,
)


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
    bridge.register_subscription(
        {
            "subscriptionId": "product-live-ingress",
            "recipient": {
                "kind": "ui_projector",
                "id": "product-live-ingress",
                "requiredCapabilities": [],
            },
            "eventTypes": ["runtime.text.delta"],
            "intents": ["observation"],
            "aggregatePrefixes": [],
            "taskIds": [],
            "capabilityRefs": ["ui.project"],
            "capacity": 4096,
            "maxAttempts": 1,
            "ackTimeoutMs": 30_000,
            "backpressureMode": "coalesce_non_effective",
            "enabled": True,
            "priority": 0,
            "metadata": {"live_only": True},
        }
    )
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
        assistant_started = ingress.emit_payload(
            {
                "schema": "zyra.provider-assistant-presentation/v1",
                "phase": "assistant_text_started",
                "sequence": 5,
                "delta_kind": "assistant_text",
                "stream_id": "provider:dispatch-1",
                "assistant_message_id": "message:assistant:provider:dispatch-1",
            }
        )
        assistant_delta = ingress.emit_payload(
            {
                "schema": "zyra.provider-assistant-presentation/v1",
                "phase": "assistant_text_delta",
                "sequence": 6,
                "delta_kind": "assistant_text",
                "stream_id": "provider:dispatch-1",
                "assistant_message_id": "message:assistant:provider:dispatch-1",
                "content": "你好 **world**",
                "segment_index": 1,
            }
        )
        live_messages = bridge.poll_live("product-live-ingress")
        assistant_ended = ingress.emit_payload(
            {
                "schema": "zyra.provider-assistant-presentation/v1",
                "phase": "assistant_text_ended",
                "sequence": 7,
                "delta_kind": "assistant_text",
                "stream_id": "provider:dispatch-1",
                "assistant_message_id": "message:assistant:provider:dispatch-1",
                "segment_index": 2,
            }
        )
        compact = ingress.emit_payload(
            {
                "phase": "context_compacted",
                "sequence": 8,
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
        assert assistant_started[0].event_type == "runtime.text.started"
        assert assistant_delta[0].event_type is None
        assert assistant_delta[0].live_only is True
        assert assistant_delta[0].event_id is None
        assert len(live_messages) == 1
        live_event = live_messages[0]["event"]
        assert live_event["eventType"] == "runtime.text.delta"
        assert live_event["inline"]["presentation_text"] == "你好 **world**"
        assert assistant_ended[0].event_type == "runtime.text.ended"
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
            "runtime.text.started",
            "runtime.text.ended",
            "runtime.compact.started",
            "runtime.compact.completed",
        ]
        completed_text = next(
            event for event in page.events if event.event_type == "runtime.text.ended"
        )
        assert completed_text.canonical is not None
        assert (
            completed_text.canonical["inline"]["presentation_text"]
            == "你好 **world**"
        )
        assert (
            completed_text.canonical["inline"]["assistant_message_id"]
            == "message:assistant:provider:dispatch-1"
        )
        terminal = next(
            event for event in page.events if event.event_type == "runtime.tool.succeeded"
        )
        tool_call = next(
            event for event in page.events if event.event_type == "runtime.tool.called"
        )
        assert terminal.causation_id == tool_call.event_id
        assert bridge.get_task_view("task-ingress")["toolCallsSettled"] == 1

        with pytest.raises(RuntimeEventContractError, match="not monotonic"):
            ingress.emit_payload({"phase": "turn_end", "sequence": 8})
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


def test_product_sse_delivers_physical_assistant_delta_on_live_channel(
    tmp_path: Path,
) -> None:
    bridge = _bridge(tmp_path)
    facade = EventIngressApiFacade(bridge)
    ingress = CodeWorkerRuntimeEventIngress(
        bridge,
        WorkerIngressIdentity(
            run_id="run-product-live",
            task_id="task-product-live",
            session_id="session-product-live",
            worker_request_id="request-product-live",
        ),
    )
    try:
        ingress.admit_query(sequence=0)
        ingress.emit_payload(
            {
                "schema": "zyra.provider-assistant-presentation/v1",
                "phase": "assistant_text_started",
                "sequence": 1,
                "delta_kind": "assistant_text",
                "stream_id": "provider:dispatch-live",
                "assistant_message_id": "message:assistant:provider:dispatch-live",
            }
        )
        capabilities = facade.capabilities(
            "task-product-live",
            {"generation": 1},
        )
        snapshot = facade.snapshot(
            "task-product-live",
            {"generation": 1, "limit": 100},
        )
        assert snapshot.body["complete"] is True
        cursor = str(snapshot.body["cursor"])
        stream = facade.sse(
            "task-product-live",
            {
                "cursor": cursor,
                "generation": 1,
                "wait_ms": 50,
                "stream_ms": 500,
                "heartbeat_ms": 250,
            },
        )
        assert next(stream).startswith(b"retry:")
        assert b"event: ready" in next(stream)

        ingress.emit_payload(
            {
                "schema": "zyra.provider-assistant-presentation/v1",
                "phase": "assistant_text_delta",
                "sequence": 2,
                "delta_kind": "assistant_text",
                "stream_id": "provider:dispatch-live",
                "assistant_message_id": "message:assistant:provider:dispatch-live",
                "content": "实时回答",
                "segment_index": 1,
            }
        )
        live_frame = next(stream)
        assert b"event: live" in live_frame
        encoded = live_frame.split(b"data: ", 1)[1].split(b"\n", 1)[0]
        payload = json.loads(encoded.decode("utf-8"))
        assert payload["kind"] == "live"
        assert payload["eventType"] == "runtime.text.delta"
        assert payload["presentation"]["text"] == "实时回答"
        assert payload["event"]["durability"] == "live_only"
        stream.close()
    finally:
        bridge.close()
