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


OWNER_UNIT = "M1-S06A-02"
DEFAULT_LEDGER = ROOT / "packages/integrations/zyra_integrations/data/internalization_ledger_seed.json"

DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "agentscope",
        "source_commit": "b6698c5dbaa1aa916925e27402767f45e2405fa4",
        "source_path": (
            "src/agentscope/app/_service/_index_worker.py;"
            "src/agentscope/app/_service/_index_task_consumer.py;"
            "src/agentscope/app/_service/_index_sweeper.py;"
            "src/agentscope/rag/_knowledge.py"
        ),
        "capability_name": "retrieval_index_durable_admission_worker_context_integration",
        "capability_summary": (
            "Durable source admission, claim/heartbeat/sweep, fenced process publication and "
            "current-snapshot delivery are integrated into CodeWorker context and API paths."
        ),
        "source_role": "primary_implementation",
        "migration_strategy": "direct_port",
        "migration_mode": "cropped_migration_same_language_module_integration",
        "runtime_module": "zyra_workers.retrieval_context_runtime",
        "runtime_function": "WorkerRetrievalContextRuntime.prepare",
        "target_paths": [
            "packages/memory/zyra_memory/integration_runtime.py",
            "packages/memory/zyra_memory/integration_store.py",
            "packages/memory/zyra_memory/process_supervisor.py",
            "packages/code_index/zyra_code_index/jobs.py",
            "packages/code_index/zyra_code_index/worker.py",
            "packages/code_index/zyra_code_index/process_supervisor.py",
            "packages/code_index/zyra_code_index/integration.py",
            "packages/code_index/zyra_code_index/service.py",
            "packages/workers/zyra_workers/retrieval_context_runtime.py",
            "packages/workers/zyra_workers/code_worker_runtime.py",
            "apps/api/zyra_api/main.py",
        ],
        "rationale": (
            "AgentScope remains the single primary RAG/KB job-lifecycle source. Zyra adds atomic "
            "generation fencing, WorkspaceManager revision guards and QueryEngine message delivery; "
            "no AgentScope service, database or process is a runtime dependency."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/mnemopi/src/core/mmr.ts;packages/mnemopi/src/core/query-intent.ts;"
            "packages/mnemopi/src/core/temporal-parser.ts;"
            "packages/mnemopi/src/core/polyphonic-recall.ts"
        ),
        "capability_name": "retrieval_filter_polyphonic_conformance_and_recovery_refs",
        "capability_summary": (
            "Bounded filter contracts, lexical/FTS+MMR/polyphonic comparison, deterministic "
            "conformance and reference-only recovery checkpoints supplement the primary lifecycle."
        ),
        "source_role": "supplementary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "migration_mode": "bounded_cross_language_mechanism_port",
        "runtime_module": "zyra_memory.integration_runtime",
        "runtime_function": "RetrievalIntegrationRuntime.execute",
        "target_paths": [
            "packages/memory/zyra_memory/query_contract.py",
            "packages/memory/zyra_memory/integration_runtime.py",
            "packages/memory/zyra_memory/conformance.py",
            "packages/code_index/zyra_code_index/conformance.py",
            "packages/workers/zyra_workers/retrieval_context_runtime.py",
        ],
        "rationale": (
            "Only ranking/filter/recovery-reference gaps supplement AgentScope. OMP storage, "
            "embedding ownership and agent/runtime control flow are excluded, so no second "
            "canonical memory or worker owner is introduced."
        ),
    },
)


def ledger_id(decision: dict[str, Any]) -> str:
    identity = "|".join(
        (decision["source_repo"], decision["source_path"], decision["capability_name"])
    )
    return "ledger_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    targets = list(decision["target_paths"])
    tests = (
        "tests/unit/test_retrieval_index_adapters_integration.py",
        "tests/unit/test_code_index_adapters_integration.py",
        "tests/integration/test_code_index_process_worker.py",
        "tests/integration/test_code_index_patch_event_integration.py",
        "tests/integration/test_code_worker_retrieval_context_main_path.py",
    )
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
            "surfaces": [
                "memory_index_process_worker",
                "code_index_process_worker",
                "workspace_patch_invalidation",
                "code_worker_preprocessed_messages",
                "code_worker_file_test_selection",
                "runtime_event_spine",
            ],
            "event_types": [
                "retrieval_index.source_admitted",
                "retrieval_index.query_snapshot",
                "code_index.queued",
                "code_index.leased",
                "code_index.building",
                "code_index.publishing",
                "code_index.ready",
            ],
            "api_routes": ["POST /tasks/{task_id}/workers/code", "POST /tasks/{task_id}/agents"],
            "control_commands": [],
            "artifact_kinds": ["memory_source_ref", "code_source_ref", "test_selection"],
            "worker_runtime": decision["runtime_function"],
            "ui_panels": [],
        },
        "line_count_policy": "counts_as_runtime",
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "recorded",
            "license_hint": "Apache-2.0" if decision["source_repo"] == "agentscope" else "MIT",
            "notice_path": "packages/memory/THIRD_PARTY_NOTICES.md",
            "notes": "Selected mechanisms are modified and maintained inside Zyra-owned packages.",
        },
        "runtime_entry": {
            "module": decision["runtime_module"],
            "function": decision["runtime_function"],
            "protocol": "zyra-retrieval-index-integration-v1",
            "health_check": "python -m pytest " + " ".join(tests) + " -q",
            "config_refs": targets,
        },
        "test_entries": [
            {
                "path": path,
                "command": f"python -m pytest {path} -q",
                "kind": "integration" if "/integration/" in path else "unit",
                "expected_signal": "durable current-revision retrieval changes worker context and fails closed when disconnected",
                "required": True,
            }
            for path in tests
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
            "canonical_memory_owner": "SQLiteStore/MemoryRecordStore",
            "canonical_workspace_owner": "WorkspaceManagerRuntime+WorkspaceFileRevision",
            "derived_index_owner": "MemoryIndexRuntime+CodeIndexRuntime",
            "checkpoint_reference_only": True,
            "root_source_runtime_dependency": False,
            "langgraph_production_owner": False,
        },
    }


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    current = json.loads(path.read_text(encoding="utf-8"))
    entries = current if isinstance(current, list) else current["entries"]
    replacements = {
        item["capability_name"]: item for item in map(entry, DECISIONS)
    }
    output = []
    for item in entries:
        capability = str(item.get("capability_name") or "")
        if str(item.get("owner_unit") or "") == OWNER_UNIT and capability in replacements:
            output.append(replacements.pop(capability))
        else:
            output.append(item)
    output.extend(replacements.values())
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    expected: Any = output if isinstance(current, list) else {
        **current,
        "entries": output,
        "summary": to_jsonable(InternalizationLedger(typed).summary()),
    }
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
    print(f"retrieval_index_integration_source_ledger_aligned={str(aligned).lower()}")
    print(f"retrieval_index_integration_source_decision_count={count}")
    print(f"retrieval_index_integration_owner_unit={OWNER_UNIT}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
