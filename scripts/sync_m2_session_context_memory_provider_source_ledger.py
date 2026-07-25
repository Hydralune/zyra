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
    "m2_session_console_ledger_helpers",
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
OWNER_UNIT = "M2-S04B-01"
SLICE_ID = OWNER_UNIT
BASELINE_COMMIT = "af6f6dc0c66a846c739b73e8c6006c67de219ba8"
DECISION_COMMIT = "26cb8f5"
IMPLEMENTATION_COMMIT = "9cb8272"
STAMP = "2026-07-25T00:00:00.000Z"
WEB_TEST = "apps/web/test/session-context-memory-provider-panels.test.ts"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "claude-code-best",
        "source_commit": "c57f5a29e88e9a814bea47abeb9a0a6f725dc102",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "src/commands/context/context-noninteractive.ts;"
            "src/commands/context/context.tsx;"
            "src/commands/compact/compact.ts;"
            "src/commands/memory/memory.tsx;"
            "src/utils/sessionRestore.ts;"
            "src/utils/queryContext.ts;"
            "src/services/compact/compact.ts;"
            "src/services/compact/postCompactCleanup.ts;"
            "src/services/compact/reactiveCompact.ts;"
            "src/services/SessionMemory/sessionMemory.ts;"
            "src/screens/REPL.tsx"
        ),
        "capability_name": "session_context_compact_memory_console_runtime",
        "capability_summary": (
            "Session lineage, exact checkpoint admission, accountable context "
            "budgets, compact preview/restore accounting, memory retrieval export "
            "and command-receipt-to-canonical-effect reconciliation."
        ),
        "targets": [
            "apps/web/src/features/session/projection.ts",
            "apps/web/src/features/session/runtime.ts",
            "apps/web/src/features/session/checkpoint-ledger.ts",
            "apps/web/src/features/session/context-analyzer.ts",
            "apps/web/src/features/session/control-effects.ts",
            "apps/web/src/features/memory/projection.ts",
            "apps/web/src/features/memory/retrieval-session.ts",
            "packages/commands/src/registry.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "retained_control_flow_adapt/cropped_migration",
        "migration_strategy": "direct_port",
        "rationale": (
            "The cohesive TypeScript context/compact/session control flow is "
            "retained, while process-local state is replaced by Zyra canonical "
            "projections, command receipts and backend-owned checkpoints."
        ),
    },
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/app/src/context/server-session.ts;"
            "packages/app/src/components/session/session-context-metrics.ts;"
            "packages/app/src/components/session/session-context-breakdown.ts;"
            "packages/app/src/components/session/session-context-usage.tsx;"
            "packages/app/src/pages/session.tsx;"
            "packages/core/src/session/context-epoch.ts;"
            "packages/core/src/session/compaction.ts;"
            "packages/core/src/session/history.ts;"
            "packages/core/src/session/revert.ts;"
            "packages/session-ui/src/components/session-retry.tsx"
        ),
        "capability_name": "provider_placement_reconnect_projection",
        "capability_summary": (
            "Server/session scoping, reconnect invalidation, provider capability "
            "and fallback presentation, plus device/edge/cloud placement "
            "explanations without a second session or scheduler owner."
        ),
        "targets": [
            "apps/web/src/features/session/reconnect-supervisor.ts",
            "apps/web/src/features/providers/projection.ts",
            "apps/web/src/features/providers/failover.ts",
            "apps/web/src/features/providers/credential-audit.ts",
            "apps/web/src/features/placement/projection.ts",
            "apps/web/src/features/placement/admission.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration/same_language_component_integration",
        "migration_strategy": "direct_port",
        "rationale": (
            "OpenCode fills reconnect, provider catalog and fallback presentation "
            "gaps only; Zyra projections and Python scheduler retain custody."
        ),
    },
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_language": "python/typescript",
        "target_language": "typescript",
        "source_path": (
            "agent/memory_manager.py;agent/curator.py;"
            "gateway/memory_monitor.py;ui-tui/src/lib/memory.ts;"
            "ui-tui/src/lib/memoryMonitor.ts"
        ),
        "capability_name": "memory_curator_provenance_context_export",
        "capability_summary": (
            "Bounded deterministic curator, interrupted-sync diagnostics, explicit "
            "provenance/veracity and selected-memory export into later context."
        ),
        "targets": [
            "apps/web/src/features/memory/curator-ledger.ts",
            "apps/web/src/features/memory/context-export.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "bounded_cross_language_typed_supplement/cropped_migration",
        "migration_strategy": "direct_port",
        "rationale": (
            "Only bounded projection behavior is expressed in the existing "
            "TypeScript client boundary; MemoryFabric and curator custody stay in "
            "Python, so another Python facade would be forwarding-only duplication."
        ),
    },
    {
        "source_repo": "langgraph",
        "source_commit": "section-12-forward-ruling",
        "source_language": "python",
        "target_language": "none",
        "source_path": (
            "checkpoint identity/lineage;pending/committed writes;"
            "stable task id;exact-resume contracts"
        ),
        "capability_name": "checkpoint_pending_committed_exact_resume_conformance",
        "capability_summary": (
            "Challenges pending-versus-committed separation, checkpoint identity, "
            "stale/conflict handling and exact-resume correlation."
        ),
        "targets": [WEB_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "No StateGraph, channel, reducer, Pregel, stream, Store, ToolNode, SDK "
            "or UI owner enters production."
        ),
    },
    {
        "source_repo": "browser-use",
        "source_commit": "source-graph-frozen",
        "source_language": "python/typescript",
        "target_language": "none",
        "source_path": "watchdog/model/provider/terminal/remote-control source graph facts",
        "capability_name": "disconnect_failover_secret_redaction_reference",
        "capability_summary": (
            "Reference-only disconnect, failover and credential-redaction review."
        ),
        "targets": [WEB_TEST],
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "rationale": "Reference behavior only; no browser-use runtime owner is migrated.",
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
                "Replace behind canonical projection, command and backend-owner "
                "ports without moving session/memory/provider/scheduler custody."
                if production
                else f"{role} source; no production owner is selected."
            ),
            "risk_notes": [
                "CanonicalProjectionStore remains the only frontend truth.",
                "Backend session, memory, provider and scheduler owners are unchanged.",
                "Controls settle only after canonical semantic effects are observed.",
                "Credential values never enter provider projections.",
                "No runtime path depends on a parent source repository.",
                "OpenClaw remains excluded_forward_only.",
            ],
        }
    )
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "recorded",
        "license_hint": "M2 session console source role recorded.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["source_evidence"][0]["tags"] = ["m2-04b", role]
    value["main_path"] = {
        "surfaces": [
            "task_session_console_workbench",
            "context_compact_projection",
            "memory_retrieval_curator_projection",
            "provider_placement_projection",
        ],
        "event_types": [
            "checkpoint pending/committed/resumed",
            "memory retrieved/curated",
            "provider failure/fallback selected",
            "scheduler placement/migration",
        ],
        "api_routes": ["/tasks/{task_id}/commands"],
        "control_commands": [
            "/context",
            "/compact",
            "/memory",
            "/model",
            "/resume",
            "/rewind",
            "/export",
        ],
        "artifact_kinds": ["session_export", "memory_context", "checkpoint"],
        "worker_runtime": (
            "SessionConsoleRuntime -> CommandSurfaceRuntime -> canonical receipt/"
            "event -> CanonicalProjectionStore -> ControlEffectLedger"
            if production
            else f"{role}; behavior/reference only"
        ),
        "ui_panels": [
            "session_lineage",
            "context_compact",
            "memory_curator",
            "provider_model",
            "heterogeneous_placement",
        ],
    }
    value["runtime_entry"] = {
        "module": (
            "apps.web.src.features.session.runtime"
            if production
            else "apps.web.test.session-context-memory-provider-panels"
        ),
        "function": (
            "SessionConsoleRuntime"
            if production
            else "session console conformance tests"
        ),
        "protocol": "zyra.session-console/v1",
        "health_check": f"bun test ./{WEB_TEST}",
        "command": "bun run build:web" if production else "",
        "config_refs": list(decision["targets"]),
        "environment_refs": [],
    }
    value["test_entries"] = [
        {
            "path": WEB_TEST,
            "command": f"bun test ./{WEB_TEST}",
            "kind": "integration",
            "expected_signal": (
                "lineage, compact, exact effect reconciliation, memory context, "
                "provider failover/redaction, placement and disconnect behavior"
            ),
            "required": True,
        }
    ]
    value["tags"] = [
        "m2-04b",
        SLICE_ID.lower(),
        "session-console",
        "context-memory-provider-placement",
        role,
    ]
    value["metadata"].update(
        {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": role,
            "source_commit": decision["source_commit"],
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "canonical_frontend_owner": "typescript.CanonicalProjectionStore",
            "canonical_command_owner": "typescript.CommandSurfaceRuntime",
            "canonical_backend_owners": (
                "typescript.QueryEngine+ProviderControlPlane;"
                "python.SQLiteStore+MemoryFabric+Scheduler"
            ),
            "root_source_runtime_dependency": False,
            "decision_commit": DECISION_COMMIT,
            "implementation_commit": IMPLEMENTATION_COMMIT,
            "baseline_commit": BASELINE_COMMIT,
            "rationale": decision["rationale"],
        }
    )
    return value


