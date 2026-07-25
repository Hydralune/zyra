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
    "m2_permission_ledger_helpers",
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
OWNER_UNIT = "M2-S04A-02"
SLICE_ID = OWNER_UNIT
BASELINE_COMMIT = "33327d49ef1fab62d314709e169eebc781eb2a9c"
DECISION_COMMIT = "1ef817c2f7f26d2751b6760b15a093d86dee755f"
IMPLEMENTATION_COMMIT = "cc09f054ad491877eeed5951574a518d57e157a0"
STAMP = "2026-07-25T00:00:00.000Z"
WEB_TEST = "apps/web/test/permission-sealed-control-integration.test.ts"
RUNTIME_TEST = (
    "packages/runtime/claude-runtime/test/e02/permission-console-proof.test.ts"
)
API_TEST = "tests/integration/test_permission_console_api.py"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "claude-code-best",
        "source_commit": "c57f5a29e88e9a814bea47abeb9a0a6f725dc102",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "src/components/permissions/PermissionPrompt.tsx;"
            "src/components/permissions/PermissionRequest.tsx;"
            "src/components/permissions/PermissionDialog.tsx;"
            "src/components/permissions/FallbackPermissionRequest.tsx;"
            "src/components/permissions/PermissionRequestTitle.tsx;"
            "src/components/permissions/ComputerUseApproval/ComputerUseApproval.tsx;"
            "src/components/permissions/WorkerPendingPermission.tsx;"
            "src/hooks/toolPermission/PermissionContext.ts;"
            "src/hooks/toolPermission/handlers/interactiveHandler.ts;"
            "src/bridge/bridgePermissionCallbacks.ts"
        ),
        "capability_name": "permission_request_response_and_exact_resume_console",
        "capability_summary": (
            "Identity-bound permission request/detail presentation, one-shot response "
            "claiming, active-request race handling, tool-specific warning projection "
            "and exact backend callback/resume proof adapted to Zyra custody."
        ),
        "targets": [
            "packages/runtime/claude-runtime/src/permission/response-proof.ts",
            "packages/runtime/claude-runtime/src/e02/api-port-runtime.ts",
            "apps/web/src/features/permissions/canonical.ts",
            "apps/web/src/features/permissions/projection.ts",
            "apps/web/src/features/permissions/redaction.ts",
            "apps/web/src/features/permissions/response-proof.ts",
            "apps/web/src/features/permissions/response-race.ts",
            "apps/web/src/features/permissions/runtime.ts",
            "apps/web/src/features/permissions/store.ts",
            "apps/web/src/features/permissions/view-model.ts",
            "apps/web/src/features/permissions/warning-policy.ts",
            "apps/web/src/features/permissions/view/permission-detail.tsx",
            "apps/web/src/features/permissions/view/permission-workbench.tsx",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "retained_control_flow_adapt/cropped_migration",
        "migration_strategy": "direct_port",
        "rationale": (
            "Claude permission interaction and callback control flow remains in "
            "TypeScript but process-local queue/session ownership is replaced with "
            "Zyra canonical permission, custody, event and exact-permit identities."
        ),
    },
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/app/src/context/permission.tsx;"
            "packages/app/src/context/permission-auto-respond.ts;"
            "packages/app/src/pages/session/composer/session-permission-dock.tsx;"
            "packages/app/src/components/dialog-command-palette-v2.tsx"
        ),
        "capability_name": "permission_dock_session_refresh_and_reconnect_projection",
        "capability_summary": (
            "Bounded dock/list interaction, responding-state exclusion, reconnect "
            "refresh, stable session scoping and accessible action ordering without "
            "migrating auto-accept or a browser permission store."
        ),
        "targets": [
            "apps/web/src/features/permissions/event-reconciler.ts",
            "apps/web/src/features/permissions/reconnect-supervisor.ts",
            "apps/web/src/features/permissions/session-roster.ts",
            "apps/web/src/features/permissions/view/permission-queue.tsx",
            "apps/web/src/features/permissions/view/permission-timeline.tsx",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_migration/same_language_component_integration",
        "migration_strategy": "direct_port",
        "rationale": (
            "OpenCode supplements dock/reconnect/session interaction only; its "
            "permission policy, auto-response, store, SDK and session owner do not "
            "enter Zyra."
        ),
    },
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_language": "python",
        "target_language": "none",
        "source_path": "tools/approval.py;tools/write_approval.py;gateway/session.py",
        "capability_name": "approval_timeout_interrupt_redaction_conformance",
        "capability_summary": (
            "Timeout and interrupt release, secret-redacted presentation and "
            "concurrent response behavior used only for adversarial conformance."
        ),
        "targets": [WEB_TEST, RUNTIME_TEST, API_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": "Hermes contributes tests only and no blocking queue or gateway owner.",
    },
    {
        "source_repo": "agentscope",
        "source_commit": "b6698c5dbaa1aa916925e27402767f45e2405fa4",
        "source_language": "python",
        "target_language": "none",
        "source_path": (
            "src/agentscope/permission/_types.py;"
            "src/agentscope/permission/_context.py;"
            "src/agentscope/permission/_decision.py;"
            "src/agentscope/permission/_rule.py;"
            "src/agentscope/permission/_engine.py"
        ),
        "capability_name": "permission_mode_rule_and_hitl_projection_conformance",
        "capability_summary": (
            "Mode/rule ordering and HITL event projection used only to challenge "
            "sealed, expiry and canonical-event behavior."
        ),
        "targets": [WEB_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": "AgentScope contributes conformance and no second permission engine.",
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_language": "typescript",
        "target_language": "none",
        "source_path": (
            "packages/coding-agent/src/core/agent-session.ts;"
            "packages/coding-agent/src/modes/rpc/rpc-types.ts;"
            "packages/coding-agent/src/modes/rpc/rpc-client.ts"
        ),
        "capability_name": "execute_boundary_approval_reference",
        "capability_summary": (
            "Execute-boundary approval and typed RPC control used only as a negative "
            "reference against sealed authority expansion."
        ),
        "targets": [WEB_TEST, RUNTIME_TEST],
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "rationale": "OMP is a rejection/reference boundary and owns no production path.",
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_language": "typescript",
        "target_language": "none",
        "source_path": (
            "frontend/src/components/features/conversation;"
            "frontend/src/state"
        ),
        "capability_name": "pending_action_confirmation_ui_reference",
        "capability_summary": (
            "Pending-action and confirmation composition used only to review visual "
            "separation; no OpenHands state or runtime is migrated."
        ),
        "targets": [
            "apps/web/src/features/permissions/view/permission-workbench.tsx",
            WEB_TEST,
        ],
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "rationale": "OpenHands contributes UI reference only and no canonical state.",
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
                "Replace behind PermissionConsoleRuntime, response-proof and canonical "
                "event ports without moving permission decision/session/permit custody."
                if production
                else f"{role} source; no production owner is selected."
            ),
            "risk_notes": [
                "PermissionCoordinator remains canonical decision/resume/permit owner.",
                "PermissionStateStore remains durable session-custody owner.",
                "The browser owns no durable pending request, rule or permit store.",
                "Sealed controls reject and record intervention without human wait.",
                "No runtime path depends on a parent source repository.",
                "OpenClaw remains excluded_forward_only.",
            ],
        }
    )
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "recorded",
        "license_hint": "M2 permission console source role recorded.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["source_evidence"][0]["tags"] = ["m2-04a", role]
    value["main_path"] = {
        "surfaces": [
            "task_permission_workbench",
            "timeline_permission_projection",
            "browser_terminal_command_permission_status",
        ],
        "event_types": [
            "permission requested/resolved/expired/denied",
            "permission permit issued/claimed/consumed/revoked",
            "sealed intervention rejected/recovery requested",
        ],
        "api_routes": [
            "/api/e02/permissions/session/open",
            "/api/e02/permissions/session/resume",
            "/api/e02/permissions/summary",
            "/api/e02/permissions/requests",
            "/api/e02/permissions/requests/{request_id}/resolve",
            "/tasks/{task_id}/steer",
            "/tasks/{task_id}/retry",
        ],
        "control_commands": ["/permissions", "/steer", "/retry"],
        "artifact_kinds": ["permission_receipt", "permission_causal_trace"],
        "worker_runtime": (
            "PermissionConsoleRuntime -> typed custody transport -> E02 API port -> "
            "PermissionCoordinator -> canonical receipt/event -> permission selectors"
            if production
            else f"{role}; behavior/reference only"
        ),
        "ui_panels": [
            "permission_queue",
            "permission_detail",
            "permission_timeline",
            "permission_policy_status",
        ],
    }
    value["runtime_entry"] = {
        "module": (
            "apps.web.src.features.permissions.runtime"
            if production
            else "tests.permission_console_conformance"
        ),
        "function": (
            "PermissionConsoleRuntime"
            if production
            else "permission console conformance tests"
        ),
        "protocol": "zyra.permission-console/v2",
        "health_check": (
            f"bun test ./{WEB_TEST} ./{RUNTIME_TEST}; "
            f"python -m pytest -q {API_TEST}"
        ),
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
                "redaction/warnings, exact response, sealed no-human, event/permit/"
                "receipt, expiry, reconnect, custody and disable behavior"
            ),
            "required": True,
        },
        {
            "path": RUNTIME_TEST,
            "command": f"bun test ./{RUNTIME_TEST}",
            "kind": "integration",
            "expected_signal": "exact console proof resumes once; stale proof fails",
            "required": True,
        },
        {
            "path": API_TEST,
            "command": f"python -m pytest -q {API_TEST}",
            "kind": "integration",
            "expected_signal": "real Python HTTP to TypeScript proof forwarding",
            "required": True,
        },
    ]
    value["tags"] = [
        "m2-04a",
        SLICE_ID.lower(),
        "permission-console",
        "sealed-control",
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
            "canonical_decision_owner": "typescript.PermissionCoordinator",
            "canonical_session_custody_owner": (
                "python.PermissionStateStore+PermissionControlPlane"
            ),
            "canonical_permit_owner": "typescript.PermissionContinuationRuntime",
            "browser_pending_owner": False,
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
    print(f"m2_permission_console_ledger_aligned={str(aligned).lower()}")
    print(f"m2_permission_console_source_decision_count={len(DECISIONS)}")
    print(f"m2_permission_console_ledger_entry_count={count}")
    print(f"m2_permission_console_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
