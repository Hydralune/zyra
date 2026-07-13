from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for package_path in sorted((ROOT / "packages").iterdir()):
    if package_path.is_dir() and str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType  # noqa: E402
from zyra_runtime import LocalArtifactStore, WorkerRequest  # noqa: E402
from zyra_workers.browser_context.action_envelope import (  # noqa: E402
    BrowserActionEnvelopeNormalizer,
    BrowserActionEnvelopeStatus,
    BrowserActionOutputExternalizer,
)
from zyra_workers.browser_context.action_result import BrowserActionResultProjector  # noqa: E402
from zyra_workers.browser_context.api_projection import BrowserContextApiProjectionRuntime  # noqa: E402
from zyra_workers.browser_context.task_integration import (  # noqa: E402
    BrowserContextScope,
    BrowserContextScopeMismatch,
    BrowserContextTaskCheckpoint,
    BrowserContextTaskIntegrationRuntime,
    BrowserMemoryCandidateConsumerPort,
)
from zyra_workers.browser_session import (  # noqa: E402
    BrowserActionName,
    BrowserActionReceipt,
    BrowserActionStatus,
    BrowserRuntimeConfig,
    BrowserSessionCommand,
)
from zyra_workers.browser_session.models import BrowserCdpSessionRef, BrowserTargetRef  # noqa: E402
from zyra_workers.browser_session.runtime_registry import BrowserRuntimeRegistry  # noqa: E402
from zyra_workers.browser_state.contracts import (  # noqa: E402
    BrowserDisclosureBudget,
    BrowserDomCaptureRequest,
    BrowserSelectorMapRevision,
    SelectorMapIdentity,
)
from zyra_workers.browser_state.errors import BrowserSelectorIdentityMismatch  # noqa: E402
from zyra_workers.browser_state.runtime import BrowserDomStateRuntime  # noqa: E402
from zyra_workers.browser_state.selector_probe import BrowserLiveSelectorProbeRuntime  # noqa: E402
from zyra_workers.browser_state.selector_store import BrowserSelectorMapStore  # noqa: E402
from zyra_workers.browser_worker import BrowserWorkerRuntime  # noqa: E402

from tests.integration.test_api_control_commands import _get, _post, _post_with_status  # noqa: E402
from tests.integration import test_browser_message_state_compression_foundation as _foundation_module  # noqa: E402
from tests.integration.test_browser_session_productization_integration import (  # noqa: E402
    _CdpResponder,
    _DiscoveryFixture,
    _ax_tree,
    _dom_document,
    _dom_snapshot,
    _install_memory_cdp,
)


def _empty_revision() -> BrowserSelectorMapRevision:
    return BrowserSelectorMapRevision(
        revision_id="revision-action-integration",
        revision=1,
        identity=SelectorMapIdentity(
            browser_session_id="browser-action-integration",
            target_id="target-action-integration",
            target_generation=1,
            cdp_session_id="cdp-action-integration",
            cdp_generation=1,
        ),
        capture_id="capture-action-integration",
        capture_digest="sha256:action-integration",
        entries=(),
    )


def _frame_dom(*, target: str, frame: str, base: int) -> dict[str, Any]:
    payload = copy.deepcopy(_dom_document())

    def rewrite(node: dict[str, Any]) -> None:
        old = int(node.get("backendNodeId") or 0)
        node["nodeId"] = old + base
        node["backendNodeId"] = old + base
        node["frameId"] = frame
        if node.get("documentURL"):
            node["documentURL"] = f"https://{target}.test/page"
            node["baseURL"] = f"https://{target}.test/page"
        for child in node.get("children", []):
            rewrite(child)
        for child in node.get("shadowRoots", []):
            rewrite(child)
        content = node.get("contentDocument")
        if isinstance(content, dict):
            rewrite(content)

    rewrite(payload["root"])
    return payload


def _frame_snapshot(*, frame: str, base: int) -> dict[str, Any]:
    payload = copy.deepcopy(_dom_snapshot())
    payload["strings"][0] = frame
    nodes = payload["documents"][0]["nodes"]
    nodes["backendNodeId"] = [int(value) + base for value in nodes["backendNodeId"]]
    return payload


def _frame_ax(*, base: int, prefix: str) -> dict[str, Any]:
    payload = copy.deepcopy(_ax_tree())
    for node in payload["nodes"]:
        node["nodeId"] = f"{prefix}-{node['nodeId']}"
        node["backendDOMNodeId"] = int(node["backendDOMNodeId"]) + base
    return payload


