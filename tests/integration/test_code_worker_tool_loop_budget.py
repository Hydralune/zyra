from __future__ import annotations

import json
import shutil
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
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state  # noqa: E402
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_workers import CodeWorkerRuntime, CodeWorkerSidecarClient  # noqa: E402


@unittest.skipIf(shutil.which("node") is None, "node is required for code-worker sidecar")
class CodeWorkerToolLoopBudgetTests(unittest.TestCase):
    def test_sidecar_tool_loop_contract_maps_claude_code_budget_sources(self) -> None:
        contract = CodeWorkerSidecarClient(ROOT).tool_loop_contract()

        self.assertEqual(contract["source"], "claude-code-best")
        self.assertEqual(contract["ownerUnit"], "M1-02C")
        self.assertTrue(contract["inventoryExists"])
        self.assertIn("src/services/tools/toolExecution.ts", contract["sourceFiles"])
        self.assertIn("src/services/tools/toolOrchestration.ts", contract["sourceFiles"])
        self.assertIn("src/utils/toolResultStorage.ts", contract["sourceFiles"])
        self.assertIn("src/utils/ShellCommand.ts", contract["sourceFiles"])
        self.assertTrue(contract["toolInterface"]["hasInputSchema"])
        self.assertTrue(contract["toolInterface"]["hasConcurrencyFlag"])
        self.assertTrue(contract["executionPipeline"]["hasPermissionGate"])
        self.assertTrue(contract["executionPipeline"]["hasSchemaValidation"])
        self.assertTrue(contract["executionPipeline"]["hasLargeResultExternalization"])
        self.assertTrue(contract["scheduling"]["readOnlyConcurrent"])
        self.assertTrue(contract["scheduling"]["writeSerial"])
        self.assertTrue(contract["resultBudget"]["hasMaxResultSizeChars"])
        self.assertTrue(contract["shellRuntime"]["hasProcessLifecycle"])
        self.assertTrue(contract["shellRuntime"]["hasReadOnlyCommandValidation"])
        self.assertTrue(contract["sandboxRuntime"]["hasSandboxAdapter"])
        self.assertTrue(contract["failureSignals"]["hasDenialLimits"])
        self.assertEqual(
            contract["zyraRuntimeMapping"]["toolLoop"],
            "packages/runtime/zyra_runtime/tool_loop.py",
        )

    def test_runtime_batches_read_only_tools_and_serializes_conflicting_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Exercise read-only batching and write conflict protection.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "a.txt").write_text("alpha", encoding="utf-8")
            (workspace / "b.txt").write_text("beta", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "query_turns": [
                        [
                            {"tool_name": "file_read", "arguments": {"path": "a.txt"}},
                            {"tool_name": "file_read", "arguments": {"path": "b.txt"}},
                            {"tool_name": "file_write", "arguments": {"path": "same.txt", "content": "one"}},
                            {"tool_name": "file_write", "arguments": {"path": "same.txt", "content": "two"}},
                        ]
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            batches = _query_phases(run.event_records, "tool_batch_started")
            self.assertEqual([batch["execution_mode"] for batch in batches], [
                "concurrent_read_only",
                "serial_non_read_only",
                "serial_non_read_only",
            ])
            self.assertEqual(batches[0]["tool_count"], 2)
            self.assertEqual(batches[2]["conflict_protected"], "true")
            self.assertEqual(run.worker_result.metadata["tool_conflict_protected"], "1")
            self.assertEqual(run.worker_result.metadata["tool_loop_contract_owner_unit"], "M1-02C")
            self.assertEqual(run.worker_result.metadata["tool_loop_contract_read_only_concurrent"], "true")
            self.assertEqual(run.worker_result.metadata["tool_loop_contract_write_serial"], "true")
            self.assertEqual((workspace / "same.txt").read_text(encoding="utf-8"), "two")

    def test_runtime_externalizes_large_tool_result_and_emits_budget_watchdog_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Externalize large tool result.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "large.txt").write_text("budget-" * 160, encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_result_budget_chars": 140,
                    "tool_plan": [{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["tool_result_externalizations"], "1")
            self.assertEqual(run.worker_result.metadata["tool_failure_signals"], "1")
            budget_events = _query_phases(run.event_records, "tool_result_budget_exceeded")
            failure_events = _query_phases(run.event_records, "tool_failure_signal")
            watchdog_events = _query_phases(run.event_records, "watchdog_signal")
            self.assertEqual(len(budget_events), 1)
            self.assertEqual(len(failure_events), 1)
            self.assertEqual(len(watchdog_events), 1)
            self.assertEqual(failure_events[0]["signal"]["kind"], "budget_exceeded")
            self.assertEqual(watchdog_events[0]["watchdog_signal"]["route"], "artifact_externalized")
            tool_result = next(event.payload["tool_result"] for event in run.event_records if "tool_result" in event.payload)
            self.assertTrue(tool_result["output"]["truncated"])
            artifact_id = tool_result["output"]["full_output_artifact_id"]
            artifact = next(item for item in run.worker_result.artifacts if item.artifact_id == artifact_id)
            self.assertIn("budget-", json.loads(Path(artifact.uri).read_text(encoding="utf-8"))["content"])

    def test_runtime_converts_schema_error_to_failure_and_watchdog_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Schema error signals.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "continue_on_error": True,
                    "tool_plan": [{"tool_name": "file_write", "arguments": {"path": "bad.txt"}}],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["tool_schema_errors"], "1")
            self.assertEqual(run.worker_result.metadata["tool_failure_signals"], "1")
            failures = _query_phases(run.event_records, "tool_failure_signal")
            watchdogs = _query_phases(run.event_records, "watchdog_signal")
            self.assertEqual(failures[0]["signal"]["kind"], "schema_error")
            self.assertEqual(watchdogs[0]["watchdog_signal"]["route"], "repair_tool_arguments")
            tool_result = next(event.payload["tool_result"] for event in run.event_records if "tool_result" in event.payload)
            self.assertFalse(tool_result["ok"])
            self.assertEqual(tool_result["error"], "schema_error")

    def test_runtime_converts_permission_denial_to_watchdog_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Permission denial signals.")
            outside = Path(tmpdir) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "continue_on_error": True,
                    "tool_plan": [{"tool_name": "file_read", "arguments": {"path": str(outside)}}],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["tool_failure_signals"], "1")
            failures = _query_phases(run.event_records, "tool_failure_signal")
            watchdogs = _query_phases(run.event_records, "watchdog_signal")
            self.assertEqual(failures[0]["signal"]["kind"], "permission_denied")
            self.assertEqual(watchdogs[0]["watchdog_signal"]["route"], "permission_runtime")
            tool_result = next(event.payload["tool_result"] for event in run.event_records if "tool_result" in event.payload)
            self.assertFalse(tool_result["ok"])
            self.assertEqual(tool_result["error"], "permission_denied")


def _query_phases(event_records, phase: str) -> list[dict]:
    return [
        event.payload["query_session"]
        for event in event_records
        if event.payload.get("query_session", {}).get("phase") == phase
    ]


if __name__ == "__main__":
    unittest.main()
