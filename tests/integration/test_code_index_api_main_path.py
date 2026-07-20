from __future__ import annotations

import importlib
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
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_api_module() -> Any:
    module_name = "apps.api.zyra_api.main"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


class CodeIndexApiMainPathTests(unittest.TestCase):
    def test_workspace_bound_rebuild_search_symbols_and_disable_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
                "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                "ZYRA_TOOL_WORKSPACE": str(root / "legacy-workspace"),
                "ZYRA_CODE_INDEX_ROOT": str(root / "code-index"),
                "ZYRA_CODE_INDEX_DISABLED": "false",
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
                    {"goal": "Index a task-scoped workspace.", "auto_run": False},
                )
                task_id = created["task"]["task_id"]
                manager = module.get_workspace_manager()
                access = manager.acquire_for_worker(
                    task_id=task_id,
                    session_id="",
                    worker_id="CodeIndexApiTestSetup",
                )
                workspace = manager.internal_task_root(access)
                source = workspace / "src" / "calculator.py"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(
                    "def calculate_total(items):\n"
                    "    return sum(items)\n\n"
                    "def present_total(items):\n"
                    "    return calculate_total(items)\n",
                    encoding="utf-8",
                )

                rebuilt = _post(base_url, f"/tasks/{task_id}/code-index/rebuild", {})
                searched = _post(
                    base_url,
                    f"/tasks/{task_id}/code-index/search",
                    {
                        "pattern": "calculate_total",
                        "whole_word": True,
                        "file_globs": ["src/*.py"],
                        "budget": {"maximum_results": 10, "maximum_output_chars": 4_000},
                    },
                )
                symbols = _post(
                    base_url,
                    f"/tasks/{task_id}/code-index/symbols",
                    {"capability": "definition", "name": "calculate_total"},
                )
                status = _get(base_url, f"/tasks/{task_id}/code-index")

                self.assertEqual(rebuilt["receipt"]["operation"], "rebuild")
                self.assertEqual(rebuilt["data"]["file_count"], 1)
                self.assertGreater(rebuilt["receipt"]["index_generation"], 0)
                self.assertEqual(searched["data"]["matches"][0]["logical_path"], "src/calculator.py")
                self.assertEqual(searched["receipt"]["result_count"], 2)
                self.assertEqual(symbols["data"]["symbols"][0]["name"], "calculate_total")
                self.assertEqual(status["data"]["canonical_owner"], "WorkspaceManagerRuntime+WorkspaceFileRevision")
                self.assertFalse(status["receipt"]["metadata"]["physical_root_exposed"])
                self.assertTrue(any((root / "code-index").glob("workspace-*.sqlite3")))

                os.environ["ZYRA_CODE_INDEX_DISABLED"] = "true"
                disabled_status, disabled = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/code-index/search",
                    {"pattern": "calculate_total"},
                )
                self.assertEqual(disabled_status, 503)
                self.assertEqual(disabled["error"], "code_index_disabled")
                self.assertFalse(disabled["fallback"])
                self.assertFalse(disabled["details"]["legacy_recursive_scan"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                module.reset_workspace_manager()
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value


def _get(base_url: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url}{path}", method="GET")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    status, body = _post_with_status(base_url, path, payload)
    if status >= 400:
        raise AssertionError(f"unexpected HTTP {status}: {body}")
    return body


def _post_with_status(
    base_url: str,
    path: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
