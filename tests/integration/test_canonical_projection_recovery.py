from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT,
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workspace",
    ROOT / "packages" / "scheduler",
    ROOT / "packages" / "memory",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "skills",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


@contextmanager
def _isolated_server(root: Path) -> Iterator[str]:
    keys = {
        "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
        "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
        "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
        "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
        "ZYRA_WORKSPACE_STATE": str(root / "workspace-state"),
        "ZYRA_CONTROL_STATE": str(root / "control"),
        "ZYRA_PERMISSION_STORE": str(root / "permissions.json"),
        "ZYRA_EVENT_CURSOR_SECRET": "projection-recovery-secret",
    }
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    from apps.api.zyra_api.main import (  # noqa: PLC0415
        ZyraRequestHandler,
        reset_runtime_event_spine_bridge,
        reset_workspace_manager,
    )

    reset_workspace_manager()
    reset_runtime_event_spine_bridge()
    server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        reset_workspace_manager()
        reset_runtime_event_spine_bridge()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class CanonicalProjectionRecoveryIntegrationTests(unittest.TestCase):
    def test_real_api_ingress_restore_reconnect_and_browser_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with _isolated_server(Path(tmpdir)) as base_url:
                bun = ROOT / "node_modules" / ".bin" / "bun.exe"
                probe = (
                    ROOT
                    / "apps"
                    / "web"
                    / "test"
                    / "canonical-projection-probe.ts"
                )
                completed = subprocess.run(
                    [str(bun), str(probe), base_url],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
                )
                result = json.loads(completed.stdout.strip().splitlines()[-1])
                self.assertEqual(result["backend"]["status"], "cancelled")
                self.assertEqual(result["backend"]["survivorStatus"], "pending")
                self.assertGreater(result["projection"]["initialSequence"], 0)
                self.assertGreaterEqual(
                    result["projection"]["restoredSequence"],
                    result["projection"]["initialSequence"],
                )
                self.assertEqual(
                    result["projection"]["reconnectSequence"],
                    result["projection"]["restoredSequence"],
                )
                self.assertEqual(
                    result["projection"]["restoredEventCount"],
                    result["projection"]["terminalEventCount"],
                )
                self.assertGreater(
                    result["projection"]["terminalEventCount"],
                    result["projection"]["initialEventCount"],
                )
                self.assertEqual(
                    result["projection"]["lifecycle"],
                    "cancelled",
                )
                self.assertTrue(result["projection"]["integrity"])
                self.assertEqual(
                    result["disable"]["error"],
                    "PROJECTION_DISABLED",
                )


if __name__ == "__main__":
    unittest.main()
