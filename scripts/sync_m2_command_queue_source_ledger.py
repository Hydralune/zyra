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
HELPER_SPEC = importlib.util.spec_from_file_location("m2_command_ledger_helpers", HELPER_PATH)
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
OWNER_UNIT = "M2-S04A-01"
SLICE_ID = OWNER_UNIT
BASELINE_COMMIT = "53b002bac1e97eab23b7553d344da068dd8dd3c9"
DECISION_COMMIT = "3264cc479dd30bb3b0829c7f1f803c06a5d1f87e"
IMPLEMENTATION_COMMIT = "6e365ea1e5a28619a052d5d0f8d6a5ccd880f976"
STAMP = "2026-07-25T00:00:00.000Z"
TS_TEST = "packages/commands/test/commands.test.ts"
API_TEST = "tests/integration/test_command_input_queue_control.py"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "claude-code-best",
        "source_commit": "c57f5a29e88e9a814bea47abeb9a0a6f725dc102",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "src/screens/REPL.tsx;"
            "src/components/PromptInput/PromptInput.tsx;"
            "src/components/PromptInput/PromptInputQueuedCommands.tsx;"
            "src/hooks/useCommandQueue.ts;"
            "src/hooks/useQueueProcessor.ts;"
            "src/utils/handlePromptSubmit.ts;"
            "src/utils/messageQueueManager.ts;"
            "src/utils/queueProcessor.ts;"
            "src/utils/sideQuestion.ts;"
            "src/utils/forkedAgent.ts;"
            "src/types/textInputTypes.ts;"
            "src/commands/btw/btw.tsx"
        ),
        "capability_name": "prompt_input_command_queue_and_side_question_runtime",
        "capability_summary": (
            "PromptInput parsing and guarded submission, busy-run queue admission, "
            "priority/FIFO projection, cancellation/retry lifecycle, reconnect "
            "restoration, local result overlays, command history and tool-disabled "
            "single-turn side questions adapted to Zyra canonical identities."
        ),
        "targets": [
            "packages/commands/src/coordinator.ts",
            "packages/commands/src/history.ts",
            "packages/commands/src/input-engine.ts",
            "packages/commands/src/palette.ts",
            "packages/commands/src/parser.ts",
            "packages/commands/src/policy.ts",
            "packages/commands/src/queue-actions.ts",
            "packages/commands/src/queue-recovery.ts",
            "packages/commands/src/queue.ts",
            "packages/commands/src/request.ts",
            "packages/commands/src/result-store.ts",
            "packages/commands/src/side-question.ts",
            "apps/web/src/features/commands/runtime.ts",
            "apps/web/src/features/commands/queue-panel.tsx",
            "apps/web/src/components/command-input/command-input.tsx",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "retained_control_flow_adapt/cropped_migration",
        "migration_strategy": "direct_port",
        "rationale": (
            "Claude's mature PromptInput, REPL queue and side-question control flow "
            "is retained in TypeScript, but task/session/query/tool ownership is "
            "replaced by Zyra RuntimeControlDispatcher, PromptQueueRuntime and "
            "CanonicalProjectionStore ports."
        ),
    },
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/app/src/context/command.tsx;"
            "packages/app/src/components/command-palette.tsx;"
            "packages/app/src/components/dialog-command-palette-v2.tsx;"
            "packages/app/src/components/prompt-input/slash-popover.tsx;"
            "packages/app/src/components/prompt-input/submit.tsx"
        ),
        "capability_name": "typed_command_registry_palette_and_receipt_projection",
        "capability_summary": (
            "Stable command/argument palette identities, incremental grouping and "
            "ranking, typed request/receipt settlement, duplicate suppression, "
            "result projection, local diagnostics and disabled-state behavior."
        ),
        "targets": [
            "packages/commands/src/diagnostics.ts",
            "packages/commands/src/identity.ts",
            "packages/commands/src/receipt-ledger.ts",
            "packages/commands/src/receipts.ts",
            "packages/commands/src/registry.ts",
            "packages/commands/src/result-model.ts",
            "apps/web/src/components/overlays/overlay-host.tsx",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration/same_language_component_integration",
        "migration_strategy": "direct_port",
        "rationale": (
            "OpenCode supplements palette and typed settlement mechanics only. "
            "Its stores, provider/session runtime and permission owner do not enter "
            "Zyra, so it cannot become a second command or queue owner."
        ),
    },
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_language": "python",
        "target_language": "none",
        "source_path": "tui_gateway/server.py;tui_gateway/ws.py",
        "capability_name": "command_reconnect_auth_and_replay_conformance",
        "capability_summary": (
            "Gateway reconnect, authentication and replay-negative behavior used "
            "only to challenge queue restoration and identity tests."
        ),
        "targets": [TS_TEST, API_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": "Hermes contributes tests only and owns no production gateway or queue.",
    },
    {
        "source_repo": "agentscope",
        "source_commit": "b6698c5dbaa1aa916925e27402767f45e2405fa4",
        "source_language": "python",
        "target_language": "none",
        "source_path": "src/agentscope/session;src/agentscope/runtime",
        "capability_name": "session_interaction_and_queue_wakeup_conformance",
        "capability_summary": (
            "Session interaction and lifecycle expectations used only for "
            "reconnect, isolation, cancel and disabled negative tests."
        ),
        "targets": [TS_TEST, API_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": "AgentScope contributes conformance only and no session owner.",
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_language": "typescript",
        "target_language": "none",
        "source_path": (
            "packages/coding-agent/src/modes/rpc/rpc-types.ts;"
            "packages/coding-agent/src/modes/rpc/rpc-client.ts"
        ),
        "capability_name": "typed_rpc_receipt_and_cancel_conformance",
        "capability_summary": (
            "Typed RPC receipt, correlation and cancellation expectations used "
            "only for request/receipt and disable behavior tests."
        ),
        "targets": [TS_TEST, API_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": "OMP contributes conformance only and no RPC/session owner.",
    },
)


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    value = HELPER.entry(decision)
    source_role = str(decision["source_role"])
    production = source_role in {"primary_implementation", "supplementary_implementation"}
    value.update(
        {
            "owner_unit": OWNER_UNIT,
            "created_at": STAMP,
            "updated_at": STAMP,
            "downstream_units": ["M2-S04A-02", "M2-04A"],
            "replacement_plan": (
                "Replace behind CommandCoordinator, CommandQueueRecovery and "
                "CommandSurfaceRuntime ports without moving canonical task, queue, "
                "permission, event or projection custody."
                if production
                else f"{source_role} source; no production owner is selected."
            ),
            "risk_notes": [
                "No runtime path depends on a parent source repository.",
                "RuntimeControlDispatcher and PromptQueueRuntime remain canonical command/queue owners.",
                "CanonicalProjectionStore remains the committed browser projection owner.",
                "PromptInput retains only draft, palette, history and local overlay state.",
                "Malformed, disabled and unavailable controls fail without ordinary-chat fallback.",
                "OpenClaw remains excluded_forward_only.",
            ],
        }
    )
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "recorded",
        "license_hint": "M2 command input/queue source role recorded.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["source_evidence"][0]["tags"] = ["m2-04a", source_role]
    value["main_path"] = {
        "surfaces": ["web_workbench", "prompt_input", "command_result_overlay"],
        "event_types": [
            "command requested/validated/queued/running/applied/rejected/cancelled",
            "state mutation/checkpoint/artifact/tool/failure/recovery correlation",
        ],
        "api_routes": [
            "/tasks/{task_id}/commands",
            "/tasks/{task_id}/command-queue",
            "/tasks/{task_id}/commands/{request_id}/cancel",
            "/tasks/{task_id}/events",
        ],
        "control_commands": [
            "/status",
            "/graph",
            "/trace",
            "/artifacts",
            "/permissions",
            "/btw",
            "/inject",
            "/change",
            "/verify",
            "/eval",
            "/doctor",
        ],
        "artifact_kinds": ["command_result", "command_causal_trace"],
        "worker_runtime": (
            "PromptInput -> CommandSurfaceRuntime -> TaskApi -> "
            "RuntimeControlDispatcher/PromptQueueRuntime -> canonical events -> "
            "CanonicalProjectionStore -> CommandProjectionIndex/queue/result overlay"
            if production
            else f"{source_role}; behavior only"
        ),
        "ui_panels": ["command_palette", "command_queue", "command_result_overlay"],
    }
    value["runtime_entry"] = {
        "module": (
            "packages.commands.src.coordinator"
            if production
            else "tests.command_queue_conformance"
        ),
        "function": (
            "CommandCoordinator/CommandSurfaceRuntime"
            if production
            else "command queue conformance tests"
        ),
        "protocol": "zyra.control/v1+zyra.command-queue/v1",
        "health_check": (
            f"bun test ./{TS_TEST}; python -m pytest -q {API_TEST}"
        ),
        "command": "bun run build:web" if production else "",
        "config_refs": list(decision["targets"]),
        "environment_refs": [],
    }
    value["test_entries"] = [
        {
            "path": TS_TEST,
            "command": f"bun test ./{TS_TEST}",
            "kind": "unit",
            "expected_signal": (
                "registry/parser/palette/policy/receipt/queue/reconnect/cancel/retry/"
                "projection/result/history/input/BTW/disable behavior"
            ),
            "required": True,
        },
        {
            "path": API_TEST,
            "command": f"python -m pytest -q {API_TEST}",
            "kind": "integration",
            "expected_signal": (
                "real HTTP ControlCommand, busy queue, restore, cancel, retry link, "
                "event identity, disabled fail-closed and BTW isolation"
            ),
            "required": True,
        },
    ]
    value["tags"] = ["m2-04a", SLICE_ID.lower(), "command-input", "command-queue", source_role]
    value["metadata"].update(
        {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "canonical_command_owner": "python.RuntimeControlDispatcher+ControlRequestStore",
            "canonical_queue_owner": "python.PromptQueueRuntime",
            "canonical_projection_owner": "typescript.CanonicalProjectionStore",
            "transient_input_owner": "typescript.CommandInputEngine+CommandPalette",
            "transient_overlay_owner": "typescript.CommandResultStore",
            "root_source_runtime_dependency": False,
            "second_command_store": False,
            "second_queue_store": False,
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
    print(f"m2_command_queue_ledger_aligned={str(aligned).lower()}")
    print(f"m2_command_queue_source_decision_count={len(DECISIONS)}")
    print(f"m2_command_queue_ledger_entry_count={count}")
    print(f"m2_command_queue_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
