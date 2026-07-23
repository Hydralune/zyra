from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator


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
        "ZYRA_EVENT_CURSOR_SECRET": "integration-event-cursor-secret",
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


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
        return body, {key.lower(): value for key, value in response.headers.items()}


def _error_request(base_url: str, path: str) -> tuple[int, dict[str, Any]]:
    try:
        _request(base_url, path)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))
    raise AssertionError(f"{path} unexpectedly succeeded")


class EventStreamCursorIngestionIntegrationTests(unittest.TestCase):
    def test_real_snapshot_delta_cursor_scope_and_tamper_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with _isolated_server(Path(tmpdir)) as base_url:
                first, _ = _request(
                    base_url,
                    "/tasks",
                    method="POST",
                    payload={
                        "goal": "Exercise cursor scope and snapshot paging.",
                        "auto_run": False,
                    },
                )
                second, _ = _request(
                    base_url,
                    "/tasks",
                    method="POST",
                    payload={
                        "goal": "Provide a distinct cursor scope.",
                        "auto_run": False,
                    },
                )
                first_id = str(first["task"]["task_id"])
                second_id = str(second["task"]["task_id"])
                capabilities, capability_headers = _request(
                    base_url,
                    f"/tasks/{first_id}/event-ingress/capabilities?generation=7",
                )
                self.assertEqual(
                    capabilities["schema"],
                    "zyra.event-ingress-capabilities/v1",
                )
                self.assertEqual(capabilities["generation"], 7)
                self.assertTrue(capabilities["subscribeBeforeSnapshot"])
                self.assertFalse(capabilities["canonicalWriteAllowed"])
                self.assertEqual(
                    capability_headers["x-zyra-event-state-owner"],
                    "typescript.RuntimeEventSpine",
                )
                transports = {
                    item["kind"]: item for item in capabilities["transports"]
                }
                self.assertTrue(transports["sse"]["available"])
                self.assertTrue(transports["long_poll"]["available"])
                self.assertFalse(transports["websocket"]["available"])

                snapshot_cursor: str | None = None
                snapshot_frames: list[dict[str, Any]] = []
                while True:
                    query = {"generation": 7, "limit": 1}
                    if snapshot_cursor:
                        query["cursor"] = snapshot_cursor
                    snapshot, headers = _request(
                        base_url,
                        f"/tasks/{first_id}/event-ingress/snapshot?"
                        f"{urllib.parse.urlencode(query)}",
                    )
                    self.assertEqual(
                        snapshot["schema"],
                        "zyra.event-ingress-snapshot/v1",
                    )
                    self.assertEqual(snapshot["taskId"], first_id)
                    self.assertEqual(
                        int(headers["x-zyra-event-sequence"]),
                        snapshot["nextSequence"],
                    )
                    snapshot_frames.extend(snapshot["frames"])
                    snapshot_cursor = str(snapshot["cursor"])
                    if snapshot["complete"]:
                        break
                    self.assertTrue(snapshot["hasMore"])
                self.assertGreaterEqual(len(snapshot_frames), 1)
                sequences = [frame["sequence"] for frame in snapshot_frames]
                self.assertEqual(sequences, sorted(sequences))
                previous = 0
                for frame in snapshot_frames:
                    self.assertEqual(frame["previousSequence"], previous)
                    self.assertTrue(frame["cursor"])
                    self.assertEqual(
                        frame["event"]["identity"]["taskId"],
                        first_id,
                    )
                    previous = frame["sequence"]

                cancelled, _ = _request(
                    base_url,
                    f"/tasks/{first_id}/cancel",
                    method="POST",
                    payload={"reason": "Generate a committed delta event."},
                )
                self.assertEqual(cancelled["task"]["status"], "cancelled")
                delta, _ = _request(
                    base_url,
                    f"/tasks/{first_id}/event-ingress/delta?"
                    f"{urllib.parse.urlencode({'cursor': snapshot_cursor, 'generation': 7, 'limit': 10, 'wait_ms': 0})}",
                )
                self.assertEqual(delta["schema"], "zyra.event-ingress-delta/v1")
                self.assertGreaterEqual(delta["nextSequence"], snapshot["nextSequence"])
                self.assertTrue(delta["frames"])
                self.assertEqual(
                    delta["frames"][0]["previousSequence"],
                    snapshot["nextSequence"],
                )

                tampered = f"{snapshot_cursor[:-1]}{'A' if snapshot_cursor[-1] != 'A' else 'B'}"
                status, error = _error_request(
                    base_url,
                    f"/tasks/{first_id}/event-ingress/delta?"
                    f"{urllib.parse.urlencode({'cursor': tampered, 'generation': 7, 'wait_ms': 0})}",
                )
                self.assertEqual(status, 400)
                self.assertEqual(error["error"], "cursor_signature_invalid")
                self.assertTrue(error["resyncRequired"])

                status, error = _error_request(
                    base_url,
                    f"/tasks/{second_id}/event-ingress/delta?"
                    f"{urllib.parse.urlencode({'cursor': snapshot_cursor, 'generation': 7, 'wait_ms': 0})}",
                )
                self.assertEqual(status, 409)
                self.assertEqual(error["error"], "cursor_scope_mismatch")
                self.assertTrue(error["resyncRequired"])

    def test_real_browser_ingress_uses_sse_reconnect_and_long_poll_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with _isolated_server(Path(tmpdir)) as base_url:
                bun = ROOT / "node_modules" / ".bin" / "bun.exe"
                probe = ROOT / "apps" / "web" / "test" / "event-ingress-probe.ts"
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
                self.assertGreaterEqual(result["sse"]["events"], 2)
                self.assertTrue(result["sse"]["audit"])
                self.assertGreaterEqual(max(result["sse"]["generations"]), 2)
                self.assertEqual(result["longPoll"]["transport"], "long_poll")
                self.assertTrue(result["longPoll"]["audit"])
                self.assertEqual(
                    result["disable"]["error"],
                    "event_ingress_disabled",
                )


if __name__ == "__main__":
    unittest.main()
