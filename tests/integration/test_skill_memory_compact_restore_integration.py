from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

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
from zyra_workers.browser_session import BrowserRuntimeConfig, BrowserSessionCommand  # noqa: E402
from zyra_workers.browser_session.runtime_registry import BrowserRuntimeRegistry  # noqa: E402
from zyra_workers.browser_worker import BrowserWorkerRuntime  # noqa: E402
from zyra_workers.skill_memory_context import (  # noqa: E402
    BrowserSkillMemoryDeliveryConflict,
    BrowserSkillMemoryContextRuntime,
    BrowserSkillMemoryProjectionCorrupt,
    BrowserSkillMemoryToolScopeWidened,
    stable_digest,
)

from tests.integration.test_browser_session_productization_integration import (  # noqa: E402
    _CdpResponder,
    _DiscoveryFixture,
    _install_memory_cdp,
)


@dataclass(frozen=True)
class _Block:
    block_id: str


class _ContextWindow:
    def __init__(self) -> None:
        self.messages: list[Mapping[str, Any]] = []

    def seed_request_messages(self, messages: Any) -> tuple[_Block, ...]:
        values = tuple(dict(item) for item in messages)
        self.messages.extend(values)
        return tuple(_Block(f"skill-memory-block-{index}") for index, _ in enumerate(values, 1))


def _projection(
    *,
    run_id: str = "run-06c02-browser",
    task_id: str = "task-06c02-browser",
    session_id: str = "session-06c02-browser",
    allowed_tools: tuple[str, ...] = ("list_targets",),
    provider_switched: bool = False,
) -> dict[str, Any]:
    attachment_content = "Validated procedure and evidence reference; 中文 ✓ α≤β."
    unsigned: dict[str, Any] = {
        "projectionId": "browser-context-projection-06c02",
        "protocol": "zyra.browser-skill-memory-context/v1",
        "identity": {
            "runId": run_id,
            "taskId": task_id,
            "sessionId": session_id,
            "workerRequestId": "code-worker-request-06c02",
            "epoch": 4,
        },
        "boundaryId": "compact-boundary-06c02",
        "compactProjectionId": "compact-projection-06c02",
        "contextEpoch": 2,
        "preparationId": "preparation-06c02",
        "retrievalReceiptId": "retrieval-receipt-06c02",
        "fidelityComparisonId": "fidelity-comparison-06c02",
        "selectedStrategy": "extractive_artifact_refs",
        "summary": "Continue the browser task with the restored constraint and evidence.",
        "memoryIds": ["skill-memory-06c02"],
        "procedureIds": ["procedure-browser-review"],
        "evidenceIds": ["evidence-browser-06c02"],
        "artifactIds": ["artifact-browser-06c02"],
        "authoritySkillIds": ["repository-review"],
        "allowedTools": sorted(allowed_tools),
        "deniedTools": ["evaluate_js"],
        "providerId": "compatible",
        "modelId": "browser-compatible",
        "providerMessage": {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Restored browser goal, constraint, evidence and procedure references.",
                }
            ],
            "metadata": {
                "fallback_reasons": ["provider_switched"] if provider_switched else [],
                "fallback_strategy": (
                    "extractive_artifact_refs" if provider_switched else ""
                ),
                "provider_switched": provider_switched,
                "baseline_text_ref_available": True,
            },
        },
        "attachments": [
            {
                "candidateId": "procedure:procedure-browser-review",
                "kind": "plan",
                "name": "Browser review procedure",
                "sourceId": "procedure-browser-review",
                "sourceDigest": stable_digest(attachment_content),
                "content": attachment_content,
                "tokenEstimate": 32,
                "selected": True,
                "required": False,
                "metadata": {
                    "source_projection": "06B ReusableProcedureRuntime.context",
                    "context_only": True,
                },
            }
        ],
        "currentAuthorityRequired": True,
        "executableSkillBodyPresent": False,
        "createdAt": "2026-07-21T10:10:00.000Z",
        "metadata": {
            "source_worker_request_id": "code-worker-request-06c02",
            "source_epoch": 4,
            "canonical_skill_owner": "03C SkillCoordinator",
            "canonical_compact_owner": "02D ContextCompactionRuntime",
        },
    }
    return {**unsigned, "projectionDigest": stable_digest(unsigned)}


