from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
FOUNDATION_TEST = "tests/unit/test_browser_observability_foundation.py"
INTEGRATION_TEST = "tests/integration/test_browser_observability_main_path.py"
FOUNDATION_COMMAND = (
    "python -m pytest tests/unit/test_browser_observability_foundation.py -q"
)
INTEGRATION_COMMAND = (
    "python -m pytest tests/integration/test_browser_observability_main_path.py -q"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "browser-use",
        "source_path": (
            "browser_use/browser/session.py;"
            "browser_use/browser/watchdogs/local_browser_watchdog.py;"
            "browser_use/browser/watchdogs/security_watchdog.py;"
            "browser_use/browser/watchdogs/downloads_watchdog.py;"
            "browser_use/browser/watchdogs/storage_state_watchdog.py;"
            "browser_use/browser/watchdogs/permissions_watchdog.py;"
            "browser_use/browser/watchdogs/screenshot_watchdog.py;"
            "browser_use/browser/watchdogs/popups_watchdog.py;"
            "browser_use/browser/watchdogs/aboutblank_watchdog.py;"
            "browser_use/agent/views.py"
        ),
        "capability_name": "browser_attached_watchdogs_and_durable_history",
        "capability_summary": (
            "Attached watchdog lifecycle, durable action/result history, "
            "process/CDP/timeout crash detection and deterministic replay."
        ),
        "target_paths": [
            "packages/workers/zyra_workers/browser_observability/watchdogs.py",
            "packages/workers/zyra_workers/browser_observability/history_store.py",
            "packages/workers/zyra_workers/browser_observability/history_runtime.py",
            "packages/workers/zyra_workers/browser_observability/crash_detector.py",
        ],
        "source_role": "primary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "lifecycle": "productized",
        "main_path_status": "tested_main_path",
        "runtime_required": True,
        "rationale": (
            "Browser Use behavior was decomposed into Zyra scope/event/artifact "
            "contracts; its unattached CrashWatchdog is explicitly rejected."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_path": "openhands/events/**;openhands/storage/**",
        "capability_name": "browser_artifact_event_projection",
        "capability_summary": (
            "Artifact lineage, canonical event projection and queryable "
            "history/trace/download/screenshot API views."
        ),
        "target_paths": [
            "packages/workers/zyra_workers/browser_observability/artifact_publisher.py",
            "packages/workers/zyra_workers/browser_observability/api_projection.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "lifecycle": "productized",
        "main_path_status": "tested_main_path",
        "runtime_required": True,
        "rationale": (
            "Projection behavior is retained without creating a second event "
            "store or artifact owner."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_path": (
            "packages/coding-agent/src/core/agent-loop.ts;"
            "packages/coding-agent/src/tools/edit/hashline.ts"
        ),
        "capability_name": "browser_trace_pairing_and_receipts",
        "capability_summary": (
            "Tool call/result pairing, digest-linked history and artifact "
            "receipt semantics."
        ),
        "target_paths": [
            "packages/workers/zyra_workers/browser_observability/trace_runtime.py",
            "packages/workers/zyra_workers/browser_observability/replay.py",
            "packages/workers/zyra_workers/browser_observability/models.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "lifecycle": "productized",
        "main_path_status": "tested_main_path",
        "runtime_required": True,
        "rationale": (
            "OMP process/session/tool owners are not copied; pairing and "
            "receipt semantics are mapped into Zyra durable records."
        ),
    },
    {
        "source_repo": "browser-use",
        "source_path": "browser_use/browser/watchdogs/crash_watchdog.py",
        "capability_name": "upstream_unattached_crash_watchdog",
        "capability_summary": (
            "Rejected upstream CrashWatchdog; Zyra observes process exit, "
            "CDP disconnect and silent timeout directly."
        ),
        "target_paths": [
            "packages/workers/zyra_workers/browser_observability/crash_detector.py",
        ],
        "source_role": "rejected",
        "migration_strategy": "not_selected",
        "lifecycle": "candidate",
        "main_path_status": "inventoried",
        "runtime_required": False,
        "rationale": (
            "Browser Use comments this watchdog out of attach_all_watchdogs, "
            "so it cannot be represented as an attached upstream behavior."
        ),
    },
)


def ledger_id(
    decision: dict[str, Any],
) -> str:
    identity = "|".join(
        (
            str(decision["source_repo"]),
            str(decision["source_path"]),
            str(decision["capability_name"]),
        )
    )
    return "ledger_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def entry(
    decision: dict[str, Any],
) -> dict[str, Any]:
    targets = list(decision["target_paths"])
    runtime_required = bool(decision["runtime_required"])
    main_path = (
        {
            "surfaces": [
                "browser_worker",
                "browser_observability_api",
                "durable_browser_history",
            ],
            "event_types": [
                "worker_health",
                "agent_message",
                "artifact_written",
            ],
            "api_routes": [
                "POST /tasks/{task_id}/workers/browser",
                "GET /tasks/{task_id}/browser-observability",
            ],
            "control_commands": [],
            "artifact_kinds": [
                "trace",
                "structured_data",
                "screenshot",
                "file",
            ],
            "worker_runtime": "BrowserWorkerRuntime.run",
            "ui_panels": [],
        }
        if runtime_required
        else {
            "surfaces": [],
            "event_types": [],
            "api_routes": [],
            "control_commands": [],
            "artifact_kinds": [],
            "worker_runtime": "",
            "ui_panels": [],
        }
    )
    return {
        "ledger_id": ledger_id(decision),
        "source_repo": decision["source_repo"],
        "source_path": decision["source_path"],
        "capability_name": decision["capability_name"],
        "capability_summary": decision["capability_summary"],
        "target_bindings": [
            {
                "path": path,
                "role": "primary" if index == 0 else "supporting",
                "required_for_main_path": runtime_required,
            }
            for index, path in enumerate(targets)
        ],
        "migration_strategy": decision["migration_strategy"],
        "main_path_status": decision["main_path_status"],
        "lifecycle": decision["lifecycle"],
        "owner_unit": "M1-S04D-01",
        "main_path": main_path,
        "line_count_policy": (
            "counts_as_runtime"
            if runtime_required
            else "excluded_inventory_only"
        ),
        "runtime_entry": {
            "module": "zyra_workers.browser_observability.application",
            "function": (
                "BrowserObservabilityApplication.observe"
                if runtime_required
                else "BrowserObservabilitySourceAuditor.audit"
            ),
            "protocol": "zyra-browser-observability-v1",
            "health_check": INTEGRATION_COMMAND,
            "config_refs": targets,
        },
        "test_entries": [
            {
                "path": FOUNDATION_TEST,
                "command": FOUNDATION_COMMAND,
                "kind": "unit",
                "expected_signal": (
                    "durable history, attached watchdogs, crash detector and "
                    "recovery handoff behave deterministically"
                ),
                "required": True,
            },
            {
                "path": INTEGRATION_TEST,
                "command": INTEGRATION_COMMAND,
                "kind": "integration",
                "expected_signal": (
                    "default browser path publishes history/trace/artifacts "
                    "and never emits recovery_planned"
                ),
                "required": runtime_required,
            },
        ],
        "notes": (
            f"source_role={decision['source_role']}; "
            f"runtime_required={str(runtime_required).lower()}; "
            f"{decision['rationale']}"
        ),
        "metadata": {
            "owner_unit": "M1-S04D-01",
            "source_role": decision["source_role"],
            "canonical_task_owner": "SQLite/TaskState",
            "canonical_event_owner": "EventLog",
            "canonical_artifact_owner": "LocalArtifactStore",
            "recovery_planner_owner": "M1-07C",
            "root_source_runtime_dependency": False,
        },
    }


def sync(
    path: Path,
) -> int:
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict) and isinstance(document.get("entries"), list):
        entries = document["entries"]
    else:
        raise ValueError("internalization ledger seed must be a list or contain entries")
    replacements = {
        item["ledger_id"]: item
        for item in (entry(decision) for decision in DECISIONS)
    }
    output = [
        replacements.pop(str(item.get("ledger_id") or ""), item)
        for item in entries
    ]
    output.extend(replacements.values())
    if isinstance(document, list):
        updated: Any = output
    else:
        updated = {**document, "entries": output}
    path.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(replacements)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
    )
    args = parser.parse_args()
    sync(args.ledger.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
