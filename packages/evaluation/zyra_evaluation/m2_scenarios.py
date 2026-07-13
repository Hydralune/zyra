from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zyra_commands import parse_slash_command
from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, create_task_state, now_iso, to_jsonable
from zyra_orchestration import ensure_default_graph
from zyra_runtime import WorkerRequest, control_event_from_command
from zyra_runtime.permission.canonical import arguments_digest
from zyra_runtime.permission.models import (
    PermissionEffect,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
)
from zyra_runtime.permission.store import PermissionStateStore
from zyra_symbolic import apply_failure_injection, apply_requirement_change
from zyra_workers import BrowserWorkerRuntime, CodeWorkerRuntime

from .trace import evaluate_task_trace


@dataclass(slots=True)
class ScenarioRunRecord:
    name: str
    ok: bool
    task: dict[str, Any]
    events: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    evaluation: dict[str, Any]
    notes: list[str] = field(default_factory=list)


def run_m2_scenarios(project_root: str | Path, output_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve()
    out = Path(output_root).resolve()
    out.mkdir(parents=True, exist_ok=True)

    scenario_records = [
        _software_engineering_scenario(root, out / "software"),
        _browser_research_scenario(root, out / "browser"),
        _dynamic_control_scenario(root, out / "dynamic-control"),
    ]
    report = {
        "generated_at": now_iso(),
        "project_root": str(root),
        "output_root": str(out),
        "scenario_count": len(scenario_records),
        "passed": sum(1 for item in scenario_records if item.ok),
        "scenarios": [to_jsonable(item) for item in scenario_records],
    }
    (out / "m2_scenarios_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "m2_scenarios_report.md").write_text(_report_markdown(report), encoding="utf-8")
    return report


def _software_engineering_scenario(root: Path, out: Path) -> ScenarioRunRecord:
    state = create_task_state("M2 software engineering scenario: edit code, run verification, emit trace.")
    events: list[EventRecord] = [
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.TASK_CREATED,
            node_id=state.root_node_id,
            payload={"task": to_jsonable(state)},
        )
    ]
    artifacts: list[ArtifactRef] = []
    notes: list[str] = []
    if shutil.which("node") is None:
        notes.append("node is not available; CodeWorker sidecar scenario skipped.")
        return _record("software_engineering", False, state, events, artifacts, notes)

    scenario_root = out / state.task_id
    scenario_workspace = scenario_root / "workspace"
    scenario_artifacts = scenario_root / "artifacts"
    runtime = CodeWorkerRuntime(
        project_root=root,
        workspace_root=scenario_workspace,
        artifact_root=scenario_artifacts,
    )
    command = (
        f'"{sys.executable}" -c '
        '"from pathlib import Path; text=Path(\'src/app.py\').read_text(); '
        'assert \'Zyra scenario\' in text; print(\'scenario ok\')"'
    )
    session_id = f"m2-software:{state.task_id}"
    tool_steps = [
        {
            "tool_name": "file_write",
            "arguments": {
                "path": "src/app.py",
                "content": "def message():\n    return 'draft'\n",
            },
        },
        {
            "tool_name": "file_edit",
            "arguments": {
                "path": "src/app.py",
                "old": "draft",
                "new": "Zyra scenario",
            },
        },
        {"tool_name": "shell", "arguments": {"command": command}},
    ]
    _seed_exact_scenario_permissions(
        state_path=scenario_artifacts / ".permission" / "state.json",
        session_id=session_id,
        run_id=state.run_id,
        task_id=state.task_id,
        workspace_root=scenario_workspace,
        steps=tool_steps,
    )
    run = runtime.run(
        WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="CodeWorkerRuntime",
            constraints={
                "session_id": session_id,
                "query_turns": [
                    tool_steps[:2],
                    [tool_steps[2]],
                ],
                "max_turns": 3,
            },
        )
    )
    events.extend(run.event_records)
    artifacts.extend(run.worker_result.artifacts)
    state.artifacts.extend(run.worker_result.artifacts)
    state.budget.tool_calls += int(run.worker_result.metadata.get("tool_steps") or 0)
    return _record("software_engineering", run.worker_result.ok, state, events, artifacts, notes)