def _request(
    projection: Mapping[str, Any],
    *,
    browser_plan: tuple[Mapping[str, Any], ...] = ({"action": "list_targets"},),
) -> WorkerRequest:
    identity = projection["identity"]
    return WorkerRequest(
        run_id=str(identity["runId"]),
        task_id=str(identity["taskId"]),
        node_id="browser-node-06c02",
        worker_name="BrowserWorker",
        request_id="browser-worker-request-06c02",
        constraints={
            "canonical_session_id": str(identity["sessionId"]),
            "session_id": str(identity["sessionId"]),
            "browser_plan": list(browser_plan),
            "browser_allowed_tools": [
                str(item.get("action") or "") for item in browser_plan
            ],
            "browser_denied_tools": ["evaluate_js"],
            "skill_memory_restore": dict(projection),
        },
    )


class BrowserSkillMemoryContextTests(unittest.TestCase):
    def test_projection_changes_context_and_commits_durable_delivery(self) -> None:
        projection = _projection(provider_switched=True)
        request = _request(projection)
        window = _ContextWindow()
        runtime = BrowserSkillMemoryContextRuntime()

        preparation = runtime.prepare(request, context_window=window)
        self.assertIsNotNone(preparation)
        assert preparation is not None
        self.assertEqual(len(window.messages), 1)
        self.assertIn("Restored browser goal", str(window.messages[0]))
        self.assertIn("procedure-browser-review", preparation.context_text)
        self.assertIn("中文 ✓ α≤β", preparation.context_text)
        self.assertEqual(len(preparation.fallback_events), 1)
        checkpoint, delivery, event = runtime.commit(
            request,
            preparation,
            terminal_event_ids=("browser-terminal-event",),
        )
        self.assertEqual(delivery.state.value, "applied")
        self.assertEqual(checkpoint.context_epoch, 2)
        self.assertIn(projection["projectionId"], checkpoint.applied_projection_ids)
        self.assertTrue(
            event.payload["skill_memory_browser_context"][
                "changes_browser_worker_context"
            ]
        )
        public = runtime.projection_for_result(preparation, checkpoint, delivery)
        self.assertTrue(public["applied"])
        self.assertFalse(public["executable_skill_body_present"])
        self.assertTrue(public["current_authority_required"])

        replay_window = _ContextWindow()
        replay = runtime.prepare(
            request,
            context_window=replay_window,
            checkpoint_value=checkpoint.to_dict(),
        )
        self.assertIsNotNone(replay)
        assert replay is not None
        self.assertTrue(replay.replayed)
        self.assertEqual(replay_window.messages, [])
        replay_request = replace(
            request,
            request_id="browser-worker-request-06c02-replayed-failure",
        )
        replayed_delivery, replayed_event = runtime.release(
            replay_request,
            replay,
            reason="later browser attempt failed after projection was already applied",
        )
        self.assertEqual(replayed_delivery.state.value, "applied")
        self.assertEqual(
            replayed_event.payload["skill_memory_browser_context"]["replayed"],
            True,
        )

    def test_corruption_and_tool_scope_widening_fail_before_context_mutation(self) -> None:
        corrupt = _projection()
        corrupt["summary"] = "tampered after TypeScript export"
        window = _ContextWindow()
        runtime = BrowserSkillMemoryContextRuntime()
        with self.assertRaises(BrowserSkillMemoryProjectionCorrupt):
            runtime.prepare(_request(corrupt), context_window=window)
        self.assertEqual(window.messages, [])

        widened = _projection(allowed_tools=("list_targets", "capture_trace"))
        request = _request(widened, browser_plan=({"action": "list_targets"},))
        with self.assertRaises(BrowserSkillMemoryToolScopeWidened):
            runtime.prepare(request, context_window=_ContextWindow())

    def test_disable_switch_removes_06c_projection_but_leaves_browser_request_usable(self) -> None:
        runtime = BrowserSkillMemoryContextRuntime(disabled=True)
        window = _ContextWindow()
        preparation = runtime.prepare(
            _request(_projection()),
            context_window=window,
        )
        self.assertIsNone(preparation)
        self.assertEqual(window.messages, [])
        self.assertTrue(runtime.health()["disabled"])

    def test_active_delivery_is_fenced_and_released_projection_can_retry(self) -> None:
        projection = _projection()
        first_request = _request(projection)
        runtime = BrowserSkillMemoryContextRuntime()
        first_window = _ContextWindow()
        first = runtime.prepare(first_request, context_window=first_window)
        self.assertIsNotNone(first)
        assert first is not None

        second_request = replace(first_request, request_id="browser-worker-request-06c02-retry")
        conflicting_window = _ContextWindow()
        with self.assertRaises(BrowserSkillMemoryDeliveryConflict):
            runtime.prepare(second_request, context_window=conflicting_window)
        self.assertEqual(conflicting_window.messages, [])
        with self.assertRaises(BrowserSkillMemoryDeliveryConflict):
            runtime.release(
                second_request,
                first,
                reason="unrelated worker must not release active delivery",
            )

        released, _event = runtime.release(
            first_request,
            first,
            reason="browser action failed before context checkpoint commit",
        )
        self.assertEqual(released.state.value, "released")

        retry_window = _ContextWindow()
        retry = runtime.prepare(second_request, context_window=retry_window)
        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(len(retry_window.messages), 1)
        checkpoint, delivery, _event = runtime.commit(second_request, retry)
        self.assertEqual(delivery.state.value, "applied")
        self.assertEqual(delivery.worker_request_id, second_request.request_id)
        self.assertIn(projection["projectionId"], checkpoint.applied_projection_ids)


