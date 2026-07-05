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
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventType, PlanNodeStatus, create_task_state
from zyra_orchestration import GraphExecutionContext, ensure_default_graph, run_task_graph


class TaskGraphTests(unittest.TestCase):
    def test_default_graph_runs_to_completion(self) -> None:
        state = create_task_state("Run a graph.")
        created_events = ensure_default_graph(state)
        run_events = run_task_graph(state)

        self.assertEqual(len(created_events), 5)
        self.assertEqual(state.status, PlanNodeStatus.COMPLETED)
        self.assertEqual(len(state.metadata["stage_order"]), 5)
        self.assertTrue(
            any(event.event_type == EventType.NODE_UPDATED for event in run_events)
        )
        self.assertTrue(any(event.event_type == EventType.CONSTRAINT_CHECK for event in run_events))
        self.assertTrue(any(event.event_type == EventType.TOPOLOGY_ROUTE for event in run_events))
        self.assertTrue(any(event.event_type == EventType.AGENT_MESSAGE for event in run_events))
        self.assertTrue(state.decisions)
        self.assertTrue(state.decisions[0].checks)
        self.assertEqual(state.metadata["graph_version"], "m3-symbolic-v1")

    def test_graph_execute_stage_can_call_code_worker_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Run a graph through a worker runtime.")
            context = GraphExecutionContext.from_paths(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )

            events = run_task_graph(state, execution_context=context)

            self.assertEqual(state.status, PlanNodeStatus.COMPLETED)
            self.assertTrue(any("tool_result" in event.payload for event in events))
            self.assertGreaterEqual(state.budget.tool_calls, 2)
            execute_nodes = [
                node
                for node in state.plan_nodes.values()
                if node.metadata.get("stage") == "execute"
            ]
            self.assertEqual(execute_nodes[0].assigned_worker_id, "CodeWorkerRuntime")
            self.assertTrue(any(event.event_type == EventType.TOPOLOGY_ROUTE for event in events))
            self.assertTrue((Path(tmpdir) / "workspace" / "runs" / state.task_id / "execution-summary.md").exists())

    def test_graph_marks_task_failed_when_worker_runtime_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            outside = Path(tmpdir) / "outside.html"
            outside.write_text("<html><body>outside</body></html>", encoding="utf-8")
            state = create_task_state("Run a graph through a failing browser worker.")
            state.metadata["runtime_hints"] = {
                "preferred_worker": "BrowserWorker",
                "browser_plan": [{"action": "open_url", "arguments": {"url": outside.resolve().as_uri()}}],
                "allowed_schemes": ["file"],
            }
            context = GraphExecutionContext.from_paths(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )

            events = run_task_graph(state, execution_context=context)

            self.assertEqual(state.status, PlanNodeStatus.FAILED)
            self.assertTrue(any("browser_result" in event.payload for event in events))
            execute_nodes = [
                node
                for node in state.plan_nodes.values()
                if node.metadata.get("stage") == "execute"
            ]
            self.assertEqual(execute_nodes[0].status, PlanNodeStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
