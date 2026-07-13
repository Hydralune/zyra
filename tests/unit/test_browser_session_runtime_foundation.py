from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for package_path in (ROOT / "packages" / "integrations", ROOT / "packages" / "workers"):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.browser_use import BrowserEventBus, BrowserTargetDescriptor
from zyra_workers.browser_session import (
    BrowserConnectionStatus,
    BrowserProfileError,
    BrowserProfileStore,
    BrowserRequestTimeout,
    BrowserRuntime,
    BrowserRuntimeConfig,
    BrowserRuntimeDisabled,
    BrowserSessionCommand,
    BrowserTargetDetached,
    BrowserTargetError,
    BrowserTargetRuntime,
    BrowserTransportError,
    CdpRequestRuntime,
    JsonBrowserStateStore,
    MemoryCdpTransport,
)


class _CdpHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _write(self, value: object) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        state["requests"].append((self.command, self.path, self.headers.get("Authorization", "")))
        if self.path == "/json/version":
            self._write({"Browser": "ZyraFixture/1", "Protocol-Version": "1.3", "webSocketDebuggerUrl": state["ws"]})
        elif self.path == "/json/list":
            self._write(list(state["targets"]))
        elif self.path.startswith("/json/activate/"):
            self._write("Target activated")
        elif self.path.startswith("/json/new"):
            target = {"id": "blank-created", "type": "page", "url": "about:blank", "title": ""}
            state["targets"].append(target)
            self._write(target)
        else:
            self.send_error(404)

    do_PUT = do_GET


