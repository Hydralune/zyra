from __future__ import annotations

import json
import os
import threading
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

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
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))
