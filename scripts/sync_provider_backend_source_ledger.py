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

OWNER_UNIT = "M1-S05D-01"

DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_path": (
            "packages/core/src/catalog.ts;packages/core/src/integration.ts;"
            "packages/core/src/credential.ts;packages/core/src/aisdk.ts;"
            "packages/core/src/provider/model.ts;packages/opencode/src/provider/provider.ts;"
            "packages/opencode/src/provider/auth.ts;packages/opencode/src/config/config.ts;"
            "packages/opencode/src/config/parse.ts"
        ),
        "capability_name": "provider_catalog_integration_credential_control_plane",
        "capability_summary": (
            "Versioned provider/model catalog, explicit provider integration records, secret-reference "
            "credential lifecycle, route resolution, transport selection and read-only V1 compatibility."
        ),
        "target_paths": [
            "packages/runtime/provider-control-plane/src/catalog.ts",
            "packages/runtime/provider-control-plane/src/credentials.ts",
            "packages/runtime/provider-control-plane/src/resolver.ts",
            "packages/runtime/provider-control-plane/src/routing.ts",
            "packages/runtime/provider-control-plane/src/transport/runtime.ts",
            "packages/runtime/provider-control-plane/src/control-plane.ts",
            "packages/runtime/provider-control-plane/src/store.ts",
        ],
        "source_role": "primary_implementation",
        "migration_strategy": "direct_port",
        "rationale": (
            "The TypeScript control plane is decomposed around Zyra catalog revisions, route leases, "
            "credential references, attempts and events. OpenCode remains neither a runtime dependency "
            "nor a second state owner, and its V1 surface is read-only with no hidden defaults."
        ),
    },
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_path": "hermes_cli/runtime_provider.py",
        "capability_name": "provider_environment_resolution_precedence",
        "capability_summary": (
            "Deterministic environment/config resolution precedence and provider normalization without "
            "moving catalog, credential or route ownership out of the TypeScript control plane."
        ),
        "target_paths": [
            "packages/runtime/provider-control-plane/src/resolver.ts",
            "packages/runtime/provider-control-plane/src/canonical.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "rationale": (
            "Only bounded resolver precedence and normalization supplement the opencode primary; "
            "Hermes model execution, CLI, environment mutation and provider state are not imported."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/ai/src/providers/openai-completions.ts;"
            "packages/ai/src/providers/openai-responses.ts;"
            "packages/ai/src/providers/anthropic.ts;"
            "packages/ai/src/providers/simple-openai-responses.ts;"
            "packages/ai/src/utils/auth-retry.ts"
        ),
        "capability_name": "provider_protocol_wire_stream_and_auth_retry",
        "capability_summary": (
            "Typed OpenAI Chat, OpenAI Responses and Anthropic Messages wire contracts, incremental SSE "
            "decoding, structured provider error classification and credential-scoped auth retry."
        ),
        "target_paths": [
            "packages/runtime/provider-control-plane/src/wire/openai-chat.ts",
            "packages/runtime/provider-control-plane/src/wire/openai-responses.ts",
            "packages/runtime/provider-control-plane/src/wire/anthropic-messages.ts",
            "packages/runtime/provider-control-plane/src/transport/protocols.ts",
            "packages/runtime/provider-control-plane/src/transport/sse.ts",
            "packages/runtime/provider-control-plane/src/auth-retry.ts",
            "packages/runtime/provider-control-plane/src/quirks/error-classifier.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_strategy": "direct_port",
        "rationale": (
            "The mature protocol shapes and retry mechanics were retained in TypeScript but placed behind "
            "Zyra route, credential, byte-accounting and attempt owners. The OMP agent loop, UI, provider "
            "registry and process runtime are not copied or invoked."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_path": (
            "openhands/app_server/sandbox/sandbox_service.py;"
            "openhands/app_server/sandbox/process_sandbox_service.py;"
            "openhands/app_server/sandbox/docker_sandbox_service.py;"
            "openhands/app_server/sandbox/remote_sandbox_service.py"
        ),
        "capability_name": "backend_lifecycle_health_dispatch_failover",
        "capability_summary": (
            "Explicit local, container, edge and cloud backend definitions, readiness state, immutable "
            "dispatch leases, actual worker invocation, timeout reconciliation and backend-only failover."
        ),
        "target_paths": [
            "packages/scheduler/zyra_scheduler/backend_registry/models.py",
            "packages/scheduler/zyra_scheduler/backend_registry/store.py",
            "packages/scheduler/zyra_scheduler/backend_registry/registry.py",
            "packages/scheduler/zyra_scheduler/backend_registry/runtime.py",
            "packages/scheduler/zyra_scheduler/backend_registry/integration.py",
            "packages/orchestration/zyra_orchestration/task_graph.py",
        ],
        "source_role": "primary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "rationale": (
            "Backend lifecycle and readiness mechanisms were decomposed into a Zyra SQLite registry and "
            "task-graph dispatch boundary. No OpenHands server, sandbox process or remote endpoint is a "
            "runtime dependency, and provider route state remains opaque to this owner."
        ),
    },
)


