from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.browser_use import BrowserEventBus  # noqa: E402
from zyra_core import to_jsonable  # noqa: E402
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_runtime.permission.action_gate import BrowserActionPermissionGate  # noqa: E402
from zyra_runtime.permission.models import PermissionEffect, PermissionRequestRecord, PermissionResolutionResponse  # noqa: E402
from zyra_runtime.permission.request_queue import PermissionRequestQueue  # noqa: E402
from zyra_runtime.permission.store import PermissionStateStore  # noqa: E402
from zyra_workers.browser_session import (  # noqa: E402
    BrowserRuntimeConfig,
    BrowserSessionCommand,
    CdpRequestRuntime,
    JsonBrowserStateStore,
    MemoryCdpTransport,
)
from zyra_workers.browser_session.application import BrowserSessionApplication  # noqa: E402
from zyra_workers.browser_session.runtime_registry import BrowserRuntimeRegistry  # noqa: E402
from zyra_workers.browser_worker import BrowserWorkerRuntime  # noqa: E402


class _DiscoveryHandler(BaseHTTPRequestHandler):
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
        state["requests"].append((self.path, self.headers.get("Authorization", "")))
        if self.path == "/json/version":
            self._write(
                {
                    "Browser": "ZyraProductizedFixture/1",
                    "Protocol-Version": "1.3",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9/devtools/browser/productized",
                }
            )
            return
        if self.path == "/json/list":
            self._write(list(state["targets"]))
            return
        if self.path.startswith("/json/activate/"):
            self._write("Target activated")
            return
        if self.path.startswith("/json/new"):
            target = {"id": "blank-productized", "type": "page", "url": "about:blank", "title": ""}
            state["targets"].append(target)
            self._write(target)
            return
        self.send_error(404)

    do_PUT = do_GET


