from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in (ROOT / "packages" / "integrations", ROOT / "packages" / "workers"):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_workers.browser_session import (
    BrowserArtifactEventBridge,
    BrowserArtifactKind,
    BrowserArtifactError,
    BrowserLaunchFailed,
    BrowserPermissionControlBridge,
    BrowserPermissionEffect,
    BrowserPermissionRule,
    BrowserRuntime,
    BrowserRuntimeConfig,
    BrowserRuntimeDisabled,
    BrowserSessionCommand,
    JsonBrowserStateStore,
)


class _Handler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        type(self).requests.append((self.path, self.headers.get("Authorization", "")))
        if self.path == "/json/version":
            value: object = {"Browser": "ZyraFixture", "Protocol-Version": "1.3", "webSocketDebuggerUrl": "ws://127.0.0.1:9/devtools/browser/test"}
        elif self.path == "/json/list":
            value = [{"id": "page-main", "type": "page", "url": "http://fixture.local", "title": "Fixture"}]
        elif self.path.startswith("/json/activate/"):
            value = "Target activated"
        else:
            self.send_error(404)
            return
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BrowserSessionProductizationFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _command(self, root: Path) -> BrowserSessionCommand:
        workspace = root / "workspace"
        workspace.mkdir(exist_ok=True)
        return BrowserSessionCommand(
            run_id="run-productization",
            task_id="task-productization",
            worker_request_id="worker-request-productization",
            canonical_session_id="canonical-productization",
            node_id="browser-node",
            workspace_root=workspace,
            artifact_root=root / "artifacts",
            endpoint_url=self.endpoint,
            constraints={"browser_transport": "memory"},
            headers={"Authorization": "Bearer integration-secret", "X-Trace": "trace-visible"},
        )

    def test_public_runtime_facade_persists_causal_session_permission_event_and_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts", request_timeout_seconds=0.2)
            state = JsonBrowserStateStore(config.state_root)
            permission = BrowserPermissionControlBridge(sealed=True)
            permission.add_rule(BrowserPermissionRule("allow-lifecycle", action_pattern="session_*", effect=BrowserPermissionEffect.ALLOW, priority=100))
            artifacts = BrowserArtifactEventBridge(config.artifact_root, state_store=state)
            runtime = BrowserRuntime(config, state_store=state, permission_port=permission, artifact_port=artifacts)
            command = self._command(root)
            started = runtime.start(command)
            self.assertTrue(started.ok)
            self.assertEqual(started.session.run_id, command.run_id)
            self.assertEqual(started.session.task_id, command.task_id)
            self.assertEqual(started.session.active_target_id, "page-main")
            events = state.list_events(started.session.session_id)
            self.assertEqual(events[-1]["topic"], "browser.session.started")
            self.assertEqual(events[-1]["worker_request_id"], command.worker_request_id)
            receipt = artifacts.write_bytes(
                started.session.session_id,
                BrowserArtifactKind.SCREENSHOT,
                b"\x89PNG\r\n\x1a\nzyra",
                name="foundation.png",
                media_type="image/png",
                metadata={"run_id": command.run_id, "task_id": command.task_id, "target_id": started.session.active_target_id},
            )
            self.assertTrue(artifacts.verify(receipt))
            Path(receipt.uri).resolve().relative_to(config.artifact_root)
            serialized = json.dumps(runtime.snapshot(), sort_keys=True)
            self.assertNotIn("integration-secret", serialized)
            self.assertTrue(any(secret == "Bearer integration-secret" for _path, secret in _Handler.requests))
            self.assertEqual(permission.snapshot()["decisions"], 1)
            runtime.stop(started.session.session_id)
            self.assertEqual(state.get_session(started.session.session_id).status, "stopped")  # type: ignore[union-attr]

    def test_permission_deny_disabled_runtime_and_disabled_artifact_have_no_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts")
            state = JsonBrowserStateStore(config.state_root)
            deny = BrowserPermissionControlBridge(sealed=True, evaluator=lambda request: __import__(
                "zyra_workers.browser_session", fromlist=["BrowserPermissionDecision"]
            ).BrowserPermissionDecision(request.request_id, BrowserPermissionEffect.DENY, "integration deny"))
            runtime = BrowserRuntime(config, state_store=state, permission_port=deny)
            before = len(_Handler.requests)
            with self.assertRaises(BrowserLaunchFailed):
                runtime.start(self._command(root))
            self.assertEqual(len(_Handler.requests), before)
            self.assertEqual(runtime.list_sessions()[0].status, "failed")

            disabled_config = BrowserRuntimeConfig(root / "disabled-state", root / "disabled-runtime", root / "disabled-artifacts", disabled=True)
            disabled = BrowserRuntime(disabled_config)
            with self.assertRaises(BrowserRuntimeDisabled):
                disabled.start(self._command(root))
            self.assertEqual(disabled.state_store.list_sessions(), ())

            artifact_state = JsonBrowserStateStore(root / "artifact-state")
            broken_artifacts = BrowserArtifactEventBridge(root / "broken-artifacts", state_store=artifact_state, disabled=True)
            broken = BrowserRuntime(
                BrowserRuntimeConfig(root / "artifact-state", root / "artifact-runtime", root / "broken-artifacts"),
                state_store=artifact_state,
                artifact_port=broken_artifacts,
            )
            with self.assertRaises(BrowserArtifactError):
                broken.start(BrowserSessionCommand(
                    run_id="run-broken", task_id="task-broken", worker_request_id="request-broken",
                    canonical_session_id="canonical-broken", workspace_root=root / "workspace",
            artifact_root=root / "broken-artifacts", endpoint_url=self.endpoint,
            constraints={"browser_transport": "memory"},
                ))
            self.assertEqual(broken.list_sessions()[0].status, "failed")


if __name__ == "__main__":
    unittest.main()
