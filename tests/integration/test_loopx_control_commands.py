from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from zyra_core import EventType, PlanNodeStatus, create_task_state
from zyra_orchestration import (
    GraphExecutionContext,
    run_task_graph,
)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_handler() -> type[BaseHTTPRequestHandler]:
    module_name = "apps.api.zyra_api.main"
    if module_name in sys.modules:
        module = importlib.reload(sys.modules[module_name])
    else:
        module = importlib.import_module(module_name)
    return module.ZyraRequestHandler


def _configure(root: Path) -> None:
    os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
    os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
    os.environ["ZYRA_TOOL_WORKSPACE"] = str(root / "workspace")
    os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
    os.environ["ZYRA_CONTROL_STATE"] = str(root / "control")
    os.environ["ZYRA_GRAPH_STATE_STORE"] = str(root / "graph.sqlite3")
    os.environ["ZYRA_WORKER_POOL_STORE"] = str(root / "worker-pool.sqlite3")
    os.environ["ZYRA_PERMISSION_STATE"] = str(root / "permission.json")


def _request(
    base: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=(
            json.dumps(payload).encode("utf-8")
            if payload is not None
            else None
        ),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return (
                response.status,
                json.loads(response.read().decode("utf-8")),
            )
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_loopx_commands_use_canonical_commit_outbox_and_real_ack(
    tmp_path: Path,
) -> None:
    _configure(tmp_path)
    handler = _fresh_handler()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, created = _request(
            base,
            "/tasks",
            method="POST",
            payload={
                "goal": "Keep a verified long-horizon delivery moving.",
                "auto_run": False,
            },
        )
        assert status == 201
        task_id = created["task"]["task_id"]

        _, commands = _request(base, "/commands")
        names = {item["name"] for item in commands["commands"]}
        assert {
            "/loopx-connect",
            "/loopx-status",
            "/loopx-todos",
            "/loopx-claim",
            "/loopx-release",
            "/loopx-quota",
            "/loopx-interaction",
            "/loopx-sync",
            "/loopx-retry",
            "/loopx-disconnect",
        }.issubset(names)

        connect_payload = {
            "text": "/loopx-connect",
            "request_id": "loopx-connect-request",
            "idempotency_key": "loopx-connect-idempotency",
            "arguments": {
                "todo_id": "todo_delivery",
                "todo_title": "Deliver the next verified increment",
                "limit_slots": 3,
            },
        }
        status, connected = _request(
            base,
            f"/tasks/{task_id}/commands",
            method="POST",
            payload=connect_payload,
        )
        assert status == 201
        result = connected["command_result"]["data"]
        assert result["receipt"]["status"] == "applied"
        assert result["receipt"]["event_id"]
        assert result["receipt"]["artifact_id"]
        assert result["state"]["lifecycle"] == "enabled"
        assert result["state"]["canonical_state"][
            "loopx_claim_is_worker_lease"
        ] is False
        assert result["state"]["canonical_state"][
            "loopx_quota_is_execution_budget"
        ] is False

        stale_status, stale = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "claim",
                "todo_id": "todo_delivery",
                "claimant": "stale-controller",
                "expected_cursor": 0,
            },
        )
        assert stale_status == 409
        assert stale["ok"] is False
        assert stale["state"]["sync"]["cursor"] == 1
        assert stale["state"]["private_state"]["claims"] == []

        replay_status, replayed = _request(
            base,
            f"/tasks/{task_id}/commands",
            method="POST",
            payload=connect_payload,
        )
        assert replay_status == 201
        assert replayed["command_result"]["request_id"] == (
            connected["command_result"]["request_id"]
        )
        _, sync_after_replay = _request(
            base,
            f"/tasks/{task_id}/commands",
            method="POST",
            payload={"text": "/loopx-sync"},
        )
        assert sync_after_replay["command_result"]["data"]["sync"][
            "acked"
        ] == 1

        claim_status, claimed = _request(
            base,
            f"/tasks/{task_id}/commands",
            method="POST",
            payload={
                "text": "/loopx-claim",
                "arguments": {
                    "todo_id": "todo_delivery",
                    "claimant": "loopx-controller-a",
                },
            },
        )
        assert claim_status == 201
        assert claimed["command_result"]["data"]["receipt"]["status"] == (
            "applied"
        )
        _, todo = _request(
            base,
            f"/tasks/{task_id}/commands",
            method="POST",
            payload={
                "text": "/loopx-todo todo_delivery",
            },
        )
        assert todo["command_result"]["data"]["todo"]["claimed_by"] == (
            "loopx-controller-a"
        )

        release_status, released = _request(
            base,
            f"/tasks/{task_id}/commands",
            method="POST",
            payload={
                "text": "/loopx-release",
                "arguments": {
                    "todo_id": "todo_delivery",
                    "claimant": "loopx-controller-a",
                },
            },
        )
        assert release_status == 201
        assert released["command_result"]["data"]["receipt"]["status"] == (
            "applied"
        )
        _, released_todo = _request(
            base,
            f"/tasks/{task_id}/commands",
            method="POST",
            payload={"text": "/loopx-todo todo_delivery"},
        )
        assert "claimed_by" not in released_todo["command_result"]["data"][
            "todo"
        ]

        typed_headers = {
            "X-Zyra-Api-Version": "1.0",
            "X-Request-Id": "request_loopx-interaction-one",
            "X-Zyra-Operation": "task.loopx.command",
            "X-Zyra-Contract": "zyra.loopx-control-result.v1",
            "Idempotency-Key": "interaction-one",
        }
        interaction_payload = {
            "action": "interaction_submit",
            "continuation_hint": "Continue the bounded delivery",
            "input_ref": "event:permission-checked-input",
            "idempotency_key": "interaction-one",
        }
        interaction_status, interaction = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload=interaction_payload,
            headers=typed_headers,
        )
        assert interaction_status == 201, interaction
        assert interaction["schema"] == "zyra.loopx-control-result/v1"
        assert interaction["receipt"]["status"] == "committed"
        assert interaction["receipt"]["operation"] == "task.loopx.command"
        assert interaction["sync_receipt"]["status"] == "applied"
        assert interaction["state"]["continuation"]["allowed"] is True
        duplicate_status, duplicate_interaction = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload=interaction_payload,
            headers={
                **typed_headers,
                "X-Request-Id": "request_loopx-interaction-two",
            },
        )
        assert duplicate_status == 201
        assert duplicate_interaction["receipt"]["status"] == "replayed"
        assert duplicate_interaction["sync_receipt"]["status"] == "applied"
        assert duplicate_interaction["state"]["sync"]["acked"] == (
            interaction["state"]["sync"]["acked"]
        )

        _, task = _request(base, f"/tasks/{task_id}")
        continuation = task["task"]["metadata"]["loopx_continuation"]
        assert continuation["enabled"] is True
        assert continuation["continuation_allowed"] is True
        assert continuation["obligation"] == (
            "Deliver the next verified increment"
        )
        mutations = task["task"]["metadata"]["control_mutations"]
        assert mutations[-1]["canonical_owner"] == "GraphStateCustody"
        assert mutations[-1]["private_state_owner"] == "LoopX"

        disconnected_status, disconnected = _request(
            base,
            f"/tasks/{task_id}/commands",
            method="POST",
            payload={"text": "/loopx-disconnect"},
        )
        assert disconnected_status == 201
        disconnected_state = disconnected["command_result"]["data"]["state"]
        assert disconnected_state["lifecycle"] == "disabled"
        assert disconnected_state["continuation"]["allowed"] is False

        denied_status, denied = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "disconnect",
                "sealed": True,
                "competition_mode": "sealed_autonomous",
            },
        )
        assert denied_status == 409
        assert denied["command_result"]["error"]["code"] == (
            "permission_denied"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_claim_conflict_and_quota_exhaustion_return_typed_receipts(
    tmp_path: Path,
) -> None:
    _configure(tmp_path)
    handler = _fresh_handler()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        _, created = _request(
            base,
            "/tasks",
            method="POST",
            payload={"goal": "Exercise LoopX failures.", "auto_run": False},
        )
        task_id = created["task"]["task_id"]
        _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "connect",
                "todo_id": "todo_failure",
                "todo_title": "Bounded failure case",
                "limit_slots": 1,
            },
        )
        _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "claim",
                "todo_id": "todo_failure",
                "claimant": "controller-a",
            },
        )
        conflict_status, conflict = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "claim",
                "todo_id": "todo_failure",
                "claimant": "controller-b",
            },
        )
        assert conflict_status == 409
        assert conflict["receipt"]["status"] == "claim_conflict"
        assert conflict["receipt"]["apply_receipt"]["claim_conflict"][
            "worker_lease_changed"
        ] is False
        release_status, _ = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "release",
                "todo_id": "todo_failure",
                "claimant": "controller-a",
            },
        )
        assert release_status == 201
        retry_conflict_status, recovered_conflict = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={"action": "sync_retry"},
        )
        assert retry_conflict_status == 201
        assert recovered_conflict["state"]["sync"]["dead_letter"] == 0
        assert recovered_conflict["state"]["private_state"]["claims"] == [
            {
                "todo_id": "todo_failure",
                "claimant": "controller-b",
            }
        ]

        exhausted_status, exhausted = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "interaction_submit",
                "input_ref": "event:quota-test",
                "spend_slots": 1,
            },
        )
        assert exhausted_status == 201
        assert exhausted["receipt"]["status"] == "quota_exhausted"
        assert exhausted["state"]["private_state"]["quota"]["exhausted"] is (
            True
        )
        assert exhausted["state"]["continuation"]["allowed"] is False
        retry_status, retry_exhausted = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "interaction_submit",
                "input_ref": "event:quota-test-again",
                "spend_slots": 1,
            },
        )
        assert retry_status == 201
        assert retry_exhausted["receipt"]["status"] == "quota_exhausted"
        assert retry_exhausted["receipt"]["apply_receipt"][
            "spend_applied_slots"
        ] == 0
        assert retry_exhausted["state"]["private_state"]["quota"][
            "spent_slots"
        ] == 1
        recovery_status, recovered_quota = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "connect",
                "todo_id": "todo_failure",
                "todo_title": "Bounded failure case",
                "limit_slots": 4,
            },
        )
        assert recovery_status == 201
        assert recovered_quota["state"]["private_state"]["quota"][
            "remaining_slots"
        ] == 3
        assert recovered_quota["state"]["continuation"]["allowed"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_loopx_continuation_gate_changes_real_worker_dispatch(
    tmp_path: Path,
) -> None:
    blocked = create_task_state("Run a gated long-horizon increment.")
    blocked.metadata["loopx_continuation"] = {
        "schema": "zyra.loopx-continuation/v1",
        "enabled": True,
        "lifecycle": "degraded",
        "goal_id": "goal_gate",
        "todo_id": "todo_gate",
        "obligation": "Repair durable sync before continuing",
        "continuation_allowed": False,
    }
    context = GraphExecutionContext.from_paths(
        project_root=ROOT,
        workspace_root=tmp_path / "blocked-workspace",
        artifact_root=tmp_path / "blocked-artifacts",
    )
    events = run_task_graph(blocked, execution_context=context)
    execute = next(
        item
        for item in blocked.plan_nodes.values()
        if item.metadata.get("stage") == "execute"
    )
    assert execute.status is PlanNodeStatus.BLOCKED
    assert blocked.budget.tool_calls == 0
    assert not any("tool_result" in event.payload for event in events)
    gate_events = [
        event
        for event in events
        if event.event_type is EventType.SYSTEM_NOTICE
        and event.payload.get("schema")
        == "zyra.loopx-continuation-blocked/v1"
    ]
    assert len(gate_events) == 1
    assert gate_events[0].payload["physical_worker_dispatched"] is False
    assert gate_events[0].payload["execution_budget_spent"] is False

    baseline = create_task_state("Run the same increment without LoopX.")
    baseline.metadata["loopx_continuation"] = {
        **blocked.metadata["loopx_continuation"],
        "enabled": False,
        "lifecycle": "disabled",
    }
    baseline_events = run_task_graph(
        baseline,
        execution_context=GraphExecutionContext.from_paths(
            project_root=ROOT,
            workspace_root=tmp_path / "baseline-workspace",
            artifact_root=tmp_path / "baseline-artifacts",
        ),
    )
    assert any("tool_result" in event.payload for event in baseline_events)
    assert baseline.budget.tool_calls >= 2