class _DiscoveryFixture:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _DiscoveryHandler)
        self.server.daemon_threads = True
        self.server.state = {  # type: ignore[attr-defined]
            "requests": [],
            "targets": [
                {
                    "id": "page-productized",
                    "type": "page",
                    "url": "about:blank",
                    "title": "Productized",
                }
            ],
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "_DiscoveryFixture":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class _CdpResponder:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.fail_after: int | None = None

    def __call__(self, raw: str) -> str | None:
        request = json.loads(raw)
        method = str(request.get("method") or "")
        self.methods.append(method)
        if self.fail_after is not None and len(self.methods) > self.fail_after:
            return None
        result: dict[str, object]
        if method == "Page.captureScreenshot":
            result = {"data": base64.b64encode(b"\x89PNG\r\n\x1a\nproductized").decode("ascii")}
        elif method == "Runtime.evaluate":
            expression = str(request.get("params", {}).get("expression") or "")
            value = (
                json.dumps({
                    "width": 1280, "height": 720, "dpr": 1,
                    "scrollX": 0, "scrollY": 0,
                    "documentWidth": 1280, "documentHeight": 1600,
                })
                if "documentWidth" in expression
                else "productized-evaluation"
            )
            result = {"result": {"type": "string", "value": value}}
        elif method == "DOM.getDocument":
            result = _dom_document()
        elif method == "DOMSnapshot.captureSnapshot":
            result = _dom_snapshot()
        elif method == "Accessibility.getFullAXTree":
            result = _ax_tree()
        elif method == "Page.getFrameTree":
            result = {
                "frameTree": {
                    "frame": {
                        "id": "frame-productized",
                        "loaderId": "loader-productized",
                        "url": "https://example.test/productized",
                        "securityOrigin": "https://example.test",
                    }
                }
            }
        elif method == "Target.getTargets":
            result = {
                "targetInfos": [
                    {
                        "targetId": "page-productized",
                        "type": "page",
                        "url": "about:blank",
                        "title": "Productized",
                    }
                ]
            }
        else:
            result = {"frameId": "frame-productized"}
        return json.dumps({"id": request["id"], "result": result})


def _dom_document() -> dict[str, object]:
    def node(node_id: int, backend_id: int, node_type: int, name: str, value: str = "", **extra: object) -> dict[str, object]:
        return {
            "nodeId": node_id,
            "backendNodeId": backend_id,
            "nodeType": node_type,
            "nodeName": name,
            "nodeValue": value,
            **extra,
        }

    return {"root": node(
        1, 1, 9, "#document",
        documentURL="https://example.test/productized",
        baseURL="https://example.test/productized",
        frameId="frame-productized",
        children=[node(
            2, 2, 1, "HTML", frameId="frame-productized", children=[node(
                3, 3, 1, "BODY", frameId="frame-productized", children=[
                    node(4, 4, 1, "H1", frameId="frame-productized", children=[
                        node(5, 5, 3, "#text", "Productized browser state", frameId="frame-productized"),
                    ]),
                    node(
                        6, 6, 1, "BUTTON", frameId="frame-productized",
                        attributes=["id", "continue", "aria-label", "Continue safely"],
                        children=[node(7, 7, 3, "#text", "Continue", frameId="frame-productized")],
                    ),
                ],
            )],
        )],
    )}


def _dom_snapshot() -> dict[str, object]:
    strings = ["frame-productized", "block", "visible", "1", "auto", "pointer"]
    styles = [[1, 2, 3, 4, 4, 5] for _ in range(7)]
    return {
        "strings": strings,
        "documents": [{
            "frameId": 0,
            "contentSize": {"width": 1280, "height": 1600},
            "scrollOffsetX": 0,
            "scrollOffsetY": 0,
            "computedStyleNames": ["display", "visibility", "opacity", "overflow", "overflow-y", "cursor"],
            "nodes": {
                "backendNodeId": [1, 2, 3, 4, 5, 6, 7],
                "isClickable": {"index": [5]},
            },
            "layout": {
                "nodeIndex": [0, 1, 2, 3, 4, 5, 6],
                "bounds": [
                    [0, 0, 1280, 1600], [0, 0, 1280, 1600], [0, 0, 1280, 1600],
                    [20, 20, 500, 50], [20, 20, 500, 50], [20, 100, 180, 44], [30, 110, 140, 24],
                ],
                "clientRects": [
                    [0, 0, 1280, 720], [0, 0, 1280, 720], [0, 0, 1280, 720],
                    [20, 20, 500, 50], [20, 20, 500, 50], [20, 100, 180, 44], [30, 110, 140, 24],
                ],
                "scrollRects": [[0, 0, 1280, 1600] for _ in range(7)],
                "paintOrders": list(range(7)),
                "styles": styles,
            },
        }],
    }


def _ax_tree() -> dict[str, object]:
    def ax(node_id: str, backend_id: int, role: str, name: str) -> dict[str, object]:
        return {
            "nodeId": node_id,
            "backendDOMNodeId": backend_id,
            "ignored": False,
            "role": {"type": "role", "value": role},
            "name": {"type": "computedString", "value": name},
            "properties": [],
        }

    return {"nodes": [
        ax("ax-document", 1, "RootWebArea", "Productized"),
        ax("ax-heading", 4, "heading", "Productized browser state"),
        ax("ax-button", 6, "button", "Continue safely"),
    ]}


def _command(root: Path, endpoint: str, request_id: str = "request-productized") -> BrowserSessionCommand:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return BrowserSessionCommand(
        run_id="run-productized",
        task_id="task-productized",
        worker_request_id=request_id,
        canonical_session_id="canonical-productized",
        node_id="browser-productized",
        workspace_root=workspace,
        artifact_root=root / "artifacts",
        endpoint_url=endpoint,
        headers={"Authorization": "Bearer productized-secret", "X-Zyra-Trace": "trace-04a-02"},
        keep_alive=True,
        constraints={"browser_transport": "memory"},
    )


def _request(root: Path) -> WorkerRequest:
    return WorkerRequest(
        run_id="run-productized",
        task_id="task-productized",
        node_id="browser-productized",
        worker_name="BrowserWorker",
        constraints={
            "browser_backend": "zyra-browser-productized",
            "permission_mode": "sealed",
            "allowed_schemes": ["file"],
            "permission_state_path": str(root / "permission-state.json"),
        },
    )


def _permission_gate(request: WorkerRequest, root: Path) -> BrowserActionPermissionGate:
    return BrowserActionPermissionGate.for_worker_request(
        request,
        workspace_root=root / "workspace",
        state_path=root / "permission-state.json",
    )


def _install_memory_cdp(runtime: object, session_id: str, responder: _CdpResponder) -> BrowserEventBus:
    bus = BrowserEventBus()
    bus.start()
    cdp = CdpRequestRuntime(
        session_id,
        0.08,
        bus,
        transport_factory=lambda: MemoryCdpTransport(responder),
    )
    cdp.connect()
    runtime._runtime._cdp[session_id] = cdp  # type: ignore[attr-defined]
    return bus


def _find_pending(value: object) -> PermissionRequestRecord | None:
    if isinstance(value, dict):
        pending = value.get("pending_request")
        if isinstance(pending, dict):
            return PermissionRequestRecord.from_dict(pending)
        for item in value.values():
            found = _find_pending(item)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _find_pending(item)
            if found is not None:
                return found
    return None


def _approve_exact(state_path: Path, record: PermissionRequestRecord) -> None:
    outcome = PermissionRequestQueue(PermissionStateStore(state_path), record.session_id).resolve(
        PermissionResolutionResponse(
            request_id=record.request_id,
            session_id=record.session_id,
            tool_use_id=record.tool_use_id,
            tool_identity=record.tool_identity,
            arguments_digest=record.arguments_digest,
            request_fingerprint=record.request_fingerprint,
            scope=record.scope,
            effect=PermissionEffect.ALLOW,
            actor_id="productized-browser-test-authority",
            expected_revision=record.revision,
            channel="test",
            reason="approve the exact productized browser action",
            idempotency_key=f"productized-browser:{record.request_id}",
        )
    )
    if not outcome.accepted:
        raise AssertionError(f"permission approval failed: {outcome.code}: {outcome.reason}")


class BrowserSessionProductizationIntegrationTests(unittest.TestCase):
    def test_registry_snapshot_evicts_entry_after_runtime_roots_are_removed(self) -> None:
        registry = BrowserRuntimeRegistry()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts")
            registry.get_or_create(config)
            before = registry.snapshot()
            self.assertEqual(before["entries"], 1)
            self.assertEqual(before["stale_evictions"], 0)
            runtime_key = before["runtimes"][0]["runtime_key"]

        after = registry.snapshot()

        self.assertEqual(after["entries"], 0)
        self.assertEqual(after["stale_evictions"], 1)
        self.assertEqual(after["runtimes"], [])
        self.assertEqual(after["stale_runtimes"][-1]["runtime_key"], runtime_key)
        self.assertIn("removed_runtime_roots", after["stale_runtimes"][-1]["reason"])

    def test_json_state_store_recreates_deleted_parent_tree_on_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "removed-parent" / "state"
            store = JsonBrowserStateStore(state_root)
            shutil.rmtree(state_root.parent)

            store.record_request("request-after-removal", "fingerprint-after-removal", {"ok": True})

            self.assertTrue(store.path.is_file())
            self.assertTrue(store.journal_path.is_file())
            self.assertEqual(store.snapshot().requests, 1)

    def test_registry_capture_reconnect_stop_and_resume_preserve_session_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            config = BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts")
            registry = BrowserRuntimeRegistry()
            runtime = registry.get_or_create(config)
            started = runtime.start(_command(root, discovery.endpoint, request_id="resume-start"))
            capsule = registry.capture_resume_capsule(config, started.session.session_id)
            reconnected = runtime.reconnect(started.session.session_id)
            stopped = runtime.stop(started.session.session_id, reason="resume-test-stop")
            resumed = registry.resume(
                config,
                started.session.session_id,
                expected_run_id=started.session.run_id,
                expected_task_id=started.session.task_id,
                worker_request_id="resume-request",
            )
            self.assertEqual(capsule.browser_session_id, started.session.session_id)
            self.assertTrue(reconnected.ok)
            self.assertTrue(stopped.ok)
            self.assertTrue(resumed.ok, resumed.error)
            self.assertEqual(resumed.session.session_id, started.session.session_id)
            self.assertGreaterEqual(resumed.session.revision, started.session.revision)
            self.assertGreaterEqual(runtime.diagnose(started.session.session_id).event_bus_generation, 1)
            runtime.stop(started.session.session_id, force=True, reason="resume-test-complete")

    def test_productized_exact_approval_executes_once_and_replay_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            permission_state = root / "permission-state.json"
            runtime = BrowserRuntimeRegistry().get_or_create(
                BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts")
            )
            worker = BrowserWorkerRuntime(
                project_root=ROOT, workspace_root=root / "workspace", artifact_root=root / "artifacts",
                permission_state_path=permission_state, browser_session_runtime=runtime,
            )
            base_constraints = {
                "browser_endpoint_url": discovery.endpoint,
                "browser_transport": "memory",
                "canonical_session_id": "canonical-productized-approval",
                "keep_alive": True,
                "browser_plan": [{"action": "navigate", "arguments": {"url": "https://example.test/exact"}}],
            }
            first = worker.run(WorkerRequest(
                run_id="run-productized-approval", task_id="task-productized-approval", node_id="node-browser",
                worker_name="BrowserWorker", constraints=base_constraints,
            ))
            self.assertFalse(first.worker_result.ok)
            self.assertEqual(first.worker_result.metadata["browser_permission_action_execution_count"], "0")
            pending = _find_pending([to_jsonable(event) for event in first.event_records])
            self.assertIsNotNone(pending)
            assert pending is not None
            _approve_exact(permission_state, pending)
            session_id = first.worker_result.metadata["browser_session_id"]
            runtime._runtime._cdp[session_id].close()
            responder = _CdpResponder()
            bus = _install_memory_cdp(runtime, session_id, responder)
            authority = {
                "permission_session_id": pending.session_id,
                "permission_session_custody_token": first.permission_session_custody_token,
            }
            try:
                approved = worker.run(WorkerRequest(
                    run_id="run-productized-approval", task_id="task-productized-approval", node_id="node-browser",
                    worker_name="BrowserWorker", constraints={**base_constraints, **authority},
                ))
                replay = worker.run(WorkerRequest(
                    run_id="run-productized-approval", task_id="task-productized-approval", node_id="node-browser",
                    worker_name="BrowserWorker", constraints={**base_constraints, **authority},
                ))
            finally:
                bus.stop()
                runtime.stop(session_id, force=True, reason="exact_approval_complete")
            self.assertTrue(approved.worker_result.ok, approved.worker_result.error)
            self.assertEqual(approved.worker_result.metadata["browser_permission_action_execution_count"], "1")
            self.assertEqual(responder.methods.count("Page.navigate"), 1)
            self.assertFalse(replay.worker_result.ok)
            self.assertEqual(responder.methods.count("Page.navigate"), 1)

    def test_productized_network_permission_blocks_before_cdp_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            runtime = BrowserRuntimeRegistry().get_or_create(
                BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts")
            )
            worker = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=root / "workspace",
                artifact_root=root / "artifacts",
                permission_state_path=root / "permission-state.json",
                browser_session_runtime=runtime,
            )
            run = worker.run(WorkerRequest(
                run_id="run-permission-productized",
                task_id="task-permission-productized",
                node_id="node-browser",
                worker_name="BrowserWorker",
                constraints={
                    "browser_endpoint_url": discovery.endpoint,
                    "browser_transport": "memory",
                    "keep_alive": True,
                    "browser_plan": [
                        {"action": "navigate", "arguments": {"url": "https://example.test/blocked"}},
                    ],
                },
            ))
            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["browser_backend"], "zyra-browser-productized")
            self.assertEqual(run.worker_result.metadata["browser_permission_action_execution_count"], "0")
            self.assertTrue(any(event.event_type == "recovery_planned" for event in run.event_records))
            session_id = run.worker_result.metadata["browser_session_id"]
            cdp = runtime._runtime._cdp[session_id]
            self.assertEqual(cdp.snapshot().completed_requests, 0)
            self.assertNotIn("static", json.dumps(to_jsonable(run.worker_result)).lower())
            runtime.stop(session_id, force=True, reason="permission_test_complete")

    def test_browser_worker_defaults_to_productized_and_reuses_keep_alive_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            runtime = BrowserRuntimeRegistry().get_or_create(
                BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts")
            )
            worker = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=root / "workspace",
                artifact_root=root / "artifacts",
                permission_state_path=root / "permission-state.json",
                browser_session_runtime=runtime,
            )
            constraints = {
                "browser_endpoint_url": discovery.endpoint,
                "browser_transport": "memory",
                "canonical_session_id": "canonical-worker-default",
                "keep_alive": True,
                "permission_mode": "sealed",
                "browser_plan": [
                    {"action": "list_targets", "arguments": {}},
                    {"action": "capture_trace", "arguments": {}},
                ],
            }
            prepared = runtime.ensure_started(BrowserSessionCommand(
                run_id="run-worker-default",
                task_id="task-worker-default",
                worker_request_id="request-worker-default-setup",
                canonical_session_id="canonical-worker-default",
                node_id="node-browser",
                workspace_root=root / "workspace",
                artifact_root=root / "artifacts",
                endpoint_url=discovery.endpoint,
                keep_alive=True,
                constraints={"browser_transport": "memory"},
            ))
            runtime._runtime._cdp[prepared.session.session_id].close()
            responder = _CdpResponder()
            bus = _install_memory_cdp(runtime, prepared.session.session_id, responder)
            try:
                first = worker.run(WorkerRequest(
                    run_id="run-worker-default", task_id="task-worker-default", node_id="node-browser",
                    worker_name="BrowserWorker", constraints=constraints,
                ))
                second = worker.run(WorkerRequest(
                    run_id="run-worker-default", task_id="task-worker-default", node_id="node-browser",
                    worker_name="BrowserWorker", constraints=constraints,
                ))
            finally:
                bus.stop()
            self.assertTrue(first.worker_result.ok, first.worker_result.error)
            self.assertTrue(second.worker_result.ok, second.worker_result.error)
            self.assertEqual(first.worker_result.metadata["browser_backend"], "zyra-browser-productized")
            self.assertEqual(
                first.worker_result.metadata["browser_session_id"],
                second.worker_result.metadata["browser_session_id"],
            )
            self.assertEqual(second.worker_result.metadata["browser_session_reused"], "true")
            self.assertTrue(any(artifact.kind == "trace" for artifact in first.worker_result.artifacts))
            cancel = worker.run(WorkerRequest(
                run_id="run-worker-default", task_id="task-worker-default", node_id="node-browser",
                worker_name="BrowserWorker", constraints={
                    "browser_lifecycle_command": "cancel",
                    "browser_session_id": first.worker_result.metadata["browser_session_id"],
                    "canonical_session_id": "canonical-worker-default",
                    "reason": "integration_cancel",
                },
            ))
            self.assertTrue(cancel.worker_result.ok, cancel.worker_result.error)
            self.assertEqual(cancel.worker_result.metadata["browser_lifecycle_command"], "cancel")

    def test_registry_application_executes_only_on_the_started_session_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            config = BrowserRuntimeConfig(
                root / "state",
                root / "runtime",
                root / "artifacts",
                request_timeout_seconds=0.08,
            )
            registry = BrowserRuntimeRegistry()
            runtime = registry.get_or_create(config)
            self.assertIs(runtime, registry.get_or_create(config))
            started = runtime.start(_command(root, discovery.endpoint))
            responder = _CdpResponder()
            bus = _install_memory_cdp(runtime, started.session.session_id, responder)
            page = root / "workspace" / "page.html"
            page.write_text("<html><body>productized</body></html>", encoding="utf-8")
            request = _request(root)
            application = BrowserSessionApplication(runtime)
            try:
                result = application.execute_plan(
                    request,
                    started,
                    [
                        {"action": "navigate", "arguments": {"url": page.resolve().as_uri()}},
                        {"action": "list_targets", "arguments": {}},
                        {"action": "take_screenshot", "arguments": {"name": "productized.png"}},
                    ],
                    _permission_gate(request, root),
                )
            finally:
                bus.stop()
                runtime.stop(started.session.session_id, reason="integration_complete")

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.metadata["browser_backend"], "zyra-browser-productized")
            self.assertEqual(result.metadata["browser_session_id"], started.session.session_id)
            self.assertEqual(result.metadata["browser_canonical_session_id"], "canonical-productized")
            self.assertEqual(len(result.action_receipts), 3)
            self.assertIn("Page.navigate", responder.methods)
            self.assertIn("Page.captureScreenshot", responder.methods)
            self.assertTrue(any(artifact.kind == "screenshot" for artifact in result.artifacts))
            projected = json.dumps(result.metadata, sort_keys=True)
            self.assertNotIn("productized-secret", projected)

    def test_disabled_application_and_disabled_cdp_fail_without_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            config = BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts")
            runtime = BrowserRuntimeRegistry().get_or_create(config)
            started = runtime.start(_command(root, discovery.endpoint))
            request = _request(root)
            gate = _permission_gate(request, root)
            disabled = BrowserSessionApplication(runtime, disabled=True).execute_plan(
                request,
                started,
                [{"action": "list_targets", "arguments": {}}],
                gate,
            )
            self.assertFalse(disabled.ok)
            self.assertEqual(disabled.error, "browser_session_application_disabled")
            self.assertEqual(disabled.action_receipts, ())
            self.assertFalse(disabled.artifacts)
            self.assertNotIn("static", json.dumps(disabled.metadata).lower())

            bus = BrowserEventBus()
            bus.start()
            runtime._runtime._cdp[started.session.session_id] = CdpRequestRuntime(  # type: ignore[attr-defined]
                started.session.session_id,
                0.05,
                bus,
                disabled=True,
            )
            try:
                failed = BrowserSessionApplication(runtime).execute_plan(
                    request,
                    started,
                    [{"action": "take_screenshot", "arguments": {}}],
                    gate,
                )
            finally:
                bus.stop()
                runtime.stop(started.session.session_id, reason="disabled_cdp_complete")
            self.assertFalse(failed.ok)
            self.assertIn(
                failed.error,
                {"browser_cdp_disabled", "browser_cdp_runtime_disabled", "browser_action_transport_unavailable"},
            )
            self.assertFalse(failed.artifacts)
            self.assertEqual(failed.metadata["browser_backend"], "zyra-browser-productized")

    def test_persisted_session_without_live_target_or_cdp_fails_explicitly_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            config = BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts")
            first_runtime = BrowserRuntimeRegistry().get_or_create(config)
            started = first_runtime.start(_command(root, discovery.endpoint))
            session_id = started.session.session_id
            first_runtime._runtime._cdp.pop(session_id, None)  # type: ignore[attr-defined]
            first_runtime._runtime._targets.pop(session_id, None)  # type: ignore[attr-defined]
            request = _request(root)

            resumed = BrowserSessionApplication(first_runtime).execute_plan(
                request,
                started,
                [{"action": "list_targets", "arguments": {}}],
                _permission_gate(request, root),
            )

            self.assertFalse(resumed.ok)
            self.assertIn(
                resumed.error,
                {"browser_session_state_lost", "browser_session_lease_unavailable", "browser_connection_lost"},
            )
            self.assertEqual(resumed.metadata["browser_session_id"], session_id)
            self.assertEqual(len(resumed.action_receipts), 1)
            self.assertFalse(resumed.action_receipts[0].ok)
            self.assertTrue(
                any(
                    "recovery" in json.dumps(to_jsonable(event), sort_keys=True).lower()
                    for event in resumed.events
                )
            )


if __name__ == "__main__":
    unittest.main()
