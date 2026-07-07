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
    ROOT / "packages" / "workers",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType, create_task_state, to_jsonable
from zyra_runtime import ContextSessionRuntime, ToolCall, ToolExecutionContext, ToolExecutor, WorkerRequest
from zyra_workers import BrowserWorkerRuntime, CodeWorkerRuntime


class M2RuntimeAcceptanceScenario(unittest.TestCase):
    @unittest.skipIf(shutil.which("node") is None, "node is required for CodeWorker sidecar")
    def test_code_browser_trace_checkpoint_and_session_runtime_work_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            state = create_task_state("M2 runtime acceptance scenario.")

            code_workspace = base / "code-workspace"
            code_run = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=code_workspace,
                artifact_root=base / "code-artifacts",
            ).run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "tool_plan": [
                            {
                                "tool_name": "file_write",
                                "arguments": {"path": "app.py", "content": "message = 'hello'\n"},
                            },
                            {
                                "tool_name": "file_edit",
                                "arguments": {"path": "app.py", "old": "hello", "new": "hello zyra"},
                            },
                            {
                                "tool_name": "shell",
                                "arguments": {
                                    "command": f'"{sys.executable}" -c "from pathlib import Path; print(Path(\'app.py\').read_text())"',
                                    "approved": True,
                                },
                            },
                        ],
                    },
                )
            )
            self.assertTrue(code_run.worker_result.ok)
            self.assertEqual(code_run.worker_result.metadata["loop"], "zyra_claude_query_engine_runtime")
            self.assertEqual(code_run.worker_result.metadata["query_contract_source"], "zyra-claude-productized")
            self.assertEqual(code_run.worker_result.metadata["sidecar_contracts_used"], "false")
            self.assertEqual(code_run.worker_result.metadata["tool_steps"], "3")
            self.assertIn("hello zyra", (code_workspace / "app.py").read_text(encoding="utf-8"))

            browser_workspace = base / "browser-workspace"
            browser_workspace.mkdir()
            page = browser_workspace / "page.html"
            target_page = browser_workspace / "target.html"
            page.write_text(
                "<html><head><title>M2 Page</title></head><body><a href='target.html'>Target</a><h1>Zyra Browser Evidence</h1></body></html>",
                encoding="utf-8",
            )
            target_page.write_text(
                "<html><head><title>M2 Target</title></head><body><p>Zyra target evidence</p></body></html>",
                encoding="utf-8",
            )
            browser_run = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=browser_workspace,
                artifact_root=base / "browser-artifacts",
            ).run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="BrowserWorker",
                    constraints={
                        "browser_plan": [
                            {"action": "open_url", "arguments": {"url": page.resolve().as_uri()}},
                            {"action": "extract_text"},
                            {"action": "click_element", "arguments": {"index": 0}},
                            {"action": "search_page", "arguments": {"pattern": "target evidence"}},
                        ],
                        "allowed_schemes": ["file"],
                    },
                )
            )
            self.assertTrue(browser_run.worker_result.ok)
            self.assertTrue(
                any(
                    event.payload.get("browser_result", {}).get("output", {}).get("match_count") == 1
                    for event in browser_run.event_records
                )
            )
            self.assertGreaterEqual(len(browser_run.worker_result.artifacts), 4)

            combined_events = [to_jsonable(event) for event in [*code_run.event_records, *browser_run.event_records]]
            tool_context = ToolExecutionContext.for_workspace(
                base / "tool-workspace",
                base / "tool-artifacts",
                event_reader=lambda _task_id: combined_events,
                checkpoint_reader=lambda task_id: {
                    **to_jsonable(state),
                    "task_id": task_id,
                    "artifacts": [to_jsonable(artifact) for artifact in browser_run.worker_result.artifacts],
                },
            )
            trace_result = ToolExecutor(tool_context).execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="trace",
                    arguments={"limit": 20, "write_artifact": True},
                )
            )
            checkpoint_result = ToolExecutor(tool_context).execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="checkpoint",
                    arguments={"write_artifact": True},
                )
            )
            self.assertTrue(trace_result.ok)
            self.assertTrue(checkpoint_result.ok)
            self.assertEqual(checkpoint_result.output["summary"]["task_id"], state.task_id)

            clear_event = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.CONTROL_COMMAND,
                node_id=state.root_node_id,
                payload={"raw": "acceptance clear"},
            )
            session_events = [*combined_events, to_jsonable(clear_event)]
            cleared = ContextSessionRuntime(session_events).clear(state, clear_event)
            self.assertGreater(cleared["data"]["cleared_visible_events"], 1)
            self.assertEqual(cleared["data"]["session"]["visible_events"], 0)


if __name__ == "__main__":
    unittest.main()
