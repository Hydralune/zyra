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


OWNER_UNIT = "M1-S06B-01"
DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
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
        "capability_name": "memory_curator_worker_evidence_decision_validation_commit_lifecycle",
        "capability_summary": (
            "Serialized lifecycle work, pre-compress evidence extraction, protected trajectory "
            "boundaries, failure isolation and deterministic model-unavailable degradation are "
            "adapted into the Zyra MemoryCuratorWorker and transactional commit path."
        ),
        "target_paths": [
            "packages/memory/zyra_memory/curator_models.py",
            "packages/memory/zyra_memory/curator_store.py",
            "packages/memory/zyra_memory/curator_evidence.py",
            "packages/memory/zyra_memory/curator_decision.py",
            "packages/memory/zyra_memory/curator_validation.py",
            "packages/memory/zyra_memory/curator_commit.py",
            "packages/memory/zyra_memory/curator_runtime.py",
            "packages/workers/zyra_workers/memory_curator.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "primary_implementation",
        "migration_strategy": "direct_port",
        "migration_mode": "cropped_migration_same_language_module_integration",
        "runtime_module": "zyra_memory.curator_runtime",
        "runtime_function": "MemoryCuratorWorker",
        "license_hint": "MIT",
        "rationale": (
            "Hermes supplies the primary worker/lifecycle and trajectory curation mechanisms. "
            "Zyra replaces file memory, provider registry and session storage with task-bound "
            "evidence, candidate/decision stores, deterministic validation, MemoryFabric ownership "
            "and atomic event/index outbox writes. No Hermes runtime or parent path is loaded."
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
        "capability_name": "memory_candidate_job_lease_watermark_and_consolidation_state_machine",
        "capability_summary": (
            "Ownership token, lease, heartbeat, retry and input/success watermark mechanics plus "
            "explicit duplicate/contradiction/supersede candidate consolidation supplement the "
            "Hermes-derived worker lifecycle in a retained TypeScript runtime."
        ),
        "target_paths": [
            "packages/memory/curator-state-machine/src/types.ts",
            "packages/memory/curator-state-machine/src/job-state-machine.ts",
            "packages/memory/curator-state-machine/src/consolidation.ts",
            "packages/memory/curator-state-machine/src/protocol.ts",
            "packages/memory/curator-state-machine/src/main.ts",
            "packages/memory/zyra_memory/curator_typescript_port.py",
            "packages/memory/zyra_memory/curator_store.py",
            "packages/memory/zyra_memory/curator_decision.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_strategy": "direct_port",
        "migration_mode": "cropped_migration_same_language_module_integration",
        "runtime_module": "zyra_memory.curator_typescript_port",
        "runtime_function": "TypeScriptCuratorStatePort",
        "license_hint": "MIT",
        "rationale": (
            "Only the bounded job/collision state machine is retained in TypeScript. The process "
            "receives JSON candidate projections, no database path, and has no canonical write "
            "authority. OMP SQLite/file memory, model-to-file writes, embeddings and agent loop are "
            "excluded; Python validator/committer rechecks every result."
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
    test_command = (
        "python -m pytest tests/unit/test_memory_curator_worker_foundation.py "
        "tests/integration/test_memory_curator_api_main_path.py -q"
    )
    return {
        "ledger_id": ledger_id(decision),
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
        "migration_strategy": decision["migration_strategy"],
        "main_path_status": "tested_main_path",
        "lifecycle": "productized",
        "owner_unit": OWNER_UNIT,
        "main_path": {
            "surfaces": [
                "memory_curator_worker",
                "task_end_scheduler",
                "memory_curator_api",
                "memory_fabric_commit",
                "retrieval_index_outbox",
            ],
            "event_types": [
                "memory_curator_scheduled",
                "memory.candidate.proposed",
                "memory.candidate.validated",
                "memory_curator_committed",
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
            "license_hint": decision["license_hint"],
            "notice_path": "packages/memory/THIRD_PARTY_NOTICES.md",
            "notes": (
                "Selected mechanisms and revisions are recorded; Zyra owns the modified runtime "
                "and has no runtime dependency on the parent repository."
            ),
        },
        "runtime_entry": {
            "module": decision["runtime_module"],
            "function": decision["runtime_function"],
            "protocol": "zyra.memory-curator.v1",
            "health_check": test_command,
            "config_refs": targets,
        },
        "test_entries": [
            {
                "path": "tests/unit/test_memory_curator_worker_foundation.py",
                "command": "python -m pytest tests/unit/test_memory_curator_worker_foundation.py -q",
                "kind": "unit",
                "expected_signal": (
                    "real evidence extraction, validation, lease fencing, CAS commit, conflict and outbox recovery"
                ),
                "required": True,
            },
            {
                "path": "tests/integration/test_memory_curator_api_main_path.py",
                "command": "python -m pytest tests/integration/test_memory_curator_api_main_path.py -q",
                "kind": "integration",
                "expected_signal": "HTTP trace to candidate, canonical memory, event and retrieval index",
                "required": True,
            },
            {
                "path": "packages/memory/curator-state-machine/test/state-machine.test.ts",
                "command": "bun test ./packages/memory/curator-state-machine/test/state-machine.test.ts",
                "kind": "unit",
                "expected_signal": "TypeScript ownership-token/watermark and explicit consolidation behavior",
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
            "typescript_has_database_access": False,
            "model_can_write": False,
            "atomic_memory_event_index_outbox": True,
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
    aligned, count = synchronize(args.ledger.resolve(), write=args.write or not args.check)
    print(f"memory_curator_source_ledger_aligned={str(aligned).lower()}")
    print(f"memory_curator_source_decision_count={count}")
    print(f"memory_curator_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
