from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "scheduler",
    ROOT / "packages" / "symbolic",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType, create_task_state
from zyra_orchestration.topology_policy.contracts import canonical_digest
from zyra_scheduler import (
    RecoveryPlanner,
    ResourceLocation,
    ResourceScheduler,
    RuntimeWatchdog,
    WorkerBackendKind,
    WorkerManifest,
    WorkerPool,
    source_to_target_ledger,
)


class SchedulerTests(unittest.TestCase):
    def test_default_worker_pool_contains_local_edge_cloud_and_memory_manifests(self) -> None:
        pool = WorkerPool()
        manifest_ids = {manifest.worker_id for manifest in pool.manifests()}

        self.assertIn("provider-code-worker", manifest_ids)
        self.assertIn("edge-browser-worker", manifest_ids)
        self.assertIn("cloud-planner-verifier", manifest_ids)
        self.assertIn("local-memory-curator", manifest_ids)
        self.assertTrue(any(entry["source_repo"] == "claude-code-best" for entry in source_to_target_ledger()))

    def test_scheduler_selects_edge_browser_for_url_and_records_model_split(self) -> None:
        state = create_task_state("Open https://example.com and extract DOM evidence.")
        decision = ResourceScheduler().decide(state)

        self.assertEqual(decision.selected_manifest_id, "edge-browser-worker")
        self.assertEqual(decision.selected_worker, "BrowserWorker")
        self.assertEqual(decision.model_split["strategy"], "edge_browser_local_trace")
        self.assertGreater(decision.signals.contains_url, 0)

    def test_failure_memory_penalizes_failed_browser_and_reroutes_recovery(self) -> None:
        state = create_task_state("Recover browser evidence after browser timeout.")
        state.metadata["failure_injections"] = [
            {"text": "BrowserWorker timeout and browser crash", "target_node_id": state.root_node_id}
        ]
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.FAILURE_INJECTED,
            node_id=state.root_node_id,
            payload={"raw": "BrowserWorker timeout"},
        )

        signal = RuntimeWatchdog().classify(state, event=event)
        plan = RecoveryPlanner().plan(state, signal, node=state.plan_nodes[state.root_node_id], cause_event=event)

        self.assertEqual(signal.failed_worker, "BrowserWorker")
        self.assertNotEqual(plan.selected_manifest_id, "edge-browser-worker")
        self.assertTrue(state.metadata["recovery_plans"])
        self.assertEqual(state.metadata["last_resource_decision"]["selected_manifest_id"], plan.selected_manifest_id)

    def test_operator_candidate_ignores_infrastructure_endpoint_and_credential_metadata(self) -> None:
        state = create_task_state("测试，收到请回复 ok")
        scheduler = ResourceScheduler(
            WorkerPool(
                [
                    WorkerManifest(
                        worker_id="code-worker",
                        display_name="Code worker",
                        runtime_worker="CodeWorkerRuntime",
                        location=ResourceLocation.CLOUD,
                        backend=WorkerBackendKind.CLOUD_MODEL,
                        capabilities=["agent_task", "provider-reasoning"],
                        tools=[],
                        models=["zhipu/glm-5.2"],
                        privacy_level="public_only",
                        max_concurrency=2,
                        latency_ms=50,
                    ),
                    WorkerManifest(
                        worker_id="memory-worker",
                        display_name="Memory worker",
                        runtime_worker="MemoryContinuityRuntime",
                        location=ResourceLocation.LOCAL,
                        backend=WorkerBackendKind.LOCAL_PROCESS,
                        capabilities=["agent_task", "memory-refresh"],
                        tools=[],
                        models=[],
                        privacy_level="sensitive_ok",
                        max_concurrency=2,
                        latency_ms=5,
                    ),
                ]
            )
        )
        candidate = {
            "operator_id": "worker:code-worker",
            "operator_ref": "worker:code-worker@1",
            "operator_type": "worker",
            "version": "1",
            "profile_digest": "d" * 64,
            "layer_index": 1,
            "layer_rank": 1,
            "required_permissions": ["worker.dispatch"],
            "allowed_locations": ["cloud"],
            "allowed_privacy_classes": ["internal", "project"],
            "health_status": "healthy",
            "available_capacity": 2,
            "capabilities": ["agent_task", "provider-reasoning"],
            "source_ref": "code-worker",
            "estimated_tokens": 128,
            "estimated_cost_usd": 0.001,
            "estimated_latency_ms": 50,
            "proposal_score": 8_000.0,
        }
        operator_input = {
            "schema_version": "zyra.operator-candidate-set/v1",
            "run_id": state.run_id,
            "task_id": state.task_id,
            "proposal_id": "proposal-provider-code-worker",
            "proposal_digest": "a" * 64,
            "catalog_digest": "b" * 64,
            "committed_graph_signature": "c" * 64,
            "placement_owner": "ResourceScheduler",
            "lease_owner": "WorkerPoolFoundationRuntime",
            "mode": "default",
            "allow_explicit_baseline": True,
            "allowed_permissions": ["worker.dispatch"],
            "privacy_class": "project",
            "expected_breadth": 1,
            "expected_depth": 1,
            "remaining_tokens": 2_000,
            "remaining_cost_usd": 1.0,
            "remaining_time_ms": 30_000,
            "candidates": [candidate],
        }
        operator_input["candidate_set_digest"] = canonical_digest(operator_input)

        decision = scheduler.decide(
            state,
            cause_event={
                "payload": {
                    "resource_decision": {
                        "endpoint": "http://127.0.0.1:8311",
                        "credential_version": 7,
                    }
                }
            },
            operator_input=operator_input,
        )

        self.assertFalse(decision.signals.contains_url)
        self.assertEqual(decision.signals.privacy_mode, "project")
        self.assertEqual(decision.signals.task_profile, "general")
        self.assertEqual(decision.selected_manifest_id, "code-worker")
        self.assertEqual(
            decision.metadata["operator_placement"]["route_mode"],
            "operator_constrained",
        )


if __name__ == "__main__":
    unittest.main()
