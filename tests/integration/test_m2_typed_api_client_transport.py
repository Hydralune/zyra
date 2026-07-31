from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_api_handler() -> type[BaseHTTPRequestHandler]:
    module_name = "apps.api.zyra_api.main"
    if module_name in sys.modules:
        module = importlib.reload(sys.modules[module_name])
    else:
        module = importlib.import_module(module_name)
    return module.ZyraRequestHandler


def _shutdown_api_product_bootstrap() -> None:
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
    ):
        reset = getattr(module, name, None)
        if callable(reset):
            reset()


def _configure_runtime_environment(root: Path, *, auth_token: str) -> None:
    workspace = root / "workspace"
    paths = {
        "ZYRA_SQLITE_PATH": root / "api.sqlite3",
        "ZYRA_EVENT_LOG": root / "events.jsonl",
        "ZYRA_TOOL_WORKSPACE": workspace,
        "ZYRA_ARTIFACT_ROOT": root / "artifacts",
        "ZYRA_PERMISSION_STATE": root / "permission-state.json",
        "ZYRA_WORKER_POOL_STORE": root / "worker-pool.sqlite3",
        "ZYRA_GRAPH_STATE_STORE": root / "graph.sqlite3",
        "ZYRA_FAULT_RUNTIME_STORE": root / "fault.sqlite3",
        "ZYRA_RECOVERY_RUNTIME_STORE": root / "recovery.sqlite3",
        "ZYRA_MCP_STATE": root / "mcp",
        "ZYRA_TERMINAL_STATE": root / "terminal",
        "ZYRA_CONTROL_STATE": root / "control",
    }
    os.environ.update({name: str(path) for name, path in paths.items()})
    os.environ["ZYRA_API_AUTH_TOKEN"] = auth_token


def _request(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        headers=dict(headers or {}),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return (
                response.status,
                json.loads(response.read().decode("utf-8")),
                dict(response.headers.items()),
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            json.loads(error.read().decode("utf-8")),
            dict(error.headers.items()),
        )


class M2TypedApiClientTransportTests(unittest.TestCase):
    def test_runtime_readiness_payload_fails_closed_for_an_owner_failure(self) -> None:
        module = importlib.import_module("apps.api.zyra_api.typed_transport")
        body = module.runtime_readiness_payload(
            {
                "task_store": True,
                "event_log": False,
                "checkpoint_store": True,
                "artifact_store": True,
                "control_runtime": True,
                "typed_transport": True,
            },
            details={"event_log": {"available": False, "status": 503}},
        )
        self.assertFalse(body["ready"])
        self.assertEqual(body["status"], "blocked")
        self.assertFalse(body["owners"]["event_log"])
        self.assertIn("event_log", body["blockers"])
        self.assertEqual(body["details"]["event_log"]["status"], 503)

        omitted = module.runtime_readiness_payload()
        self.assertFalse(omitted["ready"])
        self.assertEqual(
            omitted["blockers"],
            [
                "task_store",
                "event_log",
                "checkpoint_store",
                "artifact_store",
                "control_runtime",
                "typed_transport",
            ],
        )

    def test_embedded_real_store_lifecycle_idempotency_and_disable_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _configure_runtime_environment(
                root,
                auth_token="embedded-transport-secret",
            )
            sqlite_path = root / "api.sqlite3"

            handler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                command = [
                    str(ROOT / "node_modules" / ".bin" / "bun.exe"),
                    str(ROOT / "apps" / "web" / "test" / "embedded-client-probe.ts"),
                    base_url,
                    "embedded-transport-secret",
                ]
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
                )
                result = json.loads(completed.stdout.strip().splitlines()[-1])

                self.assertEqual(result["health"]["status"], "ok")
                self.assertEqual(result["health"]["apiVersion"], "1.0")
                self.assertTrue(
                    result["readiness"]["ready"],
                    msg=json.dumps(result["readiness"], sort_keys=True),
                )
                self.assertTrue(result["readiness"]["owners"]["typed_transport"])
                self.assertTrue(result["create"]["replayed"])
                self.assertTrue(result["create"]["pageContainsTask"])
                self.assertEqual(result["resume"]["status"], "completed")
                self.assertTrue(result["resume"]["replayed"])
                self.assertEqual(result["cancel"]["status"], "cancelled")
                self.assertTrue(result["cancel"]["replayed"])
                self.assertEqual(result["disable"]["error"], "transport_disabled")

                connection = sqlite3.connect(sqlite_path)
                try:
                    task_count = connection.execute(
                        "SELECT COUNT(*) FROM tasks"
                    ).fetchone()[0]
                    receipts = connection.execute(
                        """
                        SELECT operation, state, COUNT(*)
                        FROM typed_api_receipts
                        GROUP BY operation, state
                        ORDER BY operation
                        """
                    ).fetchall()
                finally:
                    connection.close()
                self.assertEqual(task_count, result["disable"]["taskCountBeforeDisable"])
                self.assertEqual(
                    receipts,
                    [
                        ("task.cancel", "committed", 1),
                        ("task.create", "committed", 2),
                        ("task.resume", "committed", 1),
                    ],
                )
                event_lines = [
                    line
                    for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                self.assertGreater(len(event_lines), 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
                _shutdown_api_product_bootstrap()
                os.environ.pop("ZYRA_API_AUTH_TOKEN", None)

    def test_real_server_rejects_auth_and_version_mismatch_with_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _configure_runtime_environment(root, auth_token="server-secret")
            handler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            request_id = "request_000000000000001_0123456789abcdefabcd"
            try:
                status, body, headers = _request(
                    base_url,
                    "/health",
                    headers={
                        "X-Zyra-Api-Version": "1.0",
                        "X-Zyra-Client": "zyra-web",
                        "X-Zyra-Operation": "health",
                        "X-Zyra-Contract": "zyra.health.v1",
                        "X-Request-Id": request_id,
                        "Authorization": "Bearer wrong-secret",
                    },
                )
                self.assertEqual(status, 401)
                self.assertEqual(body["error"], "authentication_required")
                self.assertFalse(body["fallback"])
                self.assertEqual(headers["X-Request-Id"], request_id)
                self.assertEqual(headers["X-Zyra-Api-Version"], "1.0")

                status, body, headers = _request(
                    base_url,
                    "/health",
                    headers={
                        "X-Zyra-Api-Version": "9.0",
                        "X-Zyra-Client": "zyra-web",
                        "X-Zyra-Operation": "health",
                        "X-Zyra-Contract": "zyra.health.v1",
                        "X-Request-Id": request_id,
                        "Authorization": "Bearer server-secret",
                    },
                )
                self.assertEqual(status, 426)
                self.assertEqual(body["error"], "api_version_mismatch")
                self.assertEqual(body["details"]["supported_versions"], ["1.0"])
                self.assertFalse(body["fallback"])
                self.assertEqual(headers["X-Request-Id"], request_id)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
                _shutdown_api_product_bootstrap()
                os.environ.pop("ZYRA_API_AUTH_TOKEN", None)


if __name__ == "__main__":
    unittest.main()
