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
from zyra_runtime.permission.canonical import arguments_digest
from zyra_runtime.permission.models import (
    PermissionEffect,
    PermissionEvaluationRequest,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
    ToolIdentity,
)
from zyra_runtime.permission.runtime import ToolPermissionRuntime
from zyra_runtime.permission.store import PermissionStateStore
from zyra_workers import BrowserWorkerRuntime, CodeWorkerRuntime


class M2RuntimeAcceptanceScenario(unittest.TestCase):
    @unittest.skipIf(shutil.which("node") is None, "node is required for CodeWorker sidecar")
    def test_code_browser_trace_checkpoint_and_session_runtime_work_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            state = create_task_state("M2 runtime acceptance scenario.")

            code_workspace = base / "code-workspace"
            code_artifacts = base / "code-artifacts"
            command = f'"{sys.executable}" -c "from pathlib import Path; print(Path(\'app.py\').read_text())"'
            code_steps = [
                {
                    "tool_name": "file_write",
                    "arguments": {"path": "app.py", "content": "message = 'hello'\n"},
                },
                {
                    "tool_name": "file_edit",
                    "arguments": {"path": "app.py", "old": "hello", "new": "hello zyra"},
                },
                {"tool_name": "shell", "arguments": {"command": command}},
            ]
            code_session_id = f"m2-acceptance:{state.task_id}"
            _seed_exact_policy_rules(
                state_path=code_artifacts / ".permission" / "state.json",
                session_id=code_session_id,
                state=state,
                workspace=code_workspace,
                steps=code_steps,
            )
            code_run = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=code_workspace,
                artifact_root=code_artifacts,
            ).run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "session_id": code_session_id,
                        "tool_plan": code_steps,
                    },
                )
            )
            self.assertTrue(
                code_run.worker_result.ok,
                f"{code_run.worker_result.error}: {code_run.worker_result.metadata}",
            )
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
                    "browser_backend": "static",
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
            trace_result = _execute_exact_policy_tool(
                tool_context,
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="trace",
                    arguments={"limit": 20, "write_artifact": True},
                ),
                session_id=f"m2-inspection:{state.task_id}",
            )
            checkpoint_result = _execute_exact_policy_tool(
                tool_context,
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name="checkpoint",
                    arguments={"write_artifact": True},
                ),
                session_id=f"m2-inspection:{state.task_id}",
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


def _seed_exact_policy_rules(
    *,
    state_path: Path,
    session_id: str,
    state: object,
    workspace: Path,
    steps: list[dict[str, object]],
) -> None:
    store = PermissionStateStore(state_path)
    for index, step in enumerate(steps, start=1):
        tool_name = str(step["tool_name"])
        arguments = dict(step["arguments"])
        store.add_global_rule(
            PermissionRuleRecord(
                rule_id=f"m2-acceptance-{state.task_id}-{index}",
                effect=PermissionEffect.ALLOW,
                source=PermissionRuleSource.POLICY,
                scope=PermissionScope(
                    PermissionScopeKind.ACTION,
                    session_id=session_id,
                    task_id=state.task_id,
                    run_id=state.run_id,
                    workspace_root=str(workspace.resolve()),
                    tool_namespace="builtin",
                    tool_name=tool_name,
                    argument_digest=arguments_digest(arguments),
                ),
                namespace_pattern="builtin",
                tool_pattern=tool_name,
                max_uses=1,
                reason="exact non-benchmark acceptance policy",
            )
        )


def _execute_exact_policy_tool(
    context: ToolExecutionContext,
    call: ToolCall,
    *,
    session_id: str,
):
    runtime = ToolPermissionRuntime.for_session(
        session_id=session_id,
        state_path=context.artifact_store.root / ".permission" / "state.json",
        workspace_root=context.workspace_root,
    )
    runtime.rule_store.add(
        PermissionRuleRecord(
            rule_id=f"m2-inspection-{call.tool_call_id}",
            effect=PermissionEffect.ALLOW,
            source=PermissionRuleSource.SESSION,
            scope=PermissionScope(
                PermissionScopeKind.ACTION,
                session_id=session_id,
                task_id=call.task_id,
                run_id=call.run_id,
                workspace_root=str(context.workspace_root),
                tool_namespace="builtin",
                tool_name=call.tool_name,
                argument_digest=arguments_digest(call.arguments),
            ),
            namespace_pattern="builtin",
            tool_pattern=call.tool_name,
            max_uses=1,
            reason="exact inspection artifact action",
        )
    )
    guarded = runtime.guard(
        PermissionEvaluationRequest(
            run_id=call.run_id,
            task_id=call.task_id,
            session_id=session_id,
            worker_request_id="m2-inspection-worker",
            node_id=call.node_id,
            tool_use_id=call.tool_call_id,
            tool_identity=ToolIdentity(namespace="builtin", name=call.tool_name),
            arguments=dict(call.arguments),
            workspace_root=str(context.workspace_root),
        )
    )
    return ToolExecutor(context, permission_authority=runtime).execute(
        call,
        permission_grant=guarded.execution_grant,
    )


if __name__ == "__main__":
    unittest.main()
