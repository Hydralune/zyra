from __future__ import annotations

import json
import sys
import tempfile
import unittest
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

from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_workers.browser_context.history import (  # noqa: E402
    BrowserHistoryNormalizer,
    BrowserHistoryPolicy,
)
from zyra_workers.browser_context.models import (  # noqa: E402
    BrowserMessageKind,
    BrowserMessagePart,
    BrowserMessageRole,
)
from zyra_workers.browser_context.turn_store import BrowserTurnProjectionStore  # noqa: E402
from zyra_workers.browser_session import BrowserRuntimeConfig, BrowserSessionCommand  # noqa: E402
from zyra_workers.browser_session.runtime_registry import BrowserRuntimeRegistry  # noqa: E402
from zyra_workers.browser_state.capture_policy import BrowserDomCapturePolicy  # noqa: E402
from zyra_workers.browser_state.contracts import BrowserDomCaptureRequest  # noqa: E402
from zyra_workers.browser_state.errors import (  # noqa: E402
    BrowserDomCaptureFailed,
    BrowserSelectorStale,
)
from zyra_workers.browser_worker import BrowserWorkerRuntime  # noqa: E402

from tests.integration.test_browser_session_productization_integration import (  # noqa: E402
    _CdpResponder,
    _DiscoveryFixture,
    _install_memory_cdp,
)


