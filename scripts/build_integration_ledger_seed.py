from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_WORKSPACE = ROOT / "provenance"
INTEGRATIONS_PATH = ROOT / "packages" / "integrations"
if str(INTEGRATIONS_PATH) not in sys.path:
    sys.path.insert(0, str(INTEGRATIONS_PATH))

from zyra_integrations import (
    InternalizationLedger,
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LicenseNotice,
    LineCountPolicy,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    RuntimeEntry,
    SourceEvidence,
    TargetBinding,
    TestEntry,
    package_seed_path,
    stable_ledger_id,
)


IGNORED_DIRS = {
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "target",
    ".next",
    ".turbo",
    "coverage",
    "tmp",
}

SOURCE_SUFFIXES = {
    ".cs",
    ".css",
    ".go",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".rs",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class SourceBucket:
    repo: str
    label: str
    roots: tuple[str, ...]
    target_paths: tuple[str, ...]
    owner_unit: str
    milestone: str
    strategy: MigrationStrategy
    lifecycle: LedgerLifecycle
    status: MainPathStatus
    runtime: RuntimeEntry
    tests: tuple[TestEntry, ...]
    main_path: MainPathBinding
    line_policy: LineCountPolicy
    tags: tuple[str, ...]
    max_entries: int
    summary: str
    source_index_ref: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-workspace",
        default=str(DEFAULT_SOURCE_WORKSPACE),
        help=(
            "Source workspace used to refresh the historical seed. Defaults to "
            "the repository-local provenance snapshot; pass an explicit upstream "
            "workspace only when intentionally rebuilding the seed."
        ),
    )
    args = parser.parse_args()
    ledger = InternalizationLedger(
        build_seed_entries(source_workspace=Path(args.source_workspace))
    )
    output = package_seed_path()
    ledger.save(output)
    print(json.dumps({"output": str(output), "summary": ledger.summary().to_dict()}, ensure_ascii=False, indent=2, sort_keys=True))


def build_seed_entries(
    *,
    source_workspace: Path = DEFAULT_SOURCE_WORKSPACE,
) -> list[InternalizationLedgerEntry]:
    entries: list[InternalizationLedgerEntry] = []
    seen: set[str] = set()
    for bucket in buckets():
        for source_path in selected_paths(bucket, source_workspace=source_workspace):
            entry = entry_from_bucket(bucket, source_path)
            if entry.ledger_id in seen:
                continue
            seen.add(entry.ledger_id)
            entries.append(entry)
    entries.extend(legacy_active_entries(seen))
    return sorted(entries, key=lambda item: (item.owner_unit, item.source_repo.lower(), item.capability_name, item.source_path))


def selected_paths(bucket: SourceBucket, *, source_workspace: Path) -> list[str]:
    repo_root = source_workspace / bucket.repo
    paths: list[str] = []
    if not repo_root.exists():
        return paths
    for root in bucket.roots:
        start = repo_root / root
        if start.is_file():
            paths.append(start.relative_to(repo_root).as_posix())
            continue
        if not start.exists():
            continue
        for path in start.rglob("*"):
            if len(paths) >= bucket.max_entries:
                break
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            relative_parts = set(path.relative_to(repo_root).parts)
            if relative_parts & IGNORED_DIRS:
                continue
            paths.append(path.relative_to(repo_root).as_posix())
        if len(paths) >= bucket.max_entries:
            break
    return sorted(dict.fromkeys(paths))[: bucket.max_entries]