def _seed_exact_scenario_permissions(
    *,
    state_path: Path,
    session_id: str,
    run_id: str,
    task_id: str,
    workspace_root: Path,
    steps: list[dict[str, Any]],
) -> None:
    """Install narrow test-policy capabilities for the non-benchmark M2 demo."""

    store = PermissionStateStore(state_path)
    for index, step in enumerate(steps, start=1):
        tool_name = str(step["tool_name"])
        arguments = dict(step["arguments"])
        store.add_global_rule(
            PermissionRuleRecord(
                rule_id=f"m2-scenario-{task_id}-{index}",
                effect=PermissionEffect.ALLOW,
                source=PermissionRuleSource.POLICY,
                scope=PermissionScope(
                    PermissionScopeKind.ACTION,
                    session_id=session_id,
                    task_id=task_id,
                    run_id=run_id,
                    workspace_root=str(workspace_root.resolve()),
                    tool_namespace="builtin",
                    tool_name=tool_name,
                    argument_digest=arguments_digest(arguments),
                ),
                namespace_pattern="builtin",
                tool_pattern=tool_name,
                max_uses=1,
                reason="sealed test harness exact allowlist for the legacy M2 demo",
                metadata={"formal_benchmark": False, "human_intervention_count": 0},
            )
        )


def _browser_research_scenario(root: Path, out: Path) -> ScenarioRunRecord:
    state = create_task_state("M2 browser research scenario: navigate, operate, extract evidence.")
    events: list[EventRecord] = [
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.TASK_CREATED,
            node_id=state.root_node_id,
            payload={"task": to_jsonable(state)},
        )
    ]
    workspace = out / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    page = workspace / "index.html"
    target = workspace / "target.html"
    page.write_text(
        "<html><head><title>Research Index</title></head><body>"
        "<a href='target.html'>Open evidence</a><input name='query' />"
        "<p>Research landing page.</p></body></html>",
        encoding="utf-8",
    )
    target.write_text(
        "<html><head><title>Evidence</title></head><body>"
        "<p>Dynamic heterogeneous agents need traceable browser evidence.</p>"
        "</body></html>",
        encoding="utf-8",
    )
    runtime = BrowserWorkerRuntime(
        project_root=root,
        workspace_root=workspace,
        artifact_root=out / "artifacts",
    )
    run = runtime.run(
        WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="BrowserWorker",
            constraints={
                "browser_backend": "static",
                "browser_plan": [
                    {"action": "open_url", "arguments": {"url": page.resolve().as_uri()}},
                    {"action": "input_text", "arguments": {"index": 0, "text": "browser evidence"}},
                    {"action": "click_element", "arguments": {"index": 0}},
                    {"action": "search_page", "arguments": {"pattern": "traceable browser evidence"}},
                    {"action": "extract_text"},
                ],
                "allowed_schemes": ["file"],
            },
        )
    )
    events.extend(run.event_records)
    state.artifacts.extend(run.worker_result.artifacts)
    state.budget.tool_calls += int(run.worker_result.metadata.get("browser_steps") or 0)
    return _record("browser_research", run.worker_result.ok, state, events, run.worker_result.artifacts, [])