class BrowserMessageStateCompressionFoundationTests(unittest.TestCase):
    def _prepare(
        self,
        root: Path,
        discovery: _DiscoveryFixture,
        *,
        canonical_session_id: str,
    ) -> tuple[object, BrowserWorkerRuntime, object, object]:
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
        prepared = runtime.ensure_started(BrowserSessionCommand(
            run_id="run-04b-foundation",
            task_id="task-04b-foundation",
            worker_request_id="request-04b-setup",
            canonical_session_id=canonical_session_id,
            node_id="browser-04b",
            workspace_root=root / "workspace",
            artifact_root=root / "artifacts",
            endpoint_url=discovery.endpoint,
            keep_alive=True,
            constraints={"browser_transport": "memory"},
        ))
        runtime._runtime._cdp[prepared.session.session_id].close()
        responder = _CdpResponder()
        bus = _install_memory_cdp(runtime, prepared.session.session_id, responder)
        return runtime, worker, responder, bus

    @staticmethod
    def _request(
        discovery: _DiscoveryFixture,
        *,
        canonical_session_id: str,
        marker: str,
    ) -> WorkerRequest:
        return WorkerRequest(
            run_id="run-04b-foundation",
            task_id="task-04b-foundation",
            node_id="browser-04b",
            worker_name="BrowserWorker",
            constraints={
                "browser_endpoint_url": discovery.endpoint,
                "browser_transport": "memory",
                "browser_backend": "zyra-browser-productized",
                "canonical_session_id": canonical_session_id,
                "permission_mode": "sealed",
                "permission_session_id": f"permission-{canonical_session_id}",
                "keep_alive": True,
                "goal": f"inspect the productized state and continue safely {marker}",
                "browser_plan": [
                    {"action": "list_targets", "arguments": {}},
                    {"action": "capture_trace", "arguments": {"marker": marker}},
                ],
            },
        )

    def test_default_worker_reaches_dom_selector_message_context_artifact_and_memory_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            runtime, worker, responder, bus = self._prepare(
                root, discovery, canonical_session_id="canonical-04b-main-path"
            )
            request = self._request(
                discovery,
                canonical_session_id="canonical-04b-main-path",
                marker="first-read",
            )
            try:
                result = worker.run(request)
            finally:
                bus.stop()
                runtime.stop_all(force=True, reason="04b_main_path_complete")

            self.assertTrue(result.worker_result.ok, result.worker_result.error)
            self.assertIn("DOM.getDocument", responder.methods)
            self.assertIn("DOMSnapshot.captureSnapshot", responder.methods)
            self.assertIn("Accessibility.getFullAXTree", responder.methods)
            self.assertEqual(result.worker_result.metadata["browser_backend"], "zyra-browser-productized")
            self.assertTrue(result.worker_result.metadata["browser_dom_capture_id"])
            self.assertTrue(result.worker_result.metadata["browser_selector_revision_id"])
            self.assertTrue(result.worker_result.metadata["browser_context_disclosure_id"])
            self.assertTrue(result.worker_result.metadata["browser_context_receipt_id"])
            self.assertGreater(int(result.worker_result.metadata["browser_context_tokens"]), 0)
            self.assertGreater(int(result.worker_result.metadata["browser_memory_candidate_count"]), 0)
            self.assertGreaterEqual(int(result.worker_result.metadata["browser_state_artifact_count"]), 3)

            projected_events = [event.payload for event in result.event_records]
            context_events = [item for item in projected_events if "browser_context_disclosure" in item]
            self.assertEqual(len(context_events), 1)
            payload = context_events[0]
            self.assertTrue(payload["browser_next_context"]["accepted"])
            self.assertTrue(payload["browser_disclosure_fidelity"]["valid"])
            self.assertEqual(payload["browser_disclosure_fidelity"]["selector_fidelity"], 1.0)
            self.assertEqual(payload["browser_disclosure_fidelity"]["action_pair_fidelity"], 1.0)
            self.assertTrue(payload["browser_semantic_outline"]["sections"])
            self.assertIn("full_state_bytes", payload["browser_low_entropy_metrics"])
            self.assertIn("disclosure_bytes", payload["browser_low_entropy_metrics"])
            self.assertIn("artifact_offload_ratio", payload["browser_low_entropy_metrics"])

            artifact_ids = {item.artifact_id for item in result.worker_result.artifacts}
            disclosure_artifacts = set(payload["browser_context_disclosure"]["artifact_ids"])
            self.assertTrue(disclosure_artifacts)
            self.assertTrue(disclosure_artifacts <= artifact_ids)
            for artifact in result.worker_result.artifacts:
                self.assertTrue(worker.artifact_store.resolve_path(artifact).is_file())

            application = worker.browser_message_state_application
            session_id = result.worker_result.metadata["browser_session_id"]
            revision = application.selector_store.latest(session_id, "page-productized")
            self.assertTrue(revision.entries)
            resolution = application.selector_preflight(
                revision.entries[0].ref.opaque_ref,
                browser_session_id=session_id,
                target_id="page-productized",
            )
            self.assertTrue(resolution.current)
            self.assertEqual(resolution.revision.revision_id, revision.revision_id)
            snapshot = application.snapshot()
            self.assertEqual(snapshot["turn_store"]["records"], 1)
            self.assertEqual(snapshot["selector_store"]["resolutions"], 1)
            self.assertEqual(snapshot["watchdog"]["accepted"], 1)

    def test_second_read_reuses_previous_facts_and_projection_index_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            runtime, worker, _responder, bus = self._prepare(
                root, discovery, canonical_session_id="canonical-04b-history"
            )
            try:
                first = worker.run(self._request(
                    discovery,
                    canonical_session_id="canonical-04b-history",
                    marker="history-one",
                ))
                second_request = self._request(
                    discovery,
                    canonical_session_id="canonical-04b-history",
                    marker="history-two",
                )
                second_request.constraints["permission_session_custody_token"] = (
                    first.permission_session_custody_token
                )
                second = worker.run(second_request)
            finally:
                bus.stop()
                runtime.stop_all(force=True, reason="04b_history_complete")
            self.assertTrue(first.worker_result.ok, first.worker_result.error)
            self.assertTrue(second.worker_result.ok, second.worker_result.error)
            application = worker.browser_message_state_application
            store_snapshot = application.turn_store.snapshot().to_dict()
            self.assertEqual(store_snapshot["records"], 2)
            facts = application.turn_store.previous_facts("canonical-04b-history")
            self.assertIn("Productized browser state", facts)

            restored = BrowserTurnProjectionStore(application.turn_store.root)
            self.assertEqual(restored.snapshot().records, 2)
            history = restored.history("canonical-04b-history")
            self.assertEqual([item.sequence for item in history], [2, 1])
            self.assertTrue(all(item.read_once_consumed for item in history))
            self.assertNotEqual(history[0].worker_request_id, history[1].worker_request_id)
            self.assertEqual(restored.by_request(history[0].worker_request_id), history[0])
            self.assertEqual(restored.by_disclosure(history[0].disclosure_id), history[0])

    def test_stale_selector_is_rejected_and_disabled_selector_store_breaks_real_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
            root = Path(directory)
            runtime, worker, _responder, bus = self._prepare(
                root, discovery, canonical_session_id="canonical-04b-selector-disable"
            )
            try:
                first = worker.run(self._request(
                    discovery,
                    canonical_session_id="canonical-04b-selector-disable",
                    marker="selector-current",
                ))
                self.assertTrue(first.worker_result.ok, first.worker_result.error)
                application = worker.browser_message_state_application
                session_id = first.worker_result.metadata["browser_session_id"]
                revision = application.selector_store.latest(session_id, "page-productized")
                selector_ref = revision.entries[0].ref.opaque_ref
                application.selector_store.mark_stale(
                    session_id,
                    "page-productized",
                    reason="test_invalidation",
                    expected_revision_id=revision.revision_id,
                )
                with self.assertRaises(BrowserSelectorStale):
                    application.selector_preflight(
                        selector_ref,
                        browser_session_id=session_id,
                        target_id="page-productized",
                    )
                application.selector_store.disabled = True
                failed_request = self._request(
                    discovery,
                    canonical_session_id="canonical-04b-selector-disable",
                    marker="selector-disabled",
                )
                failed_request.constraints["permission_session_custody_token"] = (
                    first.permission_session_custody_token
                )
                failed = worker.run(failed_request)
            finally:
                bus.stop()
                runtime.stop_all(force=True, reason="04b_selector_disable_complete")
            self.assertFalse(failed.worker_result.ok)
            self.assertIn("BrowserSelectorStoreDisabled", failed.worker_result.error or "")
            self.assertEqual(failed.worker_result.metadata["browser_backend"], "zyra-browser-productized")
            self.assertNotIn("static", json.dumps(failed.worker_result.metadata).lower())

    def test_disabled_dom_compressor_externalizer_and_context_port_each_fail_closed(self) -> None:
        switches = (
            ("dom_runtime", lambda app: setattr(app.dom_runtime, "disabled", True), "BrowserDomCaptureDisabled"),
            ("watchdog", lambda app: setattr(app.watchdog, "disabled", True), "BrowserDomCaptureFailed"),
            ("message_manager", lambda app: setattr(app.message_manager, "disabled", True), "BrowserMessageManagerDisabled"),
            ("action_projector", lambda app: setattr(app.message_manager.action_projector, "disabled", True), "BrowserActionProjectionFailed"),
            ("compressor", lambda app: setattr(app.message_manager.compressor, "disabled", True), "BrowserStateBudgetExceeded"),
            ("externalizer", lambda app: setattr(app.message_manager.externalizer, "disabled", True), "BrowserStateExternalizationFailed"),
            ("fidelity", lambda app: setattr(app.message_manager.fidelity_auditor, "disabled", True), "RuntimeError"),
            ("history", lambda app: setattr(app.message_manager.history_normalizer, "disabled", True), "RuntimeError"),
            ("next_context", lambda app: setattr(app.message_manager.next_context, "disabled", True), "BrowserNextContextUnavailable"),
            ("turn_store", lambda app: setattr(app.turn_store, "disabled", True), "BrowserMessageManagerDisabled"),
        )
        for name, disable, expected in switches:
            with self.subTest(component=name):
                with tempfile.TemporaryDirectory() as directory, _DiscoveryFixture() as discovery:
                    root = Path(directory)
                    runtime, worker, _responder, bus = self._prepare(
                        root, discovery, canonical_session_id=f"canonical-04b-disabled-{name}"
                    )
                    disable(worker.browser_message_state_application)
                    try:
                        result = worker.run(self._request(
                            discovery,
                            canonical_session_id=f"canonical-04b-disabled-{name}",
                            marker=name,
                        ))
                    finally:
                        bus.stop()
                        runtime.stop_all(force=True, reason=f"04b_disabled_{name}_complete")
                    self.assertFalse(result.worker_result.ok)
                    self.assertIn(expected, result.worker_result.error or "")
                    self.assertEqual(result.worker_result.metadata["browser_backend"], "zyra-browser-productized")
                    self.assertNotIn("browser_context_disclosure_id", result.worker_result.metadata)

    def test_capture_policy_rejects_fail_open_privileged_scheme_and_oversized_state(self) -> None:
        policy = BrowserDomCapturePolicy(max_raw_bytes=512)
        request = BrowserDomCaptureRequest(
            run_id="run-policy",
            task_id="task-policy",
            canonical_session_id="canonical-policy",
            browser_session_id="browser-policy",
            worker_request_id="request-policy",
            target_id="target-policy",
            cdp_session_id="cdp-policy",
            session_revision=1,
            target_generation=1,
            cdp_generation=1,
            constraints={"browser_dom_fail_open": True},
        )
        denied = policy.admit_request(request)
        self.assertFalse(denied.allowed)
        with self.assertRaises(BrowserDomCaptureFailed):
            policy.require_allowed(denied)

        allowed_request = BrowserDomCaptureRequest(
            run_id=request.run_id,
            task_id=request.task_id,
            canonical_session_id=request.canonical_session_id,
            browser_session_id=request.browser_session_id,
            worker_request_id="request-policy-allowed",
            target_id=request.target_id,
            cdp_session_id=request.cdp_session_id,
            session_revision=1,
            target_generation=1,
            cdp_generation=1,
            constraints={},
        )
        response = policy.inspect_responses(
            allowed_request,
            dom={
                "root": {
                    "nodeId": 1, "backendNodeId": 1, "documentURL": "file:///secret",
                    "children": [{"nodeId": index, "backendNodeId": index} for index in range(2, 60)],
                }
            },
            ax={"nodes": [{"nodeId": "ax", "backendDOMNodeId": 1}]},
            snapshot={"strings": [], "documents": [{"nodes": {"backendNodeId": list(range(1, 60))}}]},
            frame_tree={"frameTree": {"frame": {"id": "frame", "url": "file:///secret"}}},
        )
        self.assertFalse(response.allowed)
        self.assertGreater(response.raw_bytes, 512)
        self.assertTrue(any("privileged" in item for item in response.reasons))
        with self.assertRaises(BrowserDomCaptureFailed):
            policy.require_allowed(response)

    def test_history_normalizer_keeps_tool_pairs_atomic_and_blocks_orphans(self) -> None:
        messages: list[BrowserMessagePart] = []
        for index in range(6):
            projection_id = f"projection-{index}"
            call = BrowserMessagePart(
                message_id=f"call-{index}",
                role=BrowserMessageRole.ASSISTANT,
                kind=BrowserMessageKind.ACTION_CALL,
                content=json.dumps({"tool": "browser.click", "index": index}),
                source_id=f"request-{index}",
                causation_id=f"receipt-{index}",
                priority=800,
                metadata={"projection_id": projection_id},
            )
            result = BrowserMessagePart(
                message_id=f"result-{index}",
                role=BrowserMessageRole.TOOL,
                kind=BrowserMessageKind.ACTION_RESULT,
                content=json.dumps({"ok": True, "index": index, "payload": "x" * 300}),
                source_id=f"receipt-{index}",
                causation_id=f"request-{index}",
                priority=800,
                metadata={"projection_id": projection_id, "ok": True},
            )
            messages.extend((call, result))
        normalizer = BrowserHistoryNormalizer()
        projection = normalizer.project(
            messages,
            policy=BrowserHistoryPolicy(
                max_tokens=700,
                max_messages=8,
                preserve_recent_pairs=3,
                preserve_failures=2,
                preserve_state_messages=1,
                max_message_tokens=160,
                summary_tokens=120,
            ),
        )
        self.assertLess(projection.output_messages, projection.input_messages)
        self.assertEqual(projection.orphan_calls, 0)
        self.assertEqual(projection.orphan_results, 0)
        self.assertGreaterEqual(projection.tool_pairs_output, 3)
        kinds = [item.kind for item in projection.messages]
        self.assertEqual(kinds.count(BrowserMessageKind.ACTION_CALL), kinds.count(BrowserMessageKind.ACTION_RESULT))
        with self.assertRaises(ValueError):
            normalizer.project(messages[:-1])


if __name__ == "__main__":
    unittest.main()