def ledger_id(decision: dict[str, Any]) -> str:
    identity = "|".join(
        (str(decision["source_repo"]), str(decision["source_path"]), str(decision["capability_name"]))
    )
    return "ledger_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    targets = list(decision["target_paths"])
    return {
        "ledger_id": ledger_id(decision),
        "source_repo": decision["source_repo"],
        "source_path": decision["source_path"],
        "capability_name": decision["capability_name"],
        "capability_summary": decision["capability_summary"],
        "target_bindings": [
            {"path": path, "role": "primary" if index == 0 else "supporting", "required_for_main_path": True}
            for index, path in enumerate(targets)
        ],
        "migration_strategy": decision["migration_strategy"],
        "main_path_status": "tested_main_path",
        "lifecycle": "productized",
        "owner_unit": OWNER_UNIT,
        "main_path": {
            "surfaces": ["provider_control_plane", "backend_registry", "task_graph", "task_api"],
            "event_types": [
                "provider.route.acquired",
                "provider.dispatch.completed",
                "provider.dispatch.failed",
                "backend.dispatch.started",
                "backend.dispatch.completed",
                "backend.dispatch.failed",
            ],
            "api_routes": [
                "GET /providers/health",
                "POST /providers/routes",
                "POST /providers/dispatch",
                "GET /backends",
                "POST /backends",
                "POST /tasks",
            ],
            "control_commands": [],
            "artifact_kinds": [],
            "worker_runtime": "BackendDispatchRuntime.dispatch",
            "ui_panels": [],
        },
        "line_count_policy": "counts_as_runtime",
        "license_notice": (
            {
                "source_repo": decision["source_repo"],
                "status": "recorded",
                "license_hint": (
                    "MIT direct-port source; the Responses wire contract also records its "
                    "openai-node Apache-2.0 derivation."
                ),
                "notice_path": "packages/runtime/provider-control-plane/THIRD_PARTY_NOTICES.md",
                "notes": (
                    "Source and revision are recorded; Zyra owns the modified package and has no "
                    "runtime dependency on the parent repository."
                ),
            }
            if decision["migration_strategy"] == "direct_port"
            else None
        ),
        "runtime_entry": {
            "module": (
                "@zyra/provider-control-plane"
                if decision["source_repo"] != "OpenHands"
                else "zyra_scheduler.backend_registry"
            ),
            "function": (
                "ProviderControlPlane.dispatch"
                if decision["source_repo"] != "OpenHands"
                else "BackendDispatchRuntime.dispatch"
            ),
            "protocol": "zyra-provider-backend-dispatch-v1",
            "health_check": (
                "node --experimental-strip-types --test "
                "packages/runtime/provider-control-plane/test/provider-control-plane.test.ts; "
                "python -m pytest tests/unit/test_backend_registry.py "
                "tests/integration/test_provider_backend_api.py -q"
            ),
            "config_refs": targets,
        },
        "test_entries": [
            {
                "path": "packages/runtime/provider-control-plane/test/provider-control-plane.test.ts",
                "command": (
                    "node --experimental-strip-types --test "
                    "packages/runtime/provider-control-plane/test/provider-control-plane.test.ts"
                ),
                "kind": "unit",
                "expected_signal": "real protocol bytes, route pinning, zero-byte auth fencing and retry separation",
                "required": True,
            },
            {
                "path": "tests/unit/test_backend_registry.py",
                "command": "python -m pytest tests/unit/test_backend_registry.py -q",
                "kind": "unit",
                "expected_signal": "actual backend dispatch, backend-only failover, timeout reconciliation and lease custody",
                "required": True,
            },
            {
                "path": "tests/integration/test_provider_backend_api.py",
                "command": "python -m pytest tests/integration/test_provider_backend_api.py -q",
                "kind": "integration",
                "expected_signal": "provider and backend owners are reachable through real API and task-graph paths",
                "required": True,
            },
        ],
        "notes": (
            f"source_role={decision['source_role']}; source_commit={decision['source_commit']}; "
            f"{decision['rationale']}"
        ),
        "metadata": {
            "owner_unit": OWNER_UNIT,
            "source_role": decision["source_role"],
            "source_commit": decision["source_commit"],
            "canonical_provider_owner": "TypeScript ProviderControlPlaneStore",
            "canonical_backend_owner": "Python BackendRegistryStore",
            "provider_backend_state_separated": True,
            "dispatch_envelope_contains_credentials": False,
            "root_source_runtime_dependency": False,
        },
    }


def rewrite(document: Any) -> Any:
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict) and isinstance(document.get("entries"), list):
        entries = document["entries"]
    else:
        raise ValueError("internalization ledger seed must be a list or contain entries")
    replacements = {item["ledger_id"]: item for item in (entry(decision) for decision in DECISIONS)}
    output = [replacements.pop(str(item.get("ledger_id") or ""), item) for item in entries]
    output.extend(replacements.values())
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    if isinstance(document, list):
        return output
    return {**document, "entries": output, "summary": to_jsonable(InternalizationLedger(typed).summary())}


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
    print(f"provider_backend_source_ledger_aligned={str(aligned).lower()}")
    print(f"provider_backend_source_decision_count={count}")
    print(f"provider_backend_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