def _dynamic_control_scenario(root: Path, out: Path) -> ScenarioRunRecord:
    state = create_task_state("M2 dynamic control scenario: inject change and failure, then evaluate trace.")
    events: list[EventRecord] = [
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.TASK_CREATED,
            node_id=state.root_node_id,
            payload={"task": to_jsonable(state)},
        )
    ]
    events.extend(ensure_default_graph(state))
    for text in [
        "/change add stricter verifier evidence and preserve current run",
        "/inject browser_worker_timeout node=execute",
    ]:
        parsed = parse_slash_command(text, run_id=state.run_id, task_id=state.task_id)
        if parsed is None:
            continue
        event = control_event_from_command(parsed.control_command, node_id=state.root_node_id)
        events.append(event)
        controls = state.metadata.setdefault("control_commands", [])
        controls.append(
            {
                "event_id": event.event_id,
                "name": parsed.control_command.name,
                "arguments": parsed.control_command.arguments,
                "metadata": parsed.control_command.metadata,
                "created_at": event.created_at,
            }
        )
        if event.event_type == EventType.REQUIREMENT_CHANGE:
            events.extend(apply_requirement_change(state, event))
        if event.event_type == EventType.FAILURE_INJECTED:
            events.extend(apply_failure_injection(state, event))

    skill_event = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.SKILL_INVOKED,
        node_id=state.root_node_id,
        payload={
            "skill_invocation": {
                "skill_name": "failure-recovery",
                "source": "zyra harness + claude-code-best verifier",
                "preferred_runtime": "Verifier",
                "allowed_tools": ["trace", "checkpoint", "shell"],
                "status": "recorded",
            }
        },
    )
    events.append(skill_event)
    state.metadata.setdefault("skill_invocations", []).append(
        {
            "event_id": skill_event.event_id,
            "node_id": skill_event.node_id,
            "skill_name": "failure-recovery",
            "preferred_runtime": "Verifier",
            "allowed_tools": ["trace", "checkpoint", "shell"],
            "status": "recorded",
            "created_at": skill_event.created_at,
        }
    )

    artifact = ArtifactRef(
        kind=ArtifactKind.TRACE,
        uri=str((out / "dynamic-control-trace.md").resolve()),
        title="Dynamic control trace",
        producer_node_id=state.root_node_id,
    )
    Path(artifact.uri).parent.mkdir(parents=True, exist_ok=True)
    Path(artifact.uri).write_text(
        "# Dynamic Control Trace\n\nRequirement change and failure injection were recorded without restarting the task.\n",
        encoding="utf-8",
    )
    state.artifacts.append(artifact)
    events.append(
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.ARTIFACT_WRITTEN,
            node_id=state.root_node_id,
            payload={"artifact": to_jsonable(artifact)},
        )
    )
    return _record("dynamic_control", True, state, events, [artifact], [])


def _record(
    name: str,
    ok: bool,
    state: Any,
    events: list[EventRecord],
    artifacts: list[ArtifactRef],
    notes: list[str],
) -> ScenarioRunRecord:
    task = to_jsonable(state)
    event_dicts = [to_jsonable(event) for event in events]
    evaluation = evaluate_task_trace(task, event_dicts)
    return ScenarioRunRecord(
        name=name,
        ok=ok and evaluation["score"] > 0,
        task=task,
        events=event_dicts,
        artifacts=[to_jsonable(artifact) for artifact in artifacts],
        evaluation=evaluation,
        notes=notes,
    )


def _report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Zyra M2 Scenario Report",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- passed: `{report['passed']}/{report['scenario_count']}`",
        "",
    ]
    for scenario in report["scenarios"]:
        evaluation = scenario["evaluation"]
        metrics = evaluation["metrics"]
        lines.extend(
            [
                f"## {scenario['name']}",
                "",
                f"- ok: `{str(scenario['ok']).lower()}`",
                f"- score: `{evaluation['score']}`",
                f"- events: `{metrics['event_count']}`",
                f"- artifacts: `{metrics['artifact_count']}`",
                f"- tool_results: `{metrics['tool_result_count']}`",
                f"- browser_results: `{metrics['browser_result_count']}`",
                f"- skill_invocations: `{metrics['skill_invocation_count']}`",
                f"- requirement_changes: `{metrics['requirement_change_count']}`",
                f"- failure_injections: `{metrics['failure_injection_count']}`",
                "",
            ]
        )
    return "\n".join(lines)
