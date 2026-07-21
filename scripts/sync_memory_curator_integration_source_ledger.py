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


OWNER_UNIT = "M1-S06B-02"
DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
TEST_COMMAND = (
    "python -m pytest tests/unit/test_memory_curator_worker_foundation.py "
    "tests/integration/test_memory_curator_api_main_path.py "
    "tests/integration/test_memory_curator_worker_integration.py -q"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "hermes-agent",
        "source_commit": "44ddc552f5e054759a6970af8997ea588a9d81c9",
        "source_path": (
            "agent/memory_provider.py;agent/memory_manager.py;agent/curator.py;"
            "agent/context_compressor.py;agent/conversation_compression.py;"
            "trajectory_compressor.py"
        ),
        "capability_name": "memory_curator_worker_transactional_integration_and_recall",
        "capability_summary": (
            "Hermes-derived lifecycle curation is integrated with Zyra runtime-event ingress, "
            "immutable outcomes, canonical MemoryFabric commits, derived-index publication, "
            "worker-context recall verification and crash-resumable delivery."
        ),
        "target_paths": [
            "packages/workers/zyra_workers/memory_curator_integration.py",
            "packages/workers/zyra_workers/memory_curator_ingress.py",
            "packages/memory/zyra_memory/curator_outcomes.py",
            "packages/memory/zyra_memory/curator_context.py",
            "packages/memory/zyra_memory/curator_integration_store.py",
            "packages/workers/zyra_workers/memory_curator.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "cropped_migration_same_language_module_integration",
        "runtime_module": "zyra_workers.memory_curator_integration",
        "runtime_function": "MemoryCuratorIntegrationApplication",
        "rationale": (
            "The prior productized Hermes worker remains the only candidate/validation/commit "
            "control flow. This slice adds Zyra-owned integration state and delivery around it; "
            "Hermes files, providers and sessions are not loaded at runtime."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/coding-agent/src/memories/storage.ts;"
            "packages/coding-agent/src/memories/index.ts;"
            "packages/mnemopi/src/core/extraction.ts;"
            "packages/mnemopi/src/core/beam/consolidate.ts;"
            "packages/mnemopi/src/core/veracity-consolidation.ts;"
            "packages/mnemopi/src/core/episodic-graph.ts;"
            "packages/mnemopi/src/core/orchestrator.ts"
        ),
        "capability_name": "memory_curator_outcome_delivery_lease_and_consolidation_integration",
        "capability_summary": (
            "The bounded OMP-derived candidate consolidation and ownership-token state machine "
            "is connected to versioned outcome fan-out, delivery leases, stale-owner fencing and "
            "idempotent consumer checkpoints without obtaining canonical memory ownership."
        ),
        "target_paths": [
            "packages/memory/curator-state-machine/src/job-state-machine.ts",
            "packages/memory/curator-state-machine/src/consolidation.ts",
            "packages/memory/zyra_memory/curator_delivery.py",
            "packages/memory/zyra_memory/curator_integration_models.py",
            "packages/memory/zyra_memory/curator_integration_store.py",
            "packages/workers/zyra_workers/memory_curator_integration.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "retained_typescript_with_zyra_python_protocol_integration",
        "runtime_module": "zyra_memory.curator_delivery",
        "runtime_function": "CuratorDownstreamDispatcher",
        "rationale": (
            "OMP remains restricted to candidate/consolidation and lease mechanics. Python owns "
            "the language-neutral delivery contract and rechecks durable outcomes; no OMP SQLite, "
            "MEMORY.md, MCP service, model write or parent-repository path participates."
        ),
    },
)


def _ledger_id(decision: dict[str, Any]) -> str:
    identity = "|".join(
        (
            str(decision["source_repo"]),
            str(decision["source_path"]),
            str(decision["capability_name"]),
        )
    )
    return "ledger_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _entry(decision: dict[str, Any]) -> dict[str, Any]:
    targets = list(decision["target_paths"])
    return {
        "ledger_id": _ledger_id(decision),
        "source_repo": decision["source_repo"],
        "source_path": decision["source_path"],
        "capability_name": decision["capability_name"],
        "capability_summary": decision["capability_summary"],
        "target_bindings": [
            {
                "path": path,
                "role": "primary" if index == 0 else "supporting",
                "required_for_main_path": True,
            }
            for index, path in enumerate(targets)
        ],
        "migration_strategy": "direct_port",
        "main_path_status": "tested_main_path",
        "lifecycle": "productized",
        "owner_unit": OWNER_UNIT,
        "downstream_units": ["M1-06C", "M1-07B", "M1-07C"],
        "dependencies": ["M1-S02C", "M1-S02D", "M1-S04D", "M1-S05C", "M1-S06A", "M1-S06B-01"],
        "source_evidence": [
            {
                "source_repo": decision["source_repo"],
                "source_path": decision["source_path"],
                "exists_in_workspace": True,
                "source_kind": "bounded_module_group",
                "reason": (
                    "Verified against the pinned workspace source graph and the concrete files "
                    "used by the 06B primary/supplementary migration."
                ),
                "symbols": [decision["runtime_function"]],
                "tags": [decision["source_role"], "M1-S06B-02"],
            }
        ],
        "main_path": {
            "surfaces": [
                "runtime_event_curator_ingress",
                "memory_curator_worker",
                "curator_outcome_projection",
                "worker_retrieval_context",
                "curator_downstream_delivery",
            ],
            "event_types": [
                "memory_curator_candidate",
                "memory_curator_accepted",
                "memory_curator_committed",
                "memory_curator_index_published",
            ],
            "api_routes": [
                "GET /tasks/{task_id}/memory/curator",
                "POST /tasks/{task_id}/memory/curator",
                "POST /tasks/{task_id}/memory/curator/task-end",
                "POST /tasks/{task_id}/memory/curator/recover",
            ],
            "control_commands": [],
            "artifact_kinds": ["memory_evidence_bundle", "skill_candidate", "failure_pattern"],
            "worker_runtime": decision["runtime_function"],
            "ui_panels": [],
        },
        "line_count_policy": "counts_as_runtime",
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "recorded",
            "license_hint": "MIT",
            "notice_path": "packages/memory/THIRD_PARTY_NOTICES.md",
            "notes": "Selected mechanisms and revisions are recorded; Zyra owns the modified runtime.",
        },
        "runtime_entry": {
            "module": decision["runtime_module"],
            "function": decision["runtime_function"],
            "protocol": "zyra.memory-curator-integration.v1",
            "health_check": TEST_COMMAND,
            "config_refs": targets,
        },
        "test_entries": [
            {
                "path": "tests/integration/test_memory_curator_worker_integration.py",
                "command": "python -m pytest tests/integration/test_memory_curator_worker_integration.py -q",
                "kind": "integration",
                "expected_signal": (
                    "05C trace ingestion, transactional outcomes, real worker recall, versioned "
                    "delivery, idempotent replay and lease fencing"
                ),
                "required": True,
            },
            {
                "path": "tests/unit/test_memory_curator_worker_foundation.py",
                "command": "python -m pytest tests/unit/test_memory_curator_worker_foundation.py -q",
                "kind": "unit",
                "expected_signal": "validator/committer, contradiction race, job lease and outbox recovery",
                "required": True,
            },
            {
                "path": "tests/integration/test_memory_curator_api_main_path.py",
                "command": "python -m pytest tests/integration/test_memory_curator_api_main_path.py -q",
                "kind": "integration",
                "expected_signal": "API task lifecycle reaches canonical memory and derived index",
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
            "migration_mode": decision["migration_mode"],
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "candidate_owner": "CuratorCandidateStore",
            "integration_owner": "CuratorIntegrationStore",
            "retrieval_index_is_derived": True,
            "typescript_has_database_access": False,
            "model_can_write": False,
            "root_source_runtime_dependency": False,
        },
    }


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict) and isinstance(document.get("entries"), list):
        entries = document["entries"]
    else:
        raise ValueError("internalization ledger seed must be a list or contain entries")
    replacements = {item["ledger_id"]: item for item in (_entry(item) for item in DECISIONS)}
    output = [replacements.pop(str(item.get("ledger_id") or ""), item) for item in entries]
    output.extend(replacements.values())
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    expected = (
        output
        if isinstance(document, list)
        else {
            **document,
            "entries": output,
            "summary": to_jsonable(InternalizationLedger(typed).summary()),
        }
    )
    aligned = _canonical(document) == _canonical(expected)
    if write and not aligned:
        path.write_text(_canonical(expected), encoding="utf-8", newline="\n")
        aligned = True
    return aligned, len(DECISIONS)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    aligned, count = synchronize(args.ledger.resolve(), write=args.write or not args.check)
    print(f"memory_curator_integration_source_ledger_aligned={str(aligned).lower()}")
    print(f"memory_curator_integration_source_decision_count={count}")
    print(f"memory_curator_integration_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
