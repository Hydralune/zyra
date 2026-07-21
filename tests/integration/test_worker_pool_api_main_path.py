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


def test_task_api_uses_physical_lease_dynamic_graph_projection_and_real_cancel(tmp_path: Path) -> None:
    with _api(tmp_path) as base_url:
        created = _post(
            base_url,
            "/tasks",
            {"goal": "Exercise the 07A worker pool main path.", "auto_run": False},
        )
        task = created["task"]
        task_id = task["task_id"]
        physical = task["metadata"]["worker_pool"]
        assert physical["attempt_number"] == 1
        assert physical["worker_id"] == "local-code-worker"
        assert physical["graph_ref"]["revision"] >= 2

        pool = _get(base_url, "/worker-pool")
        leases = _get(base_url, f"/worker-pool/leases?task_id={task_id}")
        graph = _get(base_url, f"/worker-pool/graphs/graph:{task_id}")
        assert pool["custody"]["physical_attempt"] == "WorkerLeaseManager"
        assert any(item["lease_id"] == physical["lease_id"] for item in leases["leases"])
        execute_nodes = [
            item
            for item in graph["snapshot"]["nodes"]
            if item["metadata"].get("stage") == "execute"
        ]
        assert execute_nodes[0]["worker_lease_ref"] == physical["lease_id"]

        cancelled = _post(
            base_url,
            f"/tasks/{task_id}/cancel",
            {"reason": "verify physical cancellation"},
        )
        assert physical["lease_id"] in cancelled["worker_pool_control"]["cancelled_lease_ids"]
        after = _get(base_url, f"/worker-pool/leases?task_id={task_id}")
        selected = next(item for item in after["leases"] if item["lease_id"] == physical["lease_id"])
        assert selected["state"] == "cancelled"


