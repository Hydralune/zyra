from __future__ import annotations

import argparse
import hashlib
import json
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


OWNER_UNIT = "M1-S06C-01"
DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
TEST_COMMAND = (
    "bun test packages/memory/skill-memory-runtime/test "
    "packages/runtime/claude-runtime/test/skill-memory && "
    "python -m pytest tests/unit/test_reusable_procedure_miner.py "
    "tests/integration/test_reusable_procedure_api_main_path.py -q"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "claude-code-best",
        "source_commit": "c57f5a29e88e9a814bea47abeb9a0a6f725dc102",
        "source_path": (
            "src/tools/SkillTool/SkillTool.ts;src/tools/SkillTool/src/Tool.ts;"
            "src/services/compact/reactiveCompact.ts;src/services/compact/microCompact.ts;"
            "src/services/compact/postCompactCleanup.ts;src/services/contextCollapse/operations.ts"
        ),
        "capability_name": "skill_outcome_compact_restore_context_chain",
        "capability_summary": (
            "Claude SkillTool invocation provenance and compact/post-compact context flow are "
            "cropped into Zyra-owned outcome, archive, restore projection and next-context state."
        ),
        "target_paths": [
            "packages/memory/skill-memory-runtime/src/skill-outcome-adapter.ts",
            "packages/memory/skill-memory-runtime/src/outcome-runtime.ts",
            "packages/memory/skill-memory-runtime/src/context-projector.ts",
            "packages/memory/skill-memory-runtime/src/restore-bridge.ts",
            "packages/memory/skill-memory-runtime/src/runtime.ts",
            "packages/runtime/claude-runtime/src/skills/coordinator.ts",
            "packages/runtime/claude-runtime/src/query-engine.ts",
            "packages/runtime/claude-runtime/src/e01/coordinator.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "cropped_migration_retained_typescript_control_flow_adapt",
        "runtime_module": "@zyra/skill-memory-runtime",
        "runtime_function": "SkillMemoryApplication",
        "state_owner": "SkillMemoryApplication and CompactRestoreMemoryBridge",
        "rationale": (
            "03C remains the sole skill loader/version/policy/invocation owner. The migrated "
            "primary chain begins only after an immutable invocation result and routes restore "
            "through the existing 02B/02D ports."
        ),
    },
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_path": "agent/memory_manager.py;agent/memory_provider.py;tools/memory_tool.py",
        "capability_name": "validated_success_trajectory_reusable_procedure_memory",
        "capability_summary": (
            "Hermes procedure-memory concepts are retained as deterministic Python mining from "
            "published 06B outcomes with canonical session/tool/artifact/skill-version evidence."
        ),
        "target_paths": [
            "packages/memory/zyra_memory/procedure_models.py",
            "packages/memory/zyra_memory/procedure_store.py",
            "packages/memory/zyra_memory/procedure_miner.py",
            "packages/memory/zyra_memory/procedure_runtime.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration_same_language_procedure_adapt",
        "runtime_module": "zyra_memory.procedure_runtime",
        "runtime_function": "ReusableProcedureRuntime",
        "state_owner": "ReusableProcedureStore",
        "rationale": (
            "Only validated 06B skill-candidate outcomes can activate procedures. Static skills, "
            "fixtures and model summaries cannot publish an active procedure or invoke a skill."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/coding-agent/src/extensibility/extensions/compact-handler.ts;"
            "packages/coding-agent/src/session/compact-modes.ts;"
            "packages/coding-agent/src/session/messages.ts;"
            "packages/coding-agent/src/session/snapcompact-inline.ts"
        ),
        "capability_name": "compact_trigger_safe_cut_archive_and_experimental_frame_contract",
        "capability_summary": (
            "OMP trigger modes, atomic tool-call/result cut semantics and archive references are "
            "adapted under Zyra compact ownership; snapcompact is compatibility-gated and off."
        ),
        "target_paths": [
            "packages/memory/skill-memory-runtime/src/compact-trigger-runtime.ts",
            "packages/memory/skill-memory-runtime/src/safe-cut-runtime.ts",
            "packages/memory/skill-memory-runtime/src/archive-runtime.ts",
            "packages/memory/skill-memory-runtime/src/snapcompact-experimental.ts",
            "packages/runtime/claude-runtime/src/query-engine.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration_retained_typescript_protocol_adapt",
        "runtime_module": "@zyra/skill-memory-runtime",
        "runtime_function": "CompactTriggerRuntime",
        "state_owner": "02D compact boundary plus 06C archive/projection state",
        "rationale": (
            "OMP session storage and Mnemopi stores are not restored as canonical state. Frame "
            "compression has no default-path or effectiveness claim."
        ),
    },
)


def _ledger_id(decision: dict[str, Any]) -> str:
    identity = "|".join(
        (
            str(decision["source_repo"]),
            str(decision["source_path"]),
            str(decision["capability_name"]),
        )
    )
    return "ledger_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _entry(decision: dict[str, Any]) -> dict[str, Any]:
    targets = list(decision["target_paths"])
    return {
        "ledger_id": _ledger_id(decision),
        "source_repo": decision["source_repo"],
        "source_path": decision["source_path"],
        "capability_name": decision["capability_name"],
        "capability_summary": decision["capability_summary"],
        "target_bindings": [
            {
                "path": path,
                "role": "primary" if index == 0 else "supporting",
                "required_for_main_path": True,
            }
            for index, path in enumerate(targets)
        ],
        "migration_strategy": "direct_port",
        "main_path_status": "tested_main_path",
        "lifecycle": "productized",
        "owner_unit": OWNER_UNIT,
        "downstream_units": ["M1-S06C-02", "M1-07C", "M2-memory-skill-panel"],
        "dependencies": ["M1-S02B", "M1-S02D", "M1-S03C", "M1-S05C", "M1-S06B-02"],
        "source_evidence": [{
            "source_repo": decision["source_repo"],
            "source_path": decision["source_path"],
            "exists_in_workspace": True,
            "source_kind": "bounded_module_group",
            "reason": "Verified against the pinned source graph and concrete source files read for M1-S06C-01.",
            "symbols": [decision["runtime_function"]],
            "tags": [decision["source_role"], OWNER_UNIT],
        }],
        "main_path": {
            "surfaces": [
                "skill_coordinator_outcome",
                "skill_memory_application",
                "compact_restore_context_projection",
                "reusable_procedure_api",
            ],
            "event_types": [
                "skill_memory_updated",
                "procedure_mined",
                "compact_triggered",
                "compact_restored",
            ],
            "api_routes": [
                "GET /tasks/{task_id}/memory/procedures",
                "POST /tasks/{task_id}/memory/procedures/mine",
                "POST /tasks/{task_id}/memory/procedures/routing",
                "POST /tasks/{task_id}/memory/procedures/recovery",
                "POST /tasks/{task_id}/memory/procedures/context",
            ],
            "control_commands": [],
            "artifact_kinds": ["compact_archive", "skill_outcome", "reusable_procedure"],
            "worker_runtime": decision["runtime_function"],
            "ui_panels": [],
        },
        "line_count_policy": "counts_as_runtime",
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "recorded",
            "license_hint": "source license retained in repository notice inventory",
            "notice_path": "packages/memory/THIRD_PARTY_NOTICES.md",
            "notes": "Selected mechanisms and pinned revisions are recorded; Zyra owns the modified runtime.",
        },
        "runtime_entry": {
            "module": decision["runtime_module"],
            "function": decision["runtime_function"],
            "protocol": "zyra.skill-memory/v1",
            "health_check": TEST_COMMAND,
            "config_refs": targets,
        },
        "test_entries": [
            {
                "path": "packages/runtime/claude-runtime/test/skill-memory/skill-outcome-integration.behavior.test.ts",
                "command": "bun test packages/runtime/claude-runtime/test/skill-memory/skill-outcome-integration.behavior.test.ts",
                "kind": "integration",
                "expected_signal": "real 03C outcome and compact restore dynamically reach 06C without owner transfer",
                "required": True,
            },
            {
                "path": "tests/unit/test_reusable_procedure_miner.py",
                "command": "python -m pytest tests/unit/test_reusable_procedure_miner.py -q",
                "kind": "unit",
                "expected_signal": "validated 06B outcome creates a provenance-bound procedure visible to routing and recovery",
                "required": True,
            },
            {
                "path": "tests/integration/test_reusable_procedure_api_main_path.py",
                "command": "python -m pytest tests/integration/test_reusable_procedure_api_main_path.py -q",
                "kind": "integration",
                "expected_signal": "live API exposes procedure mine/routing/recovery/context without model activation",
                "required": True,
            },
        ],
        "notes": (
            f"source_role={decision['source_role']}; source_commit={decision['source_commit']}; "
            f"{decision['rationale']}"
        ),
        "metadata": {
            "owner_unit": OWNER_UNIT,
            "source_role": decision["source_role"],
            "source_commit": decision["source_commit"],
            "migration_mode": decision["migration_mode"],
            "state_owner": decision["state_owner"],
            "skill_loader_owner": "03C SkillCoordinator",
            "canonical_compact_owner": "02D ContextCompactionRuntime",
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "model_can_activate_procedure": False,
            "root_source_runtime_dependency": False,
            "openclaw_forward_excluded": True,
        },
    }


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict) and isinstance(document.get("entries"), list):
        entries = document["entries"]
    else:
        raise ValueError("internalization ledger seed must be a list or contain entries")
    replacements = {item["ledger_id"]: item for item in (_entry(item) for item in DECISIONS)}
    output = [replacements.pop(str(item.get("ledger_id") or ""), item) for item in entries]
    output.extend(replacements.values())
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    expected = (
        output
        if isinstance(document, list)
        else {
            **document,
            "entries": output,
            "summary": to_jsonable(InternalizationLedger(typed).summary()),
        }
    )
    aligned = _canonical(document) == _canonical(expected)
    if write and not aligned:
        path.write_text(_canonical(expected), encoding="utf-8", newline="\n")
        aligned = True
    return aligned, len(DECISIONS)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    aligned, count = synchronize(
        arguments.ledger.resolve(),
        write=arguments.write or not arguments.check,
    )
    print(f"skill_memory_source_ledger_aligned={str(aligned).lower()}")
    print(f"skill_memory_source_decision_count={count}")
    print(f"skill_memory_source_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={arguments.ledger.resolve()}")
    return 0 if aligned or not arguments.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
