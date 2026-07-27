from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SchedulerApiTests(unittest.TestCase):
    def test_scheduler_endpoints_and_command_expose_m5_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            environment_names = (
                "ZYRA_SQLITE_PATH",
                "ZYRA_EVENT_LOG",
                "ZYRA_TOOL_WORKSPACE",
                "ZYRA_ARTIFACT_ROOT",
            )
            previous_environment = {
                name: os.environ.get(name) for name in environment_names
            }
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api import main as api

            api.reset_api_product_bootstrap()
            api.reset_runtime_owner_composition()
            api.reset_fault_runtime_api()
            api.reset_recovery_runtime_api()
            ZyraRequestHandler = api.ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                health = _get(base_url, "/health")
                manifests = _get(base_url, "/scheduler/manifests")
                created = _post(base_url, "/tasks", {"goal": "M5 API scheduler task.", "auto_run": True})
                task_id = created["task"]["task_id"]
                scheduler = _get(base_url, f"/tasks/{task_id}/scheduler")
                injected = _post(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {"text": "/inject worker_lost worker_id=BrowserWorker"},
                )
                self.assertEqual(
                    injected["command_result"]["data"]["signal"]["refs"]["run_id"],
                    created["task"]["run_id"],
                    injected["command_result"]["data"]["signal"],
                )
                recovery = _get(base_url, f"/tasks/{task_id}/recovery")
                command = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/scheduler"})

                self.assertEqual(health["phase"], "m5-resource-scheduler-fault-recovery")
                self.assertGreaterEqual(len(manifests["manifests"]), 4)
                self.assertTrue(scheduler["preview_decision"]["selected_manifest_id"])
                self.assertEqual(
                    injected["command_result"]["data"]["signal"]["kind"],
                    "worker_unavailable",
                )
                self.assertEqual(
                    injected["command_result"]["data"]["handoff"]["metadata"]["consumer"],
                    "M1-S07C.RecoveryPlanner",
                )
                self.assertTrue(injected["task"]["metadata"]["fault_injection"])
                self.assertIsInstance(recovery["plans"], list)
                self.assertEqual(recovery["route"], {})
                self.assertEqual(command["command_result"]["name"], "/scheduler")
                self.assertTrue(command["command_result"]["data"]["manifests"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                api.reset_api_product_bootstrap()
                api.reset_runtime_owner_composition()
                api.reset_fault_runtime_api()
                api.reset_recovery_runtime_api()
                for name, previous in previous_environment.items():
                    if previous is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = previous


# Auto-run waits for the real worker, canonical event projection and scheduler
# state to commit.  Fifteen seconds was below the observed cold/combined-process
# envelope and caused client cleanup to race an otherwise successful request.
HTTP_TIMEOUT_SECONDS = 30


def _get(base_url: str, path: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(
            f"{base_url}{path}",
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise AssertionError(error.read().decode("utf-8")) from error


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
