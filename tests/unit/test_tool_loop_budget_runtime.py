from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state  # noqa: E402
from zyra_runtime import (  # noqa: E402
    ToolBatchExecutionMode,
    ToolExecutionContext,
    ToolFailureKind,
    ToolLoopScheduler,
    ToolResult,
    ToolResultBudgeter,
    default_tool_registry,
    tool_failure_signal_from_result,
)


class ToolLoopBudgetRuntimeTests(unittest.TestCase):
    def test_scheduler_batches_read_only_tools_and_conflict_protects_repeated_writes(self) -> None:
        state = create_task_state("Plan tool batches.")
        scheduler = ToolLoopScheduler(default_tool_registry(), max_read_only_concurrency=4)

        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[
                {"tool_name": "file_read", "arguments": {"path": "a.txt"}},
                {"tool_name": "file_read", "arguments": {"path": "b.txt"}},
                {"tool_name": "file_write", "arguments": {"path": "same.txt", "content": "one"}},
                {"tool_name": "file_write", "arguments": {"path": "same.txt", "content": "two"}},
            ],
        )

        self.assertEqual(len(plan.batches), 3)
        self.assertEqual(plan.batches[0].execution_mode, ToolBatchExecutionMode.CONCURRENT_READ_ONLY)
        self.assertEqual([request.tool_name for request in plan.batches[0].requests], ["file_read", "file_read"])
        self.assertEqual(plan.batches[1].execution_mode, ToolBatchExecutionMode.SERIAL_NON_READ_ONLY)
        self.assertEqual(plan.batches[2].execution_mode, ToolBatchExecutionMode.SERIAL_NON_READ_ONLY)
        self.assertTrue(plan.batches[2].conflict_protected)
        self.assertEqual(plan.conflict_protected_count, 1)
        self.assertEqual(plan.read_only_count, 2)
        self.assertEqual(plan.write_count, 2)

    def test_scheduler_validates_tool_schema_before_execution(self) -> None:
        state = create_task_state("Validate schema.")
        scheduler = ToolLoopScheduler(default_tool_registry())

        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[
                {"tool_name": "file_write", "arguments": {"path": "missing-content.txt"}},
                {"tool_name": "shell", "arguments": {"command": "echo ok", "timeout_seconds": "fast"}},
                {"tool_name": "missing_tool", "arguments": {}},
            ],
        )

        self.assertEqual(plan.schema_error_count, 3)
        error_fields = [error.field for request in plan.requests for error in request.schema_errors]
        self.assertIn("content", error_fields)
        self.assertIn("timeout_seconds", error_fields)
        self.assertIn("tool_name", error_fields)
        schema_result = scheduler.schema_error_result(plan.requests[0])
        self.assertFalse(schema_result.ok)
        self.assertEqual(schema_result.error, "schema_error")
        self.assertEqual(schema_result.metadata["failure_kind"], "schema_error")

    def test_budgeter_externalizes_large_tool_result_as_structured_artifact(self) -> None:
        state = create_task_state("Budget tool result.")
        scheduler = ToolLoopScheduler(default_tool_registry())
        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            context = ToolExecutionContext.for_workspace(
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            result = ToolResult(
                tool_call_id=plan.requests[0].call.tool_call_id,
                ok=True,
                summary="Read large.txt",
                output={"content": "x" * 500, "path": "large.txt"},
            )

            bounded, decision = ToolResultBudgeter(max_chars=80).apply(
                request=plan.requests[0],
                result=result,
                artifact_store=context.artifact_store,
            )

            self.assertTrue(decision.applied)
            self.assertTrue(bounded.output["truncated"])
            self.assertEqual(bounded.output["full_output_artifact_id"], decision.artifact_id)
            self.assertEqual(bounded.metadata["tool_result_budget_applied"], "true")
            artifact_path = Path(bounded.artifacts[-1].uri)
            self.assertTrue(artifact_path.exists())
            self.assertEqual(json.loads(artifact_path.read_text(encoding="utf-8"))["content"], "x" * 500)

    def test_failure_signal_maps_budget_schema_timeout_and_runtime_failures(self) -> None:
        state = create_task_state("Map tool failures.")
        scheduler = ToolLoopScheduler(default_tool_registry())
        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[
                {"tool_name": "file_write", "arguments": {"path": "missing-content.txt"}},
                {"tool_name": "shell", "arguments": {"command": "sleep 10", "approved": True}},
            ],
        )
        schema_signal = tool_failure_signal_from_result(
            plan.requests[0],
            scheduler.schema_error_result(plan.requests[0]),
        )
        timeout_signal = tool_failure_signal_from_result(
            plan.requests[1],
            ToolResult(
                tool_call_id=plan.requests[1].call.tool_call_id,
                ok=False,
                summary="shell timed out after 1 second(s)",
                error="tool_timeout",
            ),
        )
        runtime_signal = tool_failure_signal_from_result(
            plan.requests[1],
            ToolResult(
                tool_call_id=plan.requests[1].call.tool_call_id,
                ok=False,
                summary="shell failed",
                error="RuntimeError",
            ),
        )

        self.assertIsNotNone(schema_signal)
        self.assertIsNotNone(timeout_signal)
        self.assertIsNotNone(runtime_signal)
        self.assertEqual(schema_signal.kind, ToolFailureKind.SCHEMA_ERROR)
        self.assertEqual(schema_signal.watchdog_route, "repair_tool_arguments")
        self.assertFalse(schema_signal.retryable)
        self.assertEqual(timeout_signal.kind, ToolFailureKind.TIMEOUT)
        self.assertEqual(timeout_signal.watchdog_route, "retry_or_background")
        self.assertTrue(timeout_signal.retryable)
        self.assertEqual(runtime_signal.kind, ToolFailureKind.RUNTIME_ERROR)
        self.assertEqual(runtime_signal.watchdog_route, "recovery_planner")


if __name__ == "__main__":
    unittest.main()
