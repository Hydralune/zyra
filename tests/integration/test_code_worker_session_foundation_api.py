from __future__ import annotations

import gc
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class CodeWorkerSessionFoundationApiTests(unittest.TestCase):
    def test_session_foundation_endpoint_returns_typescript_contract(self) -> None:
        environment_keys = (
            "ZYRA_SQLITE_PATH",
            "ZYRA_EVENT_LOG",
            "ZYRA_TOOL_WORKSPACE",
            "ZYRA_ARTIFACT_ROOT",
        )
        environment_before = {
            key: os.environ.get(key) for key in environment_keys
        }
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "ZYRA_SQLITE_PATH": str(Path(tmpdir) / "api.sqlite3"),
                "ZYRA_EVENT_LOG": str(Path(tmpdir) / "events.jsonl"),
                "ZYRA_TOOL_WORKSPACE": str(Path(tmpdir) / "workspace"),
                "ZYRA_ARTIFACT_ROOT": str(Path(tmpdir) / "artifacts"),
            },
            clear=False,
        ):

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                payload = _get(base_url, "/workers/code/session-foundation")
                inventory = _get(base_url, "/workers/code/inventory")

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["canonicalOwner"], "typescript")
                self.assertEqual(payload["source"], "zyra-typescript-runtime")
                self.assertEqual(payload["snapshotVersion"], "zyra.typescript-query-session.v1")
                self.assertTrue(payload["exactResume"])
                self.assertFalse(payload["pythonProjectionIsCanonical"])
                self.assertTrue(payload["defaultRoute"])
                self.assertFalse(payload["fallbackUsed"])
                self.assertEqual(inventory["canonicalOwner"], "typescript")
                self.assertFalse(inventory["requiresRootSourceRepo"])
                self.assertFalse(inventory["requiresVendorRuntime"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                from apps.api.zyra_api.main import reset_api_product_bootstrap
                from apps.api.zyra_api.experiment_api import reset_experiment_api

                reset_experiment_api(wait=True)
                reset_api_product_bootstrap()
                gc.collect()
        self.assertEqual(
            {key: os.environ.get(key) for key in environment_keys},
            environment_before,
        )

    def test_inventory_fails_closed_when_typescript_runtime_is_unavailable(self) -> None:
        environment_keys = ("ZYRA_SQLITE_PATH", "ZYRA_EVENT_LOG")
        environment_before = {
            key: os.environ.get(key) for key in environment_keys
        }
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "ZYRA_SQLITE_PATH": str(Path(tmpdir) / "api.sqlite3"),
                "ZYRA_EVENT_LOG": str(Path(tmpdir) / "events.jsonl"),
            },
            clear=False,
        ):

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with patch(
                    "apps.api.zyra_api.main.CodeWorkerSidecarClient.runtime_inventory",
                    side_effect=RuntimeError("typescript runtime unavailable"),
                ):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        _get(base_url, "/workers/code/inventory")
                payload = json.loads(raised.exception.read().decode("utf-8"))
                self.assertEqual(raised.exception.code, 503)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error"], "code_worker_runtime_unavailable")
                self.assertEqual(payload["canonicalOwner"], "typescript")
                self.assertFalse(payload["fallbackUsed"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                from apps.api.zyra_api.main import reset_api_product_bootstrap
                from apps.api.zyra_api.experiment_api import reset_experiment_api

                reset_experiment_api(wait=True)
                reset_api_product_bootstrap()
                gc.collect()
        self.assertEqual(
            {key: os.environ.get(key) for key in environment_keys},
            environment_before,
        )


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
