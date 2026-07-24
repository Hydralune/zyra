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
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import (  # noqa: E402
    InternalizationLedger,
    InternalizationLedgerEntry,
)
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402


PRIOR_PATH = ROOT / "scripts" / "sync_m2_worker_causal_timeline_source_ledger.py"
PRIOR_SPEC = importlib.util.spec_from_file_location(
    "m2_worker_timeline_ledger_helpers",
    PRIOR_PATH,
)
if PRIOR_SPEC is None or PRIOR_SPEC.loader is None:
    raise RuntimeError("M2-S02B-01 ledger helper could not be loaded")
PRIOR = importlib.util.module_from_spec(PRIOR_SPEC)
PRIOR_SPEC.loader.exec_module(PRIOR)

DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNER_UNIT = "M2-S02B-02"
SLICE_ID = OWNER_UNIT
IMPLEMENTATION_COMMIT = "f3302a3c5366218068cb8acf9790c973e4b62033"
BASELINE_COMMIT = "46be71eb4ced2da38f5b05fce24907da7ffc8f6c"
STAMP = "2026-07-24T00:00:00.000Z"
WEB_TESTS = (
    "apps/web/test/recovery-control-sealed-timeline.test.ts",
    "apps/web/test/scaled-recovery-timeline.test.ts",
)
INTEGRATION_TEST = (
    "tests/integration/test_recovery_control_sealed_timeline_integration.py"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/app/src/pages/session/use-session-commands.tsx;"
            "packages/app/src/context/permission.tsx;"
            "packages/app/src/pages/session/timeline/"
            "{model.ts,row-reconciliation.ts,message-timeline.tsx}"
        ),
        "capability_name": (
            "durable_recovery_control_and_scaled_timeline_orchestration"
        ),
        "capability_summary": (
            "Durable control submission, abort-before-revert ordering, "
            "canonical completion reconciliation, stable row identity and "
            "bounded large-history window behavior."
        ),
        "targets": [
            "apps/web/src/features/timeline/control/commands.ts",
            "apps/web/src/features/timeline/control/contracts.ts",
            "apps/web/src/features/timeline/control/observation.ts",
            "apps/web/src/features/timeline/control/transport.ts",
            "apps/web/src/features/timeline/control/validation.ts",
            "apps/web/src/features/timeline/scale/causal-fold.ts",
            "apps/web/src/features/timeline/scale/contracts.ts",
            "apps/web/src/features/timeline/scale/goal-drift.ts",
            "apps/web/src/features/timeline/scale/projector.ts",
            "apps/web/src/features/timeline/scale/search-index.ts",
            "apps/web/src/features/timeline/scale/virtualizer.ts",
            "apps/web/src/features/timeline/view/timeline-workbench.tsx",
        ],
        "source_role": "primary_implementation",
        "migration_mode": (
            "cropped_migration/same_language_component_integration"
        ),
        "migration_strategy": "direct_port",
        "rationale": (
            "The TypeScript session-control and timeline mechanisms were "
            "cropped into Zyra control and scale modules over TaskApi and "
            "CanonicalProjectionStore. OpenCode session, permission, provider, "
            "tool and persistence owners are not copied or invoked."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "frontend/src/hooks/mutation/"
            "{use-unified-stop-conversation.ts,use-unified-start-conversation.ts,"
            "use-v1-resume-conversation.ts};"
            "frontend/src/components/features/controls/agent-status.tsx;"
            "frontend/src/hooks/use-sandbox-recovery.ts"
        ),
        "capability_name": (
            "control_receipt_connection_and_panel_state_reconciliation"
        ),
        "capability_summary": (
            "Stop/resume pending state, rollback/refetch recovery, live status "
            "and connection-aware receipt reconciliation without optimistic "
            "worker mutation."
        ),
        "targets": [
            "apps/web/src/features/timeline/control/ledger.ts",
            "apps/web/src/features/timeline/control/runtime.ts",
            "apps/web/src/features/timeline/view/recovery-control-panel.tsx",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration",
        "migration_strategy": "selective_port",
        "rationale": (
            "Only receipt and connection-state mechanics supplement the "
            "OpenCode-derived orchestration. OpenHands Redux, router, sandbox, "
            "conversation and approval owners remain excluded."
        ),
    },
    {
        "source_repo": "browser-use",
        "source_commit": "18484f23ac96bb955259a1c54530a7d265dfffdb",
        "source_language": "python",
        "target_language": "typescript",
        "source_path": (
            "browser_use/agent/views.py::{AgentState,ActionResult,"
            "AgentHistoryList.errors};browser_use/agent/service.py::"
            "{_check_stop_or_pause,pause,resume,reconnection recovery,"
            "consecutive-failure reset}"
        ),
        "capability_name": (
            "action_failure_reconnect_bounded_retry_timeline_semantics"
        ),
        "capability_summary": (
            "Action failures remain visible until canonical recovery, "
            "connection restoration supersedes without erasure, bounded retry "
            "attempts stay observable and no missing result becomes success."
        ),
        "targets": [
            "apps/web/src/features/timeline/control/receipts.ts",
            "apps/web/src/features/timeline/control/sealed.ts",
            "apps/web/src/features/timeline/scale/overlays.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "semantic_port/production",
        "migration_strategy": "semantic_port",
        "rationale": (
            "This bounded Python-to-TypeScript presentation port consumes "
            "canonical action and recovery facts only. It does not port the "
            "browser controller, DOM state, executor, retry loop, persistence "
            "or watchdog."
        ),
    },
    {
        "source_repo": "agent-framework",
        "source_commit": "d50698bb797710bfd1ebf34eb621c905a4009b2d",
        "source_language": "python/dotnet",
        "target_language": "none",
        "source_path": "workflow/checkpoint and AG-UI approval/history contracts",
        "capability_name": "control_approval_history_conformance",
        "capability_summary": (
            "Checkpoint lifecycle and approval/history behavior conformance."
        ),
        "targets": list(WEB_TESTS),
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "Agent Framework is behavior comparison only. No workflow, "
            "checkpoint, history, middleware or approval owner is migrated."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_language": "typescript",
        "target_language": "none",
        "source_path": "lifecycle/control contracts",
        "capability_name": "sealed_control_negative_examples",
        "capability_summary": (
            "Lifecycle and control negative examples for fail-closed tests."
        ),
        "targets": [WEB_TESTS[0], INTEGRATION_TEST],
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "OMP is used only for bounded negative examples. It owns no "
            "production control, recovery, process, tool or session path."
        ),
    },
    {
        "source_repo": "langgraph",
        "source_commit": "5931a5f0b313feff24e2516a586c55601b868ac1",
        "source_language": "python/typescript",
        "target_language": "none",
        "source_path": (
            "source-graphs/langgraph/source-graph.md#12 narrow "
            "checkpoint/interrupt contracts"
        ),
        "capability_name": "exact_resume_control_conformance",
        "capability_summary": (
            "Checkpoint identity, pending/committed writes, stable task id and "
            "interrupt/resume correlation conformance."
        ),
        "targets": [WEB_TESTS[0], INTEGRATION_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "LangGraph remains limited to exact-resume conformance. StateGraph, "
            "channels, reducers, Pregel, stream, Store, ToolNode, SDK, server "
            "and deployment code own no production path."
        ),
    },
)


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    value = PRIOR.entry(decision)
    source_role = str(decision["source_role"])
    production = source_role in {
        "primary_implementation",
        "supplementary_implementation",
    }
    value.update(
        {
            "owner_unit": OWNER_UNIT,
            "created_at": STAMP,
            "updated_at": STAMP,
            "downstream_units": ["M2-03A", "M2-04A"],
            "replacement_plan": (
                "Replace behind RecoveryControlRuntime or "
                "ScaledTimelineRuntime without moving command, permission, "
                "worker, recovery, graph, checkpoint or event custody."
                if production
                else f"{source_role} source; no production runtime is selected."
            ),
        }
    )
    value["tags"] = [
        "m2-02b",
        SLICE_ID.lower(),
        "recovery-control",
        "scaled-timeline",
        source_role,
    ]
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "recorded",
        "license_hint": (
            "M2 recovery control and scaled timeline source role recorded."
        ),
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["main_path"] = {
        "surfaces": ["web_workbench", "task_detail"],
        "event_types": [
            "command.*",
            "permission.*",
            "worker.*",
            "recovery.*",
            "topology.*",
            "checkpoint.*",
        ],
        "api_routes": ["/tasks/{task_id}/commands"],
        "control_commands": [
            "/kill",
            "/steer",
            "/retry",
            "/reassign",
            "/resume",
        ],
        "artifact_kinds": ["control_receipt", "scaled_timeline_projection"],
        "worker_runtime": (
            "TaskDetail -> RecoveryControlRuntime -> TaskApi.controlCommand -> "
            "RuntimeControlDispatcher -> canonical owner -> "
            "CanonicalProjectionStore -> timeline receipt observer"
            if production
            else f"{source_role}; behavior only"
        ),
        "ui_panels": [
            "recovery_control_panel",
            "worker_causal_timeline",
            "scaled_timeline_viewport",
        ],
    }
    value["runtime_entry"] = {
        "module": (
            "apps.web.src.features.timeline.control.runtime"
            if production
            else "apps.web.test.recovery-control-sealed-timeline"
        ),
        "function": (
            "RecoveryControlRuntime/ScaledTimelineRuntime"
            if production
            else "recovery control conformance tests"
        ),
        "protocol": "zyra.recovery-control-timeline/v1",
        "health_check": (
            "bun test ./apps/web/test/"
            "recovery-control-sealed-timeline.test.ts "
            "./apps/web/test/scaled-recovery-timeline.test.ts"
        ),
        "command": "bun run build:web" if production else "",
        "config_refs": list(decision["targets"]),
        "environment_refs": [],
    }
    value["test_entries"] = [
        {
            "path": WEB_TESTS[0],
            "command": f"bun test ./{WEB_TESTS[0]}",
            "kind": "unit",
            "expected_signal": (
                "five control mappings, durable timeout/late receipt, "
                "sealed denial, race, disconnect and disabled binding"
            ),
            "required": True,
        },
        {
            "path": WEB_TESTS[1],
            "command": f"bun test ./{WEB_TESTS[1]}",
            "kind": "unit",
            "expected_signal": (
                "five-thousand-row bounded virtualization, causal folds, "
                "indexed search, goal drift and effective overlays"
            ),
            "required": True,
        },
        {
            "path": INTEGRATION_TEST,
            "command": f"python -m pytest -q {INTEGRATION_TEST}",
            "kind": "integration",
            "expected_signal": (
                "real HTTP kill/steer/retry/reassign/resume, sealed deny/replan "
                "and canonical state-owner effects"
            ),
            "required": production,
        },
    ]
    value["metadata"].update(
        {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "canonical_command_owner": "python.RuntimeControlDispatcher",
            "canonical_permission_owner": "python.ToolPermissionRuntime",
            "canonical_worker_owner": "python.WorkerPoolStore",
            "canonical_recovery_owner": "python.RecoveryApplication",
            "canonical_graph_owner": "python.GraphStateCustody",
            "canonical_checkpoint_owner": (
                "python.SessionControlRuntime/RecoveryApplication"
            ),
            "frontend_event_owner": "typescript.CanonicalProjectionStore",
            "transient_control_owner": "typescript.RecoveryControlRuntime",
            "transient_scale_owner": "typescript.ScaledTimelineRuntime",
            "root_source_runtime_dependency": False,
            "implementation_commit": IMPLEMENTATION_COMMIT,
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
    retained = [
        item
        for item in entries
        if str(item.get("owner_unit") or "") != OWNER_UNIT
        and str((item.get("metadata") or {}).get("slice_id") or "")
        != SLICE_ID
    ]
    output = [*retained, *(entry(decision) for decision in DECISIONS)]
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
        item
        for item in entries
        if str(item.get("owner_unit") or "") == OWNER_UNIT
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
    print(f"m2_recovery_timeline_ledger_aligned={str(aligned).lower()}")
    print(f"m2_recovery_timeline_source_decision_count={len(DECISIONS)}")
    print(f"m2_recovery_timeline_ledger_entry_count={count}")
    print(f"m2_recovery_timeline_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return (
        0
        if aligned and not errors and count == len(DECISIONS)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