def entry_from_bucket(bucket: SourceBucket, source_path: str) -> InternalizationLedgerEntry:
    capability_name = f"{bucket.label}: {Path(source_path).stem}"
    entry = InternalizationLedgerEntry(
        ledger_id=stable_ledger_id(bucket.repo, source_path, capability_name),
        source_repo=bucket.repo,
        source_path=source_path,
        capability_name=capability_name,
        capability_summary=bucket.summary,
        target_bindings=[
            TargetBinding(path, role="primary" if index == 0 else "supporting")
            for index, path in enumerate(bucket.target_paths)
        ],
        migration_strategy=bucket.strategy,
        main_path_status=bucket.status,
        lifecycle=bucket.lifecycle,
        runtime_entry=bucket.runtime,
        test_entries=list(bucket.tests),
        main_path=bucket.main_path,
        line_count_policy=bucket.line_policy,
        license_notice=LicenseNotice(
            source_repo=bucket.repo,
            status=NoticeStatus.PENDING,
            license_hint=license_hint(bucket.repo),
            notice_path="third_party/NOTICE.md",
            notes="Seeded for M1-01A; must be finalized during M3 source-map freeze.",
        ),
        owner_unit=bucket.owner_unit,
        milestone=bucket.milestone,
        downstream_units=downstream_units(bucket.owner_unit),
        source_evidence=[
            SourceEvidence(
                source_repo=bucket.repo,
                source_path=source_path,
                exists_in_workspace=True,
                source_kind="file",
                reason=bucket.source_index_ref,
                tags=list(bucket.tags),
            )
        ],
        tags=list(bucket.tags),
        replacement_plan=f"Use {bucket.owner_unit} to decide direct migration, productized adapter, or rejection; do not depend on parent workspace paths at runtime.",
        metadata={
            "source_index_ref": bucket.source_index_ref,
            "generated_by": "scripts/build_integration_ledger_seed.py",
            "source_root": bucket.roots[0] if bucket.roots else "",
            "seed_scope": "M1-01A initial source-to-target ledger",
        },
    )
    return entry


