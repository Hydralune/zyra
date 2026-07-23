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
OWNER_UNIT = "M2-S02A-01"
SLICE_ID = OWNER_UNIT
IMPLEMENTATION_COMMIT = "7ea026f1de635197d84b3179d5494e96ca687577"
STAMP = "2026-07-24T00:00:00.000Z"
UNIT_TEST = "apps/web/test/topology-projection.test.ts"
INTEGRATION_TEST = "tests/integration/test_topology_route_placement_projection.py"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "zyra",
        "source_commit": "f7fff49be91fb0f797260c03ff9cca76c06c5974",
        "source_path": (
            "apps/web/src/events/ingress;"
            "apps/web/src/state/{store,reducer,projectors,selectors,causality,panel-selectors}.ts"
        ),
        "capability_name": "canonical_topology_route_placement_projection",
        "capability_summary": (
            "One deeply immutable read model for dynamic graph revisions, routes, "
            "placements, checkpoints, branches, requirement changes and evidence."
        ),
        "targets": [
            "apps/web/src/features/topology/projection/projector.ts",
            "apps/web/src/features/topology/projection/record-reader.ts",
            "apps/web/src/features/topology/projection/nodes.ts",
            "apps/web/src/features/topology/projection/edges.ts",
            "apps/web/src/features/topology/projection/routes.ts",
            "apps/web/src/features/topology/projection/placements.ts",
            "apps/web/src/features/topology/projection/checkpoints.ts",
            "apps/web/src/features/topology/projection/changes.ts",
            "apps/web/src/features/topology/projection/evidence.ts",
            "apps/web/src/features/topology/projection/analysis.ts",
            "apps/web/src/features/topology/projection/diagnostics.ts",
            "apps/web/src/state/panel-selectors.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_component_integration",
        "migration_strategy": "direct_port",
        "main_path_status": "tested_main_path",
        "lifecycle": "productized",
        "line_count_policy": "counts_as_runtime",
        "rationale": (
            "The existing TypeScript ingress transaction, canonical state tables, "
            "causality index, selector dependency keys and disable semantics are "
            "retained. The topology feature is a pure selector and creates no second "
            "store, reducer, subscription, cursor or persistence owner."
        ),
    },
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_path": "packages/tui/src/routes/session/index.tsx",
        "capability_name": "single_state_session_topology_view_reference",
        "capability_summary": (
            "Session hierarchy, status and event-relationship presentation from one "
            "synchronized state source."
        ),
        "targets": [UNIT_TEST],
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "main_path_status": "inventoried",
        "lifecycle": "candidate",
        "line_count_policy": "excluded_inventory_only",
        "rationale": (
            "OpenCode is a view-conformance reference only. No Solid store, session "
            "cache, UI state owner, command runtime or production code is migrated."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_path": (
            "frontend/src/routes/planner-tab.tsx;"
            "frontend/src/routes/task-list-tab.tsx"
        ),
        "capability_name": "planner_hierarchy_event_state_reference",
        "capability_summary": (
            "Planner/task hierarchy and event-backed state-presentation conformance."
        ),
        "targets": [UNIT_TEST],
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "main_path_status": "inventoried",
        "lifecycle": "candidate",
        "line_count_policy": "excluded_inventory_only",
        "rationale": (
            "OpenHands is a presentation reference only. No router, Redux store, "
            "conversation owner, sandbox service or backend is migrated."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/coding-agent/src/modes/rpc/rpc-types.ts;"
            "packages/coding-agent/src/jsonrpc/message-framing.ts"
        ),
        "capability_name": "typed_route_revision_interaction_reference",
        "capability_summary": (
            "Typed route identities, immutable revision changes, request correlation "
            "and concise state drill-down conformance."
        ),
        "targets": [UNIT_TEST],
        "source_role": "reference_only",
        "migration_mode": "reference_only",
        "migration_strategy": "not_selected",
        "main_path_status": "inventoried",
        "lifecycle": "candidate",
        "line_count_policy": "excluded_inventory_only",
        "rationale": (
            "OMP is a typed interaction reference only. No RPC, TUI, provider owner "
            "or agent loop is migrated."
        ),
    },
    {
        "source_repo": "langgraph",
        "source_commit": "5931a5f0b313feff24e2516a586c55601b868ac1",
        "source_path": (
            "libs/checkpoint/langgraph/checkpoint/base/__init__.py;"
            "libs/langgraph/langgraph/pregel/_loop.py"
        ),
        "capability_name": "checkpoint_pending_committed_exact_resume_conformance",
        "capability_summary": (
            "Checkpoint identity and lineage, pending versus committed writes, stable "
            "task identity, interrupt/resume correlation and exact-resume conformance."
        ),
        "targets": [UNIT_TEST, INTEGRATION_TEST],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "main_path_status": "inventoried",
        "lifecycle": "candidate",
        "line_count_policy": "excluded_inventory_only",
        "rationale": (
            "LangGraph is limited to the narrow checkpoint conformance contract. "
            "StateGraph, channels, reducers, Pregel, stream, Store, ToolNode, SDK, "
            "server and deploy code own no production path."
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
        "downstream_units": ["M2-S02A-02", "M2-03A", "M2-04A"],
        "blockers": [],
        "replacement_plan": (
            "CanonicalProjectionStore remains replaceable behind normalized ingress "
            "and typed selectors."
            if production
            else f"{source_role} source; no production runtime is selected."
        ),
        "risk_notes": [
            "No runtime path may depend on a parent source repository.",
            "Canonical backend graph, scheduler and checkpoint stores remain authoritative.",
            "CanonicalProjectionStore remains the sole browser projection state owner.",
            (
                "The reference/conformance source cannot become a second state owner."
                if not production
                else "The selector cannot write backend or canonical browser state."
            ),
        ],
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "not_required" if decision["source_repo"] == "zyra" else "recorded",
            "license_hint": "M2 topology projection source role recorded.",
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
            "surfaces": ["web_workbench", "canonical_projection_store"],
            "event_types": [
                "topology.*",
                "scheduler.*",
                "resource.*",
                "recovery.*",
                "requirement.changed",
            ],
            "api_routes": [],
            "control_commands": [],
            "artifact_kinds": ["topology_projection"],
            "worker_runtime": (
                "RuntimeEventSpine -> normalized ingress -> "
                "CanonicalProjectionStore -> selectTopologyProjection"
                if production
                else f"{source_role}; behavior only"
            ),
            "ui_panels": ["topology"],
        },
        "runtime_entry": {
            "module": (
                "apps.web.src.features.topology.projection.projector"
                if production
                else "apps.web.test.topology-projection"
            ),
            "function": (
                "buildTopologyProjection"
                if production
                else "topology projection conformance tests"
            ),
            "protocol": "zyra.topology-projection/v1",
            "health_check": f"bun test ./{UNIT_TEST}",
            "command": "bun run --cwd apps/web build" if production else "",
            "config_refs": targets,
            "environment_refs": [],
        },
        "test_entries": [
            {
                "path": UNIT_TEST,
                "command": f"bun test ./{UNIT_TEST}",
                "kind": "unit",
                "expected_signal": (
                    "open-world graph, fixed route, placement, checkpoint, branch, "
                    "requirement, causality, stale/gap and disable behavior"
                ),
                "required": True,
            },
            {
                "path": INTEGRATION_TEST,
                "command": f"python -m pytest -q {INTEGRATION_TEST}",
                "kind": "integration",
                "expected_signal": (
                    "real scheduler input changes backend route and projected graph route"
                ),
                "required": production or source_role == "conformance_only",
            },
        ],
        "tags": ["m2-02a", SLICE_ID.lower(), "topology-projection", source_role],
        "metadata": {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "migration_mode": decision["migration_mode"],
            "canonical_graph_owner": "python.GraphStateCustody",
            "canonical_scheduler_owner": "python.ResourceScheduler",
            "canonical_checkpoint_owner": "existing_M1_checkpoint_recovery_stores",
            "canonical_ui_projection_owner": "typescript.CanonicalProjectionStore",
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
        and str((item.get("metadata") or {}).get("slice_id") or "") != SLICE_ID
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
    print(f"m2_topology_projection_ledger_aligned={str(aligned).lower()}")
    print(f"m2_topology_projection_source_decision_count={len(DECISIONS)}")
    print(f"m2_topology_projection_ledger_entry_count={count}")
    print(f"m2_topology_projection_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
