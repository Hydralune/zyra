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
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = dict(headers or {})
    if data is not None:
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers=request_headers,
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
                "session_title": "Readable session",
                "metadata": {"query_session_id": f"task:{task_id}"},
            }
        ],
        f"session_{task_id}",
    )
    assert detail["session"]["resume_task_id"] == task_id
    assert detail["session"]["terminal"] is True
    assert detail["session"]["title"] == "Readable session"


def test_conversation_deletion_is_atomic_durable_and_preserves_other_sessions() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _configure(root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _fresh_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        session_id = "session_delete_001"
        try:
            from apps.api.zyra_api.main import get_store
            from zyra_core import PlanNodeStatus
            from zyra_memory import SQLiteStore

            _, first = _request(base_url, "/tasks", payload={"goal": "delete test first", "auto_run": False, "session_id": session_id})
            _, second = _request(base_url, "/tasks", payload={"goal": "delete test second", "auto_run": False, "session_id": session_id})
            _, other = _request(base_url, "/tasks", payload={"goal": "keep test", "auto_run": False})
            ids = {first["task"]["task_id"], second["task"]["task_id"]}
            store = get_store()
            state = store.load_task(first["task"]["task_id"])
            state.status = PlanNodeStatus.COMPLETED
            store.save_checkpoint(state)
            status, rejected = _request(base_url, f"/sessions/{session_id}/delete", payload={})
            assert status == 409 and rejected["error"] == "session_not_terminal"
            assert all(store.load_task(task_id) is not None for task_id in ids)

            state = store.load_task(second["task"]["task_id"])
            state.status = PlanNodeStatus.COMPLETED
            store.save_checkpoint(state)
            status, deleted = _request(base_url, f"/sessions/{session_id}/delete", payload={})
            assert status == 200 and deleted["schema"] == "zyra.session-deletion.v1"
            assert set(deleted["task_ids"]) == ids
            assert _request(base_url, f"/sessions/{session_id}/delete", payload={}) == (200, deleted)
            assert _request(base_url, f"/sessions/{session_id}")[0] == 404
            assert all(_request(base_url, f"/tasks/{task_id}")[0] == 404 for task_id in ids)
            remaining = _request(base_url, "/tasks")[1]["tasks"]
            assert {row["task_id"] for row in remaining} == {other["task"]["task_id"]}
            restarted = SQLiteStore(store.path)
            assert all(restarted.load_task(task_id) is None for task_id in ids)
            try:
                restarted.save_checkpoint(state)
            except ValueError as error:
                assert "deleted" in str(error)
            else:
                raise AssertionError("A stale checkpoint resurrected a deleted conversation")
            assert restarted.task_events(state.task_id)  # Execution audit remains intact.
            assert _request(base_url, "/sessions/session_missing_delete/delete", payload={})[0] == 404
            alias = f"task:{other['task']['task_id']}"
            kept = restarted.load_task(other["task"]["task_id"])
            kept.status = PlanNodeStatus.CANCELLED
            restarted.save_checkpoint(kept)
            alias_status, alias_result = _request(base_url, f"/sessions/{alias}/delete", payload={})
            assert alias_status == 200
            assert alias_result["session_id"] == f"session_{kept.task_id}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
            _shutdown()


def test_user_input_api_answers_the_exact_canonical_request_idempotently() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _configure(root)
        handler = _fresh_handler()
        module = sys.modules["apps.api.zyra_api.main"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            create_status, created = _request(
                base_url,
                "/tasks",
                payload={"goal": "Ask before choosing a database.", "auto_run": False},
            )
            assert create_status == 201
            task = created["task"]
            request_id = "request_000000000000001_0123456789abcdefabcd"
            questions = [
                {
                    "header": "数据库",
                    "id": "database",
                    "question": "这个服务应该使用哪种数据库？",
                    "options": [
                        {"label": "SQLite", "description": "保持单机部署和零配置。"},
                        {"label": "PostgreSQL", "description": "支持共享服务和并发写入。"},
                    ],
                }
            ]
            store = module.SQLiteStore(module.sqlite_path())
            store.create_user_input_request(
                request_id=request_id,
                run_id=task["run_id"],
                task_id=task["task_id"],
                node_id=task["root_node_id"],
                tool_call_id="toolcall_user_input_001",
                request_digest=module.canonical_user_input_digest(questions),
                questions=questions,
                created_at=module.now_iso(),
            )

            pending_status, pending = _request(
                base_url,
                f"/tasks/{task['task_id']}/user-input",
            )
            assert pending_status == 200
            assert pending["schema"] == "zyra.user-input-list/v1"
            assert pending["pending_count"] == 1
            assert pending["requests"][0]["request_id"] == request_id
            assert pending["requests"][0]["questions"] == questions

            answer_payload = {
                "task_id": task["task_id"],
                "run_id": task["run_id"],
                "request_id": request_id,
                "expected_revision": 0,
                "answer_id": "answer_000000000000001_0123456789abcdefabcd",
                "answers": {"database": {"answers": ["PostgreSQL"]}},
                "responder": "integration-test",
            }
            answer_headers = {
                "Idempotency-Key": "answer_000000000000001_0123456789abcdefabcd",
            }
            answer_status, answered = _request(
                base_url,
                f"/tasks/{task['task_id']}/user-input/{request_id}/answer",
                payload=answer_payload,
                headers=answer_headers,
            )
            replay_status, replayed = _request(
                base_url,
                f"/tasks/{task['task_id']}/user-input/{request_id}/answer",
                payload=answer_payload,
                headers=answer_headers,
            )
            assert answer_status == replay_status == 200, (
                answer_status,
                answered,
                replay_status,
                replayed,
            )
            assert answered == replayed
            assert answered["request"]["status"] == "answered"
            assert answered["request"]["revision"] == 1
            assert answered["request"]["answers"] == {
                "database": {"answers": ["PostgreSQL"]}
            }

            history_status, history = _request(
                base_url,
                f"/tasks/{task['task_id']}/user-input?include_terminal=true",
            )
            assert history_status == 200
            assert history["pending_count"] == 0
            assert history["requests"][0]["answer_id"] == answer_payload["answer_id"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
            _shutdown()
