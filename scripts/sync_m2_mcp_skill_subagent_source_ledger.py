from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for package_path in (ROOT / "packages" / "core", ROOT / "packages" / "integrations"):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import InternalizationLedger, InternalizationLedgerEntry  # noqa: E402
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402


HELPER_PATH = ROOT / "scripts" / "sync_m2_causal_trace_source_ledger.py"
HELPER_SPEC = importlib.util.spec_from_file_location(
    "m2_extension_panel_ledger_helpers",
    HELPER_PATH,
)
if HELPER_SPEC is None or HELPER_SPEC.loader is None:
    raise RuntimeError("M2 ledger helper could not be loaded")
HELPER = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(HELPER)

DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNER_UNIT = "M2-S04B-02"
BASELINE_COMMIT = "2d3d8c16075f1dda298831578cd339478f63afad"
DECISION_COMMIT = "d17bcdee550db9742abe975672a7a58c12675bb4"
IMPLEMENTATION_COMMIT = "21b1165df4fbb941391d97f4aac304c13c7e312e"
STAMP = "2026-07-25T00:00:00.000Z"
TESTS = (
    "apps/web/test/mcp-panel-integration.test.ts",
    "apps/web/test/skill-workbench-integration.test.ts",
    "apps/web/test/subagent-panel-integration.test.ts",
    "packages/runtime/claude-runtime/test/e02/command-owner-lifecycle.behavior.test.ts",
    "tests/integration/test_e02_typescript_api_cutover.py",
    "tests/integration/test_worker_pool_api_main_path.py",
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "claude-code-best",
        "source_commit": "c57f5a29e88e9a814bea47abeb9a0a6f725dc102",
        "source_language": "typescript/tsx",
        "target_language": "typescript/tsx",
        "source_path": (
            "src/services/mcp;src/tools/SkillTool;src/tools/AgentTool;"
            "src/services/agent;src/screens/REPL.tsx"
        ),
        "capability_name": "mcp_skill_subagent_console_runtime",
        "capability_summary": (
            "Canonical MCP, skill and subagent projection, lifecycle, control, "
            "sealed denial and later-effect reconciliation."
        ),
        "targets": [
            "apps/web/src/features/mcp/controller.ts",
            "apps/web/src/features/mcp/projection.ts",
            "apps/web/src/features/skills/controller.ts",
            "apps/web/src/features/skills/projection.ts",
            "apps/web/src/features/subagents/control.ts",
            "apps/web/src/features/subagents/projection.ts",
            "packages/commands/src/registry.ts",
            "packages/commands/zyra_commands/runtime/owner_handlers.py",
            "packages/integrations/claude-mcp/src/connection/connection-runtime.ts",
            "packages/runtime/claude-runtime/src/e02/api-port-runtime.ts",
            "packages/runtime/claude-runtime/src/e02/coordinator.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": (
            "retained_control_flow_adapt/cropped_migration/"
            "same_language_component_integration"
        ),
        "migration_strategy": "direct_port",
        "rationale": (
            "The TypeScript MCP/SkillTool/AgentTool control flow is retained and "
            "adapted to Zyra command receipts, permissions and canonical selectors."
        ),
    },
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_language": "typescript/tsx",
        "target_language": "typescript/tsx",
        "source_path": (
            "packages/app/src/components;packages/app/src/pages/session.tsx;"
            "packages/core/src/session"
        ),
        "capability_name": "extension_catalog_and_child_hierarchy_supplement",
        "capability_summary": (
            "Revision-bound catalog paging, status presentation and child-run "
            "hierarchy without another runtime or store owner."
        ),
        "targets": [
            "apps/web/src/features/mcp/catalog.ts",
            "apps/web/src/features/skills/catalog.ts",
            "apps/web/src/features/subagents/hierarchy.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration/same_language_component_integration",
        "migration_strategy": "direct_port",
        "rationale": "OpenCode fills bounded catalog and hierarchy presentation gaps only.",
    },
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_language": "python",
        "target_language": "typescript/tsx",
        "source_path": "gateway/mcp;agent/skills;skills/provenance;skills/security",
        "capability_name": "mcp_auth_skill_provenance_supply_chain_supplement",
        "capability_summary": (
            "Credential-presence admission, skill hash/provenance and bounded "
            "supply-chain policy expressed at the existing TypeScript UI boundary."
        ),
        "targets": [
            "apps/web/src/features/mcp/auth.ts",
            "apps/web/src/features/skills/provenance.ts",
            "apps/web/src/features/skills/supply-chain.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "bounded_cross_language_semantic_port/cropped_migration",
        "migration_strategy": "semantic_port",
        "rationale": (
            "A bounded semantic port avoids a forwarding-only Python facade and "
            "does not move MCP, skill or credential custody."
        ),
    },
    {
        "source_repo": "agent-framework",
        "source_commit": "source-graph-frozen",
        "source_language": "python/csharp",
        "target_language": "none",
        "source_path": "workflow/checkpoint and AG-UI approval/history contracts",
        "capability_name": "approval_history_conformance",
        "capability_summary": "Challenges permission-bound control and exact lifecycle history.",
        "targets": list(TESTS),
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": "Conformance only; no framework workflow or store owner is migrated.",
    },
    {
        "source_repo": "agentscope",
        "source_commit": "source-graph-frozen",
        "source_language": "python",
        "target_language": "none",
        "source_path": "worker lifecycle/inbox/wakeup contracts",
        "capability_name": "subagent_lifecycle_conformance",
        "capability_summary": "Challenges crash, reconnect, heartbeat and late-result behavior.",
        "targets": [TESTS[2]],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": "Conformance only; no AgentScope worker owner is migrated.",
    },
    {
        "source_repo": "browser-use",
        "source_commit": "source-graph-frozen",
        "source_language": "python/typescript",
        "target_language": "none",
        "source_path": "watchdog/browser-close/restore source graph facts",
        "capability_name": "close_restore_disable_reference",
        "capability_summary": "Reference-only close, reconnect, restore and disable review.",
        "targets": list(TESTS),
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "rationale": "Reference behavior only; no browser-use runtime is migrated.",
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "source-graph-frozen",
        "source_language": "typescript",
        "target_language": "none",
        "source_path": "TaskTool child-session/typed settlement/heartbeat source graph facts",
        "capability_name": "agent_task_drilldown_reference",
        "capability_summary": "Reference-only agent/task drill-down and status review.",
        "targets": [TESTS[2]],
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "rationale": "Reference behavior only; no Oh My Pi task owner is migrated.",
    },
)


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    value = HELPER.entry(decision)
    role = str(decision["source_role"])
    production = role in {"primary_implementation", "supplementary_implementation"}
    value.update(
        {
            "owner_unit": OWNER_UNIT,
            "created_at": STAMP,
            "updated_at": STAMP,
            "downstream_units": ["M2-04B", "M2-04"],
            "replacement_plan": (
                "Replace behind canonical selectors, command and permission ports "
                "without moving MCP, skill or subagent custody."
                if production
                else f"{role} source; no production owner is selected."
            ),
            "risk_notes": [
                "CanonicalProjectionStore is the only frontend truth.",
                "M1 MCP, skill, subagent and permission runtimes retain custody.",
                "Receipts do not prove semantic effects without later canonical events.",
                "Sealed mode denies mutation and records the attempt.",
                "No runtime path depends on a parent source repository.",
                "OpenClaw remains excluded_forward_only.",
            ],
        }
    )
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "recorded",
        "license_hint": "M2 extension panel source role recorded.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["source_evidence"][0]["reason"] = f"{OWNER_UNIT} source-to-target decision"
    value["source_evidence"][0]["tags"] = ["m2-04b", role]
    value["main_path"] = {
        "surfaces": ["mcp_panel", "skill_panel", "subagent_panel"],
        "event_types": [
            "MCP capability/auth/elicitation",
            "skill registry/invocation",
            "subagent lifecycle/checkpoint/result",
        ],
        "api_routes": ["/tasks/{task_id}/commands"],
        "control_commands": ["/mcp", "/skills", "/agents", "/tasks"],
        "artifact_kinds": ["skill_resource", "subagent_result", "checkpoint"],
        "worker_runtime": (
            "CanonicalProjectionStore -> panel controller -> CommandSurfaceRuntime "
            "-> permission/runtime owner -> event -> selector reconciliation"
            if production
            else f"{role}; behavior/reference only"
        ),
        "ui_panels": ["mcp", "skills", "subagents"],
    }
    value["runtime_entry"] = {
        "module": (
            "apps.web.src.features"
            if production
            else "apps.web.test.mcp-skill-subagent-panel-integration"
        ),
        "function": "panel controllers" if production else "conformance tests",
        "protocol": "zyra.extension-panels/v1",
        "health_check": "bun test ./apps/web/test",
        "command": "bun run --cwd apps/web build" if production else "",
        "config_refs": list(decision["targets"]),
        "environment_refs": [],
    }
    value["test_entries"] = [
        {
            "path": path,
            "command": (
                f"python -m pytest {path} -q"
                if path.endswith(".py")
                else f"bun test ./{path}"
            ),
            "kind": "integration",
            "expected_signal": (
                "canonical projection, permission-bound controls, sealed denial, "
                "effect reconciliation, close/restore and disable"
            ),
            "required": True,
        }
        for path in TESTS
        if path in decision["targets"] or production
    ]
    value["tags"] = ["m2-04b", OWNER_UNIT.lower(), "extension-panels", role]
    value["metadata"].update(
        {
            "owner_unit": OWNER_UNIT,
            "slice_id": OWNER_UNIT,
            "source_role": role,
            "source_commit": decision["source_commit"],
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "canonical_frontend_owner": "typescript.CanonicalProjectionStore",
            "canonical_command_owner": "typescript.CommandSurfaceRuntime",
            "canonical_backend_owners": (
                "typescript.McpRuntime+SkillRuntime+SubagentRuntime+PermissionRuntime"
            ),
            "root_source_runtime_dependency": False,
            "decision_commit": DECISION_COMMIT,
            "implementation_commit": IMPLEMENTATION_COMMIT,
            "baseline_commit": BASELINE_COMMIT,
            "rationale": decision["rationale"],
        }
    )
    return value


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def rewrite(document: Any) -> Any:
    entries = document if isinstance(document, list) else document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("ledger seed must be a list or contain entries")
    replacements = [entry(decision) for decision in DECISIONS]
    output: list[dict[str, Any]] = []
    inserted = False
    for item in entries:
        owned = (
            str(item.get("owner_unit") or "") == OWNER_UNIT
            or str((item.get("metadata") or {}).get("slice_id") or "") == OWNER_UNIT
        )
        if owned:
            if not inserted:
                output.extend(replacements)
                inserted = True
            continue
        output.append(item)
    if not inserted:
        output.extend(replacements)
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    if isinstance(document, list):
        return output
    return {
        **document,
        "entries": output,
        "summary": to_jsonable(InternalizationLedger(typed).summary()),
    }


def git_file_exists(commit: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def synchronize(path: Path, *, write: bool) -> tuple[bool, int, list[str]]:
    current = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite(current)
    aligned = canonical(current) == canonical(expected)
    if write and not aligned:
        path.write_text(canonical(expected), encoding="utf-8", newline="\n")
        current = expected
        aligned = True
    checked = current if aligned else expected
    entries = checked if isinstance(checked, list) else checked["entries"]
    selected = [item for item in entries if item.get("owner_unit") == OWNER_UNIT]
    errors: list[str] = []
    for item in selected:
        for binding in item.get("target_bindings") or []:
            target = str(binding.get("target_path") or "")
            if target and not git_file_exists(IMPLEMENTATION_COMMIT, target):
                errors.append(f"{item.get('ledger_id')}:missing:{target}")
    return aligned, len(selected), errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    aligned, count, errors = synchronize(
        arguments.ledger.resolve(),
        write=arguments.write or not arguments.check,
    )
    print(f"m2_extension_panel_ledger_aligned={str(aligned).lower()}")
    print(f"m2_extension_panel_source_decision_count={len(DECISIONS)}")
    print(f"m2_extension_panel_ledger_entry_count={count}")
    print(f"m2_extension_panel_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={arguments.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
