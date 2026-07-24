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


HELPER_PATH = ROOT / "scripts" / "sync_m2_artifact_viewer_source_ledger.py"
HELPER_SPEC = importlib.util.spec_from_file_location(
    "m2_terminal_ledger_helpers",
    HELPER_PATH,
)
if HELPER_SPEC is None or HELPER_SPEC.loader is None:
    raise RuntimeError("M2 artifact ledger helper could not be loaded")
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
OWNER_UNIT = "M2-S03B-01"
SLICE_ID = OWNER_UNIT
IMPLEMENTATION_COMMIT = "1bb50e7cf3f5f784ba11a4d107f1ec7730b7b9d8"
BASELINE_COMMIT = "f54446ec7bcbec16f189c8f50d957bd67e76e94b"
STAMP = "2026-07-24T00:00:00.000Z"
WEB_TEST = "apps/web/test/terminal-pty-viewer.test.ts"
PTY_TEST = "tests/integration/test_terminal_pty_platform_integration.py"
WEBSOCKET_TEST = "tests/integration/test_terminal_websocket_integration.py"
UNIT_TEST = "tests/unit/test_terminal_pty_runtime.py"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/app/src/context/terminal.tsx;"
            "packages/app/src/components/terminal.tsx;"
            "packages/app/src/pages/session/{terminal-panel.tsx,"
            "terminal-panel-v2.tsx};"
            "packages/app/src/utils/{terminal-writer.ts,"
            "terminal-websocket-url.ts}"
        ),
        "capability_name": "terminal_tab_session_reconnect_runtime",
        "capability_summary": (
            "Workspace-scoped terminal tabs, one-ticket reconnect, cursor "
            "resume, resize coalescing and durable viewer-reference behavior."
        ),
        "targets": [
            "apps/web/src/features/terminal/contracts.ts",
            "apps/web/src/features/terminal/runtime.ts",
            "apps/web/src/features/terminal/reconnect.ts",
            "apps/web/src/features/terminal/resize.ts",
            "apps/web/src/features/terminal/tabs.ts",
            "apps/web/src/features/terminal/diagnostics.ts",
            "apps/web/src/features/terminal/protocol-audit.ts",
            "apps/web/src/features/terminal/transcript.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": (
            "cropped_migration/same_language_component_integration"
        ),
        "migration_strategy": "direct_port",
        "rationale": (
            "OpenCode terminal session/view mechanics were split into "
            "Zyra-owned browser contracts, reconnect, tabs and runtime. "
            "OpenCode server/session stores and PTY processes are not used."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "frontend/src/components/features/terminal/terminal.tsx;"
            "frontend/src/hooks/use-terminal.ts;"
            "frontend/src/services/terminal-service.ts;"
            "frontend/src/utils/parse-terminal-output.ts"
        ),
        "capability_name": "terminal_panel_lifecycle_and_safe_projection",
        "capability_summary": (
            "Dedicated terminal panel lifecycle, inactive/completed states, "
            "safe rendering and structured read-only output projection."
        ),
        "targets": [
            "apps/web/src/features/terminal/render.ts",
            "apps/web/src/features/terminal/structured.ts",
            (
                "apps/web/src/features/terminal/view/"
                "terminal-workbench.tsx"
            ),
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": (
            "cropped_migration/same_language_component_integration"
        ),
        "migration_strategy": "selective_port",
        "rationale": (
            "Only panel and output-view behavior supplements the OpenCode "
            "primary chain. OpenHands RPC, Redux and terminal backend "
            "ownership remain excluded."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/coding-agent/src/tools/bash-interactive.ts"
        ),
        "capability_name": "terminal_input_writer_screen_and_backpressure",
        "capability_summary": (
            "PTY input normalization, ordered writer queue, bounded output "
            "screen, explicit lifecycle and backpressure semantics."
        ),
        "targets": [
            "apps/web/src/features/terminal/input.ts",
            "apps/web/src/features/terminal/writer.ts",
            "apps/web/src/features/terminal/backpressure.ts",
            "apps/web/src/features/terminal/screen.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "bounded_same_language_semantic_integration",
        "migration_strategy": "selective_port",
        "rationale": (
            "OMP TypeScript interaction semantics were integrated into "
            "bounded browser modules. OMP does not own a Zyra process, "
            "session, permission or output journal."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_language": "rust",
        "target_language": "none",
        "source_path": "crates/pi-natives/src/pty.rs",
        "capability_name": "native_pty_lifecycle_conformance",
        "capability_summary": (
            "Native PTY input, resize, exit and process-tree termination "
            "semantics used only as platform conformance."
        ),
        "targets": [PTY_TEST, UNIT_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "No Rust code, native bundle or OMP process was migrated. Zyra's "
            "standard-library POSIX PTY and ctypes ConPTY driver is the only "
            "native process owner."
        ),
    },
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_language": "python",
        "target_language": "none",
        "source_path": (
            "tui_gateway/server.py;tui_gateway/ws.py;"
            "hermes_cli/web_server.py"
        ),
        "capability_name": "terminal_ticket_auth_reconnect_conformance",
        "capability_summary": (
            "Origin-bound authentication, reconnect and replay-negative "
            "behavior used only for protocol conformance."
        ),
        "targets": [WEBSOCKET_TEST, UNIT_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "Hermes contributes authentication/reconnect negative cases only. "
            "No Hermes gateway, process, token store or WebSocket owner is "
            "loaded by Zyra."
        ),
    },
)


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    value = HELPER.PRIOR.entry(decision)
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
            "downstream_units": ["M2-S03B-02", "M2-S03B-03", "M2-04A"],
            "replacement_plan": (
                "Replace behind TerminalRuntime and TerminalSessionRegistry "
                "without moving process, workspace, permission, event or "
                "artifact custody."
                if production
                else f"{source_role} source; no production owner is selected."
            ),
            "risk_notes": [
                "No runtime path depends on a parent source repository.",
                (
                    "TerminalSessionRegistry, TerminalOutputJournal and the "
                    "platform driver are the only PTY/session/replay owners."
                ),
                (
                    "PermissionCoordinator remains the sole create/input/kill "
                    "decision and exact-permit owner."
                ),
                (
                    "Browser tabs retain view references only and closing a "
                    "tab never terminates the PTY."
                ),
            ],
        }
    )
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "recorded",
        "license_hint": "M2 terminal source role recorded.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["source_evidence"][0]["tags"] = ["m2-03b", source_role]
    value["main_path"] = {
        "surfaces": ["web_workbench", "task_detail", "task_api", "websocket"],
        "event_types": [
            "terminal_session_lifecycle",
            "terminal_output",
            "terminal_control",
        ],
        "api_routes": [
            "/tasks/{task_id}/terminals",
            "/tasks/{task_id}/terminals/{terminal_id}",
            "/tasks/{task_id}/terminals/{terminal_id}/ticket",
            "/tasks/{task_id}/terminals/{terminal_id}/connect",
            "/tasks/{task_id}/terminals/{terminal_id}/kill",
        ],
        "control_commands": ["terminal input", "terminal resize", "terminal kill"],
        "artifact_kinds": ["trace terminal spill"],
        "worker_runtime": (
            "WorkspaceManagerRuntime -> PermissionCoordinator -> "
            "TerminalSessionRegistry -> platform PTY -> output journal -> "
            "ticketed WebSocket -> typed TaskApi -> TerminalRuntime"
            if production
            else f"{source_role}; behavior only"
        ),
        "ui_panels": [
            "terminal_tabs",
            "terminal_screen",
            "terminal_structured_output",
            "terminal_diagnostics",
        ],
    }
    value["runtime_entry"] = {
        "module": (
            "apps.web.src.features.terminal.runtime"
            if production
            else "tests.terminal_conformance"
        ),
        "function": (
            "TerminalRuntime/TerminalSessionRegistry"
            if production
            else "terminal conformance tests"
        ),
        "protocol": "zyra.terminal.v1",
        "health_check": (
            f"bun test ./{WEB_TEST}; "
            f"python -m pytest -q {UNIT_TEST} {PTY_TEST} {WEBSOCKET_TEST}"
        ),
        "command": "bun run build:web" if production else "",
        "config_refs": list(decision["targets"]),
        "environment_refs": (
            [
                "ZYRA_TERMINAL_STATE",
                "ZYRA_TERMINAL_DISABLED",
                "ZYRA_TERMINAL_TICKET_TTL",
                "ZYRA_TERMINAL_MAX_SESSIONS",
            ]
            if production
            else []
        ),
    }
    value["test_entries"] = [
        {
            "path": WEB_TEST,
            "command": f"bun test ./{WEB_TEST}",
            "kind": "unit",
            "expected_signal": (
                "strict protocol, ANSI screen, input, replay, reconnect, tabs, "
                "selection, diagnostics and explicit-kill-only behavior"
            ),
            "required": production,
        },
        {
            "path": UNIT_TEST,
            "command": f"python -m pytest -q {UNIT_TEST}",
            "kind": "unit",
            "expected_signal": (
                "permission, sealed denial, tickets, redaction, spill, rate "
                "budget, state recovery and owner-disable behavior"
            ),
            "required": True,
        },
        {
            "path": PTY_TEST,
            "command": f"python -m pytest -q {PTY_TEST}",
            "kind": "integration",
            "expected_signal": (
                "real temporary workspace PTY create/input/resize/exit/tree "
                "kill and fresh API state"
            ),
            "required": True,
        },
        {
            "path": WEBSOCKET_TEST,
            "command": f"python -m pytest -q {WEBSOCKET_TEST}",
            "kind": "integration",
            "expected_signal": (
                "real upgrade, origin, one-use ticket, replay, resync, input, "
                "resize and detach-without-kill"
            ),
            "required": True,
        },
    ]
    value["tags"] = [
        "m2-03b",
        SLICE_ID.lower(),
        "terminal",
        "pty",
        source_role,
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
            "canonical_terminal_owner": (
                "python.TerminalSessionRegistry"
            ),
            "canonical_process_owner": "python.zyra-platform-pty",
            "canonical_replay_owner": "python.TerminalOutputJournal",
            "canonical_permission_owner": "typescript.PermissionCoordinator",
            "canonical_workspace_owner": "python.WorkspaceManagerRuntime",
            "transient_view_owner": "typescript.TerminalRuntime",
            "root_source_runtime_dependency": False,
            "external_pty_package": False,
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
            or str((item.get("metadata") or {}).get("slice_id") or "")
            == SLICE_ID
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
    print(f"m2_terminal_ledger_aligned={str(aligned).lower()}")
    print(f"m2_terminal_source_decision_count={len(DECISIONS)}")
    print(f"m2_terminal_ledger_entry_count={count}")
    print(f"m2_terminal_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
