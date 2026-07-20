from __future__ import annotations

import importlib
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


class RetrievalApiMainPathTests(unittest.TestCase):
    def test_memory_query_builds_and_reads_the_real_derived_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
                "ZYRA_MEMORY_INDEX_PATH": str(root / "memory-index.sqlite3"),
                "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            }
            previous = {name: os.environ.get(name) for name in environment}
            os.environ.update(environment)
            handler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(
                    base_url,
                    "/tasks",
                    {
                        "goal": "Preserve canonical replay evidence in the retrieval index.",
                        "auto_run": False,
                    },
                )
                task_id = created["task"]["task_id"]
                _post(base_url, f"/tasks/{task_id}/memory/ingest", {})
                memory = _get(base_url, f"/tasks/{task_id}/memory?q=replay")

                self.assertEqual(memory["retrieval_runtime"], "MemoryIndexRuntime")
                self.assertFalse(memory["legacy_substring_fallback"])
                self.assertTrue(memory["search_results"])
                retrieval = memory["retrieval"]["retrieval"]
                self.assertGreater(retrieval["diagnostics"]["index_generation"], 0)
                self.assertEqual(retrieval["diagnostics"]["vector_status"], "unavailable")
                self.assertTrue((root / "memory-index.sqlite3").is_file())
                self.assertNotEqual(root / "memory-index.sqlite3", root / "api.sqlite3")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
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
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
