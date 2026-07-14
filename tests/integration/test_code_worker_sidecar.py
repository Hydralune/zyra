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


QUERY_ENGINE_CONTRACT_LOOP = "zyra_typescript_query_engine_runtime"


@unittest.skipIf(shutil.which("node") is None and shutil.which("bun") is None, "TypeScript runtime is required")
class CodeWorkerTypeScriptRuntimeTests(unittest.TestCase):
    def test_entrypoint_is_the_typescript_runtime_inside_zyra(self) -> None:
        entrypoint = code_worker_entrypoint(ROOT)

        self.assertEqual(entrypoint.suffix, ".ts")
        self.assertTrue(entrypoint.exists())
        self.assertTrue(entrypoint.is_relative_to(ROOT))
        self.assertFalse((entrypoint.parent / "main.mjs").exists())

    def test_health_reports_typescript_as_canonical_owner_without_vendor(self) -> None:
        health = CodeWorkerSidecarClient(ROOT).health()

        self.assertTrue(health["ok"])
        self.assertEqual(health["runtime"], "zyra-typescript-claude-runtime")
        self.assertEqual(health["canonicalOwner"], "typescript")
        self.assertFalse(health["requiresRootSourceRepo"])
        self.assertFalse(health["requiresVendorRuntime"])
        self.assertFalse(health["requiresLegacyInspectionSidecar"])
        self.assertFalse(health["vendor"]["requiredForMainPath"])

    def test_contracts_assign_query_session_tool_and_budget_ownership(self) -> None:
        client = CodeWorkerSidecarClient(ROOT)
        query = client.query_contract()
        session = client.session_contract()
        tools = client.tool_loop_contract()

        self.assertEqual(query["canonicalOwner"], "typescript")
        self.assertIn("packages/runtime/claude-runtime/src/query-engine.ts", query["targetFiles"])
        self.assertTrue(query["toolOrchestration"]["readOnlyConcurrent"])
        self.assertTrue(query["toolOrchestration"]["writeSerial"])
        self.assertTrue(query["budgets"]["toolResultBudget"])
        self.assertTrue(query["compactRuntime"]["postCompactRestore"])
        self.assertEqual(session["snapshotVersion"], "zyra.typescript-query-session.v1")
        self.assertFalse(session["pythonProjectionIsCanonical"])
        self.assertEqual(tools["resultBudgetOwner"], "typescript")
        self.assertEqual(tools["sideEffectOwner"], "python-tool-gateway-or-typescript-capability")

    def test_default_runtime_executes_typescript_owned_structured_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Run the TypeScript CodeWorker loop.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "result.txt").write_text("typescript runtime", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            run = runtime.run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "tool_plan": [
                            {"tool_name": "file_read", "arguments": {"path": "result.txt"}},
                            {"tool_name": "file_read", "arguments": {"path": "result.txt"}},
                        ],
                    },
                )
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            self.assertEqual(run.worker_result.metadata["loop"], QUERY_ENGINE_CONTRACT_LOOP)
            self.assertEqual(run.worker_result.metadata["canonical_runtime_owner"], "typescript")
            self.assertEqual(run.worker_result.metadata["python_query_engine_fallback"], "false")
            self.assertEqual(run.worker_result.metadata["runtime_protocol"], "zyra.claude-runtime.v1")
            self.assertEqual((workspace / "result.txt").read_text(encoding="utf-8"), "typescript runtime")
            phases = [
                event.payload["query_session"]["phase"]
                for event in run.event_records
                if "query_session" in event.payload
            ]
            self.assertIn("stream_request_start", phases)
            self.assertIn("tool_call_completed", phases)
            self.assertIn("session_completed", phases)

    def test_typescript_runtime_owns_batch_budget_and_compact_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Exercise TypeScript runtime budgets.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "a.txt").write_text("a" * 1000, encoding="utf-8")
            (workspace / "b.txt").write_text("b" * 1000, encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            run = runtime.run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "tool_result_budget_chars": 100,
                        "query_context_budget_chars": 180,
                        "query_turns": [[
                            {"tool_name": "file_read", "arguments": {"path": "a.txt"}},
                            {"tool_name": "file_read", "arguments": {"path": "b.txt"}},
                        ]],
                    },
                )
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            events = [
                event.payload["query_session"]
                for event in run.event_records
                if "query_session" in event.payload
            ]
            batches = [event for event in events if event["phase"] == "tool_batch_started"]
            self.assertEqual(batches[0]["execution_mode"], "concurrent_read_only")
            self.assertEqual(batches[0]["tool_count"], 2)
            self.assertTrue(any(event["phase"] == "tool_result_budget_exceeded" for event in events))
            self.assertTrue(any(event["phase"] == "context_compacted" for event in events))
            self.assertGreaterEqual(int(run.worker_result.metadata["context_compactions"]), 1)

    def test_disabling_or_killing_typescript_runtime_fails_without_fallback(self) -> None:
        for constraint, expected in [
            ("disable_typescript_runtime", "typescript_runtime_disabled"),
            ("kill_typescript_runtime_after_start", "typescript_runtime_process_failed"),
        ]:
            with self.subTest(constraint=constraint), tempfile.TemporaryDirectory() as tmpdir:
                state = create_task_state("Disconnect the TypeScript owner.")
                runtime = CodeWorkerRuntime(
                    project_root=ROOT,
                    workspace_root=Path(tmpdir) / "workspace",
                    artifact_root=Path(tmpdir) / "artifacts",
                )
                run = runtime.run(
                    WorkerRequest(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        node_id=state.root_node_id,
                        worker_name="CodeWorkerRuntime",
                        constraints={
                            constraint: True,
                            "tool_plan": [{"tool_name": "checkpoint", "arguments": {}}],
                        },
                    )
                )

                self.assertFalse(run.worker_result.ok)
                self.assertEqual(run.worker_result.error, expected)
                self.assertEqual(run.worker_result.metadata["python_query_engine_fallback"], "false")

    def test_max_turns_is_enforced_by_typescript_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Stop after one TypeScript turn.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "input.txt").write_text("input", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            run = runtime.run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "max_turns": 1,
                        "query_turns": [
                            [{"tool_name": "file_read", "arguments": {"path": "input.txt"}}],
                            [{"tool_name": "file_read", "arguments": {"path": "input.txt"}}],
                        ],
                    },
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "max_turns_exceeded")
            self.assertEqual(run.worker_result.metadata["canonical_runtime_owner"], "typescript")


if __name__ == "__main__":
    unittest.main()