def legacy_active_entries(seen: set[str]) -> list[InternalizationLedgerEntry]:
    entries: list[InternalizationLedgerEntry] = []
    specs: list[dict[str, Any]] = [
        {
            "repo": "claude-code-best",
            "source_path": "src/QueryEngine.ts",
            "capability": "Legacy M0 CodeWorker sidecar inventory",
            "summary": "Vendored Claude Code runtime is already inside zyra/vendor and exposed through CodeWorkerSidecarClient inventory.",
            "target_paths": ["apps/code-worker/src/main.mjs", "packages/workers/zyra_workers/code_worker_bridge.py", "packages/workers/zyra_workers/code_worker_runtime.py"],
            "owner_unit": "M1-02A",
            "status": MainPathStatus.WORKER_RUNTIME_CONNECTED,
            "lifecycle": LedgerLifecycle.ACTIVE,
            "strategy": MigrationStrategy.VENDORED_RUNTIME,
            "runtime": RuntimeEntry(command="node apps/code-worker/src/main.mjs", module="zyra_workers.code_worker_bridge", protocol="json-stdio", health_check="scripts/verify_code_worker_sidecar.py"),
            "tests": [TestEntry(path="tests/integration/test_code_worker_sidecar.py", command="python -m unittest tests.integration.test_code_worker_sidecar", kind="integration")],
            "main_path": MainPathBinding(surfaces=["worker_runtime", "api", "event_log"], api_routes=["GET /workers/code/inventory"], worker_runtime="CodeWorkerRuntime", event_types=["system_notice", "agent_message"]),
        },
        {
            "repo": "browser-use",
            "source_path": "browser_use/agent/service.py",
            "capability": "Legacy M0 BrowserWorker runtime bridge",
            "summary": "Vendored browser-use runtime is inside zyra/vendor and exposed through BrowserWorkerRuntime plus browser action registry.",
            "target_paths": ["packages/workers/zyra_workers/browser_use_runtime.py", "packages/workers/zyra_workers/browser_worker.py", "packages/workers/zyra_workers/browser_actions.py"],
            "owner_unit": "M1-04A",
            "status": MainPathStatus.WORKER_RUNTIME_CONNECTED,
            "lifecycle": LedgerLifecycle.ACTIVE,
            "strategy": MigrationStrategy.VENDORED_RUNTIME,
            "runtime": RuntimeEntry(module="zyra_workers.browser_use_runtime", function="inspect_browser_use_runtime", protocol="python-adapter", health_check="scripts/smoke_browser_use_runtime.py"),
            "tests": [TestEntry(path="tests/integration/test_browser_worker.py", command="python -m unittest tests.integration.test_browser_worker", kind="integration")],
            "main_path": MainPathBinding(surfaces=["worker_runtime", "api", "artifact"], api_routes=["GET /workers/browser/actions", "GET /workers/browser/health"], worker_runtime="BrowserWorkerRuntime", event_types=["agent_message", "artifact_written"]),
        },
        {
            "repo": "OpenHands",
            "source_path": "openhands/app_server/event",
            "capability": "Legacy M0 API event and artifact boundary",
            "summary": "OpenHands event/artifact server pattern is represented by Zyra API event log and artifact store.",
            "target_paths": ["apps/api/zyra_api/main.py", "packages/core/zyra_core/event_log.py", "packages/runtime/zyra_runtime/artifacts.py"],
            "owner_unit": "M1-05C",
            "status": MainPathStatus.EVENT_LOG_CONNECTED,
            "lifecycle": LedgerLifecycle.ACTIVE,
            "strategy": MigrationStrategy.REIMPLEMENTED_PATTERN,
            "runtime": RuntimeEntry(module="apps.api.zyra_api.main", function="persist_events", protocol="http-json"),
            "tests": [TestEntry(path="tests/integration/test_api_control_commands.py", command="python -m unittest tests.integration.test_api_control_commands", kind="integration")],
            "main_path": MainPathBinding(surfaces=["api", "event_log", "artifact"], api_routes=["GET /events", "GET /artifacts"], event_types=["system_notice", "artifact_written"]),
        },
        {
            "repo": "openclaw",
            "source_path": "docs/concepts/active-memory.md",
            "capability": "Legacy M0 requirement change and fault control",
            "summary": "OpenClaw active-memory and recovery concepts are represented by Zyra requirement-change and fault-injection control paths.",
            "target_paths": ["packages/symbolic/zyra_symbolic/control.py", "packages/scheduler/zyra_scheduler/recovery.py", "apps/api/zyra_api/main.py"],
            "owner_unit": "M1-07C",
            "status": MainPathStatus.CONTROL_COMMAND_CONNECTED,
            "lifecycle": LedgerLifecycle.ACTIVE,
            "strategy": MigrationStrategy.REIMPLEMENTED_PATTERN,
            "runtime": RuntimeEntry(module="zyra_symbolic.control", function="apply_requirement_change", protocol="control-command"),
            "tests": [TestEntry(path="tests/scenarios/test_m5_scheduler_fault_recovery.py", command="python -m unittest tests.scenarios.test_m5_scheduler_fault_recovery", kind="scenario")],
            "main_path": MainPathBinding(surfaces=["control_command", "task_graph", "event_log"], control_commands=["/change", "/inject"], event_types=["requirement_change", "failure_injected", "recovery_planned"]),
        },
        {
            "repo": "langgraph",
            "source_path": "libs/langgraph/langgraph/graph/state.py",
            "capability": "Legacy M0 task graph and checkpoint pattern",
            "summary": "LangGraph state graph/checkpoint ideas are represented by Zyra task graph and SQLite checkpoint store.",
            "target_paths": ["packages/orchestration/zyra_orchestration/task_graph.py", "packages/memory/zyra_memory/sqlite_store.py"],
            "owner_unit": "M1-08",
            "status": MainPathStatus.TESTED_MAIN_PATH,
            "lifecycle": LedgerLifecycle.ACTIVE,
            "strategy": MigrationStrategy.REIMPLEMENTED_PATTERN,
            "runtime": RuntimeEntry(module="zyra_orchestration.task_graph", function="run_task_graph", protocol="python-state-graph"),
            "tests": [TestEntry(path="tests/unit/test_task_graph.py", command="python -m unittest tests.unit.test_task_graph", kind="unit")],
            "main_path": MainPathBinding(surfaces=["task_graph", "checkpoint", "event_log"], event_types=["node_created", "node_updated", "topology_route"]),
        },
        {
            "repo": "hermes-agent",
            "source_path": "trajectory_compressor.py",
            "capability": "Legacy M0 trajectory compression pattern",
            "summary": "Hermes trajectory compression pattern is represented by MemoryFabric compact and replay paths.",
            "target_paths": ["packages/memory/zyra_memory/fabric.py", "apps/api/zyra_api/main.py"],
            "owner_unit": "M1-06C",
            "status": MainPathStatus.API_CONNECTED,
            "lifecycle": LedgerLifecycle.ACTIVE,
            "strategy": MigrationStrategy.REIMPLEMENTED_PATTERN,
            "runtime": RuntimeEntry(module="zyra_memory.fabric", function="MemoryFabric.compact_context", protocol="python-memory"),
            "tests": [TestEntry(path="tests/unit/test_memory_fabric.py", command="python -m unittest tests.unit.test_memory_fabric", kind="unit")],
            "main_path": MainPathBinding(surfaces=["memory", "api", "artifact"], api_routes=["POST /tasks/{task_id}/memory/compact", "GET /tasks/{task_id}/trajectory"], event_types=["system_notice"]),
        },
        {
            "repo": "agentscope",
            "source_path": "src/agentscope/workspace",
            "capability": "Legacy M0 worker manifest and workspace pattern",
            "summary": "AgentScope workspace/manifest ideas are represented by WorkerPool and ResourceScheduler.",
            "target_paths": ["packages/scheduler/zyra_scheduler/pool.py", "packages/scheduler/zyra_scheduler/scheduler.py"],
            "owner_unit": "M1-07A",
            "status": MainPathStatus.API_CONNECTED,
            "lifecycle": LedgerLifecycle.ACTIVE,
            "strategy": MigrationStrategy.REIMPLEMENTED_PATTERN,
            "runtime": RuntimeEntry(module="zyra_scheduler.pool", function="WorkerPool.manifests", protocol="python-scheduler"),
            "tests": [TestEntry(path="tests/unit/test_scheduler.py", command="python -m unittest tests.unit.test_scheduler", kind="unit")],
            "main_path": MainPathBinding(surfaces=["scheduler", "api", "worker_runtime"], api_routes=["GET /scheduler/manifests"], event_types=["resource_decision", "worker_health"]),
        },
        {
            "repo": "agent-framework",
            "source_path": "docs/decisions/0019-python-context-compaction-strategy.md",
            "capability": "Legacy M0 compact-aware recovery pattern",
            "summary": "Agent Framework compact and feedback control ideas are represented by MemoryFabric and RecoveryPlanner.",
            "target_paths": ["packages/memory/zyra_memory/fabric.py", "packages/scheduler/zyra_scheduler/recovery.py", "packages/evaluation/zyra_evaluation/trace.py"],
            "owner_unit": "M1-07C",
            "status": MainPathStatus.API_CONNECTED,
            "lifecycle": LedgerLifecycle.ACTIVE,
            "strategy": MigrationStrategy.REIMPLEMENTED_PATTERN,
            "runtime": RuntimeEntry(module="zyra_scheduler.recovery", function="RecoveryPlanner.plan", protocol="python-recovery"),
            "tests": [TestEntry(path="tests/unit/test_scheduler.py", command="python -m unittest tests.unit.test_scheduler", kind="unit")],
            "main_path": MainPathBinding(surfaces=["scheduler", "memory", "evaluation"], api_routes=["GET /tasks/{task_id}/recovery"], event_types=["recovery_planned", "evaluation"]),
        },
    ]
    for spec in specs:
        entry = InternalizationLedgerEntry(
            ledger_id=stable_ledger_id(spec["repo"], spec["source_path"], spec["capability"]),
            source_repo=spec["repo"],
            source_path=spec["source_path"],
            capability_name=spec["capability"],
            capability_summary=spec["summary"],
            target_bindings=[TargetBinding(path, role="primary" if index == 0 else "supporting") for index, path in enumerate(spec["target_paths"])],
            migration_strategy=spec["strategy"],
            main_path_status=spec["status"],
            lifecycle=spec["lifecycle"],
            runtime_entry=spec["runtime"],
            test_entries=spec["tests"],
            main_path=spec["main_path"],
            line_count_policy=LineCountPolicy.COUNTS_AS_RUNTIME,
            license_notice=LicenseNotice(
                source_repo=spec["repo"],
                status=NoticeStatus.RECORDED,
                license_hint=license_hint(spec["repo"]),
                notice_path="third_party/NOTICE.md",
                notes="Legacy M0 source mapping is recorded here; M3 must consolidate final NOTICE text.",
            ),
            owner_unit=spec["owner_unit"],
            milestone="M1",
            downstream_units=downstream_units(spec["owner_unit"]),
            source_evidence=[SourceEvidence(source_repo=spec["repo"], source_path=spec["source_path"], exists_in_workspace=(WORKSPACE / spec["repo"] / spec["source_path"]).exists(), reason="legacy M0 source-to-target ledger")],
            tags=["legacy-m0", "main-path", "seeded"],
            metadata={"source_index_ref": "docs/比赛项目开源Agent架构借鉴分析.md#internalization-index", "legacy": True},
        )
        if entry.ledger_id not in seen:
            entries.append(entry)
            seen.add(entry.ledger_id)
    return entries


