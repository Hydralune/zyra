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


OWNER_UNIT = "M1-S06A-01"
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
        "source_repo": "agentscope",
        "source_commit": "b6698c5dbaa1aa916925e27402767f45e2405fa4",
        "source_path": (
            "src/agentscope/rag/_document.py;src/agentscope/rag/_knowledge.py;"
            "src/agentscope/rag/_chunker/_base.py;"
            "src/agentscope/app/_service/_index_worker.py;"
            "src/agentscope/app/_service/_index_task_consumer.py;"
            "src/agentscope/app/_service/_index_sweeper.py"
        ),
        "capability_name": "retrieval_index_document_job_worker_lifecycle",
        "capability_summary": (
            "Typed index documents, chunk/index boundaries and durable worker-task lifecycle are "
            "adapted into MemoryIndex, staged publication, generation fencing and expired-lease recovery."
        ),
        "target_paths": [
            "packages/memory/zyra_memory/retrieval_models.py",
            "packages/memory/zyra_memory/retrieval_store.py",
            "packages/memory/zyra_memory/index_jobs.py",
            "packages/memory/zyra_memory/index_worker.py",
            "packages/memory/zyra_memory/memory_index.py",
            "packages/memory/zyra_memory/incremental_index.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "primary_implementation",
        "migration_strategy": "direct_port",
        "migration_mode": "cropped_migration_same_language_module_integration",
        "runtime_module": "zyra_memory.memory_index",
        "runtime_function": "MemoryIndexRuntime.synchronize_task",
        "rationale": (
            "AgentScope supplies the primary RAG/index-job lifecycle shape. Zyra replaces live-vector "
            "writes with staging plus atomic publication, adds lease epoch/token fences and retains "
            "SQLiteStore/MemoryRecordStore as canonical memory owner. No AgentScope service or store runs."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/mnemopi/src/core/mmr.ts;packages/mnemopi/src/core/query-intent.ts;"
            "packages/mnemopi/src/core/temporal-parser.ts;"
            "packages/mnemopi/src/core/polyphonic-recall.ts;"
            "packages/mnemopi/src/core/vector-index.ts;packages/mnemopi/src/core/memory.ts"
        ),
        "capability_name": "retrieval_fusion_temporal_intent_and_rebuild_recovery",
        "capability_summary": (
            "Deterministic RRF/MMR fusion, query-intent and temporal signals, explicit vector degradation "
            "and interrupted derived-index rebuild recovery supplement the AgentScope lifecycle."
        ),
        "target_paths": [
            "packages/memory/zyra_memory/retrieval_query.py",
            "packages/memory/zyra_memory/vector_adapter.py",
            "packages/memory/zyra_memory/retrieval_context.py",
            "packages/memory/zyra_memory/skill_memory_index.py",
            "packages/code_index/zyra_code_index/symbols.py",
            "packages/code_index/zyra_code_index/service.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_strategy": "reimplemented_pattern",
        "runtime_module": "zyra_memory.retrieval_query",
        "runtime_function": "combine_retrieval_hits",
        "rationale": (
            "Only bounded ranking, temporal/intent interpretation and rebuild-safety mechanisms fill "
            "specific primary gaps. OMP memory storage, embedding ownership, agent loop and process "
            "runtime are not copied and do not become a second canonical owner."
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
                "memory_fabric",
                "memory_index_worker",
                "retrieval_api",
                "code_index_api",
                "workspace_patch_invalidation",
            ],
            "event_types": [
                "index.queued",
                "index.leased",
                "index.building",
                "index.stale",
                "index.requeued",
                "index.publishing",
                "index.ready",
            ],
            "api_routes": [
                "GET /tasks/{task_id}/memory",
                "POST /tasks/{task_id}/memory/ingest",
                "GET /tasks/{task_id}/code-index",
                "POST /tasks/{task_id}/code-index/rebuild",
                "POST /tasks/{task_id}/code-index/search",
                "POST /tasks/{task_id}/code-index/symbols",
            ],
            "control_commands": ["/memory"],
            "artifact_kinds": ["memory_artifact_link", "code_source_ref"],
            "worker_runtime": decision["runtime_function"],
            "ui_panels": [],
        },
        "line_count_policy": "counts_as_runtime",
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "recorded",
            "license_hint": (
                "Apache-2.0"
                if decision["source_repo"] == "agentscope"
                else "MIT"
            ),
            "notice_path": "packages/memory/THIRD_PARTY_NOTICES.md",
            "notes": (
                "Selected mechanisms and revisions are recorded; Zyra owns the modified runtime "
                "and has no runtime dependency on the parent repository."
            ),
        },
        "runtime_entry": {
            "module": decision["runtime_module"],
            "function": decision["runtime_function"],
            "protocol": "zyra-retrieval-index-v1",
            "health_check": (
                "python -m pytest tests/unit/test_retrieval_index_adapters_foundation.py "
                "tests/integration/test_index_worker_process_recovery.py "
                "tests/unit/test_code_index_foundation.py "
                "tests/integration/test_retrieval_api_main_path.py "
                "tests/integration/test_code_index_api_main_path.py -q"
            ),
            "config_refs": targets,
        },
        "test_entries": [
            {
                "path": "tests/unit/test_retrieval_index_adapters_foundation.py",
                "command": "python -m pytest tests/unit/test_retrieval_index_adapters_foundation.py -q",
                "kind": "unit",
                "expected_signal": "real FTS5 filters, deterministic retrieval, budgets and vector degradation",
                "required": True,
            },
            {
                "path": "tests/integration/test_index_worker_process_recovery.py",
                "command": "python -m pytest tests/integration/test_index_worker_process_recovery.py -q",
                "kind": "integration",
                "expected_signal": "real process kill, lease expiry, stale fencing and restart rebuild",
                "required": True,
            },
            {
                "path": "tests/unit/test_code_index_foundation.py",
                "command": "python -m pytest tests/unit/test_code_index_foundation.py -q",
                "kind": "unit",
                "expected_signal": "workspace-safe discovery, content/symbol queries, budgets and patch invalidation",
                "required": True,
            },
            {
                "path": "tests/integration/test_code_index_api_main_path.py",
                "command": "python -m pytest tests/integration/test_code_index_api_main_path.py -q",
                "kind": "integration",
                "expected_signal": "real WorkspaceManager-bound code index API and fail-closed disable effect",
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
            "migration_mode": decision.get(
                "migration_mode",
                "bounded_cross_language_mechanism_port",
            ),
            "canonical_memory_owner": "SQLiteStore/MemoryRecordStore",
            "canonical_workspace_owner": "WorkspaceManagerRuntime+WorkspaceFileRevision",
            "canonical_index_owner": "Zyra MemoryIndexRuntime+CodeIndexRuntime",
            "index_is_derived": True,
            "atomic_generation_publication": True,
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
    print(f"retrieval_index_source_ledger_aligned={str(aligned).lower()}")
    print(f"retrieval_index_source_decision_count={count}")
    print(f"retrieval_index_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned or not args.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
