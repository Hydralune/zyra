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
OWNER_UNIT = "M2-S01A-01"
SLICE_ID = "M2-S01A-01"
STAMP = "2026-07-23T00:00:00.000Z"
TEST_PATH = "tests/integration/test_m2_typed_api_client_transport.py"
TEST_COMMAND = (
    "python -m pytest -q -p no:cacheprovider "
    "tests/integration/test_m2_typed_api_client_transport.py"
)
UNIT_TEST_PATH = "packages/core/typed-api-client/test/transport.test.ts"
UNIT_TEST_COMMAND = (
    "bun test ./packages/core/typed-api-client/test/transport.test.ts"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_path": (
            "packages/protocol/src/{api,errors,middleware/auth,middleware/schema-error,"
            "groups/session,groups/event,groups/health}.ts;"
            "packages/app/src/context/{server-sdk,server-sync,server-session}.tsx;"
            "packages/core/src/{util/retry,id/id}.ts"
        ),
        "capability_name": "typed_api_protocol_transport_and_request_lifecycle",
        "capability_summary": (
            "Single typed client, version/auth/error envelope, opaque cursor, "
            "request correlation, bounded idempotent retry, cancellation, "
            "transport registry and transient event polling."
        ),
        "targets": [
            "packages/core/typed-api-client/src/protocol.ts",
            "packages/core/typed-api-client/src/transport.ts",
            "packages/core/typed-api-client/src/registry.ts",
            "packages/core/typed-api-client/src/request.ts",
            "packages/core/typed-api-client/src/response.ts",
            "packages/core/typed-api-client/src/retry.ts",
            "packages/core/typed-api-client/src/identifiers.ts",
            "packages/core/typed-api-client/src/polling.ts",
            "apps/web/src/api/client.ts",
            "apps/web/src/api/task-api.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "same_language_adapt",
        "migration_strategy": "direct_port",
        "line_count_policy": "counts_as_runtime",
        "runtime_required": True,
        "rationale": (
            "The OpenCode protocol/SDK/session mechanisms were decomposed into "
            "Zyra-owned TypeScript modules. OpenCode schemas, Solid stores and "
            "SDK dependencies are absent; M1 task/event/checkpoint owners remain canonical."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_path": (
            "frontend/src/api/conversation-service/{conversation-service.api,"
            "v1-conversation-service.api}.ts;"
            "frontend/src/api/{event-service/event-service.api,"
            "sandbox-service/sandbox-service.api}.ts"
        ),
        "capability_name": "task_lifecycle_auth_timeout_and_resume_mapping",
        "capability_summary": (
            "Create/cancel/resume lifecycle mapping, per-session authentication "
            "separation and typed lifecycle error handling."
        ),
        "targets": [
            "apps/web/src/api/lifecycle.ts",
            "apps/web/src/api/task-api.ts",
            "apps/api/zyra_api/typed_transport.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "same_language_adapt_and_python_owner_extension",
        "migration_strategy": "reimplemented_pattern",
        "line_count_policy": "counts_as_runtime",
        "runtime_required": True,
        "rationale": (
            "Only bounded lifecycle/auth semantics supplement the primary client. "
            "No Axios client, runtime URL owner, conversation store or sandbox owner is copied."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/coding-agent/src/modes/rpc/rpc-types.ts;"
            "packages/coding-agent/src/jsonrpc/message-framing.ts;"
            "packages/coding-agent/src/modes/acp/acp-client-bridge.ts"
        ),
        "capability_name": "rpc_acp_request_correlation_and_cancellation_conformance",
        "capability_summary": (
            "Explicit request/response IDs, cancellation targets, malformed "
            "envelope rejection and capability/auth separation conformance."
        ),
        "targets": [UNIT_TEST_PATH],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "line_count_policy": "excluded_inventory_only",
        "runtime_required": False,
        "rationale": (
            "OMP RPC/ACP is a behavior oracle only and owns no Zyra production "
            "transport, session, permission or terminal state."
        ),
    },
    {
        "source_repo": "agent-framework",
        "source_commit": "d50698bb797710bfd1ebf34eb621c905a4009b2d",
        "source_path": "AG-UI approval/history request and response envelopes",
        "capability_name": "agui_envelope_correlation_conformance",
        "capability_summary": "Approval/history envelope correlation comparison.",
        "targets": [UNIT_TEST_PATH],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "line_count_policy": "excluded_inventory_only",
        "runtime_required": False,
        "rationale": (
            "Agent Framework remains a conformance oracle and does not regain "
            "workflow, checkpoint, session or client ownership."
        ),
    },
    {
        "source_repo": "agentscope",
        "source_commit": "b6698c5dbaa1aa916925e27402767f45e2405fa4",
        "source_path": (
            "src/agentscope/app/_router/_session.py;"
            "src/agentscope/app/_service/{_session,_session_projection}.py;"
            "examples/web_ui/frontend/src/api/{chat,session}.ts"
        ),
        "capability_name": "session_projection_envelope_conformance",
        "capability_summary": "Session lifecycle projection and correlation comparison.",
        "targets": [UNIT_TEST_PATH],
        "source_role": "conformance_only",
        "migration_mode": "conformance_only",
        "migration_strategy": "not_selected",
        "line_count_policy": "excluded_inventory_only",
        "runtime_required": False,
        "rationale": (
            "AgentScope is a conformance oracle only for this slice and owns no "
            "Zyra session store, event stream or frontend client."
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
    runtime_required = bool(decision["runtime_required"])
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
        "main_path_status": "tested_main_path" if runtime_required else "inventoried",
        "lifecycle": "productized" if runtime_required else "candidate",
        "line_count_policy": decision["line_count_policy"],
        "owner_unit": OWNER_UNIT,
        "milestone": "M2",
        "created_at": STAMP,
        "updated_at": STAMP,
        "dependencies": [],
        "downstream_units": ["M2-01A", "M2-01B", "M2-02A", "M2-03A"],
        "blockers": [],
        "replacement_plan": (
            "Zyra-owned typed transport is the only browser client and delegates "
            "canonical state to existing M1 owners."
            if runtime_required
            else "Conformance-only source; no production runtime is selected."
        ),
        "risk_notes": [
            "No runtime path may depend on a parent source repository.",
            "A conformance source cannot become a second transport or state owner.",
        ],
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "recorded",
            "license_hint": "Source mechanism recorded for Zyra-owned M2 transport internalization.",
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
                "required_for_main_path": runtime_required,
                "must_exist_for_statuses": (
                    ["tested_main_path"] if runtime_required else ["inventoried"]
                ),
            }
            for index, path in enumerate(targets)
        ],
        "main_path": {
            "surfaces": ["typed_api_client", "web_api", "zyra_api"],
            "event_types": [],
            "api_routes": (
                [
                    "GET /health",
                    "GET /runtime/readiness",
                    "GET /tasks",
                    "GET /tasks/{task_id}",
                    "GET /tasks/{task_id}/events",
                    "POST /tasks",
                    "POST /tasks/{task_id}/run",
                    "POST /tasks/{task_id}/cancel",
                ]
                if runtime_required
                else []
            ),
            "control_commands": ["cancel", "resume"] if runtime_required else [],
            "artifact_kinds": [],
            "worker_runtime": (
                "ZyraApiClient -> TransportRegistry -> FetchApiTransport -> ZyraRequestHandler"
                if runtime_required
                else f"conformance_only; behavior is tested by {UNIT_TEST_PATH}"
            ),
            "ui_panels": ["typed_transport_boundary"] if runtime_required else [],
        },
        "runtime_entry": {
            "module": (
                "apps.web.src.api.client"
                if runtime_required
                else "packages.core.typed-api-client.test.transport"
            ),
            "function": (
                "ZyraApiClient.endpoint"
                if runtime_required
                else "typed transport conformance tests"
            ),
            "protocol": "zyra-typed-api-v1",
            "health_check": TEST_COMMAND if runtime_required else UNIT_TEST_COMMAND,
            "command": "",
            "config_refs": targets,
            "environment_refs": ["ZYRA_API_AUTH_TOKEN"] if runtime_required else [],
        },
        "test_entries": [
            {
                "path": UNIT_TEST_PATH,
                "command": UNIT_TEST_COMMAND,
                "kind": "unit",
                "expected_signal": (
                    "transport/auth/version/timeout/disconnect/malformed/disable "
                    "contracts fail closed"
                ),
                "required": True,
            },
            {
                "path": TEST_PATH,
                "command": TEST_COMMAND,
                "kind": "integration",
                "expected_signal": (
                    "embedded real API/store create, resume, cancel and replay "
                    "produce one canonical mutation per idempotency key"
                ),
                "required": runtime_required,
            },
        ],
        "tags": ["m2-01a", SLICE_ID.lower(), "typed-transport", source_role],
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
            "root_source_runtime_dependency": False,
            "implementation_commit": "b4e6b5b554ff6ee8ebd786bddc239d00efa9cbc2",
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
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    write = args.write or not args.check
    aligned, count = synchronize(args.ledger.resolve(), write=write)
    print(f"m2_typed_transport_source_ledger_aligned={str(aligned).lower()}")
    print(f"m2_typed_transport_source_decision_count={count}")
    print(f"m2_typed_transport_owner_slice={SLICE_ID}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
