from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "skills",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "evaluation",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_commands import default_command_registry, parse_slash_command
from zyra_core import EventType, PlanNodeStatus, create_task_state
from zyra_integrations import (
    browser_use_snapshot,
    claude_code_best_snapshot,
    validate_vendor_snapshot,
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
    control_event_from_command,
    default_tool_registry,
    default_worker_descriptors,
)
from zyra_skills import default_skill_registry
from zyra_orchestration import GraphExecutionContext, run_task_graph
from zyra_workers import (
    BrowserWorkerRuntime,
    CodeWorkerRuntime,
    CodeWorkerSidecarClient,
    default_browser_action_registry,
    inspect_browser_use_runtime,
)
from zyra_evaluation import evaluate_task_trace, run_m2_scenarios


def main() -> None:
    validate_vendor_snapshot(claude_code_best_snapshot(ROOT))
    validate_vendor_snapshot(browser_use_snapshot(ROOT))

    state = create_task_state("Verify M2 runtime protocol.")
    parsed = parse_slash_command(
        "/change add stricter failure recovery evidence",
        run_id=state.run_id,
        task_id=state.task_id,
    )
    assert parsed is not None
    event = control_event_from_command(parsed.control_command, node_id=state.root_node_id)
    assert event.event_type == EventType.REQUIREMENT_CHANGE

    commands = default_command_registry()
    for command_name in [
        "/status",
        "/graph",
        "/trace",
        "/artifacts",
        "/tools",
        "/permissions",
        "/help",
        "/bashes",
        "/clear",
        "/compact",
        "/context",
        "/rewind",
        "/resume",
        "/export",
        "/memory",
        "/init",
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
    ]:
        assert commands.get(command_name) is not None, command_name
    assert commands.get("/clear").metadata["runtime_status"] == "stateful"
    assert commands.get("/rewind").metadata["runtime_status"] == "stateful"
    assert commands.get("/resume").metadata["runtime_status"] == "stateful"
    assert commands.get("/compact").metadata["runtime_status"] == "stateful"
    assert commands.get("/export").metadata["runtime_status"] == "stateful"
    assert commands.get("/verify").metadata["runtime_status"] == "stateful"
    assert commands.get("/eval").metadata["runtime_status"] == "stateful"
    assert commands.get("/skills").metadata["category"] == "extension_team"
    skills = default_skill_registry()
    for skill_name in [
        "codebase-analysis",
        "code-change",
        "verification",
        "web-research",
        "pdf-analysis",
        "report-writing",
        "trace-summary",
        "failure-recovery",
        "requirement-change",
        "competition-demo",
    ]:
        assert skills.get(skill_name) is not None, skill_name
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
    assert browser_health.modules["agent_service"]
    assert browser_health.modules["agent_history"]
    assert browser_health.modules["llm_models"]
    assert browser_health.classes["Agent"] == "Agent"
    assert browser_health.classes["AgentHistoryList"] == "AgentHistoryList"
    assert browser_health.modules["upload_file_action"]
    assert browser_health.modules["scroll_action"]
    assert browser_health.modules["send_keys_action"]
    assert browser_health.modules["screenshot_action"]
    assert browser_health.modules["save_as_pdf_action"]
    assert browser_health.paths.config_dir.relative_to(ROOT).parts[0] == "tmp"
    assert browser_health.paths.temp_dir.relative_to(ROOT).parts[0] == "tmp"
    code_inventory = CodeWorkerSidecarClient(ROOT).runtime_inventory()
    assert code_inventory["source"] == "claude-code-best"
    assert code_inventory["toolRuntime"]["baseToolCount"] > 5
    code_contract = CodeWorkerSidecarClient(ROOT).query_contract()
    assert code_contract["source"] == "claude-code-best"
    assert "src/query.ts" in code_contract["sourceFiles"]
    assert code_contract["toolOrchestration"]["readOnlyConcurrent"]
    assert code_contract["toolOrchestration"]["writeSerial"]
    assert code_contract["budgets"]["toolResultBudget"]
    assert code_contract["permissionRuntime"]["tracksPermissionDenials"]
    session_runtime = ContextSessionRuntime([])
    assert session_runtime.summarize(state)["active_session_id"] == "session_initial"
    skill = skills.get("web-research")
    skill_event = EventType.SKILL_INVOKED
    assert skill is not None
    assert skill_event == EventType.SKILL_INVOKED
    skill_root = Path(skill.skill_root).resolve()
    assert skill.provenance.source_kind.value == "builtin"
    assert skill_root.is_relative_to((ROOT / "skills" / "builtin").resolve())
    skill_invocation_event = {
        "event_type": "skill_invoked",
        "payload": {
            "skill_invocation": {
                "skill_name": skill.metadata.name,
                "preferred_runtime": skill.metadata.preferred_runtime,
                "allowed_tools": [
                    selector.canonical_name
                    for selector in (skill.metadata.allowed_tools or ())
                ],
            }
        },
    }
    skill_evaluation = evaluate_task_trace(
        {
            "task_id": state.task_id,
            "run_id": state.run_id,
            "plan_nodes": {"root": {}},
            "artifacts": [{"artifact_id": "artifact_skill"}],
            "metadata": {"control_commands": [{"name": "/skills"}]},
        },
        [
            {"event_type": "task_created", "payload": {}},
            {"event_type": "agent_message", "payload": {"tool_result": {"ok": True}}},
            skill_invocation_event,
        ],
    )
    assert skill_evaluation["metrics"]["skill_invocation_count"] == 1

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

        run = CodeWorkerRuntime(
            project_root=ROOT,
            workspace_root=Path(tmpdir) / "worker-workspace",
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
                            "tool_name": "file_write",
                            "arguments": {"path": "worker.txt", "content": "code worker runtime"},
                        }
                    ]
                },
            )
        )
        assert run.worker_result.ok
        assert run.worker_result.metadata["vendor_complete"] == "false"
        assert run.worker_result.metadata["inventory_source"] == "zyra-claude-productized"
        assert run.worker_result.metadata["loop"] == "zyra_claude_query_engine_runtime"
        assert run.worker_result.metadata["query_contract_source"] == "zyra-claude-productized"
        assert run.worker_result.metadata["query_contract_write_serial"] == "true"
        assert run.worker_result.metadata["query_turns"] == "1"
        assert run.worker_result.metadata["query_session_id"].startswith("codesession_")
        assert run.worker_result.metadata["context_compactions"] == "0"
        assert any("query_session" in event.payload for event in run.event_records)
        trace_artifact = next(
            artifact
            for artifact in run.worker_result.artifacts
            if artifact.title.startswith("CodeWorker trace ")
        )
        code_worker_preview = LocalArtifactStore(Path(tmpdir) / "worker-artifacts").read_preview(
            trace_artifact
        )
        assert "CodeWorker Runtime Trace" in code_worker_preview["content"]

        browser_workspace = Path(tmpdir) / "browser-workspace"
        browser_workspace.mkdir()
        page = browser_workspace / "m2.html"
        target_page = browser_workspace / "target.html"
        page.write_text(
            "<html><head><title>M2 Browser</title></head><body><a href='target.html'>Target</a><p>browser worker runtime</p></body></html>",
            encoding="utf-8",
        )
        target_page.write_text(
            "<html><head><title>M2 Target</title></head><body><p>browser target evidence</p></body></html>",
            encoding="utf-8",
        )
        browser_run = BrowserWorkerRuntime(
            project_root=ROOT,
            workspace_root=browser_workspace,
            artifact_root=Path(tmpdir) / "browser-artifacts",
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
        assert browser_run.worker_result.ok
        assert browser_run.worker_result.metadata["vendor"] == "browser-use"
        assert browser_run.worker_result.metadata["action_registry_source"] == "browser-use"
        assert any(
            event.payload.get("browser_result", {}).get("output", {}).get("match_count") == 1
            for event in browser_run.event_records
        )

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

        scenario_report = run_m2_scenarios(ROOT, Path(tmpdir) / "scenario-report")
        assert scenario_report["scenario_count"] == 3
        assert scenario_report["passed"] >= 2
        assert (Path(tmpdir) / "scenario-report" / "m2_scenarios_report.json").exists()

    print("M2 verification passed")


if __name__ == "__main__":
    main()
