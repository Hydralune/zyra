from __future__ import annotations

import importlib
import base64
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_handler() -> type[BaseHTTPRequestHandler]:
    module_name = "apps.api.zyra_api.main"
    module = (
        importlib.reload(sys.modules[module_name])
        if module_name in sys.modules
        else importlib.import_module(module_name)
    )
    return module.ZyraRequestHandler


def _configure(root: Path) -> None:
    os.environ.update(
        {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
            "ZYRA_WORKER_POOL_STORE": str(root / "worker-pool.sqlite3"),
            "ZYRA_GRAPH_STATE_STORE": str(root / "graph.sqlite3"),
            "ZYRA_TERMINAL_STATE": str(root / "terminal"),
            "ZYRA_CONTROL_STATE": str(root / "control"),
        }
    )


def _request(
    base_url: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _shutdown() -> None:
    module = sys.modules.get("apps.api.zyra_api.main")
    if module is None:
        return
    for name in (
        "reset_api_product_bootstrap",
        "reset_worker_pool_api",
        "reset_runtime_event_spine_bridge",
        "reset_fault_runtime_api",
        "reset_recovery_runtime_api",
        "reset_runtime_owner_composition",
        "reset_workspace_manager",
    ):
        reset = getattr(module, name, None)
        if callable(reset):
            reset()


def test_real_session_api_is_task_backed_and_fails_closed_on_ambiguity() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _configure(root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _fresh_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        session_id = "session_000000000000001_0123456789abcdefabcd"
        try:
            first_status, first = _request(
                base_url,
                "/tasks",
                payload={"goal": "first task", "auto_run": False, "session_id": session_id},
            )
            assert first_status == 201

            list_status, session_list = _request(base_url, "/sessions?limit=1")
            assert list_status == 200
            assert session_list["schema"] == "zyra.session-list.v1"
            assert session_list["state_owner"] == "task_store_projection"
            assert session_list["sessions"][0]["resume_task_id"] == first["task"]["task_id"]

            second_status, second = _request(
                base_url,
                "/tasks",
                payload={"goal": "second task", "auto_run": False, "session_id": session_id},
            )
            assert second_status == 201
            assert second["task"]["task_id"] != first["task"]["task_id"]

            detail_status, detail = _request(base_url, f"/sessions/{session_id}")
            assert detail_status == 200
            projection = detail["session"]
            assert projection["resolution"] == "ambiguous"
            assert projection["resume_task_id"] is None
            assert set(projection["active_task_ids"]) == {
                first["task"]["task_id"],
                second["task"]["task_id"],
            }

            missing_status, missing = _request(
                base_url,
                "/sessions/session_000000000000099_0123456789abcdefabcd",
            )
            assert missing_status == 404
            assert missing["error"] == "session_not_found"

            cursor_status, cursor_error = _request(base_url, "/sessions?cursor=not-base64")
            assert cursor_status == 400
            assert cursor_error["error"] == "cursor_invalid"

            malformed_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "v": 1,
                        "scope": "session-list",
                        "offset": 0,
                        "status": "",
                        "limit": "not-an-integer",
                    }
                ).encode("utf-8")
            ).decode("ascii").rstrip("=")
            malformed_status, malformed_error = _request(
                base_url,
                f"/sessions?cursor={malformed_cursor}",
            )
            assert malformed_status == 400
            assert malformed_error["error"] == "cursor_invalid"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
            _shutdown()


def test_default_task_session_alias_is_resolvable() -> None:
    from apps.api.zyra_api.session_api import session_detail

    task_id = "task_000000000000001_0123456789abcdefabcd"
    detail = session_detail(
        [
            {
                "task_id": task_id,
                "status": "completed",
                "created_at": "2026-08-04T00:00:00.000Z",
                "updated_at": "2026-08-04T00:00:01.000Z",
                "metadata": {"query_session_id": f"task:{task_id}"},
            }
        ],
        f"session_{task_id}",
    )
    assert detail["session"]["resume_task_id"] == task_id
    assert detail["session"]["terminal"] is True