def test_subagent_api_admits_through_typescript_omp_gate_before_child_execution(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        created = _post(
            base_url,
            "/tasks",
            {"goal": "Run one E03 child under the 07A physical worker pool.", "auto_run": False},
        )
        task_id = created["task"]["task_id"]
        payload = {
            "prompt": "Return a deterministic completion without tools.",
            "execution_mode": "foreground",
            "subagent_task_id": "physical-e03-child",
            "idempotency_key": "physical-e03-child-create",
            "request_id": "physical-e03-child-request",
        }
        first_status, suspended = _post_with_status(
            base_url,
            f"/tasks/{task_id}/subagents",
            payload,
        )
        assert first_status == 409
        assert suspended["error"] == "permission_suspended"
        assert suspended["physical_receipt"] is None
        spawned = _approve_and_retry_subagent(
            base_url,
            parent=created["task"],
            payload=payload,
            suspended=suspended,
        )
        assert spawned["canonical_agent_owner"] == "typescript"
        assert spawned["record"]["status"] == "completed"
        physical = spawned["physical_worker"]
        receipt = spawned["physical_receipt"]
        assert physical["typescript_dispatch_gate"] == "typescript.OmpWorkerDispatchRuntime"
        assert physical["logical_task_not_duplicated"] is True
        assert receipt["outcome"] == "succeeded"
        assert receipt["metadata"]["dispatch_gate"] == "typescript.OmpWorkerDispatchRuntime"

        leases = _get(base_url, "/worker-pool/leases?task_id=physical-e03-child")["leases"]
        selected = next(item for item in leases if item["lease_id"] == physical["lease_id"])
        assert selected["state"] == "released"
        journal = _get(base_url, "/worker-pool/journal?limit=500")["records"]
        operations = [
            item["operation"]
            for item in journal
            if item.get("task_id") == "physical-e03-child"
        ]
        assert operations.index("attempt_started") < operations.index("execution_receipt_committed")


def test_subagent_api_fails_closed_and_records_failed_receipt_when_omp_gate_is_disabled(
    tmp_path: Path,
) -> None:
    previous = os.environ.get("ZYRA_OMP_WORKER_CONTROL_DISABLED")
    os.environ["ZYRA_OMP_WORKER_CONTROL_DISABLED"] = "1"
    try:
        with _api(tmp_path) as base_url:
            created = _post(
                base_url,
                "/tasks",
                {"goal": "Prove the OMP gate cannot be bypassed.", "auto_run": False},
            )
            task_id = created["task"]["task_id"]
            payload = {
                "prompt": "This child must not execute.",
                "execution_mode": "foreground",
                "subagent_task_id": "disabled-omp-child",
                "idempotency_key": "disabled-omp-child-create",
                "request_id": "disabled-omp-child-request",
            }
            first_status, suspended = _post_with_status(
                base_url,
                f"/tasks/{task_id}/subagents",
                payload,
            )
            assert first_status == 409
            spawned = _approve_and_retry_subagent(
                base_url,
                parent=created["task"],
                payload=payload,
                suspended=suspended,
            )
            assert spawned["record"]["status"] == "failed"
            assert "omp_worker_control_disabled" in str(spawned["record"])
            assert spawned["physical_receipt"]["outcome"] == "failed"
            assert spawned["physical_receipt"]["metadata"]["dispatch_gate"] == (
                "typescript.OmpWorkerDispatchRuntime"
            )
    finally:
        if previous is None:
            os.environ.pop("ZYRA_OMP_WORKER_CONTROL_DISABLED", None)
        else:
            os.environ["ZYRA_OMP_WORKER_CONTROL_DISABLED"] = previous


def test_subagent_fanout_maps_each_child_to_one_physical_attempt_and_receipt(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as base_url:
        created = _post(
            base_url,
            "/tasks",
            {"goal": "Run bounded OMP fanout under canonical physical leases.", "auto_run": False},
        )
        payload = {
            "shared_context": "Return deterministic no-tool completions.",
            "items": [
                {"task_id": "physical-fanout-a", "prompt": "child A", "background": False},
                {"task_id": "physical-fanout-b", "prompt": "child B", "background": False},
            ],
            "failure_policy": "collect",
            "maximum_concurrency": 2,
            "idempotency_key": "physical-fanout-create",
            "request_id": "physical-fanout-request",
        }
        first_status, suspended = _post_with_status(
            base_url,
            f"/tasks/{created['task']['task_id']}/subagents/fanout",
            payload,
        )
        assert first_status == 409
        assert suspended["error"] == "permission_suspended"
        assert suspended["physical_receipts"] == [None, None]

        completed = _approve_and_retry_subagent(
            base_url,
            parent=created["task"],
            payload=payload,
            suspended=suspended,
            route="subagents/fanout",
        )
        assert [item["task_id"] for item in completed["physical_workers"]] == [
            "physical-fanout-a",
            "physical-fanout-b",
        ]
        assert [item["outcome"] for item in completed["physical_receipts"]] == [
            "succeeded",
            "succeeded",
        ]
        for task_id in ("physical-fanout-a", "physical-fanout-b"):
            leases = _get(base_url, f"/worker-pool/leases?task_id={task_id}")["leases"]
            assert len(leases) == 1
            assert leases[0]["state"] == "released"


@contextmanager
def _api(root: Path) -> Iterator[str]:
    previous = {
        name: os.environ.get(name)
        for name in (
            "ZYRA_SQLITE_PATH",
            "ZYRA_EVENT_LOG",
            "ZYRA_TOOL_WORKSPACE",
            "ZYRA_ARTIFACT_ROOT",
            "ZYRA_WORKER_POOL_STORE",
            "ZYRA_GRAPH_STATE_STORE",
            "ZYRA_WORKSPACE_STORE",
        )
    }
    os.environ.update(
        {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(root / "tool-workspace"),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_WORKER_POOL_STORE": str(root / "worker-pool.sqlite3"),
            "ZYRA_GRAPH_STATE_STORE": str(root / "graph-state.sqlite3"),
            "ZYRA_WORKSPACE_STORE": str(root / "workspace.sqlite3"),
        }
    )
    api_main._WORKER_POOL_API = None
    api_main._WORKER_POOL_RUNTIME = None
    api_main._WORKER_POOL_KEY = None
    api_main.reset_subagent_runtime()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_main.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
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
    with urllib.request.urlopen(f"{base_url}{path}", timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_with_status(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _get_with_headers(
    base_url: str,
    path: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url}{path}", headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _approve_and_retry_subagent(
    base_url: str,
    *,
    parent: dict[str, Any],
    payload: dict[str, Any],
    suspended: dict[str, Any],
    expected_status: int = 201,
    route: str = "subagents",
) -> dict[str, Any]:
    session = suspended["permission_session"]
    token = session["session_custody_token"]
    session_id = session["session_id"]
    headers = {"Authorization": f"Bearer {token}"}
    pending = _get_with_headers(
        base_url,
        (
            "/permissions/requests"
            f"?session_id={session_id}&run_id={parent['run_id']}"
            f"&task_id={parent['task_id']}&pending_only=true"
        ),
        headers,
    )["requests"]["items"]
    assert len(pending) == 1
    status, resolved = _post_with_status(
        base_url,
        f"/permissions/requests/{pending[0]['request_id']}/resolve",
        {
            "session_id": session_id,
            "run_id": parent["run_id"],
            "task_id": parent["task_id"],
            "effect": "allow",
            "idempotency_key": (
                f"approve-{payload.get('subagent_task_id') or payload.get('request_id') or 'agent'}"
            ),
        },
        headers=headers,
    )
    assert status == 200, resolved
    retry_status, retry = _post_with_status(
        base_url,
        f"/tasks/{parent['task_id']}/{route}",
        {**payload, "session_id": session_id},
        headers=headers,
    )
    metadata = retry.get("worker_result", {}).get("metadata", {})
    diagnostic = {
        "error": retry.get("error"),
        "typescript_runtime_error": metadata.get("typescript_runtime_error"),
        "typescript_runtime_error_message": metadata.get("typescript_runtime_error_message"),
        "record_status": (retry.get("record") or {}).get("status"),
        "record_error": (retry.get("record") or {}).get("error"),
        "worker_summary": retry.get("worker_result", {}).get("summary"),
    }
    assert retry_status == expected_status, json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
    return retry