def buckets() -> list[SourceBucket]:
    b: list[SourceBucket] = []
    b.extend(claude_buckets())
    b.extend(browser_buckets())
    b.extend(openhands_buckets())
    b.extend(openclaw_buckets())
    b.extend(agentscope_buckets())
    b.extend(agent_framework_buckets())
    b.extend(hermes_buckets())
    b.extend(langgraph_buckets())
    return b


def claude_buckets() -> list[SourceBucket]:
    return [
        bucket("claude-code-best", "query-session-tool-loop", ("src/query", "src/QueryEngine.ts", "src/query.ts", "src/tools", "src/Tool.ts"), ("packages/workers/zyra_workers/code_query_loop.py", "packages/runtime/zyra_runtime/tools.py"), "M1-02B", MigrationStrategy.VENDORED_RUNTIME, "Query/session/tool loop source candidates for CodeWorkerRuntime.", "m2-runtime-index", max_entries=44, tags=("claude", "code-worker", "tool-loop")),
        bucket("claude-code-best", "permission-runtime", ("src/hooks/toolPermission", "src/cli/src/utils/permissions", "src/bridge"), ("packages/runtime/zyra_runtime/permissions.py", "packages/commands/zyra_commands/registry.py"), "M1-03A", MigrationStrategy.VENDORED_RUNTIME, "Allow/deny/ask permission source candidates.", "m2-runtime-index", max_entries=36, tags=("claude", "permission")),
        bucket("claude-code-best", "mcp-runtime", ("src/services/mcp", "src/commands/mcp"), ("packages/integrations/zyra_integrations/mcp_runtime.py", "packages/runtime/zyra_runtime/tools.py"), "M1-03B", MigrationStrategy.VENDORED_RUNTIME, "MCP client/config/auth/resources/tools/prompts candidates.", "m2-runtime-index", max_entries=36, tags=("claude", "mcp")),
        bucket("claude-code-best", "skills-subagents-commands", ("src/tools/SkillTool", "src/tools/AgentTool", "src/commands", "src/skills"), ("packages/skills/zyra_skills/registry.py", "packages/workers/zyra_workers/code_worker_runtime.py", "packages/commands/zyra_commands/registry.py"), "M1-03D", MigrationStrategy.VENDORED_RUNTIME, "SkillTool, AgentTool, slash commands, forked subagent source candidates.", "m2-runtime-index", max_entries=48, tags=("claude", "skills", "subagents", "commands")),
        bucket("claude-code-best", "context-compact-session", ("src/services/compact", "src/context", "src/state", "src/memdir"), ("packages/memory/zyra_memory/fabric.py", "packages/runtime/zyra_runtime/session.py"), "M1-02D", MigrationStrategy.VENDORED_RUNTIME, "Context construction, compact, restore, session state candidates.", "m4-memory-index", max_entries=36, tags=("claude", "compact", "session")),
    ]


