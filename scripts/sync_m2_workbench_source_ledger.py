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
OWNER_UNIT = "M2-S01A-02"
SLICE_ID = "M2-S01A-02"
STAMP = "2026-07-23T00:00:00.000Z"
TEST_PATH = "apps/web/test/workbench-shell.test.tsx"
TEST_COMMAND = "bun test ./apps/web/test/workbench-shell.test.tsx"
IMPLEMENTATION_COMMIT = "4b7f5f8bd5bc3e2819921bc62d27daf36ed8e682"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_path": (
            "packages/app/src/context/layout.tsx;"
            "packages/app/src/utils/session-route.ts;"
            "packages/app/src/components/prompt-input.tsx;"
            "packages/app/src/components/prompt-input/{submit,submission-state,"
            "history}.ts;packages/app/src/components/prompt-input/slash-popover.tsx"
        ),
        "capability_name": "workbench_shell_routing_layout_and_task_navigation",
        "capability_summary": (
            "Route-derived workbench state, responsive split layout, task navigation, "
            "request-state rendering and keyboard-oriented command surface."
        ),
        "targets": [
            "apps/web/src/app/workbench-app.tsx",
            "apps/web/src/shell/router.ts",
            "apps/web/src/shell/layout-runtime.ts",
            "apps/web/src/components/tasks/task-list.tsx",
            "apps/web/src/components/tasks/task-detail.tsx",
            "apps/web/src/components/command-input/command-input.tsx",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_crop_and_adapt",
        "migration_strategy": "direct_port",
        "rationale": (
            "OpenCode shell, route and prompt interaction mechanisms were decomposed "
            "into Zyra-owned React/TypeScript modules. Solid stores, OpenCode session "
            "owners and SDK dependencies were not copied."
        ),
    },
    {
        "source_repo": "claude-code-best",
        "source_commit": "c57f5a29aa7012308252cf739f4f8c3430a1c034",
        "source_path": (
            "src/screens/REPL.tsx;src/components/PromptInput/PromptInput.tsx;"
            "src/components/PromptInput/PromptInputQueuedCommands.tsx;"
            "src/utils/messageQueueManager.ts;src/hooks/useCommandQueue.ts;"
            "src/utils/handlePromptSubmit.ts"
        ),
        "capability_name": "command_input_queue_history_completion_and_submission",
        "capability_summary": (
            "Structured slash-command parsing, argument completion, draft/history "
            "restore, bounded queued submission, fail-closed disablement and "
            "keyboard/focus semantics."
        ),
        "targets": [
            "apps/web/src/command/parser.ts",
            "apps/web/src/command/catalog.ts",
            "apps/web/src/command/argument-completion.ts",
            "apps/web/src/command/history.ts",
            "apps/web/src/command/draft-store.ts",
            "apps/web/src/command/queue.ts",
            "apps/web/src/command/coordinator.ts",
            "apps/web/src/command/execution-policy.ts",
            "apps/web/src/components/command-input/command-input.tsx",
            "apps/web/src/components/overlays/overlay-host.tsx",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "same_language_crop_and_adapt",
        "migration_strategy": "direct_port",
        "rationale": (
            "Only the mature prompt, queue, completion and focus mechanisms supplement "
            "the OpenCode shell. Claude session, query-engine and permission state do "
            "not become frontend owners."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_path": (
            "frontend/src/routes/{root-layout,conversation}.tsx;"
            "frontend/src/api/conversation-service/"
            "{conversation-service.api,v1-conversation-service.api}.ts"
        ),
        "capability_name": "workbench_lifecycle_request_states_and_recovery",
        "capability_summary": (
            "Task creation/detail lifecycle presentation, loading/empty/error/reconnect "
            "states, retry and bounded cancel/resume affordances."
        ),
        "targets": [
            "apps/web/src/shell/workbench-controller.ts",
            "apps/web/src/shell/route-loader.ts",
            "apps/web/src/shell/retry-supervisor.ts",
            "apps/web/src/components/status/request-state.tsx",
            "apps/web/src/components/tasks/task-detail.tsx",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "same_language_crop_and_adapt",
        "migration_strategy": "reimplemented_pattern",
        "rationale": (
            "OpenHands contributes bounded lifecycle and request-state behavior only. "
            "Its conversation service, Redux/query cache and runtime URL ownership are "
            "not retained."
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
    return {
        "ledger_id": ledger_id(decision),
        "source_repo": decision["source_repo"],
        "source_path": decision["source_path"],
        "capability_name": decision["capability_name"],
        "capability_summary": decision["capability_summary"],
        "migration_strategy": decision["migration_strategy"],
        "notes": (
            f"source_role={source_role}; source_commit={decision['source_commit']}; "
            f"{decision['rationale']}"
        ),
        "main_path_status": "tested_main_path",
        "lifecycle": "productized",
        "line_count_policy": "counts_as_runtime",
        "owner_unit": OWNER_UNIT,
        "milestone": "M2",
        "created_at": STAMP,
        "updated_at": STAMP,
        "dependencies": [],
        "downstream_units": ["M2-01B", "M2-02A", "M2-03A"],
        "blockers": [],
        "replacement_plan": (
            "Zyra-owned workbench shell is the browser entrypoint and delegates all "
            "canonical task, event, receipt and control mutation to existing typed API "
            "and M1 owners."
        ),
        "risk_notes": [
            "No runtime path may depend on a parent source repository.",
            "Frontend route, focus, overlay, draft and queue state cannot become canonical task state.",
            "Supplementary sources cannot become a second session or request owner.",
        ],
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "recorded",
            "license_hint": "Source mechanism recorded for Zyra-owned M2 shell internalization.",
            "notice_path": "third_party/NOTICE.md",
            "source_url": "",
            "notes": "No runtime dependency on the parent source repository.",
        },
        "source_evidence": [
            {
                "source_repo": decision["source_repo"],
                "source_path": decision["source_path"],
                "source_kind": "file",
                "exists_in_workspace": True,
                "symbols": [],
                "reason": f"{SLICE_ID} source-to-target decision",
                "tags": ["m2-01a", source_role],
            }
        ],
        "target_bindings": [
            {
                "target_path": path,
                "role": "primary" if index == 0 else "supporting",
                "required_for_main_path": True,
                "must_exist_for_statuses": ["tested_main_path"],
            }
            for index, path in enumerate(targets)
        ],
        "main_path": {
            "surfaces": ["web_workbench", "typed_api_client", "zyra_api"],
            "event_types": [],
            "api_routes": [
                "GET /health",
                "GET /runtime/readiness",
                "GET /tasks",
                "GET /tasks/{task_id}",
                "POST /tasks",
                "POST /tasks/{task_id}/run",
                "POST /tasks/{task_id}/cancel",
            ],
            "control_commands": ["cancel", "resume"],
            "artifact_kinds": [],
            "worker_runtime": (
                "WorkbenchApp -> WorkbenchController -> ZyraApiClient -> "
                "ZyraRequestHandler -> M1 canonical owners"
            ),
            "ui_panels": [
                "task_list",
                "task_detail",
                "command_input",
                "command_queue",
                "request_state",
            ],
        },
        "runtime_entry": {
            "module": "apps.web.src.main",
            "function": "WorkbenchApp",
            "protocol": "zyra-typed-api-v1",
            "health_check": TEST_COMMAND,
            "command": "bun run build:web",
            "config_refs": targets,
            "environment_refs": ["ZYRA_API_BASE_URL", "ZYRA_API_AUTH_TOKEN"],
        },
        "test_entries": [
            {
                "path": TEST_PATH,
                "command": TEST_COMMAND,
                "kind": "unit",
                "expected_signal": (
                    "route, lifecycle, command, queue, keyboard, overlay, focus, "
                    "responsive layout and large-list behaviors fail when disconnected"
                ),
                "required": True,
            },
            {
                "path": "tests/integration/test_m2_typed_api_client_transport.py",
                "command": (
                    "python -m pytest -q -p no:cacheprovider "
                    "tests/integration/test_m2_typed_api_client_transport.py"
                ),
                "kind": "integration",
                "expected_signal": (
                    "shell lifecycle commands traverse the single typed transport and "
                    "produce one canonical backend mutation"
                ),
                "required": True,
            },
        ],
        "tags": ["m2-01a", SLICE_ID.lower(), "workbench-shell", source_role],
        "metadata": {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "migration_mode": decision["migration_mode"],
            "canonical_task_owner": "SQLiteStore/TaskState",
            "canonical_event_owner": "EventLog",
            "canonical_artifact_owner": "LocalArtifactStore",
            "canonical_control_owner": "M1 control runtimes",
            "local_ui_state_owner": "WorkbenchController",
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
        raise ValueError("internalization ledger seed must be a list or contain entries")
    replacements = {
        item["ledger_id"]: item
        for item in (entry(decision) for decision in DECISIONS)
    }
    retained = [
        item
        for item in entries
        if str(item.get("owner_unit") or "") != OWNER_UNIT
        and str((item.get("metadata") or {}).get("slice_id") or "") != SLICE_ID
    ]
    output = [
        replacements.pop(str(item.get("ledger_id") or ""), item)
        for item in retained
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
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    write = args.write or not args.check
    aligned, count = synchronize(args.ledger.resolve(), write=write)
    print(f"m2_workbench_source_ledger_aligned={str(aligned).lower()}")
    print(f"m2_workbench_source_decision_count={count}")
    print(f"m2_workbench_owner_slice={SLICE_ID}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
