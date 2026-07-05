from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "evaluation",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_evaluation import evaluate_task_trace


class EvaluationTests(unittest.TestCase):
    def test_trace_evaluator_scores_runtime_evidence(self) -> None:
        task = {
            "task_id": "task_test",
            "run_id": "run_test",
            "plan_nodes": {"node": {}},
            "artifacts": [{"artifact_id": "artifact"}],
            "metadata": {"control_commands": [{"name": "/eval"}]},
        }
        events = [
            {"event_type": "task_created", "payload": {}},
            {"event_type": "agent_message", "payload": {"tool_result": {"ok": True}}},
            {"event_type": "agent_message", "payload": {"low_entropy": True, "message": {"summary": "structured"}}},
            {"event_type": "constraint_check", "payload": {"ok": True}},
            {"event_type": "topology_route", "payload": {"selected_worker": "CodeWorkerRuntime"}},
            {"event_type": "skill_invoked", "payload": {"skill_invocation": {"skill_name": "verification"}}},
        ]

        evaluation = evaluate_task_trace(task, events)

        self.assertGreater(evaluation["score"], 0.8)
        self.assertEqual(evaluation["metrics"]["tool_result_count"], 1)
        self.assertEqual(evaluation["metrics"]["skill_invocation_count"], 1)
        self.assertEqual(evaluation["metrics"]["constraint_check_count"], 1)
        self.assertEqual(evaluation["metrics"]["topology_route_count"], 1)
        self.assertEqual(evaluation["metrics"]["structured_message_count"], 1)
        self.assertIn("recommendations", evaluation)


if __name__ == "__main__":
    unittest.main()
