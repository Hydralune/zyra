from __future__ import annotations

import json
import os
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class WorkspaceManagerApiTests(unittest.TestCase):
    def test_task_creation_file_snapshot_restore_and_event_causality(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _workspace_api(root) as base_url:
                created = _post(
                    base_url,
                    "/tasks",
                    {"goal": "Exercise the task workspace lifecycle.", "auto_run": False},
                )
                task = created["task"]
                task_id = task["task_id"]
                workspace = task["metadata"]["workspace_ref"]
                workspace_id = workspace["workspace_id"]

                self.assertEqual(workspace["lifecycle_state"], "open")
                self.assertTrue(workspace["physical_location_redacted"])
                self.assertNotIn(str(root), json.dumps(created, sort_keys=True))
                workspace_events = [
                    event["payload"]["workspace_event"]["event_type"]
                    for event in created["events"]
                    if "workspace_event" in event.get("payload", {})
                ]
                self.assertIn("workspace.ready", workspace_events)
                self.assertIn("workspace.opened", workspace_events)

                listing = _get(base_url, "/workspaces")
                detail = _get(base_url, f"/workspaces/{workspace_id}")
                self.assertEqual(listing["count"], 1)
                self.assertEqual(detail["workspace"]["task_id"], task_id)
                self.assertNotIn(str(root), json.dumps(detail, sort_keys=True))

                absent = _get(
                    base_url,
                    f"/workspaces/{workspace_id}/files?"
                    + urllib.parse.urlencode(
                        {"path": "notes/progress.txt", "read": "true", "encoding": "utf-8"}
                    ),
                )
                self.assertTrue(absent["write_precondition_available"])
                write = _post(
                    base_url,
                    f"/workspaces/{workspace_id}/files",
                    {"path": "notes/progress.txt", "encoding": "utf-8", "content": "version one"},
                )
                self.assertTrue(write["write"]["created"])
                read = _get(
                    base_url,
                    f"/workspaces/{workspace_id}/files?"
                    + urllib.parse.urlencode(
                        {"path": "notes/progress.txt", "read": "true", "encoding": "utf-8"}
                    ),
                )
                self.assertEqual(read["content"], "version one")

                snapshot_response = _post(base_url, f"/workspaces/{workspace_id}/snapshot", {})
                snapshot_id = snapshot_response["snapshot"]["snapshot_id"]
                self.assertEqual(snapshot_response["snapshot"]["state"], "committed")
                _post(
                    base_url,
                    f"/workspaces/{workspace_id}/files",
                    {"path": "notes/progress.txt", "encoding": "utf-8", "content": "version two"},
                )
                restored = _post(
                    base_url,
                    f"/workspaces/{workspace_id}/restore",
                    {"snapshot_id": snapshot_id},
                )
                self.assertEqual(restored["restore"]["snapshot_id"], snapshot_id)
                after = _get(
                    base_url,
                    f"/workspaces/{workspace_id}/files?"
                    + urllib.parse.urlencode(
                        {"path": "notes/progress.txt", "read": "true", "encoding": "utf-8"}
                    ),
                )
                self.assertEqual(after["content"], "version one")

                trace = _get(base_url, f"/tasks/{task_id}/events")
                event_types = [
                    item["payload"]["workspace_event"]["event_type"]
                    for item in trace["events"]
                    if "workspace_event" in item.get("payload", {})
                ]
                self.assertIn("workspace.snapshot.committed", event_types)
                self.assertIn("workspace.restore.committed", event_types)

    def test_disabled_local_workspace_rejects_task_without_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _workspace_api(root, enabled=False) as base_url:
                status, body = _post_error(
                    base_url,
                    "/tasks",
                    {"goal": "This task must fail closed.", "auto_run": False},
                )
                self.assertEqual(status, 503)
                self.assertEqual(body["error"]["code"], "workspace_backend_disabled")
                self.assertFalse((root / "workspace-data").exists())
                self.assertFalse((root / "legacy-global-workspace").exists())

    def test_workspace_api_never_returns_internal_root_even_in_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with _workspace_api(root) as base_url:
                created = _post(base_url, "/tasks", {"goal": "Redaction test", "auto_run": False})
                workspace_id = created["task"]["metadata"]["workspace_ref"]["workspace_id"]
                status, body = _get_error(
                    base_url,
                    f"/workspaces/{workspace_id}/files?"
                    + urllib.parse.urlencode({"path": "../outside", "read": "true"}),
                )
                self.assertEqual(status, 400)
                encoded = json.dumps(body, sort_keys=True)
                self.assertNotIn(str(root), encoded)
                self.assertTrue(body["physical_location_redacted"])


@contextmanager
def _workspace_api(root: Path, *, enabled: bool = True) -> Iterator[str]:
    environment = {
        "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
        "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
        "ZYRA_TOOL_WORKSPACE": str(root / "legacy-global-workspace"),
        "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
        "ZYRA_WORKSPACE_STATE_ROOT": str(root / "workspace-state"),
        "ZYRA_WORKSPACE_DATA_ROOT": str(root / "workspace-data"),
        "ZYRA_LOCAL_WORKSPACE_ENABLED": "1" if enabled else "0",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    from apps.api.zyra_api.main import ZyraRequestHandler, reset_workspace_manager

    reset_workspace_manager()
    server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        reset_workspace_manager()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as response:
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


def _get_error(base_url: str, path: str) -> tuple[int, dict[str, Any]]:
    try:
        _get(base_url, path)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))
    raise AssertionError("request unexpectedly succeeded")


def _post_error(base_url: str, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        _post(base_url, path, payload)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))
    raise AssertionError("request unexpectedly succeeded")


if __name__ == "__main__":
    unittest.main()
