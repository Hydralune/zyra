from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for package_path in sorted((ROOT / "packages").iterdir()):
    if package_path.is_dir() and str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.browser_use import BrowserEventBus  # noqa: E402
from zyra_workers.browser_session import CdpRequestRuntime, MemoryCdpTransport  # noqa: E402
from tests.integration.test_browser_session_productization_integration import _CdpResponder  # noqa: E402


class _CdpHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _write(self, value: object) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/json/version":
            self._write(
                {
                    "Browser": "ZyraApiFixture/1",
                    "Protocol-Version": "1.3",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9/devtools/browser/api",
                }
            )
            return
        if self.path == "/json/list":
            self._write([{"id": "api-page", "type": "page", "url": "about:blank", "title": "API"}])
            return
        if self.path.startswith("/json/activate/"):
            self._write("Target activated")
            return
        self.send_error(404)


class BrowserSessionProductizationApiTests(unittest.TestCase):
    def test_api_start_diagnose_list_and_stop_share_the_registry_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cdp = ThreadingHTTPServer(("127.0.0.1", 0), _CdpHandler)
            cdp.daemon_threads = True
            cdp_thread = threading.Thread(target=cdp.serve_forever, daemon=True)
            cdp_thread.start()
            endpoint = f"http://127.0.0.1:{cdp.server_address[1]}"
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
            from apps.api.zyra_api.main import ZyraRequestHandler

            api = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            api.daemon_threads = True
            api_thread = threading.Thread(target=api.serve_forever, daemon=True)
            api_thread.start()
            base_url = f"http://127.0.0.1:{api.server_address[1]}"
            event_bus: BrowserEventBus | None = None
            try:
                from apps.api.zyra_api import main as api_main

                with patch.object(
                    api_main._BROWSER_RUNTIME_REGISTRY,
                    "diagnostics",
                    return_value={
                        "ok": False,
                        "blocking_findings": [],
                        "integration_audit": {
                            "ok": False,
                            "ready": False,
                            "blocking_check_ids": [],
                        },
                    },
                ):
                    unhealthy = _get(base_url, "/workers/browser/health")
                self.assertFalse(unhealthy["ok"])
                self.assertFalse(unhealthy["diagnostics"]["ok"])
                self.assertFalse(unhealthy["diagnostics"]["integration_audit"]["ok"])

                created = _post(base_url, "/tasks", {"goal": "Exercise the productized browser session.", "auto_run": False})
                task_id = created["task"]["task_id"]
                canonical_session_id = f"browser-api:{task_id}"
                status, started = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "constraints": {
                            "browser_backend": "zyra-browser-productized",
                            "browser_lifecycle_command": "start",
                            "browser_endpoint_url": endpoint,
                            "browser_transport": "memory",
                            "canonical_session_id": canonical_session_id,
                            "keep_alive": True,
                            "permission_mode": "sealed",
                            "browser_transport": "memory",
                        }
                    },
                )
                self.assertEqual(status, 201, started)
                metadata = started["worker_result"]["metadata"]
                session_id = metadata["browser_session_id"]
                self.assertEqual(metadata["browser_canonical_session_id"], canonical_session_id)
                self.assertEqual(metadata["browser_session_status"], "running")
                self.assertGreaterEqual(int(metadata["browser_session_revision"]), 1)
                self.assertTrue(any("browser_session" in event.get("payload", {}) for event in started["events"]))
                browser_runtime = api_main.get_browser_runtime()
                browser_runtime._runtime._cdp[session_id].close()  # type: ignore[attr-defined]
                event_bus = BrowserEventBus()
                event_bus.start()
                memory_cdp = CdpRequestRuntime(
                    session_id,
                    0.2,
                    event_bus,
                    transport_factory=lambda: MemoryCdpTransport(_CdpResponder()),
                )
                memory_cdp.connect()
                browser_runtime._runtime._cdp[session_id] = memory_cdp  # type: ignore[attr-defined]
                action_status, action = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "browser_plan": [
                            {"action": "list_targets", "arguments": {}},
                            {"action": "capture_trace", "arguments": {}},
                        ],
                        "constraints": {
                            "browser_endpoint_url": endpoint,
                            "browser_transport": "memory",
                            "canonical_session_id": canonical_session_id,
                            "keep_alive": True,
                            "permission_mode": "sealed",
                        },
                    },
                )
                self.assertEqual(action_status, 201, action)
                self.assertEqual(action["worker_result"]["metadata"]["browser_session_id"], session_id)
                self.assertEqual(action["worker_result"]["metadata"]["browser_action_default_gateway"], "true")
                self.assertEqual(action["worker_result"]["metadata"]["browser_action_execution_count"], "2")
                self.assertTrue(any("browser_action" in event.get("payload", {}) for event in action["events"]))
                self.assertGreaterEqual(action["task"]["budget"]["tool_calls"], 2)
                custody_token = action["permission_session"]["custody_token"]
                self.assertTrue(custody_token)

                sessions = _get(base_url, "/workers/browser/sessions")
                self.assertTrue(any(item["session_id"] == session_id for item in sessions["sessions"]))
                detail = _get(base_url, f"/workers/browser/sessions/{session_id}")
                self.assertEqual(detail["session"]["session_id"], session_id)

                diagnose_status, diagnosed = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "constraints": {
                            "browser_lifecycle_command": "diagnose",
                            "browser_session_id": session_id,
                            "canonical_session_id": canonical_session_id,
                        }
                    },
                )
                self.assertEqual(diagnose_status, 201, diagnosed)
                self.assertEqual(diagnosed["worker_result"]["metadata"]["browser_session_id"], session_id)

                resume_status, resumed = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/workers/browser",
                    {"constraints": {
                        "browser_lifecycle_command": "resume",
                        "browser_session_id": session_id,
                        "canonical_session_id": canonical_session_id,
                    }},
                )
                self.assertEqual(resume_status, 201, resumed)
                self.assertEqual(resumed["worker_result"]["metadata"]["browser_session_id"], session_id)

                pending_status, pending = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "browser_plan": [
                            {"action": "evaluate_js", "arguments": {"code": "document.title"}},
                        ],
                        "constraints": {
                            "browser_endpoint_url": endpoint,
                            "browser_transport": "memory",
                            "canonical_session_id": canonical_session_id,
                            "keep_alive": True,
                            "permission_mode": "default",
                            "permission_session_custody_token": custody_token,
                        },
                    },
                )
                self.assertEqual(pending_status, 202, pending)
                self.assertFalse(pending["worker_result"]["ok"])
                self.assertEqual(pending["worker_result"]["error"], "browser_action_permission_pending")
                self.assertTrue(pending["browser_action_continuation"]["pending"])
                self.assertTrue(pending["browser_action_continuation"]["checkpoint_id"])
                self.assertTrue(pending["browser_action_continuation"]["permission_request_id"])
                self.assertEqual(
                    pending["worker_result"]["metadata"]["browser_action_side_effect_count"],
                    "0",
                )
                self.assertEqual(
                    pending["worker_result"]["metadata"]["browser_session_status"],
                    "running",
                )

                stop_status, stopped = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "constraints": {
                            "browser_lifecycle_command": "cancel",
                            "browser_session_id": session_id,
                            "canonical_session_id": canonical_session_id,
                        }
                    },
                )
                self.assertEqual(stop_status, 201, stopped)
                self.assertEqual(stopped["worker_result"]["metadata"]["browser_lifecycle_command"], "cancel")
                stopped_detail = _get(base_url, f"/workers/browser/sessions/{session_id}")
                self.assertEqual(stopped_detail["session"]["status"], "stopped")
            finally:
                if event_bus is not None:
                    event_bus.stop()
                api.shutdown()
                api.server_close()
                api_thread.join(timeout=3)
                cdp.shutdown()
                cdp.server_close()
                cdp_thread.join(timeout=3)
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_invalid_cdp_endpoint_fails_closed_and_remains_diagnosable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
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
            from apps.api.zyra_api.main import ZyraRequestHandler

            api = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            api.daemon_threads = True
            thread = threading.Thread(target=api.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{api.server_address[1]}"
            try:
                task = _post(base_url, "/tasks", {"goal": "Reject an invalid browser endpoint.", "auto_run": False})["task"]
                status, response = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/browser",
                    {
                        "constraints": {
                            "browser_backend": "zyra-browser-productized",
                            "browser_lifecycle_command": "start",
                            "browser_endpoint_url": "ftp://invalid.example/devtools",
                            "browser_transport": "memory",
                            "canonical_session_id": f"browser-invalid:{task['task_id']}",
                        }
                    },
                )
                self.assertEqual(status, 409, response)
                self.assertFalse(response["worker_result"]["ok"])
                self.assertNotIn("static", json.dumps(response["worker_result"]).lower())
                self.assertTrue(any("browser_session" in event.get("payload", {}) for event in response["events"]))
            finally:
                api.shutdown()
                api.server_close()
                thread.join(timeout=3)
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


def _request(base_url: str, path: str, *, method: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _get(base_url: str, path: str) -> dict[str, Any]:
    status, payload = _request(base_url, path, method="GET")
    if status >= 400:
        raise AssertionError(f"GET {path} failed with {status}: {payload}")
    return payload


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    status, response = _post_with_status(base_url, path, payload)
    if status >= 400:
        raise AssertionError(f"POST {path} failed with {status}: {response}")
    return response


def _post_with_status(base_url: str, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return _request(base_url, path, method="POST", payload=payload)


if __name__ == "__main__":
    unittest.main()
