from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import InternalizationLedger, InternalizationLedgerEntry  # noqa: E402
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402


OWNER_UNIT = "M1-S06C-02"
DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
TEST_COMMAND = (
    "bun test ./packages/memory/skill-memory-runtime/test/integration.behavior.test.ts "
    "./packages/runtime/claude-runtime/test/skill-memory/skill-outcome-integration.behavior.test.ts && "
    "python -m pytest tests/integration/test_skill_memory_compact_restore_integration.py -q"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "claude-code-best",
        "source_commit": "c57f5a29e88e9a814bea47abeb9a0a6f725dc102",
        "source_path": (
            "src/services/compact/reactiveCompact.ts;src/services/compact/postCompactCleanup.ts;"
            "src/services/compact/compact.ts;src/tools/SkillTool/SkillTool.ts;"
            "src/tools/SkillTool/src/Tool.ts"
        ),
        "source_language": "typescript",
        "target_language": "typescript+python",
        "source_symbols": [
            "tryReactiveCompact",
            "runPostCompactCleanup",
            "compactConversation",
            "createSkillAttachmentIfNeeded",
            "SkillTool",
        ],
        "source_callsites": [
            "QueryEngine compact/restore lifecycle",
            "SkillTool invocation and post-compact restore",
        ],
        "source_tests": [
            "none_collocated_in_pinned_source_snapshot; Zyra behavior tests provide conformance evidence",
        ],
        "capability_name": "skill_memory_current_authority_resume_and_cross_worker_restore",
        "capability_summary": (
            "Claude compact/restore and SkillTool lifecycle mechanisms are cropped into a "
            "Zyra-owned integration that revalidates current 03C authority, persists semantic "
            "resume continuity, and changes the next CodeWorker and BrowserWorker contexts."
        ),
        "target_paths": [
            "packages/memory/skill-memory-runtime/src/authority-runtime.ts",
            "packages/memory/skill-memory-runtime/src/resume-continuity-runtime.ts",
            "packages/memory/skill-memory-runtime/src/integration-runtime.ts",
            "packages/memory/skill-memory-runtime/src/continuity-failure-runtime.ts",
            "packages/runtime/claude-runtime/src/skills/coordinator.ts",
            "packages/runtime/claude-runtime/src/query-engine.ts",
            "packages/workers/zyra_workers/skill_memory_context.py",
            "packages/workers/zyra_workers/browser_worker.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "cropped_migration_retained_typescript_control_flow_plus_bounded_python_context_port",
        "runtime_module": "@zyra/skill-memory-runtime",
        "runtime_function": "SkillMemoryIntegrationRuntime",
        "event_types": [
            "skill_memory_restore_fidelity",
            "skill_memory_browser_context_exported",
            "skill_memory_authority_revalidation_failed",
            "skill_memory_integration_deferred",
        ],
        "api_routes": ["POST /tasks/{task_id}/workers/browser"],
        "state_owner": (
            "SkillMemoryApplication integration snapshot; 03C SkillCoordinator authority; "
            "02B/02D context and compact owners remain canonical"
        ),
        "rationale": (
            "The migration retains the high-cohesion compact/restore and current SkillTool "
            "resolution contracts. Historical outcome memory never restores an executable body "
            "or permission and BrowserWorker receives only digest-validated context projections."
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
        "source_language": "typescript",
        "target_language": "typescript",
        "source_symbols": [
            "runExtensionCompact",
            "COMPACT_MODES",
            "planInlineSwaps",
            "estimateInlineSavings",
            "SnapcompactInlineTransformer",
        ],
        "source_callsites": [
            "session compact handler",
            "frame-aware compact message projection",
        ],
        "source_tests": [
            "packages/coding-agent/test/agent-session-compaction.test.ts",
            "packages/coding-agent/test/compact-modes.test.ts",
            "packages/coding-agent/test/snapcompact-inline.test.ts",
        ],
        "capability_name": "provider_restore_fidelity_and_snapcompact_ablation",
        "capability_summary": (
            "OMP compact modes and frame-aware restore contracts are adapted into three "
            "same-history/same-budget fidelity lanes with explicit provider/gateway failures and "
            "a text/reference baseline that survives experimental adapter removal."
        ),
        "target_paths": [
            "packages/memory/skill-memory-runtime/src/fidelity-runtime.ts",
            "packages/memory/skill-memory-runtime/src/integration-runtime.ts",
            "packages/memory/skill-memory-runtime/src/continuity-failure-runtime.ts",
            "packages/runtime/claude-runtime/src/query-engine.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration_retained_typescript_protocol_ablation_adapt",
        "runtime_module": "@zyra/skill-memory-runtime",
        "runtime_function": "ProviderRestoreFidelityRuntime",
        "event_types": [
            "skill_memory_restore_fidelity",
            "skill_memory_restore_fidelity_failure",
        ],
        "api_routes": [],
        "state_owner": "ProviderRestoreFidelityRuntime comparison/receipt state under 06C snapshot",
        "rationale": (
            "Snapcompact stays feature-gated and non-default. Unsupported vision, missing/corrupt "
            "frames, gateway image loss and provider changes emit explicit fallback evidence; no "
            "visual effectiveness claim is made."
        ),
    },
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_path": "agent/memory_manager.py;agent/memory_provider.py;tools/memory_tool.py",
        "source_language": "python",
        "target_language": "typescript+python",
        "source_symbols": ["MemoryManager", "MemoryProvider", "memory_tool"],
        "source_callsites": [
            "agent memory context assembly",
            "memory tool retrieval and persistence",
        ],
        "source_tests": [
            "tests/agent/test_memory_provider.py",
            "tests/tools/test_memory_tool.py",
            "tests/run_agent/test_commit_memory_session_context_engine.py",
        ],
        "capability_name": "procedure_memory_retrieval_compact_context_composition",
        "capability_summary": (
            "Validated 06B procedure memory is joined with 06A canonical memory/code retrieval and "
            "06C skill outcomes in a bounded, provenance-bearing compact restore projection."
        ),
        "target_paths": [
            "packages/memory/skill-memory-runtime/src/retrieval-composition-runtime.ts",
            "packages/memory/skill-memory-runtime/src/integration-runtime.ts",
            "packages/workers/zyra_workers/retrieval_context_runtime.py",
            "packages/memory/zyra_memory/procedure_runtime.py",
            "packages/workers/zyra_workers/skill_memory_context.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "bounded_cross_language_projection_and_context_adapt",
        "runtime_module": "zyra_workers.skill_memory_context",
        "runtime_function": "BrowserSkillMemoryContextRuntime",
        "event_types": ["skill_memory_browser_context_exported"],
        "api_routes": [
            "POST /tasks/{task_id}/memory/procedures/context",
            "POST /tasks/{task_id}/workers/browser",
        ],
        "state_owner": "ReusableProcedureStore plus derived 06A/06C retrieval delivery receipts",
        "rationale": (
            "Only validated procedures with curator and runtime evidence enter context. The "
            "projection cannot invoke skills/tools and every referenced skill requires a fresh 03C "
            "resolution after compact or resume."
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
        "downstream_units": ["M1-07C", "M1-08", "M2-memory-skill-panel"],
        "dependencies": ["M1-S02B", "M1-S02D", "M1-S03C", "M1-S06A-02", "M1-S06B-02", "M1-S06C-01"],
        "source_evidence": [{
            "source_repo": decision["source_repo"],
            "source_path": decision["source_path"],
            "exists_in_workspace": True,
            "source_kind": "bounded_module_group",
            "reason": "Pinned source graph and concrete source files verified for M1-S06C-02.",
            "symbols": list(decision["source_symbols"]),
            "tags": [decision["source_role"], OWNER_UNIT],
        }],
        "main_path": {
            "surfaces": [
                "codeworker_compact_restore",
                "provider_restore_fidelity",
                "browserworker_restored_context",
                "current_skill_authority",
                "resume_continuity",
            ],
            "event_types": list(decision["event_types"]),
            "api_routes": list(decision["api_routes"]),
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
            "notes": "Cropped and modified mechanisms are maintained inside Zyra modules.",
        },
        "runtime_entry": {
            "module": decision["runtime_module"],
            "function": decision["runtime_function"],
            "protocol": "zyra.skill-memory-integration/v1",
            "health_check": TEST_COMMAND,
            "config_refs": targets,
        },
        "test_entries": [
            {
                "path": "packages/memory/skill-memory-runtime/test/integration.behavior.test.ts",
                "command": "bun test ./packages/memory/skill-memory-runtime/test/integration.behavior.test.ts",
                "kind": "behavior",
                "expected_signal": "three restore lanes, current authority, retrieval composition and resume loss are deterministic",
                "required": True,
            },
            {
                "path": "packages/runtime/claude-runtime/test/skill-memory/skill-outcome-integration.behavior.test.ts",
                "command": "bun test ./packages/runtime/claude-runtime/test/skill-memory/skill-outcome-integration.behavior.test.ts",
                "kind": "integration",
                "expected_signal": "real QueryEngine compact and 03C reload reach the integrated runtime",
                "required": True,
            },
            {
                "path": "tests/integration/test_skill_memory_compact_restore_integration.py",
                "command": "python -m pytest tests/integration/test_skill_memory_compact_restore_integration.py -q",
                "kind": "integration",
                "expected_signal": "BrowserWorker consumes the digest-validated cross-worker restore projection",
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
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "source_symbols": list(decision["source_symbols"]),
            "source_callsites": list(decision["source_callsites"]),
            "source_tests": list(decision["source_tests"]),
            "state_owner": decision["state_owner"],
            "skill_loader_owner": "03C SkillCoordinator",
            "canonical_compact_owner": "02D ContextCompactionRuntime",
            "canonical_context_owner": "02B ContextAssemblyRuntime/BrowserContextTaskIntegrationRuntime",
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "canonical_procedure_owner": "ReusableProcedureStore",
            "experimental_default": False,
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
    print(f"skill_memory_integration_source_ledger_aligned={str(aligned).lower()}")
    print(f"skill_memory_integration_source_decision_count={count}")
    print(f"skill_memory_integration_source_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={arguments.ledger.resolve()}")
    return 0 if aligned or not arguments.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