def browser_buckets() -> list[SourceBucket]:
    return [
        bucket("browser-use", "browser-agent-session", ("browser_use/agent", "browser_use/browser"), ("packages/workers/zyra_workers/browser_use_runtime.py", "packages/workers/zyra_workers/browser_worker.py"), "M1-04A", MigrationStrategy.VENDORED_RUNTIME, "Browser-use agent loop and browser session candidates.", "m2-runtime-index", max_entries=44, tags=("browser-use", "browser-worker")),
        bucket("browser-use", "message-manager-state", ("browser_use/agent/message_manager", "browser_use/dom", "browser_use/screenshots"), ("packages/workers/zyra_workers/browser_worker.py", "packages/memory/zyra_memory/fabric.py"), "M1-04B", MigrationStrategy.VENDORED_RUNTIME, "Message manager, DOM compression, screenshot artifact candidates.", "m2-runtime-index", max_entries=36, tags=("browser-use", "dom", "memory")),
        bucket("browser-use", "action-registry-watchdogs", ("browser_use/tools", "browser_use/controller", "browser_use/browser/watchdogs", "browser_use/mcp", "browser_use/skills"), ("packages/workers/zyra_workers/browser_actions.py", "packages/scheduler/zyra_scheduler/watchdog.py", "packages/runtime/zyra_runtime/permissions.py"), "M1-04D", MigrationStrategy.VENDORED_RUNTIME, "Action registry, controller, watchdog, tool permission candidates.", "m5-scheduler-fault-index", max_entries=44, tags=("browser-use", "watchdog", "actions")),
    ]


