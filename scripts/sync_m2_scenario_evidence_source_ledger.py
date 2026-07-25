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


HELPER_PATH = ROOT / "scripts" / "sync_m2_mcp_skill_subagent_source_ledger.py"
HELPER_SPEC = importlib.util.spec_from_file_location(
    "m2_scenario_evidence_ledger_helpers",
    HELPER_PATH,
)
if HELPER_SPEC is None or HELPER_SPEC.loader is None:
    raise RuntimeError("M2 scenario evidence ledger helper could not be loaded")
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
OWNER_UNIT = "M2-S05-01"
BASELINE_COMMIT = "92537deca86376e147feb6c85248b0d3ff2298a6"
DECISION_COMMIT = "ba96726e7b6570f1df948d43972c17505f817750"
IMPLEMENTATION_COMMIT = "198087a83102e0457e201114d14549373bea9924"
STAMP = "2026-07-25T00:00:00.000Z"
TESTS = (
    "tests/scenarios/test_scenario_runner_foundation.py",
    "tests/integration/test_scenario_runner_api_main_path.py",
    "apps/web/test/scenario-runner-workbench.test.ts",
)
AUDIT_TARGETS = [
    "packages/evaluation/zyra_evaluation/scenario_runner/source_audit.py",
    "apps/web/src/features/scenarios/source-audit.ts",
    *TESTS,
]


