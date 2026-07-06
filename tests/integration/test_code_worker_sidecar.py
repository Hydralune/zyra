from __future__ import annotations

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

from zyra_core import create_task_state
from zyra_runtime import WorkerRequest
from zyra_workers import CodeWorkerRuntime, CodeWorkerSidecarClient, code_worker_entrypoint


QUERY_ENGINE_CONTRACT_LOOP = "claude_code_query_engine_contract_loop"


@unittest.skipIf(shutil.which("node") is None, "node is required for code-worker sidecar")
class CodeWorkerSidecarTests(unittest.TestCase):
    def test_entrypoint_is_inside_zyra(self) -> None:
        entrypoint = code_worker_entrypoint(ROOT)

        self.assertTrue(entrypoint.exists())
        self.assertTrue(entrypoint.is_relative_to(ROOT))

    def test_sidecar_health_reports_vendored_claude_code_runtime(self) -> None:
        client = CodeWorkerSidecarClient(ROOT)
        health = client.health()

        self.assertTrue(health["ok"])
        self.assertEqual(health["worker"], "CodeWorkerRuntime")
        self.assertTrue(health["vendor"]["complete"])
        self.assertTrue(health["productizedRuntime"]["complete"])
        self.assertGreaterEqual(health["productizedRuntime"]["effectiveLineCount"], 18_000)
        self.assertTrue(health["productizedRuntime"]["referenceCrosswalk"]["ok"])
        self.assertTrue(str(health["vendor"]["vendorRoot"]).endswith("vendor\\claude-code-best") or str(health["vendor"]["vendorRoot"]).endswith("vendor/claude-code-best"))

    def test_sidecar_snapshot_contains_priority_runtime_modules(self) -> None:
        client = CodeWorkerSidecarClient(ROOT)
        snapshot = client.vendor_snapshot()
        module_names = {module["name"] for module in snapshot["modules"]}

        self.assertIn("query-engine", module_names)
        self.assertIn("permission-runtime", module_names)
        self.assertIn("skill-runtime", module_names)
        self.assertIn("subagent-runtime", module_names)

    def test_sidecar_inventory_reads_claude_code_runtime_sources(self) -> None:
        client = CodeWorkerSidecarClient(ROOT)
        inventory = client.runtime_inventory()

        self.assertEqual(inventory["source"], "claude-code-best")
        self.assertTrue(inventory["productizedRuntime"]["complete"])
        self.assertIn("BashTool", inventory["toolRuntime"]["baseToolSymbols"])
        self.assertIn("FileReadTool", inventory["toolRuntime"]["baseToolSymbols"])
        self.assertGreater(inventory["commandRuntime"]["commandCount"], 20)
        self.assertTrue(inventory["moduleEntrypoints"]["queryEngine"])

    def test_sidecar_query_contract_reads_claude_code_query_engine_semantics(self) -> None:
        client = CodeWorkerSidecarClient(ROOT)
        contract = client.query_contract()

        self.assertEqual(contract["source"], "claude-code-best")
        self.assertIn("src/QueryEngine.ts", contract["sourceFiles"])
        self.assertIn("src/query.ts", contract["sourceFiles"])
        self.assertIn("src/services/tools/toolOrchestration.ts", contract["sourceFiles"])
        self.assertIn("maxTurns", contract["queryEngineConfigFields"])
        self.assertIn("toolUseContext", contract["loopStateFields"])
        self.assertIn("stream_request_start", contract["lifecycleEvents"])
        self.assertTrue(contract["toolOrchestration"]["readOnlyConcurrent"])
        self.assertTrue(contract["toolOrchestration"]["writeSerial"])
        self.assertEqual(contract["toolOrchestration"]["maxConcurrencyDefault"], 10)
        self.assertTrue(contract["budgets"]["toolResultBudget"])
        self.assertTrue(contract["budgets"]["reactiveCompact"])
        self.assertTrue(contract["permissionRuntime"]["tracksPermissionDenials"])

    def test_runtime_executes_structured_tool_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Run CodeWorker tool loop.")
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
                    "tool_plan": [
                        {
                            "tool_name": "file_write",
                            "arguments": {"path": "code-worker/result.txt", "content": "runtime ok"},
                        },
                        {"tool_name": "file_read", "arguments": {"path": "code-worker/result.txt"}},
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            tool_events = [event for event in run.event_records if "tool_result" in event.payload]
            query_events = [event for event in run.event_records if "query_session" in event.payload]
            self.assertEqual(len(tool_events), 2)
            self.assertGreaterEqual(len(query_events), 4)
            self.assertEqual(query_events[0].payload["query_session"]["phase"], "session_started")
            self.assertEqual(query_events[-1].payload["query_session"]["phase"], "session_completed")
            phases = [event.payload["query_session"]["phase"] for event in query_events]
            self.assertIn("stream_request_start", phases)
            self.assertIn("tool_batch_started", phases)
            self.assertIn("tool_call_started", phases)
            self.assertIn("tool_call_completed", phases)
            self.assertIn("tool_use_summary", phases)
            self.assertEqual(run.worker_result.metadata["inventory_source"], "claude-code-best")
            self.assertEqual(run.worker_result.metadata["loop"], QUERY_ENGINE_CONTRACT_LOOP)
            self.assertEqual(run.worker_result.metadata["query_contract_source"], "claude-code-best")
            self.assertEqual(run.worker_result.metadata["query_contract_read_only_concurrent"], "true")
            self.assertEqual(run.worker_result.metadata["query_contract_write_serial"], "true")
            self.assertEqual(run.worker_result.metadata["tool_orchestration_write_serial"], "true")
            self.assertEqual(run.worker_result.metadata["query_turns"], "1")
            self.assertEqual(run.worker_result.metadata["tool_steps"], "2")
            self.assertEqual(run.worker_result.metadata["context_compactions"], "0")
            self.assertTrue(run.worker_result.metadata["query_session_id"].startswith("codesession_"))
            self.assertGreater(int(run.worker_result.metadata["inventory_base_tool_count"]), 5)
            self.assertTrue((Path(tmpdir) / "workspace" / "code-worker" / "result.txt").exists())
            self.assertTrue(any(artifact.kind == "trace" for artifact in run.worker_result.artifacts))

    def test_runtime_batches_consecutive_read_only_tools_like_claude_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Batch read-only tools.")
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
                            {"tool_name": "file_write", "arguments": {"path": "out.txt", "content": "done"}},
                        ]
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            batch_events = [
                event.payload["query_session"]
                for event in run.event_records
                if event.payload.get("query_session", {}).get("phase") == "tool_batch_started"
            ]
            self.assertEqual(batch_events[0]["execution_mode"], "concurrent_read_only")
            self.assertEqual(batch_events[0]["tool_count"], 2)
            self.assertEqual(batch_events[1]["execution_mode"], "serial_non_read_only")
            self.assertEqual(batch_events[1]["tool_count"], 1)
            self.assertEqual(run.worker_result.metadata["tool_steps"], "3")
            self.assertEqual(run.worker_result.metadata["tool_use_summaries"], "2")
            self.assertEqual(run.worker_result.metadata["max_read_only_concurrency"], "10")
            summary_events = [
                event.payload["query_session"]
                for event in run.event_records
                if event.payload.get("query_session", {}).get("phase") == "tool_use_summary"
            ]
            self.assertEqual(len(summary_events), 2)
            self.assertEqual(summary_events[0]["execution_mode"], "concurrent_read_only")
            self.assertTrue((workspace / "out.txt").exists())

    def test_runtime_applies_tool_result_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Apply tool result budget.")
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
                    "tool_result_budget_chars": 120,
                    "tool_plan": [
                        {
                            "tool_name": "file_write",
                            "arguments": {"path": "budget.txt", "content": "x" * 500},
                        },
                        {"tool_name": "file_read", "arguments": {"path": "budget.txt"}},
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            read_result = [event.payload["tool_result"] for event in run.event_records if "tool_result" in event.payload][1]
            self.assertTrue(read_result["output"]["truncated"])
            self.assertEqual(read_result["metadata"]["tool_result_budget_applied"], "true")
            self.assertGreaterEqual(len(run.worker_result.artifacts), 2)

    def test_runtime_compacts_query_context_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Compact query context budget.")
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
                    "query_context_budget_chars": 160,
                    "tool_plan": [
                        {
                            "tool_name": "file_write",
                            "arguments": {"path": "context.txt", "content": "context-budget-" * 80},
                        },
                        {"tool_name": "file_read", "arguments": {"path": "context.txt"}},
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertGreaterEqual(int(run.worker_result.metadata["context_compactions"]), 1)
            compact_events = [
                event.payload["query_session"]
                for event in run.event_records
                if event.payload.get("query_session", {}).get("phase") == "context_compacted"
            ]
            self.assertTrue(compact_events)
            compact_artifact_ids = {event["artifact_id"] for event in compact_events}
            self.assertTrue(any(artifact.artifact_id in compact_artifact_ids for artifact in run.worker_result.artifacts))

    def test_runtime_honors_query_turn_max_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Stop at max turns.")
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
                    "max_turns": 1,
                    "query_turns": [
                        [{"tool_name": "file_write", "arguments": {"path": "one.txt", "content": "one"}}],
                        [{"tool_name": "file_write", "arguments": {"path": "two.txt", "content": "two"}}],
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "max_turns_exceeded")
            self.assertEqual(run.worker_result.metadata["query_turns"], "1")
            self.assertTrue((Path(tmpdir) / "workspace" / "one.txt").exists())
            self.assertFalse((Path(tmpdir) / "workspace" / "two.txt").exists())


if __name__ == "__main__":
    unittest.main()
