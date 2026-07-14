from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_FILES = (
    "apps/code-worker/src/main.ts",
    "packages/integrations/claude-mcp/src/client.ts",
    "packages/integrations/claude-mcp/src/runtime.ts",
    "packages/integrations/claude-mcp/src/transport.ts",
    "packages/runtime/claude-runtime/src/capabilities.ts",
    "packages/runtime/claude-runtime/src/capability-host.ts",
    "packages/runtime/claude-runtime/src/protocol.ts",
    "packages/runtime/claude-runtime/src/query-engine.ts",
    "packages/runtime/claude-runtime/src/session.ts",
    "packages/runtime/claude-runtime/src/tools.ts",
    "packages/runtime/claude-runtime/src/budget.ts",
    "packages/runtime/claude-runtime/src/agents/contracts.ts",
    "packages/runtime/claude-runtime/src/agents/memory.ts",
    "packages/runtime/claude-runtime/src/agents/definitions.ts",
    "packages/runtime/claude-runtime/src/agents/scope.ts",
    "packages/runtime/claude-runtime/src/agents/fork.ts",
    "packages/runtime/claude-runtime/src/agents/run-agent.ts",
    "packages/runtime/claude-runtime/src/agents/resume.ts",
    "packages/runtime/claude-runtime/src/agents/lifecycle.ts",
    "packages/runtime/claude-runtime/src/agents/agent-tool.ts",
    "packages/runtime/claude-runtime/src/agents/index.ts",
    "packages/runtime/claude-runtime/src/control/contracts.ts",
    "packages/runtime/claude-runtime/src/control/runtime.ts",
    "packages/runtime/claude-runtime/src/control/index.ts",
    "packages/runtime/claude-runtime/src/permission/canonical.ts",
    "packages/runtime/claude-runtime/src/permission/policy.ts",
    "packages/runtime/claude-runtime/src/skills/frontmatter.ts",
    "packages/runtime/claude-runtime/src/skills/runtime.ts",
    "packages/workers/zyra_workers/typescript_claude_runtime.py",
    "packages/workers/zyra_workers/subagents/typescript_port.py",
)