def inactive(
    *,
    source_repo: str,
    source_language: str,
    source_path: str,
    capability_name: str,
    capability_summary: str,
    source_role: str,
    rationale: str,
) -> dict[str, Any]:
    return {
        "source_repo": source_repo,
        "source_commit": "source-graph-frozen",
        "source_language": source_language,
        "target_language": "none",
        "source_path": source_path,
        "capability_name": capability_name,
        "capability_summary": capability_summary,
        "targets": list(AUDIT_TARGETS),
        "source_role": source_role,
        "migration_mode": source_role,
        "migration_strategy": "not_selected",
        "rationale": rationale,
    }


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "zyra",
        "source_commit": BASELINE_COMMIT,
        "source_language": "python/typescript/tsx",
        "target_language": "python/typescript/tsx",
        "source_path": (
            "apps/api/zyra_api/main.py;"
            "packages/evaluation/zyra_evaluation/trace.py;"
            "packages/orchestration;packages/memory;packages/scheduler;"
            "packages/runtime;apps/web/src/state"
        ),
        "capability_name": "sealed_scenario_runner_and_evidence_owner",
        "capability_summary": (
            "Durable scenario registry, clean admission, sealed policy, effective "
            "step classification, owner execution, evidence verification, API/CLI "
            "lifecycle and browser-independent console projection."
        ),
        "targets": [
            "packages/evaluation/zyra_evaluation/scenario_runner/runtime.py",
            "packages/evaluation/zyra_evaluation/scenario_runner/store.py",
            "packages/evaluation/zyra_evaluation/scenario_runner/effective_steps.py",
            "packages/evaluation/zyra_evaluation/scenario_runner/causal.py",
            "packages/evaluation/zyra_evaluation/scenario_runner/evidence.py",
            "packages/evaluation/zyra_evaluation/scenario_runner/metrics.py",
            "apps/api/zyra_api/scenario_api.py",
            "apps/web/src/features/scenarios/runtime.ts",
            "apps/web/src/features/scenarios/view/scenario-workbench.tsx",
            "scripts/run_first_stage_scenarios.py",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "new_owned_runtime/same_language_component_integration",
        "migration_strategy": "direct_port",
        "rationale": (
            "The slice assigns a new Zyra-owned evaluation state domain and composes "
            "existing Zyra task, event, scheduler, memory, permission, recovery and "
            "artifact owners without importing another scenario runtime."
        ),
    },
    inactive(
        source_repo="opencode",
        source_language="typescript/tsx",
        source_path=(
            "packages/opencode/src/session;packages/app/src/pages/session.tsx;"
            "packages/app/src/components;packages/console/app"
        ),
        capability_name="split_protocol_app_tui_web_desktop_audit",
        capability_summary=(
            "Session/event, permission/question, terminal/review/diff and split "
            "protocol/app/TUI/web/desktop categories audited without a second owner."
        ),
        source_role="reference_only",
        rationale="Role-aware audit only; no OpenCode runtime or console code is migrated.",
    ),
    inactive(
        source_repo="claude-code-best",
        source_language="typescript",
        source_path="src/query;src/services;src/tools;src/services/permission",
        capability_name="backend_runtime_command_session_permission_audit",
        capability_summary="Backend runtime, command, session and permission categories.",
        source_role="reference_only",
        rationale="Existing M1 Zyra/Claude-derived owners remain unchanged; audit only.",
    ),
    inactive(
        source_repo="claude-code-best",
        source_language="typescript/tsx",
        source_path="src/screens;src/components;src/commands;src/utils/ink",
        capability_name="cli_tui_repl_prompt_queue_local_jsx_dialog_ink_audit",
        capability_summary="CLI/TUI/REPL, prompt queue, local JSX, dialog and Ink categories.",
        source_role="reference_only",
        rationale="Interaction categories are audited separately from backend roles.",
    ),
    inactive(
        source_repo="oh-my-pi",
        source_language="typescript",
        source_path="packages/coding-agent/src/agent;packages/coding-agent/src/task;packages/mnemopi",
        capability_name="agentloop_tasktool_pal_mnemopi_audit",
        capability_summary="AgentLoop, TaskTool/PAL and Mnemopi category audit.",
        source_role="reference_only",
        rationale="No OMP agent, task or memory owner is selected.",
    ),
    inactive(
        source_repo="oh-my-pi",
        source_language="typescript",
        source_path="packages/ai;packages/coding-agent/src/modes;packages/coding-agent/src/tools",
        capability_name="provider_rpc_acp_hashline_roboomp_audit",
        capability_summary="Provider, RPC/ACP, Hashline and RoboOmp category audit.",
        source_role="reference_only",
        rationale="No OMP provider wire, RPC server, edit owner or subprocess is selected.",
    ),
    inactive(
        source_repo="oh-my-pi",
        source_language="typescript/native",
        source_path="packages/tui;packages/native;packages/coding-agent/src/compact",
        capability_name="tui_native_snapcompact_audit",
        capability_summary="TUI, native and Snapcompact category audit.",
        source_role="reference_only",
        rationale="No OMP TUI, native binary or compact owner is selected.",
    ),
    inactive(
        source_repo="agent-framework",
        source_language="python/csharp",
        source_path="workflow/checkpoint and AG-UI approval/history contracts",
        capability_name="workflow_checkpoint_approval_history_conformance",
        capability_summary="Workflow/checkpoint and approval/history negative conformance.",
        source_role="conformance_only",
        rationale="Conformance only; no workflow, store or approval owner is migrated.",
    ),
    inactive(
        source_repo="agentscope",
        source_language="python",
        source_path="worker lifecycle/inbox/wakeup contracts",
        capability_name="worker_lifecycle_restart_conformance",
        capability_summary="Worker lifecycle, restart, inbox and wakeup conformance.",
        source_role="conformance_only",
        rationale="Conformance only; no AgentScope runtime owner is migrated.",
    ),
    inactive(
        source_repo="langgraph",
        source_language="python",
        source_path="checkpoint identity/pending committed writes/exact resume contracts",
        capability_name="narrow_exact_resume_conformance",
        capability_summary="Narrow exact-resume and checkpoint identity conformance.",
        source_role="conformance_only",
        rationale="No StateGraph, Pregel, channel, store or SDK production owner is selected.",
    ),
    inactive(
        source_repo="browser-use",
        source_language="python/typescript",
        source_path="watchdog/browser-close/restore source graph facts",
        capability_name="browser_close_restore_watchdog_reference",
        capability_summary="Browser-close independence, restore and watchdog reference.",
        source_role="reference_only",
        rationale="Reference only; no browser-use runtime or process is migrated.",
    ),
)


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    value = HELPER.entry(decision)
    role = str(decision["source_role"])
    production = role == "primary_implementation"
    value.update(
        {
            "owner_unit": OWNER_UNIT,
            "created_at": STAMP,
            "updated_at": STAMP,
            "downstream_units": ["M2-S05-02", "M2-05", "M2"],
            "replacement_plan": (
                "Replace behind ScenarioRunnerService and ScenarioApi while retaining "
                "the durable run/event/artifact schemas."
                if production
                else f"{role}; no production runtime, store or UI owner is selected."
            ),
            "risk_notes": [
                "ScenarioRunnerService is the only scenario/evidence state owner.",
                "Existing task, event, scheduler, memory, permission, recovery and artifact custody is unchanged.",
                "A browser close detaches the viewer and never cancels backend work.",
                "Formal sealed policy has no ask/wait/manual mutation success path.",
                "No runtime path depends on a parent source repository.",
                "OpenClaw remains excluded_forward_only and has no new ledger entry.",
            ],
        }
    )
    value["source_evidence"][0]["reason"] = f"{OWNER_UNIT} source-role decision"
    value["source_evidence"][0]["tags"] = ["m2-05", role]
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "recorded",
        "license_hint": "M2 scenario/evidence source role recorded.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["main_path"] = {
        "surfaces": ["scenario_api", "scenario_cli", "scenario_workbench"],
        "event_types": [
            "scenario lifecycle",
            "effective canonical transition",
            "sealed policy receipt",
            "causal evidence receipt",
        ],
        "api_routes": ["/scenarios/registry", "/scenarios/runs"],
        "control_commands": ["create", "start", "cancel", "archive", "verify"],
        "artifact_kinds": ["scenario_evidence_manifest", "owner_artifact"],
        "worker_runtime": (
            "ScenarioRunnerService -> canonical owner execution port -> evidence collector"
            if production
            else f"{role}; audit or conformance only"
        ),
        "ui_panels": ["scenarios"],
    }
    value["runtime_entry"] = {
        "module": (
            "zyra_evaluation.scenario_runner.runtime"
            if production
            else "zyra_evaluation.scenario_runner.source_audit"
        ),
        "function": "ScenarioRunnerService" if production else "SourceRoleAuditor",
        "protocol": "zyra.scenario-runner.api/v1",
        "health_check": "python -m pytest tests/scenarios/test_scenario_runner_foundation.py -q",
        "command": "python scripts/run_first_stage_scenarios.py registry" if production else "",
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
                "clean sealed admission, effective-step causation, durable lifecycle, "
                "evidence checksum/binding, restart, close and disable behavior"
            ),
            "required": True,
        }
        for path in TESTS
    ]
    value["tags"] = ["m2-05", OWNER_UNIT.lower(), "scenario-evidence", role]
    value["metadata"] = {
        "owner_unit": OWNER_UNIT,
        "slice_id": OWNER_UNIT,
        "source_role": role,
        "source_commit": decision["source_commit"],
        "source_language": decision["source_language"],
        "target_language": decision["target_language"],
        "migration_mode": decision["migration_mode"],
        "canonical_scenario_owner": "python.ScenarioRunnerService",
        "canonical_evidence_owner": "python.EvidenceCollector",
        "canonical_frontend_projection": "typescript.ScenarioProjectionStore",
        "root_source_runtime_dependency": False,
        "decision_commit": DECISION_COMMIT,
        "implementation_commit": IMPLEMENTATION_COMMIT,
        "baseline_commit": BASELINE_COMMIT,
        "rationale": decision["rationale"],
    }
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
    print(f"m2_scenario_evidence_ledger_aligned={str(aligned).lower()}")
    print(f"m2_scenario_evidence_source_decision_count={len(DECISIONS)}")
    print(f"m2_scenario_evidence_ledger_entry_count={count}")
    print(f"m2_scenario_evidence_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={arguments.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
