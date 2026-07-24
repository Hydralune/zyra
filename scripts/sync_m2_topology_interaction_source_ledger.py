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
OWNER_UNIT = "M2-S02A-02"
SLICE_ID = OWNER_UNIT
IMPLEMENTATION_COMMIT = "6b15c5d8924a113553eac4d21922bc28239696dc"
BASELINE_COMMIT = "a2fa483a75ccae4c14bf9cd94aa0cda41346c28e"
STAMP = "2026-07-24T00:00:00.000Z"
WEB_TEST = "apps/web/test/topology-interaction.test.ts"
API_TEST = "tests/integration/test_topology_interaction_control.py"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "zyra",
        "source_commit": BASELINE_COMMIT,
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "apps/web/src/features/topology/projection/**;"
            "apps/web/src/state/{store,selectors,panel-selectors}.ts;"
            "packages/core/typed-api-client/src/**"
        ),
        "capability_name": "large_graph_topology_interaction_and_control_runtime",
        "capability_summary": (
            "Stable incremental layout, spatial virtualization, multilevel LOD "
            "clustering, search/filter layers, accessible navigation and evidence "
            "inspection over the immutable topology projection, plus typed control "
            "receipts, sealed denial and exact checkpoint rewind/resume."
        ),
        "targets": [
            "apps/web/src/features/topology/view/model.ts",
            "apps/web/src/features/topology/view/geometry.ts",
            "apps/web/src/features/topology/view/layout.ts",
            "apps/web/src/features/topology/view/spatial-index.ts",
            "apps/web/src/features/topology/view/clustering.ts",
            "apps/web/src/features/topology/view/filtering.ts",
            "apps/web/src/features/topology/view/layers.ts",
            "apps/web/src/features/topology/view/navigation.ts",
            "apps/web/src/features/topology/view/accessibility.ts",
            "apps/web/src/features/topology/view/interaction.ts",
            "apps/web/src/features/topology/view/controller.ts",
            "apps/web/src/features/topology/view/graph-canvas.tsx",
            "apps/web/src/features/topology/view/minimap.tsx",
            "apps/web/src/features/topology/view/toolbar.tsx",
            "apps/web/src/features/topology/view/inspector.tsx",
            "apps/web/src/features/topology/view/topology-workbench.tsx",
            "apps/web/src/components/tasks/task-detail.tsx",
            "apps/web/src/api/task-api.ts",
            "packages/core/typed-api-client/src/constants.ts",
            "packages/core/typed-api-client/src/protocol.ts",
            "packages/core/typed-api-client/src/normalizers.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_component_integration",
        "migration_strategy": "direct_port",
        "main_path_status": "tested_main_path",
        "lifecycle": "productized",
        "line_count_policy": "counts_as_runtime",
        "rationale": (
            "The M2-S02A-01 immutable projection is retained as the only graph-fact "
            "input. New layout, viewport, filter, selection and focus state is "
            "transient and cannot write the CanonicalProjectionStore or backend. The "
            "existing typed client, dispatcher, permission runtime, session runtime "
            "and RecoveryApplication remain canonical; bounded glue preserves "
            "checkpoint signatures and exposes immutable receipts."
        ),
    },
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_language": "typescript",
        "target_language": "none",
        "source_path": (
            "packages/app/src/pages/session/**;"
            "packages/tui/src/routes/session/index.tsx"
        ),
        "capability_name": "dense_session_graph_interaction_reference",
        "capability_summary": (
            "Dense session inspection, keyboard traversal and durable control-status "
            "presentation conformance."
        ),
        "targets": [WEB_TEST],
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "main_path_status": "inventoried",
        "lifecycle": "candidate",
        "line_count_policy": "excluded_inventory_only",
        "rationale": (
            "OpenCode remains an interaction reference. No Solid store, session "
            "runtime, command owner, terminal or provider client is migrated."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_language": "typescript",
        "target_language": "none",
        "source_path": (
            "frontend/src/routes/planner-tab.tsx;"
            "frontend/src/routes/task-list-tab.tsx"
        ),
        "capability_name": "task_topology_panel_navigation_reference",
        "capability_summary": (
            "Task hierarchy, detail navigation, timeline jumps and loading/error "
            "operator-state presentation conformance."
        ),
        "targets": [WEB_TEST],
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "main_path_status": "inventoried",
        "lifecycle": "candidate",
        "line_count_policy": "excluded_inventory_only",
        "rationale": (
            "OpenHands remains a panel behavior reference. No router, Redux state, "
            "conversation owner, sandbox service or event client is migrated."
        ),
    },
    {
        "source_repo": "langgraph",
        "source_commit": "5931a5f0b313feff24e2516a586c55601b868ac1",
        "source_language": "python",
        "target_language": "none",
        "source_path": (
            "libs/checkpoint/langgraph/checkpoint/base/__init__.py;"
            "libs/langgraph/langgraph/pregel/_loop.py"
        ),
        "capability_name": "interactive_checkpoint_exact_resume_conformance",
        "capability_summary": (
            "Checkpoint identity and lineage, pending/committed writes, interrupt/"
            "resume correlation, stable task identity and exact-resume conformance."
        ),
        "targets": [WEB_TEST, API_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "main_path_status": "inventoried",
        "lifecycle": "candidate",
        "line_count_policy": "excluded_inventory_only",
        "rationale": (
            "LangGraph remains limited to the narrow checkpoint contract. StateGraph, "
            "channels, reducers, Pregel, stream, Store, ToolNode, SDK, server and "
            "deployment code own no production path."
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
    production = source_role == "primary_implementation"
    return {
        "ledger_id": ledger_id(decision),
        "source_repo": decision["source_repo"],
        "source_path": decision["source_path"],
        "capability_name": decision["capability_name"],
        "capability_summary": decision["capability_summary"],
        "migration_strategy": decision["migration_strategy"],
        "notes": (
            f"source_role={source_role}; source_commit={decision['source_commit']}; "
            f"migration_mode={decision['migration_mode']}; {decision['rationale']}"
        ),
        "main_path_status": decision["main_path_status"],
        "lifecycle": decision["lifecycle"],
        "line_count_policy": decision["line_count_policy"],
        "owner_unit": OWNER_UNIT,
        "milestone": "M2",
        "created_at": STAMP,
        "updated_at": STAMP,
        "dependencies": [],
        "downstream_units": ["M2-S02B-01", "M2-03A", "M2-04A"],
        "blockers": [],
        "replacement_plan": (
            "Replace behind TopologyProjectionView or typed control transport without "
            "moving canonical graph, permission, session or recovery ownership."
            if production
            else f"{source_role} source; no production runtime is selected."
        ),
        "risk_notes": [
            "No runtime path may depend on a parent source repository.",
            "Canonical graph, scheduler, permission, session and recovery stores remain authoritative.",
            "CanonicalProjectionStore remains the sole browser projection fact owner.",
            (
                "Transient interaction state cannot commit canonical graph or checkpoint facts."
                if production
                else "The reference/conformance source cannot become a second state owner."
            ),
        ],
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "not_required" if decision["source_repo"] == "zyra" else "recorded",
            "license_hint": "M2 topology interaction source role recorded.",
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
                "tags": ["m2-02a", source_role],
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
            "surfaces": ["web_workbench", "task_detail", "typed_control_api"],
            "event_types": [
                "topology.*",
                "scheduler.*",
                "recovery.*",
                "requirement_change",
                "control_command",
            ],
            "api_routes": (
                ["POST /tasks/{task_id}/commands"] if production else []
            ),
            "control_commands": (
                ["/change", "/rewind", "/resume"] if production else []
            ),
            "artifact_kinds": ["topology_projection", "control_receipt"],
            "worker_runtime": (
                "CanonicalProjectionStore -> TopologyWorkbenchController -> "
                "typed command -> permission/session/recovery owner"
                if production
                else f"{source_role}; behavior only"
            ),
            "ui_panels": ["topology", "topology_inspector", "topology_minimap"],
        },
        "runtime_entry": {
            "module": (
                "apps.web.src.features.topology.view.controller"
                if production
                else "apps.web.test.topology-interaction"
            ),
            "function": (
                "TopologyWorkbenchController"
                if production
                else "topology interaction conformance tests"
            ),
            "protocol": "zyra.topology-control/v1",
            "health_check": f"bun test ./{WEB_TEST}",
            "command": "bun run --cwd apps/web build" if production else "",
            "config_refs": targets,
            "environment_refs": [],
        },
        "test_entries": [
            {
                "path": WEB_TEST,
                "command": f"bun test ./{WEB_TEST}",
                "kind": "unit",
                "expected_signal": (
                    "2400-node virtualization, LOD, stable layout, filters, layers, "
                    "accessibility, control receipts, stale fencing and disable failure"
                ),
                "required": True,
            },
            {
                "path": API_TEST,
                "command": f"python -m pytest -q {API_TEST}",
                "kind": "integration",
                "expected_signal": (
                    "real change, rewind, resume, sealed denial and intervention ledger"
                ),
                "required": production or source_role == "conformance_only",
            },
        ],
        "tags": ["m2-02a", SLICE_ID.lower(), "topology-interaction", source_role],
        "metadata": {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "canonical_graph_owner": "python.GraphStateCustody",
            "canonical_permission_owner": "python.RuntimeControlDispatcher",
            "canonical_checkpoint_owner": "python.RecoveryApplication",
            "canonical_ui_projection_owner": "typescript.CanonicalProjectionStore",
            "transient_view_owner": "typescript.TopologyWorkbenchController",
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
    print(f"m2_topology_interaction_ledger_aligned={str(aligned).lower()}")
    print(f"m2_topology_interaction_source_decision_count={len(DECISIONS)}")
    print(f"m2_topology_interaction_ledger_entry_count={count}")
    print(f"m2_topology_interaction_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
