from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "scheduler",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "orchestration",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType, create_task_state
from zyra_orchestration import GraphExecutionContext, run_task_graph
from zyra_symbolic import apply_failure_injection, apply_requirement_change


class M5SchedulerFaultRecoveryScenarioTests(unittest.TestCase):
    def test_task_graph_uses_resource_scheduler_for_real_worker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            page = workspace / "page.html"
            page.write_text("<html><body>M5 scheduler browser evidence.</body></html>", encoding="utf-8")
            state = create_task_state(f"Open {page.resolve().as_uri()} and collect browser evidence.")
            events = run_task_graph(
                state,
                execution_context=GraphExecutionContext.from_paths(
                    project_root=ROOT,
                    workspace_root=workspace,
                    artifact_root=Path(tmpdir) / "artifacts",
                ),
            )

            resource_events = [event for event in events if event.event_type == EventType.RESOURCE_DECISION]
            execute_node = next(node for node in state.plan_nodes.values() if node.metadata.get("stage") == "execute")

            self.assertTrue(resource_events)
            self.assertEqual(execute_node.assigned_worker_id, "BrowserWorker")
            self.assertEqual(execute_node.metadata["worker_manifest_id"], "edge-browser-worker")
            self.assertEqual(execute_node.metadata["dispatch_envelope"]["manifest_id"], "edge-browser-worker")
            self.assertTrue(any("browser_result" in event.payload for event in events))

    def test_requirement_change_and_fault_injection_create_scheduler_and_recovery_state(self) -> None:
        state = create_task_state("Run a long task that must adapt to injected failures.")
        run_events = run_task_graph(state)
        change_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.REQUIREMENT_CHANGE,
            node_id=state.root_node_id,
            payload={"raw": "During the same run, require extra verification evidence."},
        )
        failure_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.FAILURE_INJECTED,
            node_id=state.root_node_id,
            payload={"raw": "node=execute BrowserWorker timeout and node failure"},
        )

        change_events = apply_requirement_change(state, change_event)
        failure_events = apply_failure_injection(state, failure_event)

        self.assertTrue(run_events)
        self.assertTrue(any(event.event_type == EventType.RESOURCE_DECISION for event in change_events + failure_events))
        self.assertTrue(any(event.event_type == EventType.RECOVERY_PLANNED for event in failure_events))
        self.assertTrue(state.metadata["resource_decisions"])
        self.assertTrue(state.metadata["recovery_plans"])
        self.assertIn("recovery_plan_id", state.metadata["failure_injections"][-1])


if __name__ == "__main__":
    unittest.main()
