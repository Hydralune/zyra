from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for package_path in sorted((ROOT / "packages").iterdir()):
    if package_path.is_dir() and str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType  # noqa: E402
from zyra_runtime import WorkerResult  # noqa: E402
from zyra_workers.browser_worker import BrowserWorkerRun  # noqa: E402


@dataclass
class _Server:
    base_url: str
    server: ThreadingHTTPServer
    thread: threading.Thread


class _ObservabilityCommitter:
    def acknowledge_events(
        self,
        projection: dict[str, Any],
        *,
        committed_event_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            **projection,
            "committed_event_ids": list(committed_event_ids),
            "observation_commit": {
                "phase": "events_committed",
                "event_count": len(committed_event_ids),
            },
        }

    def acknowledge_checkpoint(
        self,
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            **projection,
            "observation_commit": {
                "phase": "checkpoint_committed",
            },
        }


class _BrowserWorkerSpy:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.browser_observability_application = _ObservabilityCommitter()

    def run(
        self,
        request: Any,
        *,
        browser_context_checkpoint: dict[str, Any] | None,
        skill_memory_context_checkpoint: dict[str, Any] | None,
    ) -> BrowserWorkerRun:
        self.requests.append(request)
        action = request.constraints["browser_plan"][0]
        event = EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "mutation_id": "mutation_browser_viewer_control",
                "browser_action": {
                    "action_id": "action_browser_viewer_control",
                    "tool_call_id": "tool_browser_viewer_control",
                    "action": action["action"],
                    "arguments": dict(action["arguments"]),
                    "browser_viewer_control_id": request.constraints[
                        "browser_viewer_control_id"
                    ],
                },
                "browser_result": {
                    "action_id": "action_browser_viewer_control",
                    "tool_call_id": "tool_browser_viewer_control",
                    "ok": True,
                    "status": "completed",
                },
            },
        )
        return BrowserWorkerRun(
            worker_result=WorkerResult(
                request_id=request.request_id,
                ok=True,
                summary="BrowserWorker applied viewer navigate control.",
                metadata={
                    "browser_session_id": request.constraints["session_id"],
                    "browser_action_execution_count": "1",
                    "browser_action_default_gateway": "true",
                },
            ),
            event_records=[event],
            browser_context_checkpoint={},
            browser_context_projection={
                "schema": "zyra.browser-context.v2",
                "state_owner": "M1-02D.ClaudeContextWindowManager",
            },
            browser_observability_projection={
                "schema": "zyra.browser-observability.v1",
                "view": "summary",
                "scope_count": 1,
                "scopes": [],
            },
            skill_memory_context_checkpoint={},
            skill_memory_context_projection={},
        )


