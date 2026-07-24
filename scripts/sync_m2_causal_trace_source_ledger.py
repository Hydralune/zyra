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


HELPER_PATH = ROOT / "scripts" / "sync_m2_browser_viewer_source_ledger.py"
HELPER_SPEC = importlib.util.spec_from_file_location("m2_trace_ledger_helpers", HELPER_PATH)
if HELPER_SPEC is None or HELPER_SPEC.loader is None:
    raise RuntimeError("M2 browser ledger helper could not be loaded")
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
OWNER_UNIT = "M2-S03B-03"
SLICE_ID = OWNER_UNIT
IMPLEMENTATION_COMMIT = "2bc9604685cd7024825cf94a34c3fc507921d661"
BASELINE_COMMIT = "ca1d1b1eee0ef04451cc619ddc2b3f944f30f99e"
STAMP = "2026-07-25T00:00:00.000Z"
WEB_TEST = "apps/web/test/causal-trace-cross-view-integration.test.ts"
API_TEST = "tests/integration/test_causal_trace_cross_view_integration.py"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "zyra",
        "source_commit": BASELINE_COMMIT,
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "apps/web/src/state/contracts.ts;"
            "apps/web/src/state/selectors.ts;"
            "apps/web/src/state/panel-selectors.ts;"
            "apps/web/src/features/timeline/projection/event-reader.ts;"
            "apps/web/src/features/timeline/projection/causal-graph.ts;"
            "apps/web/src/features/timeline/projection/critical-path.ts;"
            "apps/web/src/features/timeline/projection/drilldown.ts"
        ),
        "capability_name": "typed_causal_trace_index_analysis_and_cross_view_runtime",
        "capability_summary": (
            "A disposable task/run causal index over the canonical projection, "
            "strict typed joins, completeness diagnostics, SCC-safe critical path, "
            "search/filter/fold, variable-height virtualization, report pins and "
            "bidirectional terminal/browser/artifact/diff/timeline/topology focus."
        ),
        "targets": [
            "apps/web/src/features/trace/contracts.ts",
            "apps/web/src/features/trace/identity.ts",
            "apps/web/src/features/trace/projector.ts",
            "apps/web/src/features/trace/index/builder.ts",
            "apps/web/src/features/trace/analysis/critical-path.ts",
            "apps/web/src/features/trace/analysis/query.ts",
            "apps/web/src/features/trace/analysis/fold.ts",
            "apps/web/src/features/trace/scale/virtualizer.ts",
            "apps/web/src/features/trace/report/pins.ts",
            "apps/web/src/features/trace/view/controller.ts",
            "apps/web/src/features/trace/view/trace-workbench.tsx",
            "apps/web/src/components/tasks/task-detail.tsx",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_component_integration",
        "migration_strategy": "direct_port",
        "rationale": (
            "The existing Zyra canonical projection and timeline semantics remain "
            "the sole source of committed facts. The trace modules productize a "
            "derived index and interaction surface without creating another store."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/coding-agent/src/modes/rpc/rpc-types.ts;"
            "packages/coding-agent/src/modes/rpc/rpc-client.ts;"
            "packages/coding-agent/src/modes/rpc/rpc-subagents.ts;"
            "packages/coding-agent/src/task/yield-assembly.ts;"
            "packages/coding-agent/src/tools/bash-pty-selection.ts"
        ),
        "capability_name": "typed_trace_settlement_reconnect_and_subagent_correlation",
        "capability_summary": (
            "Typed request identities, partial/final and late settlement, reconnect "
            "epochs, parent tool/subagent ownership, yield provenance and PTY "
            "selection correlation cropped into Zyra canonical trace identities."
        ),
        "targets": [
            "apps/web/src/features/trace/identity.ts",
            "apps/web/src/features/trace/index/builder.ts",
            "apps/web/src/features/trace/index/reconciliation.ts",
            "apps/web/src/features/trace/navigation/runtime.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration/same_language_component_integration",
        "migration_strategy": "direct_port",
        "rationale": (
            "Only bounded correlation and settlement mechanics supplement the Zyra "
            "primary. OMP transport, agent loop, session files, provider runtime and "
            "task owner remain outside Zyra."
        ),
    },
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_language": "python",
        "target_language": "none",
        "source_path": "tui_gateway/server.py;tui_gateway/ws.py",
        "capability_name": "trace_reconnect_auth_and_replay_conformance",
        "capability_summary": (
            "Authentication, correlation, reconnect and replay-negative behavior "
            "used only to challenge trace reconciliation and navigation tests."
        ),
        "targets": [WEB_TEST, API_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "Hermes contributes negative conformance only. It owns no gateway, "
            "session, replay store, causal index or production path in Zyra."
        ),
    },
)


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    value = HELPER.HELPER.PRIOR.entry(decision)
    source_role = str(decision["source_role"])
    production = source_role in {"primary_implementation", "supplementary_implementation"}
    value.update(
        {
            "owner_unit": OWNER_UNIT,
            "created_at": STAMP,
            "updated_at": STAMP,
            "downstream_units": ["M2-S04A-01", "M2-04A"],
            "replacement_plan": (
                "Replace behind CausalTraceController and typed focus ports without "
                "moving canonical projection, event, task, session or artifact custody."
                if production
                else f"{source_role} source; no production owner is selected."
            ),
            "risk_notes": [
                "No runtime path depends on a parent source repository.",
                "CanonicalProjectionStore remains the only committed browser projection owner.",
                "Trace indexes, filters, folds, viewport and pins are disposable local state.",
                "Typed IDs are required; display strings never create causal joins.",
                "Closing the viewer cannot stop or mutate the task or a worker session.",
            ],
        }
    )
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "not_required" if decision["source_repo"] == "zyra" else "recorded",
        "license_hint": "M2 causal trace source role recorded.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["source_evidence"][0]["tags"] = ["m2-03b", source_role]
    value["main_path"] = {
        "surfaces": ["web_workbench", "task_detail", "causal_trace"],
        "event_types": [
            "task/session/worker lifecycle",
            "tool and browser action",
            "permission and compact/checkpoint",
            "placement/provider retry",
            "fault/recovery",
            "MCP/skill/subagent",
            "artifact and mutation",
        ],
        "api_routes": [
            "/tasks/{task_id}",
            "/tasks/{task_id}/events",
            "/tasks/{task_id}/artifacts",
        ],
        "control_commands": [],
        "artifact_kinds": ["trace_report_reference", "artifact_revision", "diff_revision"],
        "worker_runtime": (
            "CanonicalProjectionStore -> CausalTraceProjectionEngine -> "
            "CausalTraceController -> typed cross-view focus ports"
            if production
            else f"{source_role}; behavior only"
        ),
        "ui_panels": [
            "causal_trace",
            "critical_path",
            "trace_diagnostics",
            "timeline/topology/terminal/browser/artifact/diff focus",
        ],
    }
    value["runtime_entry"] = {
        "module": (
            "apps.web.src.features.trace.view.controller"
            if production
            else "tests.causal_trace_conformance"
        ),
        "function": (
            "CausalTraceController/CausalTraceWorkbench"
            if production
            else "causal trace conformance tests"
        ),
        "protocol": "zyra.causal-trace/v1",
        "health_check": f"bun test ./{WEB_TEST}; python -m pytest -q {API_TEST}",
        "command": "bun run build:web" if production else "",
        "config_refs": list(decision["targets"]),
        "environment_refs": [],
    }
    value["test_entries"] = [
        {
            "path": WEB_TEST,
            "command": f"bun test ./{WEB_TEST}",
            "kind": "unit",
            "expected_signal": (
                "typed index, critical path, filters, folds, large virtualization, "
                "late/orphan reconciliation, navigation, pins, close and disable behavior"
            ),
            "required": True,
        },
        {
            "path": API_TEST,
            "command": f"python -m pytest -q {API_TEST}",
            "kind": "integration",
            "expected_signal": (
                "real scheduler fault/recovery events reach the TypeScript trace projection"
            ),
            "required": True,
        },
    ]
    value["tags"] = ["m2-03b", SLICE_ID.lower(), "causal-trace", "cross-view", source_role]
    value["metadata"].update(
        {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "canonical_projection_owner": "typescript.CanonicalProjectionStore",
            "canonical_event_owner": "python.SQLiteStore+canonical event spine",
            "canonical_task_owner": "python.SQLiteStore+TaskState",
            "canonical_artifact_owner": "python.LocalArtifactStore+TaskState",
            "transient_trace_owner": "typescript.CausalTraceController",
            "root_source_runtime_dependency": False,
            "second_replay_store": False,
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
    return {**document, "entries": output, "summary": to_jsonable(InternalizationLedger(typed).summary())}


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
    selected = [item for item in entries if str(item.get("owner_unit") or "") == OWNER_UNIT]
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
    aligned, count, errors = synchronize(args.ledger.resolve(), write=args.write or not args.check)
    print(f"m2_causal_trace_ledger_aligned={str(aligned).lower()}")
    print(f"m2_causal_trace_source_decision_count={len(DECISIONS)}")
    print(f"m2_causal_trace_ledger_entry_count={count}")
    print(f"m2_causal_trace_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
