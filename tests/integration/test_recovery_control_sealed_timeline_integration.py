from __future__ import annotations

import json
import os
import threading
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError

from apps.api.zyra_api import main as api_main


def test_timeline_recovery_controls_reach_canonical_owners_and_fence_stale_requests(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        commands = {
            item["name"]: item
            for item in _get(base_url, "/commands")["commands"]
        }
        assert {
            "/kill",
            "/steer",
            "/retry",
            "/reassign",
            "/resume",
        }.issubset(commands)
        assert commands["/kill"]["handler_id"] == "recovery.kill"
        assert commands["/steer"]["handler_id"] == "recovery.steer"
        assert commands["/retry"]["handler_id"] == "recovery.retry"
        assert commands["/reassign"]["handler_id"] == "recovery.reassign"

        kill_task = _create_task(base_url, "Fence one live worker from the timeline.")
        kill_owner = _owner(kill_task)
        kill_status, killed = _command(
            base_url,
            kill_task,
            "kill",
            arguments={
                **kill_owner,
                "target": "worker-and-task",
                "reason": "Timeline operator observed an unsafe worker.",
                "old_fence_must_block_commit": True,
            },
        )
        assert kill_status == 201, killed["command_result"].get("error")
        assert killed["command_result"]["ok"] is True
        assert killed["command_result"]["data"]["phase"] == "applied"
        assert (
            killed["command_result"]["data"]["control_event"]["payload"][
                "control_runtime"
            ]["owner"]
            == "WorkerControlRuntime"
        )
        killed_lease = _lease(
            base_url,
            kill_owner["expected_lease_id"],
            task_id=kill_task["task_id"],
        )
        assert killed_lease["state"] == "cancelled"
        assert killed["command_result"]["data"]["observed_event_ids"]

        steer_task = _create_task(
            base_url,
            "Steer a live graph after the requirement changes.",
        )
        steer_status, steered = _command(
            base_url,
            steer_task,
            "steer",
            arguments={
                **_owner(steer_task),
                "node_id": steer_task["root_node_id"],
                "instruction": "Require verifier evidence before delivery.",
                "requirement": "Require verifier evidence before delivery.",
                "reason": "The goal changed while the run was active.",
                "change_scope": "recovery_graph_route",
            },
        )
        assert steer_status == 201, steered["command_result"].get("error")
        assert steered["command_result"]["ok"] is True
        assert steered["command_result"]["data"]["phase"] == "applied"
        assert (
            steered["command_result"]["data"]["control_event"]["payload"][
                "control_runtime"
            ]["owner"]
            == "RecoveryApplication"
        )
        assert (
            steered["command_result"]["data"]["recovery"]["plan"]["decision"][
                "selected"
            ]["action"]
            == "replan"
        )

        retry_task = _create_task(base_url, "Retry one bounded failed tool attempt.")
        retry_status, retried = _command(
            base_url,
            retry_task,
            "retry",
            arguments={
                **_owner(retry_task),
                "node_id": retry_task["root_node_id"],
                "tool_call_id": "tool-call-timeline-retry",
                "maximum_attempts": 1,
                "bounded_retry": True,
                "reason": "The observed tool failure is retryable once.",
            },
        )
        assert retry_status == 201, retried["command_result"].get("error")
        assert retried["command_result"]["ok"] is True
        assert retried["command_result"]["data"]["phase"] == "applied"
        assert retried["command_result"]["data"]["recovery"]["plan"]["decision"]
        assert (
            retried["command_result"]["data"]["recovery"]["plan"]["decision"][
                "selected"
            ]["action"]
            == "retry"
        )

        reassign_task = _create_task(
            base_url,
            "Reassign a lost worker without changing logical task ownership.",
        )
        api_main.get_worker_pool_api().ensure_default_local_worker(
            worker_id="timeline-successor-worker",
        )
        previous_owner = _owner(reassign_task)
        reassign_status, reassigned = _command(
            base_url,
            reassign_task,
            "reassign",
            arguments={
                **previous_owner,
                "node_id": reassign_task["root_node_id"],
                "exclude_worker_id": previous_owner["expected_worker_id"],
                "fence_previous_lease": True,
                "reason": "The current worker stopped producing heartbeats.",
            },
        )
        assert reassign_status == 201, reassigned["command_result"].get("error")
        assert reassigned["command_result"]["ok"] is True
        assert reassigned["command_result"]["data"]["phase"] == "applied"
        selected_reassignment = reassigned["command_result"]["data"]["recovery"][
            "plan"
        ]["decision"]["selected"]["action"]
        assert selected_reassignment == "reroute", selected_reassignment
        route_change = reassigned["command_result"]["data"]["recovery"][
            "execution"
        ]["receipts"][0]["route_decision"]["changes"][0]
        assert route_change["owner"] == "WorkerPoolFoundationRuntime"
        assert route_change["after_ref"]["worker_id"] == (
            "timeline-successor-worker"
        ), route_change
        refreshed = _get(base_url, f"/tasks/{reassign_task['task_id']}")["task"]
        replacement = refreshed["metadata"]["worker_pool"]
        assert replacement["worker_id"] == "timeline-successor-worker", reassigned[
            "command_result"
        ]["data"]["recovery"]["execution"]
        assert replacement["lease_id"] != previous_owner["expected_lease_id"]
        previous_lease = _lease(
            base_url,
            previous_owner["expected_lease_id"],
            task_id=reassign_task["task_id"],
        )
        assert previous_lease["state"] in {"cancelled", "released", "expired"}

        stale_task = _create_task(base_url, "Reject stale timeline owner evidence.")
        stale_owner = _owner(stale_task)
        stale_status, stale = _command(
            base_url,
            stale_task,
            "kill",
            arguments={
                **stale_owner,
                "expected_worker_id": "worker-owner-that-no-longer-exists",
                "target": "worker-and-task",
                "reason": "This request was composed from stale timeline data.",
            },
        )
        assert stale_status == 409, stale
        assert stale["command_result"]["ok"] is False
        live_lease = _lease(
            base_url,
            stale_owner["expected_lease_id"],
            task_id=stale_task["task_id"],
        )
        assert live_lease["state"] == "active"


def test_sealed_timeline_control_is_denied_once_without_manual_mutation_or_human_wait(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        task = _create_task(
            base_url,
            "Prove sealed recovery control remains autonomous.",
        )
        owner = _owner(task)
        payload = _command_payload(
            task,
            "kill",
            arguments={
                **owner,
                "target": "worker-and-task",
                "reason": "A benchmark observer attempted a manual kill.",
                "old_fence_must_block_commit": True,
            },
            sealed=True,
        )
        first_status, denied = _post_with_status(
            base_url,
            f"/tasks/{task['task_id']}/commands",
            payload,
        )
        second_status, replay = _post_with_status(
            base_url,
            f"/tasks/{task['task_id']}/commands",
            payload,
        )
        assert first_status == 409, denied
        assert second_status == 409, replay
        assert denied["command_result"]["ok"] is False
        assert denied["command_result"]["error"]["code"] == "permission_denied"
        assert denied["intervention_counted"] is True
        assert denied["operator_intervention_attempt_count"] == 1
        assert denied["human_intervention_count"] == 0
        assert replay["operator_intervention_attempt_count"] == 1
        state = api_main.get_store().load_task(task["task_id"])
        assert state is not None
        assert state.metadata["operator_intervention_attempt_count"] == 1
        assert state.metadata["human_intervention_count"] == 0
        assert len(state.metadata["operator_intervention_ledger"]) == 1
        sealed_denial = state.metadata["last_sealed_control_denial"]
        assert sealed_denial["manual_command_applied"] is False
        assert sealed_denial["human_wait_entered"] is False
        assert sealed_denial["recovery_phase"] in {"applied", "failed_closed"}
        assert (
            sealed_denial.get("recovery_action")
            or sealed_denial["recovery_phase"] == "failed_closed"
        )
        lease = _lease(
            base_url,
            owner["expected_lease_id"],
            task_id=task["task_id"],
        )
        # Autonomous recovery may finish and release the task lease, but the
        # rejected manual /kill must never cancel or fence it.
        assert lease["state"] not in {"cancelled", "fenced"}
        assert lease["worker_id"] == owner["expected_worker_id"]


def test_timeline_exact_resume_preserves_checkpoint_identity_and_idempotency(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        task = _create_task(base_url, "Resume an exact timeline checkpoint.")
        session_id = f"task:{task['task_id']}"
        checkpoint = _post(
            base_url,
            f"/tasks/{task['task_id']}/recovery/checkpoints",
            {
                "run_id": task["run_id"],
                "task_id": task["task_id"],
                "session_id": session_id,
                "workflow_signature": "timeline-control-workflow-v1",
                "graph_signature": "timeline-control-graph-v1",
                "topology_signature": "timeline-control-topology-v1",
                "owner_refs": {
                    "task": task["task_id"],
                    "session": session_id,
                },
                "version_refs": {"task": 1, "session": 1},
                "state_payload": {
                    "progress": 11,
                    "active_node_id": task["root_node_id"],
                },
                "completed_step_ids": ["timeline-step-10"],
            },
        )["checkpoint"]["checkpoint_id"]
        payload = _command_payload(
            task,
            "resume",
            arguments={
                **_owner(task),
                "target_session_id": session_id,
                "checkpoint_ref": checkpoint,
                "exact_resume": True,
                "candidate_step_ids": [],
            },
            session_id=session_id,
        )
        first_status, first = _post_with_status(
            base_url,
            f"/tasks/{task['task_id']}/commands",
            payload,
        )
        second_status, replay = _post_with_status(
            base_url,
            f"/tasks/{task['task_id']}/commands",
            payload,
        )
        assert first_status == 201, first
        assert second_status == 201, replay
        effect = first["command_result"]["data"]["transaction"]["effect"]
        assert effect["checkpoint_ref"] == checkpoint
        assert effect["metadata"]["exact_resume"] is True
        assert effect["metadata"]["same_session"] is True
        assert replay["command_result"] == first["command_result"]
        events = _get(base_url, f"/tasks/{task['task_id']}/events")["events"]
        resumed = [
            event
            for event in events
            if event["event_type"] == "topology_route"
            and event.get("payload", {}).get("recovery_runtime", {}).get("phase")
            == "checkpoint_resumed"
        ]
        assert len(resumed) == 1


def _create_task(base_url: str, goal: str) -> dict[str, Any]:
    return _post(
        base_url,
        "/tasks",
        {"goal": goal, "auto_run": False},
    )["task"]


def _owner(task: dict[str, Any]) -> dict[str, Any]:
    worker = task["metadata"]["worker_pool"]
    return {
        "expected_task_id": task["task_id"],
        "expected_run_id": task["run_id"],
        "expected_worker_id": worker["worker_id"],
        "expected_lease_id": worker["lease_id"],
        "expected_attempt_id": worker["attempt_id"],
    }


def _command_payload(
    task: dict[str, Any],
    action: str,
    *,
    arguments: dict[str, Any],
    sealed: bool = False,
    session_id: str | None = None,
) -> dict[str, Any]:
    suffix = task["task_id"].replace(":", "-")
    return {
        "text": f"/{action} timeline recovery control",
        "arguments": arguments,
        "request_id": f"request-timeline-{action}-{suffix}",
        "command_id": f"command-timeline-{action}-{suffix}",
        "idempotency_key": f"timeline-control-{action}-{suffix}",
        "actor_id": "sealed-benchmark-observer" if sealed else "timeline-operator",
        "session_id": session_id,
        "sealed": sealed,
        "competition_mode": "sealed_autonomous" if sealed else "interactive",
    }


def _command(
    base_url: str,
    task: dict[str, Any],
    action: str,
    *,
    arguments: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    return _post_with_status(
        base_url,
        f"/tasks/{task['task_id']}/commands",
        _command_payload(task, action, arguments=arguments),
    )


def _lease(
    base_url: str,
    lease_id: str,
    *,
    task_id: str,
) -> dict[str, Any]:
    leases = _get(
        base_url,
        f"/worker-pool/leases?task_id={task_id}",
    )["leases"]
    return next(item for item in leases if item["lease_id"] == lease_id)


@contextmanager
def _api(root: Path) -> Iterator[str]:
    variables = (
        "ZYRA_SQLITE_PATH",
        "ZYRA_RECOVERY_SQLITE_PATH",
        "ZYRA_EVENT_LOG",
        "ZYRA_ARTIFACT_ROOT",
        "ZYRA_TOOL_WORKSPACE",
        "ZYRA_WORKSPACE_ROOT",
        "ZYRA_PERMISSION_STATE",
        "ZYRA_PROVIDER_CONTROL_STATE",
        "ZYRA_WORKER_POOL_STORE",
        "ZYRA_GRAPH_STATE_STORE",
        "ZYRA_WORKSPACE_STORE",
        "ZYRA_DISABLE_RECOVERY_RUNTIME",
    )
    previous = {name: os.environ.get(name) for name in variables}
    os.environ.update(
        {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_RECOVERY_SQLITE_PATH": str(root / "recovery.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_TOOL_WORKSPACE": str(root / "tool-workspace"),
            "ZYRA_WORKSPACE_ROOT": str(root / "managed-workspaces"),
            "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
            "ZYRA_PROVIDER_CONTROL_STATE": str(
                root / "provider-control.sqlite3"
            ),
            "ZYRA_WORKER_POOL_STORE": str(root / "worker-pool.sqlite3"),
            "ZYRA_GRAPH_STATE_STORE": str(root / "graph-state.sqlite3"),
            "ZYRA_WORKSPACE_STORE": str(root / "workspace.sqlite3"),
            "ZYRA_DISABLE_RECOVERY_RUNTIME": "false",
        }
    )
    api_main._WORKER_POOL_API = None
    api_main._WORKER_POOL_RUNTIME = None
    api_main._WORKER_POOL_KEY = None
    api_main.reset_subagent_runtime()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        api_main.ZyraRequestHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        if api_main._WORKER_POOL_API is not None:
            api_main._WORKER_POOL_API.close()
        api_main._WORKER_POOL_API = None
        api_main._WORKER_POOL_RUNTIME = None
        api_main._WORKER_POOL_KEY = None
        api_main.reset_subagent_runtime()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(
    base_url: str,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    status, body = _post_with_status(base_url, path, payload)
    if status >= 400:
        raise AssertionError(body)
    return body


def _post_with_status(
    base_url: str,
    path: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))