def openhands_buckets() -> list[SourceBucket]:
    return [
        bucket("OpenHands", "event-conversation-artifact", ("openhands/app_server/event", "openhands/app_server/app_conversation", "openhands/app_server/file_store", "openhands/storage"), ("apps/api/zyra_api/main.py", "packages/core/zyra_core/event_log.py", "packages/runtime/zyra_runtime/artifacts.py"), "M1-05C", MigrationStrategy.PLANNED_ADAPTER, "OpenHands app event, conversation, file/artifact store candidates.", "m5-scheduler-fault-index", max_entries=40, tags=("openhands", "event-stream", "artifact")),
        bucket("OpenHands", "sandbox-runtime-server", ("openhands/runtime", "openhands/sandbox", "containers", "dev_config"), ("packages/scheduler/zyra_scheduler/backends.py", "packages/runtime/zyra_runtime/executor.py"), "M1-05B", MigrationStrategy.PLANNED_ADAPTER, "Sandbox/runtime backend candidates.", "m5-scheduler-fault-index", max_entries=34, tags=("openhands", "sandbox", "workspace")),
        bucket("OpenHands", "frontend-console-panels", ("frontend/src", "openhands-ui/src"), ("apps/web/src/features/events", "apps/web/src/features/artifacts", "apps/web/src/features/commands"), "M2-01A", MigrationStrategy.PLANNED_ADAPTER, "Control console API client, event, terminal/browser/file panel candidates.", "m6-ui-index", max_entries=46, tags=("openhands", "ui", "console")),
    ]


def openclaw_buckets() -> list[SourceBucket]:
    return [
        bucket("openclaw", "acp-control-runtime", ("src/acp", "packages/agent-core", "docs/concepts", "docs/gateway"), ("packages/runtime/zyra_runtime/control.py", "packages/scheduler/zyra_scheduler/recovery.py", "packages/orchestration/zyra_orchestration/task_graph.py"), "M1-05D", MigrationStrategy.PLANNED_ADAPTER, "ACP control plane, runtime registry, backend failover, policy and active-memory candidates.", "m5-scheduler-fault-index", max_entries=52, tags=("openclaw", "acp", "gateway")),
        bucket("openclaw", "trajectory-compaction-ui", ("docs/tools", "docs/concepts/compaction.md", "apps", "ui"), ("packages/memory/zyra_memory/fabric.py", "apps/web/src/features/trajectory"), "M2-03B", MigrationStrategy.PLANNED_ADAPTER, "Trajectory replay, compaction, UI turn-stream candidates.", "m6-ui-index", max_entries=30, tags=("openclaw", "trajectory", "compaction")),
    ]


def agentscope_buckets() -> list[SourceBucket]:
    return [
        bucket("agentscope", "service-workspace-permission", ("src/agentscope/app", "src/agentscope/workspace", "src/agentscope/permission", "src/agentscope/middleware"), ("packages/scheduler/zyra_scheduler/pool.py", "packages/runtime/zyra_runtime/permissions.py", "apps/api/zyra_api/main.py"), "M1-05A", MigrationStrategy.PLANNED_ADAPTER, "AgentScope service router, workspace, permission and middleware candidates.", "m5-scheduler-fault-index", max_entries=48, tags=("agentscope", "workspace", "permission")),
        bucket("agentscope", "rag-mcp-skill", ("src/agentscope/rag", "src/agentscope/app/rag", "src/agentscope/mcp", "src/agentscope/skill", "src/agentscope/tool"), ("packages/memory/zyra_memory", "packages/integrations/zyra_integrations", "packages/skills/zyra_skills/registry.py"), "M1-06A", MigrationStrategy.PLANNED_ADAPTER, "RAG, MCP, skill and tool registry candidates.", "m4-memory-index", max_entries=44, tags=("agentscope", "rag", "mcp", "skills")),
    ]


