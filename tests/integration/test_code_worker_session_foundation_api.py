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


class CodeWorkerSessionFoundationApiTests(unittest.TestCase):
    def test_session_foundation_endpoint_returns_live_projection(self) -> None:
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
                payload = _get(base_url, "/workers/code/session-foundation")
                inventory = _get(base_url, "/workers/code/inventory")

                self.assertEqual(payload["ownerUnit"], "M1-02B")
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["metadata"]["session_api_projection_ok"], "true")
                self.assertEqual(payload["sessionFoundation"]["hasSessionReplayRuntime"], True)
                self.assertEqual(payload["sessionFoundation"]["hasSessionLifecycleState"], True)
                self.assertTrue(payload["inputReport"]["ok"])
                self.assertEqual(payload["metadata"]["context_assembly_ok"], "true")
                self.assertTrue(payload["turnLifecycle"]["ok"])
                self.assertTrue(payload["sessionAcceptance"]["ok"])
                self.assertTrue(payload["sessionLifecycle"]["ok"])
                self.assertTrue(payload["sessionLineage"]["ok"])
                self.assertIn("sessionLineage", inventory)
                self.assertTrue(inventory["sessionLineage"]["ok"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
