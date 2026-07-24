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
    "m2_browser_viewer_ledger_helpers",
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
OWNER_UNIT = "M2-S03B-02"
SLICE_ID = OWNER_UNIT
IMPLEMENTATION_COMMIT = "25b58af8aca0efd6650b2a391eab08f625117b77"
BASELINE_COMMIT = "7e9483c00cd422afc537b916ab234c9221b53bda"
STAMP = "2026-07-24T00:00:00.000Z"
WEB_TEST = "apps/web/test/browser-artifact-control-viewer.test.ts"
API_TEST = "tests/integration/test_browser_artifact_control_viewer.py"
MAIN_PATH_TEST = "tests/integration/test_browser_observability_main_path.py"
SESSION_TEST = "tests/integration/test_browser_session_productization_api.py"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "browser-use",
        "source_commit": "18484f23ac96bb955259a1c54530a7d265dfffdb",
        "source_language": "python",
        "target_language": "typescript",
        "source_path": (
            "browser_use/browser/views.py;"
            "browser_use/agent/views.py;"
            "browser_use/tools/views.py;"
            "browser_use/screenshots/service.py"
        ),
        "capability_name": "browser_observability_projection_and_artifact_lineage",
        "capability_summary": (
            "Browser state/action/result models, target and frame views, "
            "screenshot metadata, DOM/AX summaries, downloads and health "
            "signals projected over the existing BrowserWorker protocol."
        ),
        "targets": [
            "apps/web/src/features/browser/projection/reader.ts",
            "apps/web/src/features/browser/projection/targets.ts",
            "apps/web/src/features/browser/projection/dom.ts",
            "apps/web/src/features/browser/projection/steps.ts",
            "apps/web/src/features/browser/projection/artifacts.ts",
            "apps/web/src/features/browser/projection/health.ts",
            "apps/web/src/features/browser/projection/projector.ts",
            "apps/web/src/features/browser/contracts.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "bounded_cross_language_semantic_integration",
        "migration_strategy": "semantic_port",
        "rationale": (
            "The slice allocates a TypeScript/React viewer over an already "
            "internalized Python BrowserWorker. Only observable model and "
            "association semantics crossed language; execution loop, CDP, "
            "permission, screenshot writing and state ownership remain Python."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "frontend/src/components/features/browser/browser.tsx;"
            "frontend/src/components/features/browser/browser-snapshot.tsx"
        ),
        "capability_name": "browser_task_panel_and_snapshot_composition",
        "capability_summary": (
            "Task-bound browser panel, current URL/snapshot composition, "
            "empty/error states and accessible action-history navigation."
        ),
        "targets": [
            "apps/web/src/features/browser/view/browser-workbench.tsx",
            "apps/web/src/styles.css",
            "apps/web/src/components/tasks/task-detail.tsx",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": (
            "cropped_migration/same_language_component_integration"
        ),
        "migration_strategy": "selective_port",
        "rationale": (
            "Only panel composition supplemented the browser-use primary "
            "projection. OpenHands Redux, browser service and state ownership "
            "were not migrated."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/coding-agent/src/modes/rpc/rpc-types.ts;"
            "packages/coding-agent/src/modes/rpc/rpc-client.ts"
        ),
        "capability_name": "browser_control_correlation_and_receipt_settlement",
        "capability_summary": (
            "Typed request/response correlation, idempotent receipt handling, "
            "abort/timeout settlement and late-result fencing for browser "
            "viewer controls."
        ),
        "targets": [
            "apps/web/src/features/browser/control/policy.ts",
            "apps/web/src/features/browser/control/receipts.ts",
            "apps/web/src/features/browser/control/runtime.ts",
            "apps/web/src/features/browser/projection/causality.ts",
            "apps/web/src/features/browser/runtime.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "bounded_same_language_semantic_integration",
        "migration_strategy": "selective_port",
        "rationale": (
            "OMP RPC settlement semantics were cropped into a browser-specific "
            "typed control runtime. OMP transport breadth, process and session "
            "owners do not enter Zyra."
        ),
    },
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_language": "python",
        "target_language": "none",
        "source_path": "tui_gateway/server.py;tui_gateway/ws.py",
        "capability_name": "browser_control_auth_and_replay_conformance",
        "capability_summary": (
            "Authentication, request correlation and replay-negative behavior "
            "used only as conformance for typed viewer controls."
        ),
        "targets": [WEB_TEST, API_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "rationale": (
            "Hermes contributes negative conformance only. It owns no browser "
            "state, replay store, gateway, process or production code in Zyra."
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
            "downstream_units": ["M2-S03B-03", "M2-04A"],
            "replacement_plan": (
                "Replace behind BrowserViewerRuntime and the typed BrowserWorker "
                "control boundary without moving browser, workspace, artifact, "
                "permission, event or checkpoint custody."
                if production
                else f"{source_role} source; no production owner is selected."
            ),
            "risk_notes": [
                "No runtime path depends on a parent source repository.",
                (
                    "BrowserWorkerRuntime and BrowserRuntimeRegistry remain the "
                    "only browser session/action/target owners."
                ),
                (
                    "ArtifactWorkbenchRuntime and LocalArtifactStore retain "
                    "integrity, MIME, redaction, range, cache and download custody."
                ),
                (
                    "The viewer keeps only bounded selection, filter, virtual "
                    "window and inflight-request state; it has no replay store."
                ),
                (
                    "Sealed controls record one attempt, apply no mutation, enter "
                    "no approval wait and preserve human_intervention_count zero."
                ),
            ],
        }
    )
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "recorded",
        "license_hint": "M2 browser viewer source role recorded.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["source_evidence"][0]["tags"] = ["m2-03b", source_role]
    value["main_path"] = {
        "surfaces": [
            "web_workbench",
            "task_detail",
            "task_api",
            "browser_observability_api",
        ],
        "event_types": [
            "browser_session_lifecycle",
            "agent_message",
            "control_command",
            "artifact_published",
            "recovery_planned",
        ],
        "api_routes": [
            "/tasks/{task_id}/browser-observability",
            "/tasks/{task_id}/workers/browser",
            "/tasks/{task_id}/artifacts",
            "/tasks/{task_id}/artifacts/{artifact_id}/content",
        ],
        "control_commands": ["navigate", "stop", "retry", "inspect"],
        "artifact_kinds": [
            "browser screenshot",
            "browser download",
            "browser trace",
            "browser DOM snapshot",
        ],
        "worker_runtime": (
            "BrowserWorkerRuntime -> BrowserObservabilityApplication -> "
            "CanonicalProjectionStore/ArtifactWorkbenchRuntime -> "
            "BrowserViewerRuntime; controls return through BrowserWorkerRuntime"
            if production
            else f"{source_role}; behavior only"
        ),
        "ui_panels": [
            "browser_sessions",
            "target_frame_tree",
            "virtual_action_history",
            "verified_screenshot",
            "dom_ax_summary",
            "browser_controls",
        ],
    }
    value["runtime_entry"] = {
        "module": (
            "apps.web.src.features.browser.runtime"
            if production
            else "tests.browser_viewer_conformance"
        ),
        "function": (
            "BrowserViewerRuntime/BrowserWorkbench"
            if production
            else "browser viewer conformance tests"
        ),
        "protocol": (
            "zyra.browser-observability.api.v1/"
            "zyra.browser-viewer.control.v1"
        ),
        "health_check": (
            f"bun test ./{WEB_TEST}; "
            f"python -m pytest -q {API_TEST} {MAIN_PATH_TEST} {SESSION_TEST}"
        ),
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
                "strict admission, target/frame/action/DOM/artifact projection, "
                "virtualization, crash/reconnect and sealed control behavior"
            ),
            "required": True,
        },
        {
            "path": API_TEST,
            "command": f"python -m pytest -q {API_TEST}",
            "kind": "integration",
            "expected_signal": (
                "real HTTP sealed denial and BrowserWorker delegation with "
                "idempotent typed receipts"
            ),
            "required": True,
        },
        {
            "path": MAIN_PATH_TEST,
            "command": f"python -m pytest -q {MAIN_PATH_TEST}",
            "kind": "integration",
            "expected_signal": (
                "durable BrowserWorker observation, tool/action/artifact linkage "
                "and real Chrome process crash detection"
            ),
            "required": production,
        },
        {
            "path": SESSION_TEST,
            "command": f"python -m pytest -q {SESSION_TEST}",
            "kind": "integration",
            "expected_signal": (
                "productized CDP session, actions, artifact projection, diagnose "
                "and stop through one BrowserRuntimeRegistry"
            ),
            "required": production,
        },
    ]
    value["tags"] = [
        "m2-03b",
        SLICE_ID.lower(),
        "browser",
        "artifact",
        "control",
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
            "canonical_browser_owner": "python.BrowserWorkerRuntime",
            "canonical_session_owner": "python.BrowserRuntimeRegistry",
            "canonical_observability_owner": (
                "python.BrowserObservabilityApplication"
            ),
            "canonical_artifact_owner": (
                "python.LocalArtifactStore+TaskState"
            ),
            "canonical_permission_owner": "typescript.PermissionCoordinator",
            "canonical_workspace_owner": "python.WorkspaceManagerRuntime",
            "transient_view_owner": "typescript.BrowserViewerRuntime",
            "root_source_runtime_dependency": False,
            "second_browser_store": False,
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
    print(f"m2_browser_viewer_ledger_aligned={str(aligned).lower()}")
    print(f"m2_browser_viewer_source_decision_count={len(DECISIONS)}")
    print(f"m2_browser_viewer_ledger_entry_count={count}")
    print(f"m2_browser_viewer_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
