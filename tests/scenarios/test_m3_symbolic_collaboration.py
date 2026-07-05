from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "evaluation",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType, PlanNodeStatus, create_task_state, to_jsonable
from zyra_evaluation import evaluate_task_trace
from zyra_orchestration import GraphExecutionContext, run_task_graph
from zyra_symbolic import apply_failure_injection, apply_requirement_change


class M3SymbolicCollaborationScenario(unittest.TestCase):
    def test_graph_change_and_failure_injection_share_symbolic_control_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Implement a code change, verify it, and adapt to injected changes.")
            context = GraphExecutionContext.from_paths(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            events = [
                EventRecord(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    event_type=EventType.TASK_CREATED,
                    node_id=state.root_node_id,
                    payload={"task": to_jsonable(state)},
                )
            ]
            events.extend(run_task_graph(state, execution_context=context))

            self.assertEqual(state.status, PlanNodeStatus.COMPLETED)
            self.assertTrue(any(event.event_type == EventType.TOPOLOGY_ROUTE for event in events))
            self.assertTrue(any(event.event_type == EventType.CONSTRAINT_CHECK for event in events))
            self.assertTrue(any(event.event_type == EventType.AGENT_MESSAGE for event in events))
            self.assertTrue(any("tool_result" in event.payload for event in events))

            change_event = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.REQUIREMENT_CHANGE,
                node_id=state.root_node_id,
                payload={"raw": "add stricter verification evidence and preserve current artifacts"},
            )
            events.append(change_event)
            events.extend(apply_requirement_change(state, change_event))

            failure_event = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.FAILURE_INJECTED,
                node_id=state.root_node_id,
                payload={"raw": "node=execute transient tool timeout"},
            )
            events.append(failure_event)
            events.extend(apply_failure_injection(state, failure_event))

            self.assertEqual(state.status, PlanNodeStatus.NEEDS_REVISION)
            self.assertGreaterEqual(len(state.decisions), 3)
            self.assertTrue(state.metadata["requirement_changes"][0]["affected_node_ids"])
            self.assertIn(state.metadata["failure_injections"][0]["recovery_node_id"], state.plan_nodes)
            self.assertTrue(any(decision.decision_type == "requirement_change_replan" for decision in state.decisions))
            self.assertTrue(any(decision.decision_type == "failure_recovery_route" for decision in state.decisions))

            events.extend(run_task_graph(state, execution_context=context))
            self.assertEqual(state.status, PlanNodeStatus.COMPLETED)

            evaluation = evaluate_task_trace(to_jsonable(state), [to_jsonable(event) for event in events])
            self.assertGreaterEqual(evaluation["metrics"]["constraint_check_count"], 4)
            self.assertGreaterEqual(evaluation["metrics"]["topology_route_count"], 3)
            self.assertGreaterEqual(evaluation["metrics"]["structured_message_count"], 5)
            self.assertGreaterEqual(evaluation["metrics"]["replanned_node_count"], 1)


if __name__ == "__main__":
    unittest.main()
