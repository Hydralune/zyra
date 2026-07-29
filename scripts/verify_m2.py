from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "evaluation",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_commands import default_control_command_registry
from zyra_core import PlanNodeStatus, create_task_state
from zyra_integrations import (
    browser_use_source_identity,
    claude_code_source_identity,
)
from zyra_runtime import (
    ContextSessionRuntime,
    JsonPermissionStore,
    LocalArtifactStore,
    PermissionEffect,
    PermissionOperation,
    PermissionRequestStatus,
    PermissionRule,
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    WorkerRequest,
    default_tool_registry,
    default_worker_descriptors,
)
from zyra_orchestration import GraphExecutionContext, run_task_graph
from zyra_workers import (
    CodeWorkerRuntime,
    CodeWorkerSidecarClient,
    default_browser_action_registry,
    inspect_browser_use_runtime,
)


def main() -> None:
    for source in (
        claude_code_source_identity(),
        browser_use_source_identity(),
    ):
        assert str(source.status) == "retired"
        assert source.availability == "not_applicable"
        assert source.filesystem_required is False
        assert source.fallback_available is False

    state = create_task_state("Verify M2 runtime protocol.")
    commands = default_control_command_registry()
    for command_name in [
        "/status",
        "/graph",
        "/trace",
        "/artifacts",
        "/tools",
        "/permissions",
        "/help",
        "/clear",
        "/compact",
        "/context",
        "/rewind",
        "/resume",
        "/export",
        "/memory",
        "/model",
        "/doctor",
        "/cost",
        "/usage",
        "/mcp",
        "/agents",
        "/hooks",
        "/skills",
        "/plan",
        "/goal",
        "/team-onboarding",
        "/inject",
        "/change",
        "/verify",
        "/eval",
        "/loopx-connect",
        "/loopx-status",
    ]:
        assert commands.get(command_name) is not None, command_name
    assert commands.get("/clear").mutation_scope.value == "session"
    assert commands.get("/rewind").mutation_scope.value == "session"
    assert commands.get("/resume").mutation_scope.value == "session"
    assert commands.get("/compact").mutation_scope.value == "context"
    assert commands.get("/export").mutation_scope.value == "artifact"
    assert commands.get("/verify").mutation_scope.value == "task_graph"
    assert commands.get("/eval").mutation_scope.value == "task_graph"
    assert commands.get("/skills").category == "extensions"
    assert default_tool_registry().get("browser") is not None
    workers = default_worker_descriptors()
    assert any(worker.name == "CodeWorkerRuntime" for worker in workers)
    browser_worker = next(worker for worker in workers if worker.name == "BrowserWorker")
    assert "browser-agent" in browser_worker.capabilities
    browser_actions = default_browser_action_registry(ROOT)
    assert browser_actions.get("navigate").action == "open_url"
    assert browser_actions.get("scroll").action == "scroll_page"
    assert browser_actions.get("evaluate").action == "evaluate_js"
    assert browser_actions.get("find_text").action == "scroll_to_text"
    assert browser_actions.get("screenshot").action == "take_screenshot"
    assert browser_actions.get("pdf").action == "save_as_pdf"
    assert browser_actions.get("upload").action == "upload_file"
    assert browser_actions.get("downloads").action == "collect_downloads"
    assert len(browser_actions.source_actions) > 10
    browser_health = inspect_browser_use_runtime(ROOT)
    assert browser_health.importable
    assert browser_health.modules
    assert all(browser_health.modules.values())
    assert browser_health.classes["Agent"] == "Agent"
    assert browser_health.classes["AgentHistoryList"] == "AgentHistoryList"
    assert browser_health.classes["UploadFileAction"] == "UploadFileAction"
    assert browser_health.classes["ScrollAction"] == "ScrollAction"
    assert browser_health.classes["SendKeysAction"] == "SendKeysAction"
    assert browser_health.classes["ScreenshotAction"] == "ScreenshotAction"
    assert browser_health.classes["SaveAsPdfAction"] == "SaveAsPdfAction"
    assert browser_health.paths.config_dir.relative_to(ROOT).parts[0] == "tmp"
    assert browser_health.paths.temp_dir.relative_to(ROOT).parts[0] == "tmp"
    code_inventory = CodeWorkerSidecarClient(ROOT).runtime_inventory()
    assert code_inventory["source"] == "zyra-typescript-runtime"
    assert len(code_inventory["toolRuntime"]["baseToolSymbols"]) >= 4
    code_contract = CodeWorkerSidecarClient(ROOT).query_contract()
    assert code_contract["source"] == "zyra-typescript-runtime"
    assert "src/query.ts" in code_contract["sourceFiles"]
    assert code_contract["toolOrchestration"]["readOnlyConcurrent"]
    assert code_contract["toolOrchestration"]["writeSerial"]
    assert code_contract["budgets"]["toolResultBudget"]
    assert code_contract["permissionRuntime"]["tracksPermissionDenials"]
    session_runtime = ContextSessionRuntime([])
    assert session_runtime.summarize(state)["active_session_id"] == "session_initial"

    with tempfile.TemporaryDirectory() as tmpdir:
        context = ToolExecutionContext.for_workspace(Path(tmpdir) / "workspace", Path(tmpdir) / "artifacts")
        result = ToolExecutor(context).execute(
            ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                tool_name="file_write",
                arguments={"path": "m2.txt", "content": "runtime tool execution"},
            )
        )
        assert not result.ok
        assert result.error == "permission_required"
        assert not (Path(tmpdir) / "workspace" / "m2.txt").exists()
        (Path(tmpdir) / "workspace" / "research.md").write_text(
            "M2 should support searchable research evidence for long-horizon tasks.",
            encoding="utf-8",
        )
        search_result = ToolExecutor(context).execute(
            ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                tool_name="web_search",
                arguments={"query": "research evidence", "paths": ["."]},
            )
        )
        assert not search_result.ok
        assert search_result.error == "permission_required"
        assert not search_result.artifacts
        browser_tool_result = ToolExecutor(context).execute(
            ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                tool_name="browser",
                arguments={
                    "action": "extract_text",
                    "html": "<html><head><title>M2 Tool Browser</title></head><body>browser tool evidence</body></html>",
                },
            )
        )
        assert not browser_tool_result.ok
        assert browser_tool_result.error == "permission_required"
        assert not browser_tool_result.artifacts
        trace_context = ToolExecutionContext.for_workspace(
            Path(tmpdir) / "trace-workspace",
            Path(tmpdir) / "trace-artifacts",
            event_reader=lambda task_id: [
                {
                    "event_id": "event_verify_m2",
                    "run_id": state.run_id,
                    "task_id": task_id,
                    "event_type": "task_created",
                    "created_at": state.created_at,
                    "payload": {},
                }
            ],
        )
        trace_result = ToolExecutor(trace_context).execute(
            ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                tool_name="trace",
                arguments={"limit": 1, "write_artifact": False},
            )
        )
        assert trace_result.ok
        assert trace_result.output["returned_count"] == 1
        assert not trace_result.artifacts
        checkpoint_context = ToolExecutionContext.for_workspace(
            Path(tmpdir) / "checkpoint-workspace",
            Path(tmpdir) / "checkpoint-artifacts",
            checkpoint_reader=lambda task_id: {
                "task_id": task_id,
                "run_id": state.run_id,
                "status": str(state.status),
                "updated_at": state.updated_at,
                "plan_nodes": {},
                "artifacts": [],
                "metadata": {},
                "budget": {},
            },
        )
        checkpoint_result = ToolExecutor(checkpoint_context).execute(
            ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                tool_name="checkpoint",
                arguments={"include_state": True, "write_artifact": False},
            )
        )
        assert checkpoint_result.ok
        assert checkpoint_result.output["summary"]["task_id"] == state.task_id
        assert not checkpoint_result.artifacts

        permission_store = JsonPermissionStore(Path(tmpdir) / "permissions.json")
        shell_result = ToolExecutor(
            ToolExecutionContext.for_workspace(
                Path(tmpdir) / "permission-workspace",
                Path(tmpdir) / "permission-artifacts",
                permission_store=permission_store,
            )
        ).execute(
            ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                tool_name="shell",
                arguments={"command": "python --version"},
            )
        )
        assert shell_result.error == "permission_required"
        pending = permission_store.list_requests(PermissionRequestStatus.PENDING)
        assert len(pending) == 1
        permission_store.add_rule(
            PermissionRule(
                operation=PermissionOperation.SHELL,
                pattern="python --version",
                effect=PermissionEffect.ALLOW,
                reason="M2 verification allow rule",
            )
        )

        worker_workspace = Path(tmpdir) / "worker-workspace"
        worker_workspace.mkdir()
        (worker_workspace / "worker.txt").write_text(
            "code worker runtime",
            encoding="utf-8",
        )
        run = CodeWorkerRuntime(
            project_root=ROOT,
            workspace_root=worker_workspace,
            artifact_root=Path(tmpdir) / "worker-artifacts",
        ).run(
            WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_plan": [
                        {
                            "tool_name": "file_read",
                            "arguments": {"path": "worker.txt"},
                        }
                    ]
                },
            )
        )
        assert run.worker_result.ok, (
            run.worker_result.error,
            dict(run.worker_result.metadata),
        )
        assert run.worker_result.metadata["canonical_runtime_owner"] == "typescript"
        assert run.worker_result.metadata["loop"] == "zyra_typescript_query_engine_runtime"
        assert run.worker_result.metadata["query_contract_source"] == "zyra-claude-productized"
        assert run.worker_result.metadata["query_turns"] == "1"
        assert run.worker_result.metadata["query_session_id"].startswith("query:")
        assert run.worker_result.metadata["context_compactions"] == "0"
        assert any("query_session" in event.payload for event in run.event_records)
        trace_artifact = next(
            artifact
            for artifact in run.worker_result.artifacts
            if artifact.title.startswith("CodeWorker E01 trace ")
        )
        code_worker_preview = LocalArtifactStore(Path(tmpdir) / "worker-artifacts").read_preview(
            trace_artifact
        )
        assert "CodeWorker E01 TypeScript Runtime Trace" in code_worker_preview["content"]

        graph_state = create_task_state("Verify graph-level worker runtime execution.")
        graph_events = run_task_graph(
            graph_state,
            execution_context=GraphExecutionContext.from_paths(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "graph-workspace",
                artifact_root=Path(tmpdir) / "graph-artifacts",
            ),
        )
        assert graph_state.status == PlanNodeStatus.COMPLETED
        assert any("tool_result" in event.payload for event in graph_events)
        assert graph_state.budget.tool_calls >= 2

    print("M2 verification passed")


if __name__ == "__main__":
    main()
