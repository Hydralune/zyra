from __future__ import annotations

import argparse
import hashlib
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


DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNER_UNIT = "M2-S02B-01"
SLICE_ID = OWNER_UNIT
IMPLEMENTATION_COMMIT = "636ba5dc9887c9d563fb0ba2949f50b6d0fcbe42"
BASELINE_COMMIT = "0980c52e1a0624a06501ccd0c39a723e624c95e3"
STAMP = "2026-07-24T00:00:00.000Z"
WEB_TEST = "apps/web/test/worker-causal-timeline.test.ts"
INTEGRATION_TEST = (
    "tests/integration/test_worker_causal_timeline_projection.py"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/app/src/pages/session/timeline/{model.ts,projection.ts,"
            "rows.ts,timeline-row.ts,row-reconciliation.ts,virtual-items.ts,"
            "message-timeline.tsx}"
        ),
        "capability_name": "worker_causal_timeline_projection_and_window",
        "capability_summary": (
            "Stable timeline row identity, incremental event reconciliation, "
            "active/visible row projection, causal inspection and bounded "
            "windowing over immutable canonical task events."
        ),
        "targets": [
            "apps/web/src/features/timeline/projection/contracts.ts",
            "apps/web/src/features/timeline/projection/event-reader.ts",
            "apps/web/src/features/timeline/projection/causal-graph.ts",
            "apps/web/src/features/timeline/projection/worker-epochs.ts",
            "apps/web/src/features/timeline/projection/critical-path.ts",
            "apps/web/src/features/timeline/projection/drilldown.ts",
            "apps/web/src/features/timeline/projection/windowing.ts",
            "apps/web/src/features/timeline/projection/projector.ts",
            "apps/web/src/features/timeline/view/controller.ts",
            "apps/web/src/features/timeline/view/timeline-workbench.tsx",
            "apps/web/src/components/tasks/task-detail.tsx",
        ],
        "source_role": "primary_implementation",
        "migration_mode": (
            "cropped_migration/same_language_component_integration"
        ),
        "migration_strategy": "direct_port",
        "rationale": (
            "The TypeScript timeline mechanisms are cropped into Zyra feature "
            "modules and rewritten against CanonicalProjectionState. No "
            "OpenCode store, session runtime, provider, command or tool owner "
            "is copied or invoked."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "frontend/src/utils/status.ts;"
            "frontend/src/utils/handle-event-for-ui.ts;"
            "frontend/src/hooks/use-filtered-events.ts"
        ),
        "capability_name": "timeline_status_and_late_event_reconciliation",
        "capability_summary": (
            "Lifecycle precedence, action/observation replacement, late "
            "result reconciliation, deduplicated fold and filtered event gaps."
        ),
        "targets": [
            "apps/web/src/features/timeline/projection/phase-machine.ts",
            "apps/web/src/features/timeline/projection/event-reconciliation.ts",
            "apps/web/src/features/timeline/projection/filtering.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration",
        "migration_strategy": "selective_port",
        "rationale": (
            "Only phase precedence and UI event-fold gaps supplement the "
            "OpenCode-derived row projection. OpenHands Redux, router, event "
            "client, conversation and sandbox owners remain excluded."
        ),
    },
    {
        "source_repo": "browser-use",
        "source_commit": "18484f23ac96bb955259a1c54530a7d265dfffdb",
        "source_language": "python",
        "target_language": "typescript",
        "source_path": (
            "browser_use/agent/views.py::{AgentHistory,"
            "AgentHistoryList.errors,action_history,agent_steps}"
        ),
        "capability_name": "browser_action_result_state_timeline_step",
        "capability_summary": (
            "Browser action, result and state grouping with ordered actions, "
            "partial-result handling and bounded error summaries."
        ),
        "targets": [
            "apps/web/src/features/timeline/projection/browser-step-adapter.ts"
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "semantic_port/production",
        "migration_strategy": "semantic_port",
        "rationale": (
            "A bounded Python-to-TypeScript semantic port is required at the "
            "browser-resident projection boundary. It consumes only safe "
            "canonical events and does not port the browser controller, DOM "
            "owner, action executor, retry loop or history persistence."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/coding-agent/src/registry/agent-lifecycle.ts;"
            "packages/coding-agent/src/tools/job.ts"
        ),
        "capability_name": "background_job_park_revive_timeline_lineage",
        "capability_summary": (
            "Background job status, park/revive lineage and recovery-attempt "
            "display over existing durable worker and recovery facts."
        ),
        "targets": [
            "apps/web/src/features/timeline/projection/background-lifecycle.ts",
            "apps/web/src/features/timeline/projection/recovery-chain.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration",
        "migration_strategy": "selective_port",
        "rationale": (
            "The original-language park/revive and job lineage mechanisms are "
            "cropped into a read-only derived projector. OMP process, session, "
            "agent loop, tool and task owners are not imported."
        ),
    },
    {
        "source_repo": "agent-framework",
        "source_commit": "d50698bb797710bfd1ebf34eb621c905a4009b2d",
        "source_language": "python/dotnet",
        "target_language": "none",
        "source_path": "workflow/executor lifecycle contracts",
        "capability_name": "worker_lifecycle_terminal_priority_conformance",
        "capability_summary": (
            "Lifecycle vocabulary and terminal/error priority conformance."
        ),
        "targets": [WEB_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "Agent Framework contributes behavior comparison only. No workflow, "
            "executor, history, middleware or approval owner is migrated."
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
        "capability_name": "timeline_interrupt_resume_lineage_conformance",
        "capability_summary": (
            "Interrupt/resume correlation, stable task identity, pending versus "
            "committed writes and exact-resume lineage conformance."
        ),
        "targets": [WEB_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "LangGraph remains limited to the narrow checkpoint contract. "
            "StateGraph, channels, reducers, Pregel, stream, Store, ToolNode, "
            "SDK, server and deployment code own no production path."
        ),
    },
)


def ledger_id(decision: dict[str, Any]) -> str:
    identity = "|".join(
        (
            str(decision["source_repo"]),
            str(decision["source_path"]),
            str(decision["capability_name"]),
        )
    )
    return "ile_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    targets = list(decision["targets"])
    source_role = str(decision["source_role"])
    production = source_role in {
        "primary_implementation",
        "supplementary_implementation",
    }
    return {
        "ledger_id": ledger_id(decision),
        "source_repo": decision["source_repo"],
        "source_path": decision["source_path"],
        "capability_name": decision["capability_name"],
        "capability_summary": decision["capability_summary"],
        "migration_strategy": decision["migration_strategy"],
        "notes": (
            f"source_role={source_role}; "
            f"source_commit={decision['source_commit']}; "
            f"migration_mode={decision['migration_mode']}; "
            f"{decision['rationale']}"
        ),
        "main_path_status": (
            "tested_main_path" if production else "inventoried"
        ),
        "lifecycle": "productized" if production else "candidate",
        "line_count_policy": (
            "counts_as_runtime"
            if production
            else "excluded_inventory_only"
        ),
        "owner_unit": OWNER_UNIT,
        "milestone": "M2",
        "created_at": STAMP,
        "updated_at": STAMP,
        "dependencies": [],
        "downstream_units": ["M2-S02B-02", "M2-03A", "M2-04A"],
        "blockers": [],
        "replacement_plan": (
            "Replace behind selectWorkerCausalTimeline without moving event, "
            "worker, lease, failure, recovery, scheduler or artifact custody."
            if production
            else f"{source_role} source; no production runtime is selected."
        ),
        "risk_notes": [
            "No runtime path may depend on a parent source repository.",
            (
                "CanonicalProjectionStore remains the sole frontend fact "
                "owner; timeline rows are derived and read-only."
            ),
            (
                "M1 worker, lease, failure, recovery, scheduler and artifact "
                "stores remain authoritative."
            ),
            (
                "The source cannot become a second canonical owner or command "
                "path."
            ),
        ],
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "recorded",
            "license_hint": "M2 worker causal timeline source role recorded.",
            "notice_path": "third_party/NOTICE.md",
            "source_url": "",
            "notes": "No runtime dependency on a parent source repository.",
        },
        "source_evidence": [
            {
                "source_repo": decision["source_repo"],
                "source_path": decision["source_path"],
                "source_kind": "file",
                "exists_in_workspace": True,
                "symbols": [],
                "reason": f"{SLICE_ID} source-to-target decision",
                "tags": ["m2-02b", source_role],
            }
        ],
        "target_bindings": [
            {
                "target_path": path,
                "role": "primary" if index == 0 else "supporting",
                "required_for_main_path": production,
                "must_exist_for_statuses": (
                    ["tested_main_path"] if production else []
                ),
            }
            for index, path in enumerate(targets)
        ],
        "main_path": {
            "surfaces": ["web_workbench", "task_detail"],
            "event_types": [
                "worker.*",
                "tool.*",
                "permission.*",
                "artifact.*",
                "watchdog.*",
                "recovery.*",
                "scheduler.*",
                "background.*",
                "browser.*",
            ],
            "api_routes": [],
            "control_commands": [],
            "artifact_kinds": [
                "timeline_projection",
                "causal_evidence",
            ],
            "worker_runtime": (
                "CanonicalProjectionStore -> "
                "selectWorkerCausalTimeline -> "
                "TimelineWorkbenchController -> task-detail timeline"
                if production
                else f"{source_role}; behavior only"
            ),
            "ui_panels": [
                "worker_causal_timeline",
                "timeline_inspector",
            ],
        },
        "runtime_entry": {
            "module": (
                "apps.web.src.features.timeline.projection.projector"
                if production
                else "apps.web.test.worker-causal-timeline"
            ),
            "function": (
                "selectWorkerCausalTimeline"
                if production
                else "worker causal timeline conformance tests"
            ),
            "protocol": "zyra.worker-causal-timeline/v1",
            "health_check": f"bun test ./{WEB_TEST}",
            "command": "bun run build:web" if production else "",
            "config_refs": targets,
            "environment_refs": [],
        },
        "test_entries": [
            {
                "path": WEB_TEST,
                "command": f"bun test ./{WEB_TEST}",
                "kind": "unit",
                "expected_signal": (
                    "phase, causal join, critical path, filter gaps, late and "
                    "partial reconciliation, replacement, background revival "
                    "and disabled-projector behavior"
                ),
                "required": True,
            },
            {
                "path": INTEGRATION_TEST,
                "command": (
                    f"python -m pytest -q {INTEGRATION_TEST}"
                ),
                "kind": "integration",
                "expected_signal": (
                    "real M1 worker dispatch, fault injection and recovery plan "
                    "drive the canonical TypeScript timeline"
                ),
                "required": production,
            },
        ],
        "tags": [
            "m2-02b",
            SLICE_ID.lower(),
            "worker-causal-timeline",
            source_role,
        ],
        "metadata": {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "canonical_event_owner": "typescript.CanonicalProjectionStore",
            "canonical_worker_owner": "python.WorkerPoolStore",
            "canonical_recovery_owner": "python.RecoveryPlanner",
            "derived_projection_owner": (
                "typescript.selectWorkerCausalTimeline"
            ),
            "transient_view_owner": (
                "typescript.TimelineWorkbenchController"
            ),
            "root_source_runtime_dependency": False,
            "implementation_commit": IMPLEMENTATION_COMMIT,
            "rationale": decision["rationale"],
        },
    }


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
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


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
        path.write_text(
            canonical(expected),
            encoding="utf-8",
            newline="\n",
        )
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
    print(f"m2_worker_timeline_ledger_aligned={str(aligned).lower()}")
    print(f"m2_worker_timeline_source_decision_count={len(DECISIONS)}")
    print(f"m2_worker_timeline_ledger_entry_count={count}")
    print(f"m2_worker_timeline_missing_target_count={len(errors)}")
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
