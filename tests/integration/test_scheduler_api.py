from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
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
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

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
                injected = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/inject BrowserWorker timeout"})
                recovery = _get(base_url, f"/tasks/{task_id}/recovery")
                command = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/scheduler"})

                self.assertEqual(health["phase"], "m5-resource-scheduler-fault-recovery")
                self.assertGreaterEqual(len(manifests["manifests"]), 4)
                self.assertTrue(scheduler["preview_decision"]["selected_manifest_id"])
                self.assertEqual(injected["command_result"]["data"]["latest_recovery_plan"]["selected_manifest_id"], injected["task"]["metadata"]["last_recovery_plan"]["selected_manifest_id"])
                self.assertTrue(recovery["recovery_plans"])
                self.assertEqual(command["command_result"]["name"], "/scheduler")
                self.assertTrue(command["command_result"]["data"]["manifests"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


HTTP_TIMEOUT_SECONDS = 15


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


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
