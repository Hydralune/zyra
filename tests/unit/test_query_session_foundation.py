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
    CodeWorkerSessionFoundationRuntime,
    CodeWorkerSessionStore,
    ContextAssemblyRuntime,
    QueryInputKind,
    QueryInputProcessor,
    ToolExecutionContext,
    WorkerRequest,
)


class QueryInputProcessorTests(unittest.TestCase):
    def test_classifies_text_slash_bash_and_structured_turns(self) -> None:
        state = create_task_state("input foundation")
        request = WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="CodeWorkerRuntime",
            constraints={
                "query_inputs": [
                    "plain request",
                    "/compact focus on scheduler",
                    "!git status --short",
                ],
                "query_turns": [[{"tool_name": "trace", "prompt": "inspect trace", "arguments": {"limit": 1}}]],
            },
        )

        report = QueryInputProcessor().process_worker_request(request)

        self.assertTrue(report.ok)
        kinds = [record.kind for record in report.records]
        self.assertIn(QueryInputKind.TEXT, kinds)
        self.assertIn(QueryInputKind.SLASH_COMMAND, kinds)
        self.assertIn(QueryInputKind.BASH, kinds)
        self.assertIn(QueryInputKind.STRUCTURED_TURN, kinds)
        self.assertEqual(report.kind_counts["slash_command"], 1)
        bash = next(record for record in report.records if record.kind == QueryInputKind.BASH)
        self.assertEqual(bash.command_name, "shell")
        self.assertTrue(bash.bash.read_only_hint)
        slash = next(record for record in report.records if record.kind == QueryInputKind.SLASH_COMMAND)
        self.assertEqual(slash.command_name, "/compact")
        self.assertEqual(slash.disposition, "route_control_command")

    def test_rejects_unknown_slash_before_query_engine(self) -> None:
        state = create_task_state("slash reject")
        request = WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="CodeWorkerRuntime",
            constraints={"raw_input": "/not-a-real-command"},
        )

        report = QueryInputProcessor().process_worker_request(request)

        self.assertFalse(report.ok)
        self.assertEqual(report.records[0].kind, QueryInputKind.SLASH_COMMAND)
        self.assertIn("slash_command_not_allowed_in_worker", report.records[0].blockers)


class ContextAndStoreFoundationTests(unittest.TestCase):
    def test_context_snapshot_and_store_share_session_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("session store")
            workspace = Path(tmpdir) / "workspace"
            artifacts = Path(tmpdir) / "artifacts"
            context = ToolExecutionContext.for_workspace(workspace, artifacts)
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "session_id": "codesession_test_seed",
                    "raw_input": "Read the workspace state",
                    "query_turns": [[{"tool_name": "trace", "arguments": {"limit": 1}}]],
                },
            )
            input_report = QueryInputProcessor().process_worker_request(request)
            snapshot = ContextAssemblyRuntime().assemble(
                request=request,
                session_id="codesession_test_seed",
                input_records=input_report.records,
                tool_specs=context.registry.list(),
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifacts,
                permission_mode="workspace",
            )
            seed = CodeWorkerSessionFoundationRuntime(
                store=CodeWorkerSessionStore(artifacts)
            ).build_seed(
                request=request,
                input_report=input_report,
                context_snapshot=snapshot,
                session_id="codesession_test_seed",
            )

            self.assertTrue(seed.ok)
            self.assertEqual(seed.session_id, "codesession_test_seed")
            self.assertEqual(snapshot.session_id, seed.session_id)
            self.assertTrue(Path(seed.store_receipt.path).exists())
            replay = CodeWorkerSessionStore(artifacts).replay_session(seed.session_id)
            self.assertTrue(replay.ok)
            record_types = [str(record.record_type) for record in replay.records]
            self.assertIn("session_seed", record_types)
            self.assertIn("input_accepted", record_types)
            self.assertIn("context_snapshot", record_types)
            lines = Path(seed.store_receipt.path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), len(replay.records))
            self.assertTrue(all(json.loads(line)["session_id"] == seed.session_id for line in lines))

    def test_store_disable_blocks_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("disabled store")
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={"raw_input": "hello"},
            )
            input_report = QueryInputProcessor().process_worker_request(request)
            snapshot = ContextAssemblyRuntime().assemble(
                request=request,
                session_id="codesession_disabled",
                input_records=input_report.records,
                tool_specs=ToolExecutionContext.for_workspace(Path(tmpdir) / "w", Path(tmpdir) / "a").registry.list(),
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "w",
                artifact_root=Path(tmpdir) / "a",
                permission_mode="workspace",
            )
            seed = CodeWorkerSessionFoundationRuntime(
                store=CodeWorkerSessionStore(Path(tmpdir) / "a")
            ).build_seed(
                request=request,
                input_report=input_report,
                context_snapshot=snapshot,
                session_id="codesession_disabled",
                disabled_store=True,
            )

            self.assertFalse(seed.ok)
            self.assertEqual(seed.store_receipt.error, "code_worker_session_store_disabled")


if __name__ == "__main__":
    unittest.main()