def agent_framework_buckets() -> list[SourceBucket]:
    return [
        bucket("agent-framework", "python-tools-skills-mcp", ("python/packages/core/agent_framework", "python/packages/ag-ui", "docs/decisions"), ("packages/runtime/zyra_runtime/tools.py", "packages/skills/zyra_skills/registry.py", "packages/evaluation/zyra_evaluation/trace.py"), "M1-03C", MigrationStrategy.PLANNED_ADAPTER, "Agent Framework tools, skills, MCP, middleware, evaluation and AG-UI candidates.", "m2-runtime-index", max_entries=52, tags=("agent-framework", "skills", "evaluation")),
        bucket("agent-framework", "dotnet-loop-evaluator", ("dotnet/src/Microsoft.Agents.AI/Harness/Loop", "dotnet/src/Microsoft.Agents.AI/Evaluation", "docs/features"), ("packages/evaluation/zyra_evaluation/trace.py", "packages/scheduler/zyra_scheduler/recovery.py"), "M1-07C", MigrationStrategy.REIMPLEMENTED_PATTERN, "LoopAgent, LoopEvaluator, durable evaluation candidates.", "m5-scheduler-fault-index", max_entries=34, tags=("agent-framework", "loop-evaluator", "durable")),
    ]


def hermes_buckets() -> list[SourceBucket]:
    return [
        bucket("hermes-agent", "memory-trajectory-curator", ("agent", "trajectory_compressor.py", "docs/session-lifecycle.md", "docs/middleware"), ("packages/memory/zyra_memory/fabric.py", "packages/memory/zyra_memory/curator.py", "packages/skills/zyra_skills/registry.py"), "M1-06B", MigrationStrategy.PLANNED_ADAPTER, "Memory manager, curator, trajectory compressor, middleware candidates.", "m4-memory-index", max_entries=46, tags=("hermes", "memory", "curator")),
        bucket("hermes-agent", "environment-error-budget", ("tools/environments", "agent/error_classifier.py", "agent/iteration_budget.py", "agent/credential_pool.py", "acp_adapter"), ("packages/scheduler/zyra_scheduler/backends.py", "packages/scheduler/zyra_scheduler/watchdog.py", "packages/scheduler/zyra_scheduler/recovery.py"), "M1-07B", MigrationStrategy.PLANNED_ADAPTER, "Environment backend, error classifier, budget and credential pool candidates.", "m5-scheduler-fault-index", max_entries=36, tags=("hermes", "environment", "recovery")),
    ]


def langgraph_buckets() -> list[SourceBucket]:
    return [
        bucket("langgraph", "state-graph-pregel-checkpoint", ("libs/langgraph/langgraph/graph", "libs/langgraph/langgraph/pregel", "libs/checkpoint", "libs/checkpoint-sqlite"), ("packages/orchestration/zyra_orchestration/task_graph.py", "packages/memory/zyra_memory/sqlite_store.py", "packages/symbolic/zyra_symbolic/topology.py"), "M1-08", MigrationStrategy.REIMPLEMENTED_PATTERN, "StateGraph, Pregel execution, checkpoint/store candidates.", "m0-m1-foundation-index", max_entries=58, tags=("langgraph", "task-graph", "checkpoint")),
        bucket("langgraph", "store-sdk-examples", ("libs/sdk-py", "examples", "docs"), ("packages/memory/zyra_memory", "packages/orchestration/zyra_orchestration/task_graph.py"), "M1-06A", MigrationStrategy.REIMPLEMENTED_PATTERN, "Store, SDK, interrupt/replay and examples for long-horizon orchestration.", "m4-memory-index", max_entries=26, tags=("langgraph", "store", "replay")),
    ]


def bucket(
    repo: str,
    label: str,
    roots: tuple[str, ...],
    target_paths: tuple[str, ...],
    owner_unit: str,
    strategy: MigrationStrategy,
    summary: str,
    source_index_ref: str,
    *,
    max_entries: int,
    tags: tuple[str, ...],
) -> SourceBucket:
    lifecycle = LedgerLifecycle.PLANNED
    status = MainPathStatus.PLANNED
    if repo in {"claude-code-best", "browser-use"}:
        status = MainPathStatus.VENDORED
    return SourceBucket(
        repo=repo,
        label=label,
        roots=roots,
        target_paths=target_paths,
        owner_unit=owner_unit,
        milestone="M1" if owner_unit.startswith("M1") else "M2",
        strategy=strategy,
        lifecycle=lifecycle,
        status=status,
        runtime=planned_runtime(owner_unit, repo, label),
        tests=(planned_test(owner_unit, repo, label),),
        main_path=planned_main_path(owner_unit, repo, label),
        line_policy=LineCountPolicy.COUNTS_WHEN_PRODUCTIZED,
        tags=tags,
        max_entries=max_entries,
        summary=summary,
        source_index_ref=f"docs/比赛项目开源Agent架构借鉴分析.md#{source_index_ref}",
    )