class _CdpFixture:
    def __init__(self, *, targets: list[dict[str, object]] | None = None) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CdpHandler)
        self.server.daemon_threads = True
        self.server.state = {  # type: ignore[attr-defined]
            "requests": [],
            "targets": targets if targets is not None else [{"id": "page-1", "type": "page", "url": "about:blank", "title": "Page"}],
            "ws": "ws://127.0.0.1:9/devtools/browser/fixture",
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "_CdpFixture":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _command(root: Path, endpoint: str, *, request_id: str = "request-1") -> BrowserSessionCommand:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    return BrowserSessionCommand(
        run_id="run-browser-foundation",
        task_id="task-browser-foundation",
        worker_request_id=request_id,
        canonical_session_id="canonical-browser-foundation",
        node_id="node-browser",
        workspace_root=workspace,
        artifact_root=root / "artifacts",
        endpoint_url=endpoint,
        constraints={"browser_transport": "memory"},
        headers={"Authorization": "Bearer must-not-leak", "X-Zyra-Test": "foundation"},
    )


class _Discovery:
    def __init__(self, targets: list[BrowserTargetDescriptor] | None = None) -> None:
        self.targets = list(targets or [])
        self.created = 0
        self.activated: list[str] = []
        self._lock = threading.Lock()

    def list_targets(self) -> tuple[BrowserTargetDescriptor, ...]:
        with self._lock:
            return tuple(self.targets)

    def create_blank_target(self) -> BrowserTargetDescriptor:
        with self._lock:
            self.created += 1
            target = BrowserTargetDescriptor("blank-1", "page", "", "about:blank", "")
            if not any(item.target_id == target.target_id for item in self.targets):
                self.targets.append(target)
            return target

    def activate_target(self, target_id: str) -> bool:
        self.activated.append(target_id)
        return True


class BrowserSessionRuntimeFoundationTests(unittest.TestCase):
    def test_profile_store_isolated_layout_and_corruption_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = JsonBrowserStateStore(root / "state")
            store = BrowserProfileStore(root / "runtime", root / "state", state_store=state)
            command = _command(root, "http://127.0.0.1:9222")
            first = store.prepare(command, "session-profile")
            self.assertTrue(first.created)
            for path in (first.profile.root, first.profile.cache_dir, first.profile.downloads_dir, first.profile.temp_dir, first.profile.state_dir):
                path.resolve().relative_to((root / "runtime").resolve())
                self.assertTrue(path.is_dir())
            (first.profile.root / "profile.json").write_text("{broken", encoding="utf-8")
            recovered = store.prepare(command, "session-profile")
            self.assertTrue(recovered.recovered)
            self.assertTrue(store.health(recovered.profile).healthy)
            self.assertEqual(len(tuple((root / "runtime" / "quarantine").iterdir())), 1)
            disabled = BrowserProfileStore(root / "disabled-runtime", root / "disabled-state", disabled=True)
            with self.assertRaises(BrowserProfileError):
                disabled.prepare(command, "disabled-session")

    def test_cdp_success_silent_timeout_and_disabled_fail_closed(self) -> None:
        bus = BrowserEventBus()
        bus.start()
        events: list[str] = []
        bus.subscribe(lambda event: events.append(event.topic), topic_prefix="browser.cdp")
        responder = lambda raw: json.dumps({"id": json.loads(raw)["id"], "result": {"targets": ["one"]}})
        runtime = CdpRequestRuntime("session-cdp", 0.15, bus, transport_factory=lambda: MemoryCdpTransport(responder))
        runtime.connect()
        self.assertEqual(runtime.send("Target.getTargets")["targets"], ["one"])
        self.assertEqual(runtime.snapshot().completed_requests, 1)
        runtime.close()

        silent = CdpRequestRuntime("session-silent", 0.05, bus, transport_factory=lambda: MemoryCdpTransport(lambda _raw: None))
        silent.connect()
        started = time.monotonic()
        with self.assertRaises(BrowserRequestTimeout):
            silent.send("Target.getTargets")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(silent.snapshot().timed_out_requests, 1)
        silent.close()
        disabled = CdpRequestRuntime("session-disabled", 1.0, bus, disabled=True)
        with self.assertRaises(BrowserTransportError):
            disabled.connect()
        bus.stop()
        self.assertIn("browser.cdp.request_timeout", events)

    def test_target_focus_recovers_and_concurrent_blank_fallback_is_singleton(self) -> None:
        bus = BrowserEventBus()
        bus.start()
        discovery = _Discovery([
            BrowserTargetDescriptor("page-a", "page", "A", "http://a.test", ""),
            BrowserTargetDescriptor("page-b", "page", "B", "http://b.test", ""),
        ])
        runtime = BrowserTargetRuntime("session-target", bus, lambda: discovery)
        runtime.start()
        runtime.attach_target("page-a", "cdp-a", url="http://a.test")
        runtime.attach_target("page-b", "cdp-b", url="http://b.test")
        runtime.focus("page-b")
        discovery.targets = [item for item in discovery.targets if item.target_id != "page-b"]
        runtime.detach_target("page-b", reason="test-drop")
        self.assertEqual(runtime.active_target_id, "page-a")
        with self.assertRaises(BrowserTargetDetached):
            runtime.cdp_session("page-b")

        empty = _Discovery([])
        fallback = BrowserTargetRuntime("session-empty", bus, lambda: empty)
        barrier = threading.Barrier(3)
        results: list[str] = []
        errors: list[BaseException] = []

        def recover() -> None:
            try:
                barrier.wait()
                results.append(fallback.ensure_valid_focus().target_id)
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=recover), threading.Thread(target=recover)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)
        bus.stop()
        self.assertFalse(errors)
        self.assertEqual(results, ["blank-1", "blank-1"])
        self.assertEqual(empty.created, 1)
        with self.assertRaises(BrowserTargetError):
            BrowserTargetRuntime("disabled", BrowserEventBus(), lambda: empty, disabled=True).start()

    def test_browser_session_idempotency_event_restart_and_124_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _CdpFixture() as endpoint:
            root = Path(directory)
            config = BrowserRuntimeConfig(
                root / "state", root / "runtime", root / "artifacts",
                request_timeout_seconds=0.2,
                reconnect_delays=(1.0, 2.0, 4.0),
                max_reconnect_attempts=4,
            )
            runtime = BrowserRuntime(config)
            command = _command(root, endpoint.url)
            first = runtime.start(command)
            replay = runtime.start(command)
            self.assertTrue(first.ok)
            self.assertTrue(first.created)
            self.assertTrue(replay.reused)
            self.assertEqual(first.session.session_id, replay.session.session_id)
            self.assertEqual(len(runtime.state_store.list_events(first.session.session_id)), 1)

            class _Snapshot:
                status = BrowserConnectionStatus.OPEN
                def to_dict(self) -> dict[str, object]:
                    return {"status": "open"}

            class _Flaky:
                def __init__(self) -> None:
                    self.calls = 0
                def reconnect(self) -> int:
                    self.calls += 1
                    if self.calls < 4:
                        raise RuntimeError("drop")
                    return self.calls
                def snapshot(self) -> _Snapshot:
                    return _Snapshot()
                def close(self, **_kwargs: object) -> None:
                    return

            flaky = _Flaky()
            runtime._runtime._cdp[first.session.session_id] = flaky  # type: ignore[assignment,attr-defined]
            delays: list[float] = []
            with patch("zyra_workers.browser_session.session_runtime.time.sleep", side_effect=lambda value: delays.append(value)):
                reconnected = runtime.reconnect(first.session.session_id)
            self.assertTrue(reconnected.reconnected)
            self.assertEqual(delays, [1.0, 2.0, 4.0])
            stopped = runtime.stop(first.session.session_id)
            self.assertTrue(stopped.ok)
            restarted = runtime.start(_command(root, endpoint.url, request_id="request-2"))
            self.assertTrue(restarted.reconnected)
            self.assertEqual(restarted.session.session_id, first.session.session_id)
            self.assertEqual(runtime.diagnose(restarted.session.session_id).event_bus_generation, 1)
            runtime.stop(restarted.session.session_id)

    def test_disabled_supervisor_never_creates_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = BrowserRuntime(BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts", disabled=True))
            with self.assertRaises(BrowserRuntimeDisabled):
                runtime.start(_command(root, "http://127.0.0.1:9222"))
            self.assertEqual(runtime.state_store.list_sessions(), ())


if __name__ == "__main__":
    unittest.main()
