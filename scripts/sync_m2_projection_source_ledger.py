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
OWNER_UNIT = "M2-S01B-02"
SLICE_ID = OWNER_UNIT
IMPLEMENTATION_COMMIT = "7446f4cb57c4a75f416a5d6707ccf47c03a67cc3"
STAMP = "2026-07-23T00:00:00.000Z"
UNIT_TEST = "apps/web/test/canonical-projection-store.test.ts"
INTEGRATION_TEST = "tests/integration/test_canonical_projection_recovery.py"
M2_01_OWNERS = {
    "M2-S01A-01",
    "M2-S01A-02",
    "M2-S01B-01",
    "M2-S01B-02",
}
AGGREGATE_PATH_CORRECTIONS = {
    "ile_bed00a8bff4c2ba2005a": (
        "packages/protocol/src/api.ts;"
        "packages/protocol/src/errors.ts;"
        "packages/protocol/src/middleware/authorization.ts;"
        "packages/protocol/src/middleware/schema-error.ts;"
        "packages/protocol/src/groups/session.ts;"
        "packages/protocol/src/groups/event.ts;"
        "packages/protocol/src/groups/health.ts;"
        "packages/app/src/context/server-sdk.tsx;"
        "packages/app/src/context/server-sync.tsx;"
        "packages/app/src/context/server-session.ts;"
        "packages/core/src/util/retry.ts;"
        "packages/core/src/id/id.ts"
    ),
}


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_path": "packages/app/src/context/server-session.ts",
        "capability_name": "canonical_projection_transaction_reconciliation",
        "capability_summary": (
            "Immutable revision transactions with deterministic identity merge, "
            "optimistic confirmation, partial settlement, tombstones and orphans."
        ),
        "targets": [
            "apps/web/src/state/reducer.ts",
            "apps/web/src/state/settlement.ts",
            "apps/web/src/state/orphans.ts",
            "apps/web/src/state/history-fold.ts",
            "apps/web/src/state/causality.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_retained_control_flow_adapt",
        "migration_strategy": "direct_port",
        "rationale": (
            "OpenCode freshness and reconciliation control flow was retained in "
            "Zyra immutable ingress transactions. Session/message DTOs and Solid "
            "store ownership were replaced by Zyra events and domain projections."
        ),
    },
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_path": (
            "packages/app/src/context/global-sync/event-reducer.ts;"
            "packages/app/src/context/global-sync/session-cache.ts;"
            "packages/app/src/context/global-sync/session-trim.ts"
        ),
        "capability_name": "projection_domains_indices_selectors_and_retention",
        "capability_summary": (
            "All required task/runtime domains, reversible indices, dependency-keyed "
            "selectors, downstream panel views and protected bounded retention."
        ),
        "targets": [
            "apps/web/src/state/projectors.ts",
            "apps/web/src/state/selectors.ts",
            "apps/web/src/state/panel-selectors.ts",
            "apps/web/src/state/retention.ts",
            "apps/web/src/state/value.ts",
            "apps/web/src/state/integrity.ts",
            "apps/web/src/state/contracts.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_crop_and_adapt",
        "migration_strategy": "direct_port",
        "rationale": (
            "Ordered projection, deletion cleanup, permission lifecycle and cache "
            "protection were decomposed into Zyra domain projectors, reversible "
            "causality and typed panel selectors. OpenCode project caches were removed."
        ),
    },
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_path": (
            "packages/app/src/context/server-sync.tsx;"
            "packages/app/src/context/global-sync/child-store.ts"
        ),
        "capability_name": "workbench_projection_lifecycle_persistence_and_restore",
        "capability_summary": (
            "One workbench-scoped projection store with selector fan-out, task pinning, "
            "versioned persistence, restore-before-bind and browser-only cleanup."
        ),
        "targets": [
            "apps/web/src/state/store.ts",
            "apps/web/src/state/state.ts",
            "apps/web/src/state/persistence.ts",
            "apps/web/src/state/migrations.ts",
            "apps/web/src/app/runtime.ts",
            "apps/web/src/app/hooks.ts",
            "apps/web/src/components/tasks/task-detail.tsx",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_crop_and_adapt",
        "migration_strategy": "direct_port",
        "rationale": (
            "Owner-scoped synchronization and cleanup became the sole Zyra browser "
            "projection lifecycle. Solid, TanStack and per-panel stores were removed."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_path": (
            "openhands/app_server/event/event_service_base.py;"
            "openhands/app_server/event/event_router.py;"
            "openhands/app_server/sandbox/remote_sandbox_service.py"
        ),
        "capability_name": "history_live_accept_missing_restore_protocol",
        "capability_summary": (
            "Stable history pagination, load-before-fold, accept-only-missing identity "
            "and one projection effect for each newly committed event."
        ),
        "targets": [
            "apps/web/src/state/history-fold.ts",
            "apps/web/src/state/migrations.ts",
            "apps/web/src/state/persistence.ts",
            "apps/web/test/canonical-projection-probe.ts",
            "tests/integration/test_canonical_projection_recovery.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "bounded_cross_language_behavior_port_exception",
        "migration_strategy": "reimplemented_pattern",
        "rationale": (
            "Only independently testable Python history/live recovery behavior was "
            "ported to the TypeScript canonical reducer target. No OpenHands store, "
            "router or second projection owner was migrated."
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
            f"migration_mode={decision['migration_mode']}; {decision['rationale']}"
        ),
        "main_path_status": "tested_main_path",
        "lifecycle": "productized",
        "line_count_policy": "counts_as_runtime",
        "owner_unit": OWNER_UNIT,
        "milestone": "M2",
        "created_at": STAMP,
        "updated_at": STAMP,
        "dependencies": [],
        "downstream_units": ["M2-02A", "M2-03A", "M2-04A"],
        "blockers": [],
        "replacement_plan": (
            "The Zyra CanonicalProjectionStore consumes createZyraApi().events and "
            "remains replaceable behind normalized ingress and typed selectors."
        ),
        "risk_notes": [
            "Backend stores and RuntimeEventSpine remain authoritative.",
            "No parent repository path or process is required at runtime.",
            "Supplementary OpenHands behavior cannot become a second state owner.",
            "Browser close detaches projection only and cannot cancel backend tasks.",
        ],
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "recorded",
            "license_hint": "M2 projection mechanism provenance recorded.",
            "notice_path": "third_party/NOTICE.md",
            "source_url": "",
            "notes": "No runtime dependency on the source workspace.",
        },
        "source_evidence": [
            {
                "source_repo": decision["source_repo"],
                "source_path": decision["source_path"],
                "source_kind": "file",
                "exists_in_workspace": True,
                "symbols": [],
                "reason": f"{SLICE_ID} prospective source-to-target decision",
                "tags": ["m2-01b", source_role],
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
            "surfaces": ["web_workbench", "typed_api_client", "runtime_event_spine"],
            "event_types": ["zyra.runtime-event/v1"],
            "api_routes": [
                "GET /tasks/{task_id}/event-ingress/snapshot",
                "GET /tasks/{task_id}/event-ingress/delta",
                "GET /tasks/{task_id}/event-ingress/sse",
            ],
            "control_commands": [],
            "artifact_kinds": ["projection_snapshot"],
            "worker_runtime": (
                "createZyraApi().events -> TaskEventTransport -> "
                "CanonicalProjectionStore -> typed panel selectors"
            ),
            "ui_panels": [
                "task_detail",
                "topology",
                "timeline",
                "artifact",
                "permission",
                "session",
            ],
        },
        "runtime_entry": {
            "module": "apps.web.src.state.store",
            "function": "CanonicalProjectionStore.bind",
            "protocol": "zyra.ui-projection/v1",
            "health_check": f"bun test ./{UNIT_TEST}",
            "command": "bun run --cwd apps/web build",
            "config_refs": targets,
            "environment_refs": ["ZYRA_API_BASE_URL", "ZYRA_EVENT_CURSOR_SECRET"],
        },
        "test_entries": [
            {
                "path": UNIT_TEST,
                "command": f"bun test ./{UNIT_TEST}",
                "kind": "unit",
                "expected_signal": (
                    "domain projection, reconciliation, causality, retention, "
                    "selectors, persistence, migration and disable semantics"
                ),
                "required": True,
            },
            {
                "path": INTEGRATION_TEST,
                "command": f"python -m pytest -q {INTEGRATION_TEST}",
                "kind": "integration",
                "expected_signal": (
                    "real API snapshot, delta, restore, reconnect and browser-close "
                    "ownership semantics"
                ),
                "required": True,
            },
        ],
        "tags": ["m2-01b", SLICE_ID.lower(), "projection-store", source_role],
        "metadata": {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "migration_mode": decision["migration_mode"],
            "canonical_event_owner": "typescript.RuntimeEventSpine",
            "canonical_backend_state_owner": "existing_M1_stores",
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
    retained: list[dict[str, Any]] = []
    for original in entries:
        if (
            str(original.get("owner_unit") or "") == OWNER_UNIT
            or str((original.get("metadata") or {}).get("slice_id") or "") == SLICE_ID
        ):
            continue
        item = dict(original)
        corrected_path = AGGREGATE_PATH_CORRECTIONS.get(str(item.get("ledger_id") or ""))
        if corrected_path is not None:
            item["source_path"] = corrected_path
            item["source_evidence"] = [
                {
                    **evidence,
                    "source_path": corrected_path,
                }
                for evidence in item.get("source_evidence") or []
            ]
        retained.append(item)
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
        item
        for item in entries
        if str(item.get("owner_unit") or "") in M2_01_OWNERS
    ]
    errors: list[str] = []
    for item in selected:
        ledger_identity = str(item.get("ledger_id") or "")
        for binding in item.get("target_bindings") or []:
            path = str(binding.get("target_path") or "")
            if path and not git_file_exists(IMPLEMENTATION_COMMIT, path):
                errors.append(f"{ledger_identity}:missing:{path}")
    return len(selected), errors


def synchronize(path: Path, *, write: bool) -> tuple[bool, int, int, list[str]]:
    current = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite(current)
    aligned = canonical(current) == canonical(expected)
    if write and not aligned:
        path.write_text(canonical(expected), encoding="utf-8", newline="\n")
        current = expected
        aligned = True
    checked = current if aligned else expected
    aggregate_count, errors = audit_targets(checked)
    return aligned, len(DECISIONS), aggregate_count, errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    write = args.write or not args.check
    aligned, count, aggregate_count, errors = synchronize(
        args.ledger.resolve(),
        write=write,
    )
    print(f"m2_projection_source_ledger_aligned={str(aligned).lower()}")
    print(f"m2_projection_source_decision_count={count}")
    print(f"m2_01_aggregate_ledger_entry_count={aggregate_count}")
    print(f"m2_01_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