def audit(project_root: Path) -> dict[str, Any]:
    missing = [path for path in REQUIRED_FILES if not (project_root / path).is_file()]
    worker_text = (
        project_root
        / "packages"
        / "workers"
        / "zyra_workers"
        / "code_worker_runtime.py"
    ).read_text(encoding="utf-8")
    host_text = (
        project_root
        / "packages"
        / "workers"
        / "zyra_workers"
        / "typescript_claude_runtime.py"
    ).read_text(encoding="utf-8")
    api_text = (
        project_root / "apps" / "api" / "zyra_api" / "main.py"
    ).read_text(encoding="utf-8")
    agent_port_text = (
        project_root
        / "packages"
        / "workers"
        / "zyra_workers"
        / "subagents"
        / "typescript_port.py"
    ).read_text(encoding="utf-8")
    permission_text = (
        project_root
        / "packages"
        / "runtime"
        / "zyra_runtime"
        / "permission"
        / "runtime.py"
    ).read_text(encoding="utf-8")
    typescript_text = "\n".join(
        (project_root / path).read_text(encoding="utf-8")
        for path in REQUIRED_FILES
        if path.endswith(".ts") and (project_root / path).is_file()
    )
    findings: list[dict[str, str]] = []
    if "query_engine_factory: Any | None = TypeScriptClaudeQueryEngine" not in worker_text:
        findings.append(
            {
                "code": "default_owner_not_typescript",
                "message": "CodeWorkerRuntime default factory is not TypeScriptClaudeQueryEngine.",
            }
        )
    if "query_engine_factory: Any | None = ZyraClaudeQueryEngine" in worker_text:
        findings.append(
            {
                "code": "python_default_owner_present",
                "message": "The old Python QueryEngine remains a default owner.",
            }
        )
    if (project_root / "apps" / "code-worker" / "src" / "main.mjs").exists():
        findings.append(
            {
                "code": "legacy_inspection_sidecar_present",
                "message": "The legacy main.mjs inspection sidecar still exists.",
            }
        )
    required_markers = {
        "typescript_permission_policy": (
            "packages/runtime/claude-runtime/src/permission/policy.ts",
            'canonical_owner: "typescript"',
        ),
        "typescript_capability_execution": (
            "packages/runtime/claude-runtime/src/capability-host.ts",
            "python_capability_fallback",
        ),
        "capability_settlement_request": (
            "packages/runtime/claude-runtime/src/protocol.ts",
            '"tool.settle"',
        ),
        "capability_settlement_response": (
            "packages/runtime/claude-runtime/src/protocol.ts",
            '"tool.settle.result"',
        ),
        "python_durable_permission_commit": (
            "packages/runtime/zyra_runtime/permission/runtime.py",
            "commit_typescript_decision",
        ),
        "python_policy_snapshot_export": (
            "packages/runtime/zyra_runtime/permission/runtime.py",
            "typescript_policy_snapshot",
        ),
        "python_settlement_consumer": (
            "packages/workers/zyra_workers/typescript_claude_runtime.py",
            "_settle_typescript_capability",
        ),
        "typescript_skill_projection": (
            "packages/workers/zyra_workers/code_worker_runtime.py",
            '"skill_tool_projection": "typescript_runtime_owner"',
        ),
        "python_skill_fallback_disabled": (
            "packages/workers/zyra_workers/code_worker_runtime.py",
            '"python_skill_projection_used": "false"',
        ),
        "legacy_python_capabilities_guarded": (
            "packages/workers/zyra_workers/code_worker_runtime.py",
            "not typescript_capability_owner",
        ),
        "typescript_agent_tool_owner": (
            "packages/runtime/claude-runtime/src/agents/agent-tool.ts",
            "class TypeScriptAgentRuntime",
        ),
        "typescript_agent_mutation_protocol": (
            "packages/runtime/claude-runtime/src/protocol.ts",
            '"agent.mutate"',
        ),
        "typescript_control_state_owner": (
            "packages/runtime/claude-runtime/src/query-engine.ts",
            "control_state_revision",
        ),
        "python_agent_durable_port": (
            "packages/workers/zyra_workers/subagents/typescript_port.py",
            "class TypeScriptAgentDurablePort",
        ),
        "python_agent_mutation_transport": (
            "packages/workers/zyra_workers/typescript_claude_runtime.py",
            '"agent.mutate"',
        ),
        "typescript_agent_durable_hydration": (
            "packages/runtime/claude-runtime/src/agents/agent-tool.ts",
            'action: "load"',
        ),
        "typescript_agent_api_control_route": (
            "apps/api/zyra_api/main.py",
            '"resume": "agent_resume"',
        ),
        "agent_child_event_domain_separation": (
            "packages/workers/zyra_workers/typescript_claude_runtime.py",
            '"agent_child_query_session"',
        ),
        "python_agent_shared_cas_lock": (
            "packages/workers/zyra_workers/subagents/typescript_port.py",
            "_shared_port_lock",
        ),
    }
    marker_sources = {
        "packages/workers/zyra_workers/code_worker_runtime.py": worker_text,
        "packages/workers/zyra_workers/typescript_claude_runtime.py": host_text,
        "packages/runtime/zyra_runtime/permission/runtime.py": permission_text,
        "packages/workers/zyra_workers/subagents/typescript_port.py": agent_port_text,
        "apps/api/zyra_api/main.py": api_text,
        **{
            path: (project_root / path).read_text(encoding="utf-8")
            for path in REQUIRED_FILES
            if path.endswith(".ts") and (project_root / path).is_file()
        },
    }
    for code, (path, marker) in required_markers.items():
        if marker not in marker_sources.get(path, ""):
            findings.append(
                {
                    "code": code,
                    "message": f"Required custody marker is absent from {path}: {marker}",
                }
            )
    if "permission_runtime.guard(" in host_text:
        findings.append(
            {
                "code": "python_permission_policy_fallback",
                "message": "TypeScript host invokes the legacy Python permission evaluator.",
            }
        )
    if "pending_typescript_settlements[tool_call_id]" not in host_text:
        findings.append(
            {
                "code": "typescript_capability_settlement_not_durable",
                "message": "Permission commit is not held pending until TypeScript capability settlement.",
            }
        )
    for constructor in (
        "AgentToolRuntime(",
        "SubagentRuntime(",
        "CodeWorkerSubagentExecutionPort(",
        "LogicalFanoutRuntime(",
        "ParentScopeBuilder(",
    ):
        if constructor in worker_text or constructor in api_text:
            findings.append(
                {
                    "code": "python_agent_default_owner_present",
                    "message": f"Default CodeWorker/API path still constructs legacy Python owner: {constructor}",
                }
            )
    if "get_typescript_agent_port().handle(" in api_text:
        findings.append(
            {
                "code": "python_agent_api_mutation_bypass",
                "message": "API invokes the Python durable port as a logical Agent control runtime.",
            }
        )
    for marker in (
        "../claude-code-best",
        "..\\claude-code-best",
        "vendor/claude-code-best",
        "vendor-runtimes/claude-code-runtime",
    ):
        if marker in typescript_text:
            findings.append(
                {
                    "code": "forbidden_source_dependency",
                    "message": f"TypeScript runtime contains forbidden source path: {marker}",
                }
            )
    return {
        "ok": not missing and not findings,
        "runtime_id": "zyra-typescript-claude-runtime",
        "canonical_owner": "typescript",
        "required_files": list(REQUIRED_FILES),
        "missing_files": missing,
        "findings": findings,
        "boundaries": {
            "query_loop": "typescript",
            "session_lifecycle": "typescript",
            "tool_registry_scheduler_budget": "typescript",
            "context_compact_restore": "typescript",
            "permission_policy_and_request_binding": "typescript",
            "mcp_transport_catalog_and_execution": "typescript",
            "skill_command_discovery_and_execution": "typescript",
            "agent_definition_scope_fork_run_resume_fanout": "typescript",
            "codeworker_local_control_state": "typescript",
            "permission_durable_commit_and_continuation": "python",
            "agent_durable_record_and_workspace_validation": "python",
            "builtin_tool_side_effects": "python",
            "event_artifact_checkpoint_projection": "python",
            "python_query_engine_fallback": False,
            "python_permission_policy_fallback": False,
            "python_mcp_skill_execution_fallback": False,
            "python_agent_logical_runtime_fallback": False,
            "legacy_python_agent_modules": "retained_non_default_compatibility_only",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit(args.project_root.resolve())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("typescript runtime custody:", "PASS" if report["ok"] else "FAIL")
        for finding in report["findings"]:
            print(f"- {finding['code']}: {finding['message']}")
        for path in report["missing_files"]:
            print(f"- missing: {path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
