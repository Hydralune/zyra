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
from zyra_scheduler import (
    RecoveryPlanner,
    ResourceScheduler,
    RuntimeWatchdog,
    WorkerPool,
    source_to_target_ledger,
)


class SchedulerTests(unittest.TestCase):
    def test_default_worker_pool_contains_local_edge_cloud_and_memory_manifests(self) -> None:
        pool = WorkerPool()
        manifest_ids = {manifest.worker_id for manifest in pool.manifests()}

        self.assertIn("local-code-worker", manifest_ids)
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


if __name__ == "__main__":
    unittest.main()
