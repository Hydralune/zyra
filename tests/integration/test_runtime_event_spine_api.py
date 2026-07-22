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


class RuntimeEventSpineApiTests(unittest.TestCase):
    def test_concurrent_reset_cannot_return_the_bridge_being_closed(self) -> None:
        previous = os.environ.get("ZYRA_SQLITE_PATH")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            import apps.api.zyra_api.main as api_main  # noqa: PLC0415

            os.environ["ZYRA_SQLITE_PATH"] = str(root / "reset-race.sqlite3")
            api_main.reset_runtime_event_spine_bridge()
            closing = api_main.get_runtime_event_spine_bridge()
            real_release = api_main.release_runtime_event_spine
            release_entered = threading.Event()
            allow_release = threading.Event()
            getter_done = threading.Event()
            acquired: list[Any] = []

            def blocked_release(bridge: Any) -> bool:
                release_entered.set()
                if not allow_release.wait(timeout=10):
                    raise TimeoutError("release was not allowed")
                return real_release(bridge)

            api_main.release_runtime_event_spine = blocked_release
            reset_thread = threading.Thread(
                target=api_main.reset_runtime_event_spine_bridge,
                daemon=True,
            )
            get_thread = threading.Thread(
                target=lambda: (
                    acquired.append(api_main.get_runtime_event_spine_bridge()),
                    getter_done.set(),
                ),
                daemon=True,
            )
            try:
                reset_thread.start()
                self.assertTrue(release_entered.wait(timeout=5))
                get_thread.start()
                self.assertFalse(getter_done.wait(timeout=0.2))
                allow_release.set()
                reset_thread.join(timeout=10)
                get_thread.join(timeout=10)
                self.assertTrue(getter_done.is_set())
                self.assertEqual(len(acquired), 1)
                self.assertIsNot(acquired[0], closing)
                self.assertTrue(acquired[0].health().ok)
            finally:
                allow_release.set()
                api_main.release_runtime_event_spine = real_release
                reset_thread.join(timeout=10)
                get_thread.join(timeout=10)
                api_main.reset_runtime_event_spine_bridge()
                if previous is None:
                    os.environ.pop("ZYRA_SQLITE_PATH", None)
                else:
                    os.environ["ZYRA_SQLITE_PATH"] = previous

    def test_server_close_drains_active_handler_before_sidecar_shutdown(self) -> None:
        previous = os.environ.get("ZYRA_SQLITE_PATH")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            from apps.api.zyra_api.main import (  # noqa: PLC0415
                ZyraRequestHandler,
                get_runtime_event_spine_bridge,
                reset_runtime_event_spine_bridge,
            )

            entered = threading.Event()
            release = threading.Event()

            class BlockingHandler(ZyraRequestHandler):
                def do_GET(self) -> None:  # noqa: N802
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test handler was not released")
                    self._send_json(200, {"ok": True})

            os.environ["ZYRA_SQLITE_PATH"] = str(root / "drain.sqlite3")
            reset_runtime_event_spine_bridge()
            bridge = get_runtime_event_spine_bridge()
            self.assertTrue(bridge.health().ok)
            server = ThreadingHTTPServer(("127.0.0.1", 0), BlockingHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            client_errors: list[BaseException] = []

            def request() -> None:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{server.server_address[1]}/block",
                        timeout=10,
                    ) as response:
                        self.assertEqual(json.loads(response.read()), {"ok": True})
                except BaseException as error:  # pragma: no cover - asserted below
                    client_errors.append(error)

            client_thread = threading.Thread(target=request, daemon=True)
            client_thread.start()
            self.assertTrue(entered.wait(timeout=5))
            server.shutdown()
            close_done = threading.Event()
            close_thread = threading.Thread(
                target=lambda: (server.server_close(), close_done.set()),
                daemon=True,
            )
            close_thread.start()
            try:
                self.assertFalse(close_done.wait(timeout=0.2))
                self.assertTrue(bridge.port.diagnostics().running)
            finally:
                release.set()
                client_thread.join(timeout=10)
                close_thread.join(timeout=10)
                server_thread.join(timeout=5)
                reset_runtime_event_spine_bridge()
                if previous is None:
                    os.environ.pop("ZYRA_SQLITE_PATH", None)
                else:
                    os.environ["ZYRA_SQLITE_PATH"] = previous
            self.assertTrue(close_done.is_set())
            self.assertFalse(bridge.port.diagnostics().running)
            self.assertEqual(client_errors, [])

    def test_database_key_rotation_evicts_closed_cached_bridge(self) -> None:
        previous = os.environ.get("ZYRA_SQLITE_PATH")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            from apps.api.zyra_api.main import (  # noqa: PLC0415
                get_runtime_event_spine_bridge,
                reset_runtime_event_spine_bridge,
            )

            try:
                reset_runtime_event_spine_bridge()
                os.environ["ZYRA_SQLITE_PATH"] = str(root / "first.sqlite3")
                first = get_runtime_event_spine_bridge()
                self.assertTrue(first.health().ok)

                os.environ["ZYRA_SQLITE_PATH"] = str(root / "second.sqlite3")
                second = get_runtime_event_spine_bridge()
                self.assertIsNot(first, second)
                self.assertTrue(second.health().ok)

                os.environ["ZYRA_SQLITE_PATH"] = str(root / "first.sqlite3")
                replacement = get_runtime_event_spine_bridge()
                self.assertIsNot(first, replacement)
                self.assertTrue(replacement.health().ok)
            finally:
                reset_runtime_event_spine_bridge()
                if previous is None:
                    os.environ.pop("ZYRA_SQLITE_PATH", None)
                else:
                    os.environ["ZYRA_SQLITE_PATH"] = previous

    def test_real_task_creation_reaches_canonical_query_and_projection_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            os.environ.update(
                {
                    "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
                    "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                    "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
                    "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                    "ZYRA_WORKSPACE_STATE": str(root / "workspace-state"),
                    "ZYRA_CONTROL_STATE": str(root / "control"),
                    "ZYRA_PERMISSION_STORE": str(root / "permissions.json"),
                }
            )
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
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _request(
                    base_url,
                    "/tasks",
                    method="POST",
                    payload={"goal": "exercise the 05C event spine", "auto_run": False},
                )
                task_id = str(created["task"]["task_id"])
                legacy_event_id = str(created["events"][0]["event_id"])

                task_events = _request(base_url, f"/tasks/{task_id}/runtime-events")
                self.assertGreaterEqual(len(task_events["events"]), 1)
                self.assertTrue(
                    all(event["identity"]["taskId"] == task_id for event in task_events["events"])
                )
                self.assertEqual(
                    sorted(event["globalSequence"] for event in task_events["events"]),
                    [event["globalSequence"] for event in task_events["events"]],
                )

                event = _request(base_url, f"/runtime-events/{legacy_event_id}")
                self.assertEqual(event["eventId"], legacy_event_id)
                self.assertEqual(event["schema"], "zyra.runtime-event/v1")

                projection = _request(
                    base_url,
                    f"/tasks/{task_id}/runtime-event-projection",
                )
                self.assertIn(projection["projection"], {"session", "task", "runtime"})
                self.assertEqual(projection["state"]["taskId"], task_id)

                metrics = _request(base_url, "/runtime-events/metrics")
                self.assertGreaterEqual(metrics["eventCount"], len(task_events["events"]))
                self.assertTrue(metrics["passedHardLimits"])

                baselines = _request(base_url, "/runtime-events/baselines")
                self.assertEqual(baselines["schema"], "zyra.low-entropy-comparison/v1")
                self.assertEqual(
                    [item["strategy"] for item in baselines["baselines"]],
                    [
                        "targeted_artifact_ref",
                        "static_route",
                        "full_broadcast",
                        "full_text_inline",
                    ],
                )
                self.assertTrue(baselines["task_success_not_significantly_lower"])
                self.assertTrue((root / "events.jsonl").is_file())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                reset_workspace_manager()
                reset_runtime_event_spine_bridge()

    def test_legacy_jsonl_projection_tracks_each_successful_canonical_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            os.environ.update(
                {
                    "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
                    "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                    "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                }
            )
            from apps.api.zyra_api.main import (  # noqa: PLC0415
                get_runtime_event_spine_bridge,
                get_store,
                persist_events,
                reset_runtime_event_spine_bridge,
            )
            from zyra_core import EventRecord  # noqa: PLC0415
            from zyra_runtime.runtime_events import RuntimeEventProcessError  # noqa: PLC0415

            reset_runtime_event_spine_bridge()
            valid = EventRecord(
                event_id="legacy-valid",
                run_id="run-jsonl",
                task_id="task-jsonl",
                event_type="task_created",
                payload={"goal": "commit the first fact"},
            )
            invalid = EventRecord(
                event_id="legacy-invalid",
                run_id="run-jsonl",
                task_id="task-jsonl",
                event_type="command_succeeded",
                payload={"request_id": "unmatched-command-correlation"},
            )
            try:
                with self.assertRaises(RuntimeEventProcessError):
                    persist_events(get_store(), [valid, invalid])
                lines = (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
                self.assertEqual([json.loads(line)["event_id"] for line in lines], ["legacy-valid"])
                bridge = get_runtime_event_spine_bridge()
                self.assertIsNotNone(bridge.get_event("legacy-valid"))
                self.assertIsNone(bridge.get_event("legacy-invalid"))
            finally:
                reset_runtime_event_spine_bridge()


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