class _MultiFrameCdp:
    def __init__(self) -> None:
        self.generation = 1
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.object_by_backend: dict[int, str] = {}

    def snapshot(self) -> Any:
        return SimpleNamespace(generation=self.generation)

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        cdp_session_id: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        del timeout_seconds
        params = dict(params or {})
        self.calls.append((cdp_session_id, method, params))
        child = cdp_session_id == "cdp-oopif"
        base = 1000 if child else 0
        frame = "oopif-target" if child else "frame-root"
        if method == "DOM.getDocument":
            return _frame_dom(target="oopif" if child else "root", frame=frame, base=base)
        if method == "DOMSnapshot.captureSnapshot":
            return _frame_snapshot(frame=frame, base=base)
        if method == "Accessibility.getFullAXTree":
            return _frame_ax(base=base, prefix="child" if child else "root")
        if method == "Page.getFrameTree":
            if child:
                return {"frameTree": {"frame": {
                    "id": "oopif-target",
                    "loaderId": "loader-oopif",
                    "url": "https://oopif.test/page",
                    "securityOrigin": "https://oopif.test",
                }}}
            return {"frameTree": {
                "frame": {
                    "id": "frame-root",
                    "loaderId": "loader-root",
                    "url": "https://root.test/page",
                    "securityOrigin": "https://root.test",
                },
                "childFrames": [{"frame": {
                    "id": "oopif-target",
                    "parentId": "frame-root",
                    "loaderId": "loader-oopif",
                    "url": "https://oopif.test/page",
                    "securityOrigin": "https://oopif.test",
                }}],
            }}
        if method == "Runtime.evaluate":
            return {"result": {"type": "string", "value": json.dumps({
                "width": 1280,
                "height": 720,
                "dpr": 1,
                "scrollX": 0,
                "scrollY": 0,
                "documentWidth": 1280,
                "documentHeight": 1600,
            })}}
        if method == "DOM.pushNodesByBackendIdsToFrontend":
            backend = int(params["backendNodeIds"][0])
            return {"nodeIds": [backend + 20000]}
        if method == "DOM.resolveNode":
            backend = int(params["backendNodeId"])
            object_id = f"object:{cdp_session_id}:{backend}"
            self.object_by_backend[backend] = object_id
            return {"object": {"type": "object", "objectId": object_id}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "object", "value": {
                "tag": "button",
                "text": "Continue safely",
                "attributes": {"id": "continue", "data-testid": f"{cdp_session_id}-continue"},
            }}}
        return {}


class _MultiFrameTarget:
    def __init__(self) -> None:
        self.generation = 1
        self.active_target_id = "target-root"
        self.sessions = {
            "target-root": BrowserCdpSessionRef("cdp-root", "target-root", 1),
            "oopif-target": BrowserCdpSessionRef("cdp-oopif", "oopif-target", 1),
        }
        self.targets = {
            "target-root": BrowserTargetRef("target-root", "page", "https://root.test/page", "Root"),
            "oopif-target": BrowserTargetRef("oopif-target", "iframe", "https://oopif.test/page", "OOPIF"),
        }

    def snapshot(self) -> Any:
        return SimpleNamespace(
            generation=self.generation,
            targets=tuple(self.targets.values()),
            cdp_sessions=tuple(self.sessions.values()),
            active_target_id=self.active_target_id,
        )

    def ensure_valid_focus(self, *, timeout: float = 3.0) -> Any:
        del timeout
        return self.targets[self.active_target_id]

    def active_cdp_session(self, *, timeout: float = 2.0) -> Any:
        del timeout
        return self.sessions[self.active_target_id]

    def cdp_session(self, target_id: str) -> Any:
        return self.sessions[target_id]

    def target(self, target_id: str) -> Any:
        return self.targets.get(target_id)