def planned_runtime(owner_unit: str, repo: str, label: str) -> RuntimeEntry:
    module = "planned"
    if owner_unit.startswith("M1-02"):
        module = "zyra_workers.code_worker_runtime"
    elif owner_unit.startswith("M1-03"):
        module = "zyra_runtime.tools"
    elif owner_unit.startswith("M1-04"):
        module = "zyra_workers.browser_worker"
    elif owner_unit.startswith("M1-05"):
        module = "zyra_scheduler.backends"
    elif owner_unit.startswith("M1-06"):
        module = "zyra_memory.fabric"
    elif owner_unit.startswith("M1-07"):
        module = "zyra_scheduler.recovery"
    elif owner_unit.startswith("M2"):
        module = "apps.web"
    return RuntimeEntry(
        module=module,
        function=f"planned_{label.replace('-', '_')}",
        protocol="planned-adapter",
        health_check=f"scripts/verify_{owner_unit.lower().replace('-', '_')}.py",
        config_refs=[f"ledger:{repo}:{owner_unit}"],
    )


def planned_test(owner_unit: str, repo: str, label: str) -> TestEntry:
    safe = label.replace("-", "_")
    folder = "integration" if owner_unit.startswith("M2") or owner_unit.startswith("M1-04") else "unit"
    return TestEntry(
        path=f"tests/{folder}/test_{owner_unit.lower().replace('-', '_')}_{safe}.py",
        command=f"python -m unittest tests.{folder}.test_{owner_unit.lower().replace('-', '_')}_{safe}",
        kind=folder,
        expected_signal=f"{repo} {label} ledger entry remains queryable and auditable",
        required=True,
    )


def planned_main_path(owner_unit: str, repo: str, label: str) -> MainPathBinding:
    surfaces = ["ledger"]
    event_types = ["system_notice"]
    api_routes = ["GET /ledger", "GET /ledger/audit"]
    controls: list[str] = []
    worker_runtime = ""
    ui_panels: list[str] = []
    if owner_unit.startswith("M1-02"):
        surfaces.extend(["worker_runtime", "event_log"])
        worker_runtime = "CodeWorkerRuntime"
    elif owner_unit.startswith("M1-03"):
        surfaces.extend(["runtime", "control_command"])
        controls.extend(["/mcp", "/skills", "/agents"])
    elif owner_unit.startswith("M1-04"):
        surfaces.extend(["worker_runtime", "artifact"])
        worker_runtime = "BrowserWorkerRuntime"
    elif owner_unit.startswith("M1-05"):
        surfaces.extend(["scheduler", "gateway"])
    elif owner_unit.startswith("M1-06"):
        surfaces.extend(["memory", "artifact"])
    elif owner_unit.startswith("M1-07"):
        surfaces.extend(["scheduler", "recovery"])
        controls.extend(["/inject", "/change"])
    elif owner_unit.startswith("M2"):
        surfaces.extend(["ui", "api"])
        ui_panels.extend(["event_stream", "artifact_viewer", "command_palette"])
    return MainPathBinding(
        surfaces=surfaces,
        event_types=event_types,
        api_routes=api_routes,
        control_commands=controls,
        worker_runtime=worker_runtime,
        ui_panels=ui_panels,
    )


def downstream_units(owner_unit: str) -> list[str]:
    if owner_unit == "M1-01A":
        return ["M1-01B"]
    number = owner_unit.split("-", 1)[-1]
    if owner_unit.startswith("M1"):
        return ["M1-08", "M2-01A"]
    if owner_unit.startswith("M2"):
        return ["M2-05", "M3-01A"]
    return []


def license_hint(repo: str) -> str:
    hints = {
        "claude-code-best": "source snapshot; finalize attribution in M3",
        "browser-use": "source snapshot; finalize attribution in M3",
        "OpenHands": "source reference; finalize attribution in M3",
        "openclaw": "source reference; finalize attribution in M3",
        "agentscope": "source reference; finalize attribution in M3",
        "agent-framework": "source reference; finalize attribution in M3",
        "hermes-agent": "source reference; finalize attribution in M3",
        "langgraph": "source reference; finalize attribution in M3",
    }
    return hints.get(repo, "pending")


if __name__ == "__main__":
    main()