class BrowserWorkerSkillMemoryMainPathTests(unittest.TestCase):
    def test_productized_browser_worker_consumes_codeworker_restore_projection(self) -> None:
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
            session_id = "session-06c02-browser"
            prepared = runtime.ensure_started(
                BrowserSessionCommand(
                    run_id="run-06c02-browser",
                    task_id="task-06c02-browser",
                    worker_request_id="browser-setup-06c02",
                    canonical_session_id=session_id,
                    node_id="browser-node-06c02",
                    workspace_root=root / "workspace",
                    artifact_root=root / "artifacts",
                    endpoint_url=discovery.endpoint,
                    keep_alive=True,
                    constraints={"browser_transport": "memory"},
                )
            )
            runtime._runtime._cdp[prepared.session.session_id].close()
            responder = _CdpResponder()
            bus = _install_memory_cdp(runtime, prepared.session.session_id, responder)
            # A default CodeWorker export restores context but no Browser tool
            # authority. The productized Browser permission runtime still
            # decides list_targets/capture_trace for this request.
            projection = _projection(allowed_tools=())
            request = WorkerRequest(
                run_id="run-06c02-browser",
                task_id="task-06c02-browser",
                node_id="browser-node-06c02",
                worker_name="BrowserWorker",
                request_id="browser-worker-request-06c02",
                constraints={
                    "browser_endpoint_url": discovery.endpoint,
                    "browser_transport": "memory",
                    "browser_backend": "zyra-browser-productized",
                    "canonical_session_id": session_id,
                    "session_id": session_id,
                    "permission_mode": "sealed",
                    "permission_session_id": "permission-06c02",
                    "keep_alive": True,
                    "goal": "continue with restored evidence",
                    "browser_allowed_tools": ["list_targets", "capture_trace"],
                    "browser_denied_tools": ["evaluate_js"],
                    "skill_memory_restore": projection,
                    "browser_plan": [
                        {"action": "list_targets", "arguments": {}},
                        {
                            "action": "capture_trace",
                            "arguments": {"marker": "skill-memory-06c02"},
                        },
                    ],
                },
            )
            try:
                result = worker.run(request)
            finally:
                bus.stop()
                runtime.stop_all(force=True, reason="06c02_test_complete")

            self.assertTrue(result.worker_result.ok, result.worker_result.error)
            self.assertEqual(
                result.worker_result.metadata["skill_memory_browser_context_applied"],
                "true",
            )
            self.assertEqual(
                result.worker_result.metadata[
                    "skill_memory_browser_context_projection_id"
                ],
                projection["projectionId"],
            )
            self.assertTrue(result.skill_memory_context_projection["applied"])
            self.assertEqual(
                result.skill_memory_context_checkpoint["context_epoch"],
                projection["contextEpoch"],
            )
            events = [item.payload for item in result.event_records]
            deliveries = [
                item["skill_memory_browser_context"]
                for item in events
                if "skill_memory_browser_context" in item
            ]
            self.assertEqual(len(deliveries), 1)
            self.assertTrue(deliveries[0]["changes_browser_worker_context"])
            self.assertFalse(deliveries[0]["executable_skill_body_present"])
            self.assertIn("DOM.getDocument", responder.methods)


if __name__ == "__main__":
    unittest.main()
