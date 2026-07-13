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
ACTIVE_INTEGRATION_TEST = "tests/unit/test_browser_observability_integration.py"
HTTP_INTEGRATION_TEST = "tests/integration/test_browser_session_productization_api.py"
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
    {
        "source_repo": "browser-use",
        "source_path": (
            "browser_use/browser/session.py;"
            "browser_use/browser/watchdogs/**;"
            "browser_use/browser/profile.py;"
            "browser_use/browser/views.py"
        ),
        "capability_name": "browser_active_watchdog_history_artifact_integration",
        "capability_summary": (
            "Pre-action event attachment, atomic history visibility, download and "
            "screenshot lifecycle cleanup, storage restore, navigation closure and "
            "typed failure handoff on the productized worker path."
        ),
        "target_paths": [
            "packages/workers/zyra_workers/browser_observability/integration/application.py",
            "packages/workers/zyra_workers/browser_observability/integration/event_bus.py",
            "packages/workers/zyra_workers/browser_observability/integration/downloads.py",
            "packages/workers/zyra_workers/browser_observability/integration/storage.py",
            "packages/workers/zyra_workers/browser_observability/integration/screenshots.py",
            "packages/workers/zyra_workers/browser_observability/integration/navigation.py",
            "packages/workers/zyra_workers/browser_observability/history_store.py",
            "packages/workers/zyra_workers/browser_session/screenshot_capture.py",
        ],
        "source_role": "primary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "lifecycle": "productized",
        "main_path_status": "tested_main_path",
        "runtime_required": True,
        "owner_unit": "M1-S04D-02",
        "rationale": (
            "Zyra reuses 04A event/CDP/profile and 04C action/download owners; "
            "04D observes and fences them without a second browser state owner."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_path": "openhands/events/**;openhands/storage/**;openhands/server/**",
        "capability_name": "browser_restart_projection_and_delivery_fence",
        "capability_summary": (
            "Restart-safe health/artifact projections, redacted HTTP views and a "
            "durable history/artifact/event/checkpoint delivery fence."
        ),
        "target_paths": [
            "packages/workers/zyra_workers/browser_observability/restart_projection.py",
            "packages/workers/zyra_workers/browser_observability/integration/commit_fence.py",
            "packages/workers/zyra_workers/browser_observability/api_projection.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "lifecycle": "productized",
        "main_path_status": "tested_main_path",
        "runtime_required": True,
        "owner_unit": "M1-S04D-02",
        "rationale": (
            "Existing Zyra EventLog, LocalArtifactStore, BrowserHistoryStore and "
            "TaskState retain custody; the fence exposes partial delivery rather "
            "than claiming a cross-store transaction."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_path": (
            "packages/coding-agent/src/core/agent-loop.ts;"
            "packages/coding-agent/src/tools/edit/hashline.ts;"
            "packages/coding-agent/src/task/**;"
            "packages/coding-agent/src/worktree/**"
        ),
        "capability_name": "browser_runtime_evidence_and_trajectory_mapping",
        "capability_summary": (
            "Strict partial/terminal provider evidence, MCP/subagent/background "
            "correlation, Hashline/worktree conflict receipts and immutable "
            "MemoryFabric trajectory input."
        ),
        "target_paths": [
            "packages/workers/zyra_workers/browser_observability/integration/contracts.py",
            "packages/workers/zyra_workers/browser_observability/integration/runtime_evidence.py",
            "packages/workers/zyra_workers/browser_observability/integration/trajectory.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "lifecycle": "productized",
        "main_path_status": "tested_main_path",
        "runtime_required": True,
        "owner_unit": "M1-S04D-02",
        "rationale": (
            "The optional mapper cannot affect canonical browser execution when "
            "disabled and never becomes provider, MCP, subagent or recovery owner."
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
    owner_unit = str(decision.get("owner_unit") or "M1-S04D-01")
    integration_slice = owner_unit == "M1-S04D-02"
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
        "owner_unit": owner_unit,
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
                "path": (
                    ACTIVE_INTEGRATION_TEST if integration_slice else INTEGRATION_TEST
                ),
                "command": (
                    "python -m pytest tests/unit/test_browser_observability_integration.py -q"
                    if integration_slice
                    else INTEGRATION_COMMAND
                ),
                "kind": "integration",
                "expected_signal": (
                    "default browser path publishes history/trace/artifacts "
                    "and never emits recovery_planned"
                ),
                "required": runtime_required,
            },
            *(
                [
                    {
                        "path": HTTP_INTEGRATION_TEST,
                        "command": (
                            "python -m pytest "
                            "tests/integration/test_browser_session_productization_api.py -q"
                        ),
                        "kind": "integration",
                        "expected_signal": (
                            "HTTP main path closes the event/checkpoint fence and "
                            "rebuilds redacted durable views"
                        ),
                        "required": True,
                    }
                ]
                if integration_slice and runtime_required
                else []
            ),
        ],
        "notes": (
            f"source_role={decision['source_role']}; "
            f"runtime_required={str(runtime_required).lower()}; "
            f"{decision['rationale']}"
        ),
        "metadata": {
            "owner_unit": owner_unit,
            "source_role": decision["source_role"],
            "canonical_task_owner": "SQLite/TaskState",
            "canonical_event_owner": "EventLog",
            "canonical_artifact_owner": "LocalArtifactStore",
            "recovery_planner_owner": "M1-07C",
            "root_source_runtime_dependency": False,
        },
    }


def rewrite(document: Any) -> Any:
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


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    current = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite(current)
    aligned = canonical(current) == canonical(expected)
    if write and not aligned:
        path.write_text(canonical(expected), encoding="utf-8", newline="\n")
        aligned = True
    return aligned, len(DECISIONS)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    write = args.write or not args.check
    aligned, count = synchronize(args.ledger.resolve(), write=write)
    print(f"browser_observability_source_ledger_aligned={str(aligned).lower()}")
    print(f"browser_observability_source_decision_count={count}")
    print(f"browser_observability_owner_units=M1-S04D-01,M1-S04D-02")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