class BrowserMessageStateCompressionIntegrationTests(unittest.TestCase):
    def test_action_envelope_normalizes_partial_malformed_conflict_and_externalizes_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalArtifactStore(Path(directory) / "artifacts")
            normalizer = BrowserActionEnvelopeNormalizer(
                externalizer=BrowserActionOutputExternalizer(store),
            )
            receipts = (
                BrowserActionReceipt(
                    receipt_id="receipt-typed",
                    request_id="request-typed",
                    request_fingerprint="sha256:typed",
                    browser_session_id="browser-action-integration",
                    action=BrowserActionName.LIST_TARGETS,
                    step_index=0,
                    status=BrowserActionStatus.SUCCEEDED,
                    started_at="2026-07-13T00:00:00Z",
                    completed_at="2026-07-13T00:00:01Z",
                    output={"targets": ["target-main"]},
                ),
                {
                    "receipt_id": "receipt-ok",
                    "request_id": "request-shared",
                    "request_fingerprint": "sha256:first",
                    "action": "evaluate_js",
                    "step_index": 0,
                    "ok": True,
                    "status": "succeeded",
                    "output": {"text": "x" * 4000},
                },
                {
                    "receipt_id": "receipt-partial",
                    "request_id": "request-partial",
                    "request_fingerprint": "sha256:partial",
                    "action": "extract",
                    "partial": True,
                    "final": False,
                    "content": [{"type": "text", "text": "partial result"}],
                },
                "malformed scalar result",
                {
                    "receipt_id": "receipt-conflict",
                    "request_id": "request-shared",
                    "request_fingerprint": "sha256:second",
                    "action": "evaluate_js",
                    "ok": True,
                    "output": {"value": 2},
                },
            )
            budget = BrowserDisclosureBudget(raw_externalize_bytes=256, max_action_result_tokens=128)
            batch = normalizer.normalize_many(
                receipts,
                run_id="run-action-integration",
                task_id="task-action-integration",
                node_id="node-action-integration",
                browser_session_id="browser-action-integration",
                worker_request_id="worker-action-integration",
                budget=budget,
            )
            self.assertEqual(len(batch.receipts), len(receipts))
            self.assertTrue(batch.pair_complete)
            self.assertEqual(batch.partial_count, 1)
            self.assertEqual(batch.malformed_count, 1)
            self.assertEqual(batch.conflict_count, 1)
            self.assertGreaterEqual(batch.externalized_count, 2)
            self.assertEqual(batch.receipts[0].status, BrowserActionEnvelopeStatus.SUCCEEDED)
            self.assertEqual(batch.receipts[0].raw_shape, "typed_object")
            self.assertEqual(batch.receipts[2].status, BrowserActionEnvelopeStatus.PARTIAL)
            self.assertFalse(batch.receipts[2].ok)
            self.assertEqual(batch.receipts[3].status, BrowserActionEnvelopeStatus.MALFORMED)
            self.assertEqual(batch.receipts[4].status, BrowserActionEnvelopeStatus.CONFLICT)
            for artifact in batch.artifacts:
                path = store.resolve_path(artifact.artifact)
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(payload["receipt_id"], artifact.receipt_id)
                self.assertTrue(artifact.verified)
            projections = BrowserActionResultProjector().project_many(
                batch.receipts,
                budget=budget,
                selector_revision=_empty_revision(),
                capture_id="capture-action-integration",
            )
            self.assertEqual(len(projections), len(receipts))
            self.assertTrue(all(item.tool_call_message.causation_id == item.receipt_id for item in projections))
            self.assertTrue(all(item.tool_result_message.causation_id == item.request_id for item in projections))

    def test_oopif_capture_merges_target_session_identity_and_live_selector_resolution(self) -> None:
        cdp = _MultiFrameCdp()
        target = _MultiFrameTarget()
        request = BrowserDomCaptureRequest(
            run_id="run-oopif",
            task_id="task-oopif",
            canonical_session_id="task:task-oopif",
            browser_session_id="browser-oopif",
            worker_request_id="worker-oopif",
            target_id="target-root",
            cdp_session_id="cdp-root",
            session_revision=1,
            target_generation=1,
            cdp_generation=1,
            node_id="node-oopif",
        )
        capture = BrowserDomStateRuntime(max_iframes=8).capture(
            request,
            cdp_runtime=cdp,
            target_runtime=target,
        )
        self.assertTrue(any(frame.oopif and frame.target_id == "oopif-target" for frame in capture.frames))
        self.assertGreater(capture.metrics.dom_nodes, 7)
        self.assertGreater(capture.metrics.ax_nodes, 3)
        self.assertIn("oopif-target", capture.raw_dom["oopif_targets"])
        with tempfile.TemporaryDirectory() as directory:
            selector_store = BrowserSelectorMapStore(Path(directory) / "selector-state")
            revision = selector_store.commit(capture)
            child_entries = [item for item in revision.entries if item.target_id == "oopif-target"]
            self.assertTrue(child_entries)
            self.assertTrue(all(item.cdp_session_id == "cdp-oopif" for item in child_entries))
            entry = next((item for item in child_entries if item.interactive), child_entries[0])
            probe = BrowserLiveSelectorProbeRuntime()
            before_calls = len(cdp.calls)
            receipt = probe.resolve(
                selector_store.resolve(
                    entry.ref.opaque_ref,
                    expected_identity=revision.identity,
                    require_current=True,
                ),
                cdp_runtime=cdp,
                target_runtime=target,
            )
            self.assertTrue(receipt.live)
            self.assertEqual(receipt.target_id, "oopif-target")
            self.assertEqual(receipt.cdp_session_id, "cdp-oopif")
            live_calls = cdp.calls[before_calls:]
            self.assertEqual(live_calls[0][0:2], ("cdp-oopif", "DOM.pushNodesByBackendIdsToFrontend"))
            self.assertEqual(live_calls[1][0:2], ("cdp-oopif", "DOM.resolveNode"))
            target.active_target_id = "another-target"
            calls_before_stale = len(cdp.calls)
            with self.assertRaises(BrowserSelectorIdentityMismatch):
                probe.resolve(
                    selector_store.resolve(
                        entry.ref.opaque_ref,
                        expected_identity=revision.identity,
                        require_current=True,
                    ),
                    cdp_runtime=cdp,
                    target_runtime=target,
                )
            self.assertEqual(len(cdp.calls), calls_before_stale)

    def test_checkpoint_restart_provider_selection_read_once_and_memory_consumer(self) -> None:
        foundation = _foundation_module.BrowserMessageStateCompressionFoundationTests(
            "test_default_worker_reaches_dom_selector_message_context_artifact_and_memory_chain"
        )
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            runtime, worker, _responder, bus = foundation._prepare(
                root,
                discovery,
                canonical_session_id="task:task-04b-foundation",
            )
            try:
                run = worker.run(foundation._request(
                    discovery,
                    canonical_session_id="task:task-04b-foundation",
                    marker="checkpoint-read-once",
                ))
            finally:
                bus.stop()
                runtime.stop_all(force=True, reason="04b_checkpoint_test")
            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            checkpoint = BrowserContextTaskCheckpoint.from_mapping(run.browser_context_checkpoint)
            self.assertEqual(checkpoint.pending_count, 1)
            self.assertEqual(checkpoint.consumed_count, 0)
            metadata = {"browser_context_runtime_state": checkpoint.to_dict()}
            integration = BrowserContextTaskIntegrationRuntime()
            batch = integration.prepare_delivery(
                metadata,
                scope=checkpoint.scope,
                consumer_worker_request_id="code-worker-next",
            )
            self.assertEqual(len(batch.messages), 1)
            source_id = batch.source_ids[0]
            provider_event = EventRecord(
                run_id=checkpoint.scope.run_id,
                task_id=checkpoint.scope.task_id,
                node_id="code-node",
                event_type=EventType.AGENT_MESSAGE,
                payload={"query_session": {
                    "phase": "model_stream_report",
                    "model_stream": {"envelope": {
                        "request_id": "provider-request",
                        "worker_request_id": "code-worker-next",
                        "turn_id": "turn-next",
                        "messages": list(batch.messages),
                    }},
                }},
            )
            selection = integration.verify_provider_selection(batch, [provider_event])
            self.assertTrue(selection.valid, selection.findings)
            consumed = integration.commit_delivery(
                metadata,
                batch=batch,
                worker_event_ids=[provider_event.event_id],
            )
            self.assertEqual(consumed.pending_count, 0)
            self.assertEqual(consumed.consumed_count, 1)
            restored = BrowserContextTaskCheckpoint.from_mapping(
                json.loads(json.dumps(metadata["browser_context_runtime_state"])),
                expected_scope=checkpoint.scope,
            )
            second = integration.prepare_delivery(
                metadata,
                scope=restored.scope,
                consumer_worker_request_id="code-worker-after-restart",
            )
            self.assertTrue(second.empty)
            self.assertNotIn(source_id, json.dumps(second.to_dict()))
            with self.assertRaises(BrowserContextScopeMismatch):
                BrowserContextTaskCheckpoint.from_mapping(
                    restored.to_dict(),
                    expected_scope=BrowserContextScope("other-run", restored.scope.task_id, restored.scope.session_id),
                )

            memory_events = [event for event in run.event_records if "browser_memory_candidate" in event.payload]
            self.assertTrue(memory_events)
            candidate = memory_events[0].payload["browser_memory_candidate"]
            receipt = BrowserMemoryCandidateConsumerPort().consume_event(
                memory_events[0],
                expected_run_id=checkpoint.scope.run_id,
                expected_task_id=checkpoint.scope.task_id,
                known_artifact_ids=run.worker_result.metadata and [item.artifact_id for item in run.worker_result.artifacts],
                known_capture_ids=[candidate["source_dom_capture_id"]],
                known_action_receipt_ids=[candidate["source_action_receipt_id"]] if candidate["source_action_receipt_id"] else [],
            )
            self.assertTrue(receipt.accepted_for_review)
            self.assertFalse(receipt.committed)

            projection = BrowserContextApiProjectionRuntime().build(
                restored,
                events=[*run.event_records, provider_event],
                task_artifact_ids=[item.artifact_id for item in run.worker_result.artifacts],
            )
            self.assertTrue(projection.ok, [item.to_dict() for item in projection.findings])
            self.assertTrue(all(item.atomic for item in projection.tool_pairs))

            # These are production dependency boundaries, not feature flags that
            # may silently fall back to the pre-04B path.  Disabling any consumer
            # must therefore fail closed instead of dropping context or evidence.
            with self.assertRaises(RuntimeError):
                BrowserContextTaskIntegrationRuntime(disabled=True).begin_browser_turn(
                    restored,
                    worker_request_id="disabled-context-consumer",
                )
            with self.assertRaises(RuntimeError):
                BrowserContextApiProjectionRuntime(disabled=True).build(restored)
            with self.assertRaises(RuntimeError):
                BrowserMemoryCandidateConsumerPort(disabled=True).consume_event(
                    memory_events[0],
                    expected_run_id=checkpoint.scope.run_id,
                    expected_task_id=checkpoint.scope.task_id,
                )

    def test_ablation_has_three_lanes_and_bitmap_is_default_off_non_authoritative(self) -> None:
        foundation = _foundation_module.BrowserMessageStateCompressionFoundationTests(
            "test_default_worker_reaches_dom_selector_message_context_artifact_and_memory_chain"
        )
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            runtime, worker, _responder, bus = foundation._prepare(
                root,
                discovery,
                canonical_session_id="canonical-ablation",
            )
            request = foundation._request(
                discovery,
                canonical_session_id="canonical-ablation",
                marker="three-lane-ablation",
            )
            request.constraints["browser_context_ablation"] = True
            try:
                result = worker.run(request)
            finally:
                bus.stop()
                runtime.stop_all(force=True, reason="04b_ablation_test")
            self.assertTrue(result.worker_result.ok, result.worker_result.error)
            disclosure = next(
                event.payload for event in result.event_records
                if "browser_context_disclosure" in event.payload
            )
            report = disclosure["browser_context_ablation"]
            self.assertEqual(len(report["lanes"]), 3)
            by_lane = {lane["lane"]: lane for lane in report["lanes"]}
            self.assertEqual(report["default_lane"], "structured_disclosure")
            self.assertTrue(report["default_path_unchanged"])
            self.assertTrue(report["structured_wins_tokens"])
            self.assertTrue(by_lane["structured_disclosure"]["authoritative"])
            bitmap = by_lane["bitmap_frame_experimental"]
            self.assertFalse(bitmap["authoritative"])
            self.assertFalse(bitmap["ocr_available"])
            self.assertEqual(bitmap["executable_selector_coverage"], 0.0)
            self.assertTrue(any(item.title.startswith("Experimental browser bitmap") for item in result.worker_result.artifacts))

    def test_api_browser_checkpoint_is_delivered_to_real_02d_provider_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "provider-input.txt").write_text("provider input", encoding="utf-8")
            environment = {
                "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
                "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                "ZYRA_TOOL_WORKSPACE": str(workspace),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
            }
            previous = {key: os.environ.get(key) for key in environment}
            os.environ.update(environment)
            from apps.api.zyra_api import main as api_main

            server = ThreadingHTTPServer(("127.0.0.1", 0), api_main.ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            runtime = None
            bus = None
            try:
                created = _post(base_url, "/tasks", {"goal": "Carry browser context into 02D.", "auto_run": False})
                task = created["task"]
                session_id = f"task:{task['task_id']}"
                runtime = BrowserRuntimeRegistry().get_or_create(
                    BrowserRuntimeConfig(root / "browser-state", root / "browser-runtime", root / "artifacts")
                )
                worker = BrowserWorkerRuntime(
                    project_root=ROOT,
                    workspace_root=workspace,
                    artifact_root=root / "artifacts",
                    permission_state_path=root / "permission-state.json",
                    browser_session_runtime=runtime,
                )
                prepared = runtime.ensure_started(BrowserSessionCommand(
                    run_id=task["run_id"],
                    task_id=task["task_id"],
                    worker_request_id="api-browser-setup",
                    canonical_session_id=session_id,
                    node_id=task["root_node_id"],
                    workspace_root=workspace,
                    artifact_root=root / "artifacts",
                    endpoint_url=discovery.endpoint,
                    keep_alive=True,
                    constraints={"browser_transport": "memory"},
                ))
                runtime._runtime._cdp[prepared.session.session_id].close()
                responder = _CdpResponder()
                bus = _install_memory_cdp(runtime, prepared.session.session_id, responder)
                with patch.object(api_main, "get_browser_runtime_services", return_value=(runtime, worker)):
                    browser_status, browser = _post_with_status(
                        base_url,
                        f"/tasks/{task['task_id']}/workers/browser",
                        {
                            "browser_plan": [
                                {"action": "list_targets", "arguments": {}},
                                {"action": "capture_trace", "arguments": {"marker": "api-context"}},
                            ],
                            "constraints": {
                                "browser_endpoint_url": discovery.endpoint,
                                "browser_transport": "memory",
                                "browser_backend": "zyra-browser-productized",
                                "permission_mode": "sealed",
                                "keep_alive": True,
                            },
                        },
                        headers={"Idempotency-Key": "api-browser-context-once"},
                    )
                    self.assertEqual(browser_status, 201, browser)
                    self.assertEqual(browser["browser_context"]["pending_count"], 1)
                    code_status, code = _post_with_status(
                        base_url,
                        f"/tasks/{task['task_id']}/workers/code",
                        {"constraints": {"tool_plan": [{
                            "tool_name": "file_read",
                            "arguments": {"path": "provider-input.txt"},
                        }]}},
                    )
                    self.assertEqual(code_status, 201, code.get("browser_context_provider_selection"))
                    self.assertTrue(code["browser_context_provider_selection"]["valid"])
                    self.assertEqual(code["browser_context"]["pending_count"], 0)
                    self.assertEqual(code["browser_context"]["consumed_count"], 1)
                    source_id = code["browser_context_provider_selection"]["selected_source_ids"][0]
                    model_reports = [
                        event["payload"]["query_session"]["model_stream"]
                        for event in code["events"]
                        if event.get("payload", {}).get("query_session", {}).get("phase") == "model_stream_report"
                    ]
                    selected = [
                        message
                        for report in model_reports
                        for message in report["envelope"]["messages"]
                        if message.get("metadata", {}).get("browser_context_source_id") == source_id
                    ]
                    self.assertEqual(len(selected), 1)
                    second_status, second = _post_with_status(
                        base_url,
                        f"/tasks/{task['task_id']}/workers/code",
                        {"constraints": {"tool_plan": [{
                            "tool_name": "file_read",
                            "arguments": {"path": "provider-input.txt"},
                        }]}},
                    )
                    # The pre-existing 02D permission-session custody rule rejects a
                    # second unqualified request for the same session.  The 04B
                    # queue must nevertheless be empty before CodeWorker starts,
                    # proving the consumed disclosure is not replayed on failure.
                    self.assertEqual(second_status, 409, second.get("worker_result"))
                    self.assertEqual(second["worker_result"]["error"], "session_custody_required")
                    self.assertEqual(second["browser_context_delivery"]["source_ids"], [])
                    view = _get(base_url, f"/tasks/{task['task_id']}/browser-context")
                    self.assertTrue(view["projection"]["ok"])
                    self.assertEqual(view["projection"]["pending_count"], 0)
            finally:
                if bus is not None:
                    bus.stop()
                if runtime is not None:
                    runtime.stop_all(force=True, reason="04b_api_context_test")
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