def rewrite(document: Any) -> Any:
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict) and isinstance(document.get("entries"), list):
        entries = document["entries"]
    else:
        raise ValueError("ledger seed must be a list or contain entries")
    replacements = [entry(decision) for decision in DECISIONS]
    output: list[dict[str, Any]] = []
    inserted = False
    for item in entries:
        owned = (
            str(item.get("owner_unit") or "") == OWNER_UNIT
            or str((item.get("metadata") or {}).get("slice_id") or "") == SLICE_ID
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


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def git_file_exists(commit: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def audit_targets(document: Any) -> tuple[int, list[str]]:
    entries = document if isinstance(document, list) else document["entries"]
    selected = [
        item for item in entries if str(item.get("owner_unit") or "") == OWNER_UNIT
    ]
    errors: list[str] = []
    for item in selected:
        identity = str(item.get("ledger_id") or "")
        for binding in item.get("target_bindings") or []:
            path = str(binding.get("target_path") or "")
            if path and not git_file_exists(IMPLEMENTATION_COMMIT, path):
                errors.append(f"{identity}:missing:{path}")
    return len(selected), errors


def synchronize(path: Path, *, write: bool) -> tuple[bool, int, list[str]]:
    current = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite(current)
    aligned = canonical(current) == canonical(expected)
    if write and not aligned:
        path.write_text(canonical(expected), encoding="utf-8", newline="\n")
        current = expected
        aligned = True
    checked = current if aligned else expected
    count, errors = audit_targets(checked)
    return aligned, count, errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    aligned, count, errors = synchronize(
        args.ledger.resolve(),
        write=args.write or not args.check,
    )
    print(f"m2_session_console_ledger_aligned={str(aligned).lower()}")
    print(f"m2_session_console_source_decision_count={len(DECISIONS)}")
    print(f"m2_session_console_ledger_entry_count={count}")
    print(f"m2_session_console_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
