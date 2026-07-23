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

from zyra_integrations import (  # noqa: E402
    InternalizationLedger,
    InternalizationLedgerEntry,
)
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402


DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNER_UNIT = "M2-S01B-01"
SLICE_ID = "M2-S01B-01"
STAMP = "2026-07-23T00:00:00.000Z"
UNIT_TEST = "apps/web/test/event-ingress.test.ts"
INTEGRATION_TEST = "tests/integration/test_event_stream_cursor_ingestion.py"
IMPLEMENTATION_COMMIT = "6cd3f3fc259a5d39e96f81c79917ce3159d46efe"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_path": "packages/app/src/context/server-sdk.tsx",
        "capability_name": "event_transport_generation_reconnect_and_heartbeat",
        "capability_summary": (
            "One live generation with abort fencing, heartbeat supervision, bounded "
            "retry, transport cooldown, frame delivery and last-subscriber shutdown."
        ),
        "targets": [
            "apps/web/src/events/ingress/coordinator.ts",
            "apps/web/src/events/ingress/reconnect.ts",
            "apps/web/src/events/ingress/transport-sources.ts",
            "apps/web/src/api/event-transport.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_crop_and_adapt",
        "migration_strategy": "direct_port",
        "rationale": (
            "OpenCode live-generation and reconnection control flow was cropped into "
            "Zyra cursor/session modules. Solid context, SDK event ownership and "
            "directory emitters were removed."
        ),
    },
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_path": "packages/app/src/context/server-session.ts",
        "capability_name": "snapshot_live_merge_ordering_identity_and_pressure",
        "capability_summary": (
            "Subscribe-before-snapshot retention, deterministic predecessor ordering, "
            "identity dedupe, partial/final assembly, orphan/tombstone handling and "
            "bounded pressure."
        ),
        "targets": [
            "apps/web/src/events/ingress/snapshot-barrier.ts",
            "apps/web/src/events/ingress/ordered-buffer.ts",
            "apps/web/src/events/ingress/identity-window.ts",
            "apps/web/src/events/ingress/assembly.ts",
            "apps/web/src/events/ingress/gap-recovery.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_retained_control_flow_adapt",
        "migration_strategy": "direct_port",
        "rationale": (
            "Event-during-request freshness and deterministic merge behavior were "
            "retained, but OpenCode session/message stores and reducers were removed. "
            "The result owns transient ingress state only."
        ),
    },
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_path": (
            "packages/app/src/context/server-sync.tsx;"
            "packages/app/src/context/global-sync/event-reducer.ts;"
            "packages/app/src/context/global-sync/session-cache.ts"
        ),
        "capability_name": "event_subscription_union_scope_and_diagnostics",
        "capability_summary": (
            "Reference-counted task subscription union, observer isolation, bounded "
            "diagnostics and a single public TaskEventTransport facade."
        ),
        "targets": [
            "apps/web/src/events/ingress/subscriptions.ts",
            "apps/web/src/events/ingress/diagnostics.ts",
            "apps/web/src/events/ingress/contracts.ts",
            "apps/web/src/api/event-transport.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_crop_and_adapt",
        "migration_strategy": "direct_port",
        "rationale": (
            "Normalized scopes, reference counting and consumer isolation were "
            "retained. OpenCode global reducers and canonical session cache were "
            "explicitly excluded."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_path": (
            "frontend/src/api/event-service/event-service.api.ts;"
            "frontend/src/api/event-service/event-service.types.ts"
        ),
        "capability_name": "typed_event_history_and_long_poll_sources",
        "capability_summary": (
            "Bounded page and wait validation, typed history/delta requests, missing "
            "event failure and protocol-equivalent long-poll fallback."
        ),
        "targets": [
            "apps/web/src/events/ingress/transport-sources.ts",
            "apps/web/src/events/ingress/validator.ts",
            "apps/web/src/api/task-api.ts",
            "packages/core/typed-api-client/src/transport.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "same_language_bounded_integration",
        "migration_strategy": "reimplemented_pattern",
        "rationale": (
            "OpenHands request paging and event service boundaries supplement the "
            "OpenCode ingress loop. Axios, conversation DTOs and frontend state owners "
            "were replaced by Zyra's existing typed client and runtime-event contract."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_path": (
            "openhands/app_server/event/event_service_base.py;"
            "openhands/app_server/event/event_router.py;"
            "openhands/app_server/event/event_store.py"
        ),
        "capability_name": "read_only_event_cursor_snapshot_delta_api",
        "capability_summary": (
            "Signed scoped cursor, stable task ordering, captured snapshot boundary, "
            "bounded delta wait, SSE response metadata and explicit transport errors."
        ),
        "targets": [
            "apps/api/zyra_api/event_stream_ingress.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "same_language_crop_and_adapt",
        "migration_strategy": "direct_port",
        "rationale": (
            "The bounded OpenHands event-service query and routing behavior was "
            "cropped into a read-only facade over Zyra's TypeScript runtime-event "
            "spine. It cannot persist, renumber, reduce or fabricate events."
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
            "Zyra-owned browser event ingress is reached through createZyraApi().events "
            "and keeps the TypeScript runtime-event spine as canonical event owner."
        ),
        "risk_notes": [
            "No runtime path may depend on a parent source repository.",
            "The Python ingress facade is read-only and cannot become an event store.",
            "Ingress batches do not own M2-S01B-02 UI projections.",
            "Supplementary sources cannot become a second stream controller.",
        ],
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "recorded",
            "license_hint": (
                "Source mechanism recorded for Zyra-owned M2 event-ingress "
                "internalization."
            ),
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
            "surfaces": ["web_workbench", "typed_api_client", "zyra_api"],
            "event_types": ["zyra.runtime-event/v1"],
            "api_routes": [
                "GET /tasks/{task_id}/event-ingress/capabilities",
                "GET /tasks/{task_id}/event-ingress/snapshot",
                "GET /tasks/{task_id}/event-ingress/delta",
                "GET /tasks/{task_id}/event-ingress/sse",
            ],
            "control_commands": [],
            "artifact_kinds": [],
            "worker_runtime": (
                "createZyraApi().events -> TaskEventTransport -> "
                "EventIngressCoordinator -> typed API -> EventIngressApiFacade -> "
                "RuntimeEventSpineBridge"
            ),
            "ui_panels": [],
        },
        "runtime_entry": {
            "module": "apps.web.src.api.event-transport",
            "function": "TaskEventTransport.subscribe",
            "protocol": "zyra.event-ingress/v1",
            "health_check": f"bun test ./{UNIT_TEST}",
            "command": "bun run build:web",
            "config_refs": targets,
            "environment_refs": [
                "ZYRA_API_BASE_URL",
                "ZYRA_API_AUTH_TOKEN",
                "ZYRA_EVENT_CURSOR_SECRET",
            ],
        },
        "test_entries": [
            {
                "path": UNIT_TEST,
                "command": f"bun test ./{UNIT_TEST}",
                "kind": "unit",
                "expected_signal": (
                    "cursor, ordering, snapshot, duplicate, gap, pressure, partial, "
                    "orphan, tombstone, retry and disable semantics fail if ingress is "
                    "disconnected"
                ),
                "required": True,
            },
            {
                "path": INTEGRATION_TEST,
                "command": f"python -m pytest -q {INTEGRATION_TEST}",
                "kind": "integration",
                "expected_signal": (
                    "real API snapshot/delta plus Bun browser SSE reconnect and "
                    "long-poll fallback fail if the default ingress path is removed"
                ),
                "required": True,
            },
        ],
        "tags": ["m2-01b", SLICE_ID.lower(), "event-ingress", source_role],
        "metadata": {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "migration_mode": decision["migration_mode"],
            "canonical_event_owner": "typescript.RuntimeEventSpine",
            "canonical_ui_projection_owner": "deferred_to_M2-S01B-02",
            "transient_ingress_owner": "browser.EventIngressCoordinator",
            "python_facade_write_allowed": False,
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
        raise ValueError(
            "internalization ledger seed must be a list or contain entries"
        )
    replacements = {
        item["ledger_id"]: item for item in (entry(decision) for decision in DECISIONS)
    }
    retained = [
        item
        for item in entries
        if str(item.get("owner_unit") or "") != OWNER_UNIT
        and str((item.get("metadata") or {}).get("slice_id") or "") != SLICE_ID
    ]
    output = [
        replacements.pop(str(item.get("ledger_id") or ""), item) for item in retained
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
    print(f"m2_event_ingress_source_ledger_aligned={str(aligned).lower()}")
    print(f"m2_event_ingress_source_decision_count={count}")
    print(f"m2_event_ingress_owner_slice={SLICE_ID}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