def _request(
    base_url: str,
    path: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return (
                response.status,
                json.loads(response.read().decode("utf-8")),
                dict(response.headers.items()),
            )
    except HTTPError as error:
        return (
            error.code,
            json.loads(error.read().decode("utf-8")),
            dict(error.headers.items()),
        )


def _post(
    server: _Server,
    path: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    return _request(
        server.base_url,
        path,
        method="POST",
        payload=payload,
        headers=headers,
    )


def _get(
    server: _Server,
    path: str,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    return _request(server.base_url, path, method="GET")


@contextmanager
def _api_server(root: Path) -> Iterator[_Server]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    environment = {
        "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
        "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
        "ZYRA_TOOL_WORKSPACE": str(workspace),
        "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
        "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
        "ZYRA_BROWSER_STATE": str(root / "browser-state"),
        "ZYRA_BROWSER_RUNTIME_ROOT": str(root / "browser-runtime"),
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    from apps.api.zyra_api import main as api_main

    # The API owns process-wide workspace/worker projections.  This fixture
    # changes their configured roots, so both entry and exit must fence them;
    # otherwise a later test can recover a task from one SQLite store while a
    # stale WorkspaceBindingStore still points at an already-deleted temp root.
    api_main.reset_workspace_manager()
    from apps.api.zyra_api.main import ZyraRequestHandler

    server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Server(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            server=server,
            thread=thread,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        api_main.reset_workspace_manager()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _create_task(server: _Server, goal: str) -> dict[str, Any]:
    status, body, _headers = _post(
        server,
        "/tasks",
        {"goal": goal, "auto_run": False},
    )
    assert status == 201, body
    return body["task"]


def _viewer_control(
    *,
    action: str,
    command_id: str,
    request_id: str,
    session_id: str,
    sealed: bool,
    url: str = "",
    retry_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "zyra.browser-viewer.control.v1",
        "action": action,
        "command_id": command_id,
        "request_id": request_id,
        "actor_id": "zyra-browser-viewer-test",
        "reason": f"Exercise BrowserWorker {action} through the viewer.",
        "browser_session_id": session_id,
        "worker_request_id": "browser_worker_request_prior",
        "action_id": "browser_action_prior",
        "url": url,
        "retry_action": retry_action,
        "expected_generation": 3,
        "expected_task_revision": 7,
        "sealed": sealed,
    }


def test_sealed_viewer_control_records_one_attempt_and_never_calls_worker() -> None:
    with tempfile.TemporaryDirectory() as directory:
        with _api_server(Path(directory)) as server:
            task = _create_task(
                server,
                "Verify sealed browser viewer controls fail closed.",
            )
            task_id = task["task_id"]
            command = _viewer_control(
                action="navigate",
                command_id="browser_control_sealed_once",
                request_id="browser_control_request_sealed_once",
                session_id="browser_session_sealed",
                sealed=True,
                url="https://safe.example.test/sealed",
            )
            from apps.api.zyra_api import main as api_main

            with patch.object(
                api_main,
                "get_browser_runtime_services",
                side_effect=AssertionError(
                    "sealed viewer control must not reach BrowserWorker.run"
                ),
            ):
                first_status, first, first_headers = _post(
                    server,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "sealed": True,
                        "competition_mode": "sealed_autonomous",
                        "browser_viewer_control": command,
                    },
                    headers={"Idempotency-Key": "sealed-browser-viewer-once"},
                )
                replay_status, replay, replay_headers = _post(
                    server,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "sealed": True,
                        "competition_mode": "sealed_autonomous",
                        "browser_viewer_control": command,
                    },
                    headers={"Idempotency-Key": "sealed-browser-viewer-once"},
                )

            assert first_status == 403, first
            receipt = first["browser_control_receipt"]
            assert receipt["status"] == "denied"
            assert receipt["sealed"] is True
            assert receipt["intervention_counted"] is True
            assert receipt["manual_mutation_applied"] is False
            assert receipt["approval_wait_entered"] is False
            assert receipt["human_intervention_count"] == 0
            assert receipt["automatic_recovery_action"] == "fail_closed"
            assert len(receipt["event_ids"]) == 1
            assert first["operator_intervention_attempt_count"] == 1
            assert first["human_intervention_count"] == 0
            assert (
                first_headers["X-Zyra-Browser-Control-State-Owner"]
                == "BrowserWorkerRuntime"
            )

            assert replay_status == 403, replay
            assert replay["browser_control_receipt"]["replayed"] is True
            assert replay["receipt_replayed"] is True
            assert replay["human_intervention_count"] == 0
            assert replay_headers["X-Zyra-Receipt-Replayed"] == "true"

            get_status, task_projection, _headers = _get(
                server,
                f"/tasks/{task_id}",
            )
            assert get_status == 200, task_projection
            metadata = task_projection["task"]["metadata"]
            assert metadata["operator_intervention_attempt_count"] == 1
            assert int(metadata.get("human_intervention_count") or 0) == 0
            denial = metadata["last_sealed_browser_viewer_control_denial"]
            assert denial["command_id"] == command["command_id"]
            assert denial["manual_mutation_applied"] is False
            assert denial["approval_wait_entered"] is False

            conflict = dict(command)
            conflict["url"] = "https://safe.example.test/conflicting-body"
            conflict_status, conflict_body, _headers = _post(
                server,
                f"/tasks/{task_id}/workers/browser",
                {
                    "sealed": True,
                    "browser_viewer_control": conflict,
                },
            )
            assert conflict_status == 409, conflict_body
            assert (
                conflict_body["error"]
                == "browser_viewer_control_idempotency_conflict"
            )


def test_interactive_navigate_delegates_exactly_once_to_browserworker() -> None:
    with tempfile.TemporaryDirectory() as directory:
        with _api_server(Path(directory)) as server:
            task = _create_task(
                server,
                "Apply a typed browser viewer control through BrowserWorker.",
            )
            task_id = task["task_id"]
            session_id = "browser_session_interactive"
            worker = _BrowserWorkerSpy()
            command = _viewer_control(
                action="navigate",
                command_id="browser_control_interactive_navigate",
                request_id="browser_control_request_interactive_navigate",
                session_id=session_id,
                sealed=False,
                url="https://safe.example.test/interactive",
            )
            from apps.api.zyra_api import main as api_main

            with patch.object(
                api_main,
                "get_browser_runtime_services",
                return_value=(object(), worker),
            ):
                status, response, headers = _post(
                    server,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "browser_viewer_control": command,
                    },
                    headers={
                        "Idempotency-Key": "interactive-browser-viewer-navigate"
                    },
                )

            assert status == 201, response
            assert len(worker.requests) == 1
            request = worker.requests[0]
            assert request.worker_name == "BrowserWorker"
            assert request.task_id == task_id
            assert request.run_id == task["run_id"]
            assert request.constraints["session_id"] == session_id
            assert request.constraints["canonical_session_id"] == session_id
            assert (
                request.constraints["browser_viewer_control_id"]
                == command["command_id"]
            )
            assert request.constraints["browser_viewer_control_action"] == "navigate"
            assert request.constraints["expected_generation"] == 3
            assert request.constraints["browser_plan"] == [
                {
                    "action": "open_url",
                    "arguments": {
                        "url": "https://safe.example.test/interactive",
                    },
                }
            ]
            receipt = response["browser_control_receipt"]
            assert receipt["status"] == "applied"
            assert receipt["sealed"] is False
            assert receipt["manual_mutation_applied"] is True
            assert receipt["approval_wait_entered"] is False
            assert receipt["worker_request_id"] == request.request_id
            assert receipt["event_ids"] == [
                response["events"][0]["event_id"],
            ]
            assert receipt["mutation_ids"] == [
                "mutation_browser_viewer_control",
            ]
            assert (
                response["browser_observability"]["observation_commit"]["phase"]
                == "checkpoint_committed"
            )
            assert (
                headers["X-Zyra-Browser-Control-State-Owner"]
                == "BrowserWorkerRuntime"
            )

            with patch.object(
                api_main,
                "get_browser_runtime_services",
                side_effect=AssertionError(
                    "idempotent replay must not execute BrowserWorker twice"
                ),
            ):
                replay_status, replay, _headers = _post(
                    server,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "browser_viewer_control": command,
                    },
                    headers={
                        "Idempotency-Key": "interactive-browser-viewer-navigate"
                    },
                )
            assert replay_status == 200, replay
            assert replay["idempotent_replay"] is True
            assert replay["browser_control_receipt"]["replayed"] is True
            assert len(worker.requests) == 1


def test_control_translation_is_bounded_and_never_creates_a_second_owner() -> None:
    from apps.api.zyra_api import main as api_main

    class _State:
        task_id = "task_translation"
        run_id = "run_translation"
        metadata: dict[str, Any] = {}

    state = _State()
    retry = _viewer_control(
        action="retry",
        command_id="browser_control_retry_translation",
        request_id="browser_control_request_retry_translation",
        session_id="browser_session_translation",
        sealed=False,
        retry_action={
            "action": "click",
            "arguments": {"selector": "#retry"},
            "retry_of_action_id": "browser_action_failed",
            "expected_argument_digest": "fnv128:1234",
        },
    )
    admitted = api_main._browser_viewer_control_request(  # type: ignore[attr-defined]
        state,
        {"browser_viewer_control": retry},
    )
    assert admitted is not None
    constraints = api_main._browser_viewer_execution_constraints(  # type: ignore[attr-defined]
        admitted
    )
    assert constraints["browser_plan"] == [
        {
            "action": "click",
            "arguments": {"selector": "#retry"},
            "metadata": {
                "retry_of_action_id": "browser_action_failed",
                "expected_argument_digest": "fnv128:1234",
                "bounded_retry": True,
            },
        }
    ]
    assert constraints["session_id"] == "browser_session_translation"
    assert constraints["canonical_session_id"] == "browser_session_translation"
    assert "state_owner" not in constraints
    assert "browser_store" not in constraints

    stop = _viewer_control(
        action="stop",
        command_id="browser_control_stop_translation",
        request_id="browser_control_request_stop_translation",
        session_id="browser_session_translation",
        sealed=False,
    )
    stop_admitted = api_main._browser_viewer_control_request(  # type: ignore[attr-defined]
        state,
        {"browser_viewer_control": stop},
    )
    assert stop_admitted is not None
    stop_constraints = api_main._browser_viewer_execution_constraints(  # type: ignore[attr-defined]
        stop_admitted
    )
    assert stop_constraints["browser_lifecycle_command"] == "stop"
    assert "browser_plan" not in stop_constraints

    inspect = _viewer_control(
        action="inspect",
        command_id="browser_control_inspect_translation",
        request_id="browser_control_request_inspect_translation",
        session_id="browser_session_translation",
        sealed=False,
    )
    inspect_admitted = api_main._browser_viewer_control_request(  # type: ignore[attr-defined]
        state,
        {"browser_viewer_control": inspect},
    )
    assert inspect_admitted is not None
    inspect_constraints = api_main._browser_viewer_execution_constraints(  # type: ignore[attr-defined]
        inspect_admitted
    )
    assert inspect_constraints["browser_lifecycle_command"] == "diagnose"
    assert "browser_plan" not in inspect_constraints
