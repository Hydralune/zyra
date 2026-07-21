from __future__ import annotations

import importlib
import gc
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


def _fresh_api_module() -> Any:
    module_name = "apps.api.zyra_api.main"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


class ReusableProcedureApiMainPathTests(unittest.TestCase):
    def test_procedure_status_mine_and_consumer_queries_are_live_api_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
                "ZYRA_MEMORY_INDEX_PATH": str(root / "memory-index.sqlite3"),
                "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                "ZYRA_WORKSPACE_ROOT": str(root / "workspace"),
            }
            previous = {name: os.environ.get(name) for name in environment}
            os.environ.update(environment)
            module = _fresh_api_module()
            server = ThreadingHTTPServer(("127.0.0.1", 0), module.ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(
                    base_url,
                    "/tasks",
                    {"goal": "Use validated procedure memory only.", "auto_run": False},
                )
                task_id = created["task"]["task_id"]

                status = _get(base_url, f"/tasks/{task_id}/memory/procedures")
                self.assertEqual(status["protocol"], "zyra.reusable-procedure-api/v1")
                self.assertEqual(status["task_id"], task_id)
                self.assertEqual(status["source_owner"], "06B CuratorIntegrationStore")
                self.assertEqual(status["procedure_owner"], "06C ReusableProcedureStore")
                self.assertEqual(status["skill_execution_owner"], "03C SkillCoordinator")
                self.assertFalse(status["model_can_activate"])
                self.assertEqual(status["procedures"], [])
                self.assertEqual(status["status"]["pending_outcome_count"], 0)

                mined = _post(
                    base_url,
                    f"/tasks/{task_id}/memory/procedures/mine",
                    {"limit": 50},
                )
                self.assertEqual(mined["operation"], "mine")
                self.assertEqual(mined["results"], [])
                self.assertEqual(mined["status"]["procedure_state_counts"]["validated"], 0)

                for operation in ("routing", "recovery", "context"):
                    projected = _post(
                        base_url,
                        f"/tasks/{task_id}/memory/procedures/{operation}",
                        {
                            "goal": "Use validated procedure memory only.",
                            "available_tools": ["file_read"],
                            "validated_only": True,
                        },
                    )
                    self.assertEqual(projected["operation"], operation)
                    self.assertEqual(projected["result"]["consumer"], operation)
                    self.assertEqual(projected["result"]["matches"], [])
                    self.assertFalse(projected["model_can_activate"])
                    self.assertFalse(projected["static_document_can_activate"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                module.reset_memory_curator_runtime()
                module.reset_runtime_event_spine_bridge()
                gc.collect()
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value


def _get(base_url: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url}{path}", method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(
    base_url: str,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
