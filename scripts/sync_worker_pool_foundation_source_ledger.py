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


OWNER_UNIT = "M1-S07A-01"
DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
TEST_COMMAND = (
    "python -m pytest -q tests/unit/test_worker_pool_foundation.py "
    "tests/unit/test_dynamic_graph_custody.py "
    "tests/integration/test_edge_worker_resource_pool.py "
    "tests/integration/test_worker_pool_api_main_path.py; "
    "bun test packages/runtime/claude-runtime/test/omp-worker-control.test.ts"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "ledger_id": "ledger_23d67419641adcd4",
        "source_repo": "agentscope",
        "source_commit": "b6698c5dbaa1aa916925e27402767f45e2405fa4",
        "source_path": (
            "src/agentscope/app/_manager/_chat_run_registry.py;"
            "src/agentscope/app/_manager/_wakeup_dispatcher.py;"
            "src/agentscope/app/_manager/_cancel_dispatcher.py;"
            "src/agentscope/app/_manager/_background_task_manager.py;"
            "src/agentscope/app/middleware/_inbox_middleware.py;"
            "src/agentscope/app/_service/_session.py"
        ),
        "source_language": "python",
        "target_language": "python",
        "source_symbols": [
            "ChatRunRegistry",
            "WakeupDispatcher",
            "CancelDispatcher",
            "BackgroundTaskManager",
            "InboxMiddleware",
            "SessionService",
        ],
        "source_callsites": [
            "durable inbox to wakeup dispatcher to single-flight chat run",
            "session cancellation and background result reinjection",
            "session delete cancellation before storage and bus cleanup",
        ],
        "source_tests": [
            "tests/app/service_wakeup_dispatcher_test.py",
            "tests/app/service_cancel_dispatcher_test.py",
            "tests/app/service_inbox_middleware_test.py",
            "tests/app/tool_offload_middleware_test.py",
        ],
        "capability_name": "durable_physical_worker_lifecycle_lease_heartbeat_inbox_and_cancel",
        "capability_summary": (
            "AgentScope lifecycle, inbox-before-wakeup, active-session requeue, single-flight and "
            "cancel ordering are cropped into a Zyra-owned physical worker pool with SQLite "
            "attempt/lease fencing, telemetry, execution receipts and explicit recovery signals."
        ),
        "target_paths": [
            "packages/scheduler/zyra_scheduler/worker_pool/store.py",
            "packages/scheduler/zyra_scheduler/worker_pool/lifecycle.py",
            "packages/scheduler/zyra_scheduler/worker_pool/leases.py",
            "packages/scheduler/zyra_scheduler/worker_pool/heartbeat.py",
            "packages/scheduler/zyra_scheduler/worker_pool/inbox.py",
            "packages/scheduler/zyra_scheduler/worker_pool/cancellation.py",
            "packages/scheduler/zyra_scheduler/worker_pool/recovery.py",
            "packages/scheduler/zyra_scheduler/worker_pool/application.py",
            "apps/api/zyra_api/worker_pool_api.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "cropped_same_language_migration_with_zyra_schema_event_and_transaction_ownership",
        "runtime_module": "zyra_scheduler.worker_pool",
        "runtime_function": "WorkerPoolFoundationRuntime",
        "event_types": [
            "worker_health",
            "resource_decision",
            "control_command",
            "constraint_check",
            "artifact_written",
        ],
        "api_routes": [
            "GET /worker-pool",
            "GET /worker-pool/workers",
            "GET /worker-pool/leases",
            "GET /worker-pool/health",
            "POST /tasks/{task_id}/worker-pool-lease",
            "POST /tasks/{task_id}/worker-pool-cancel",
            "POST /tasks",
            "POST /tasks/{task_id}/cancel",
        ],
        "state_owner": (
            "WorkerPoolStore owns WorkerInstance, TaskAttempt, WorkerLease, heartbeat telemetry, "
            "inbox/wakeup and execution receipts; 03D remains logical AgentTask owner"
        ),
        "rationale": (
            "The source lifecycle ordering is retained while AgentScope bus/process globals are "
            "replaced by Zyra SQLite transactions, compare-and-swap versions, fence epochs and "
            "canonical event projection."
        ),
    },
    {
        "ledger_id": "ledger_38a2f75c0e47f1b3",
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/coding-agent/src/task/index.ts;"
            "packages/coding-agent/src/task/executor.ts;"
            "packages/coding-agent/src/task/parallel.ts;"
            "packages/coding-agent/src/task/provider-concurrency.ts;"
            "packages/coding-agent/src/async/job-manager.ts;"
            "packages/coding-agent/src/registry/agent-registry.ts;"
            "packages/coding-agent/test/task/**;packages/roboomp/src/**"
        ),
        "source_language": "typescript",
        "target_language": "typescript",
        "source_symbols": [
            "TaskTool.execute",
            "runSubprocess",
            "Semaphore",
            "mapWithConcurrencyLimit",
            "AsyncJobManager",
            "AgentRegistry",
            "WorkerPool claim",
            "restart requeue",
        ],
        "source_callsites": [
            "foreground/background task execution and structured yield",
            "park/revive/drain and session semaphore",
            "BEGIN IMMEDIATE external-worker claim and restart requeue",
        ],
        "source_tests": [
            "packages/coding-agent/test/task/task-batch.test.ts",
            "packages/coding-agent/test/task/task-spawn.test.ts",
            "packages/coding-agent/test/async-job-manager.test.ts",
            "packages/coding-agent/test/sdk-async-job-manager-singleton.test.ts",
            "roboomp queue claim/restart behavior from source graph batch-06",
        ],
        "capability_name": "physical_attempt_progress_drain_takeover_and_independent_edge_protocol",
        "capability_summary": (
            "OMP attempt/progress/park/revive/drain and durable external-worker claim semantics "
            "supplement the AgentScope lifecycle with explicit attempt takeover, authenticated "
            "independent edge IPC, gateway artifact return and no-fallback edge-only failure."
        ),
        "target_paths": [
            "packages/runtime/claude-runtime/src/omp-worker-control/contracts.ts",
            "packages/runtime/claude-runtime/src/omp-worker-control/semaphore.ts",
            "packages/runtime/claude-runtime/src/omp-worker-control/job-manager.ts",
            "packages/runtime/claude-runtime/src/omp-worker-control/dispatch-runtime.ts",
            "packages/runtime/claude-runtime/src/tasks/executor.ts",
            "packages/runtime/claude-runtime/src/agents/agent-tool.ts",
            "packages/scheduler/zyra_scheduler/worker_pool/models.py",
            "packages/workers/zyra_workers/edge_pool/protocol.py",
            "packages/workers/zyra_workers/edge_pool/server.py",
            "packages/workers/zyra_workers/edge_pool/connector.py",
            "packages/workers/zyra_workers/edge_pool/runtime.py",
            "apps/api/zyra_api/worker_pool_api.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_same_language_worker_control_with_python_canonical_lease_projection",
        "runtime_module": "@zyra/claude-runtime/omp-worker-control",
        "runtime_function": "OmpWorkerDispatchRuntime",
        "event_types": ["worker_health", "resource_decision", "artifact_written", "node_failed"],
        "api_routes": ["GET /worker-pool/workers", "GET /worker-pool/health"],
        "state_owner": (
            "Python WorkerPoolStore owns physical attempt/lease/receipt durability; TypeScript "
            "OmpWorkerDispatchRuntime owns only process-local admission, semaphore and AsyncJob "
            "projection; no upstream OMP AgentRegistry, JSONL, SQLite, RPC process or global "
            "registry is a runtime dependency"
        ),
        "rationale": (
            "OMP TaskTool concurrency and AsyncJob park/revive/drain control flow is cropped in "
            "its original TypeScript and gates the real E03 child execution using a read-only "
            "Python lease projection. Logical task trees stay 03D-owned, durable physical leases "
            "stay WorkerPoolStore-owned and worktree state stays 05A-owned."
        ),
    },
)


def _ledger_id(decision: Mapping[str, Any]) -> str:
    explicit = str(decision.get("ledger_id") or "").strip()
    if explicit:
        return explicit
    identity = "|".join(
        (str(decision["source_repo"]), str(decision["source_path"]), str(decision["capability_name"]))
    )
    return "ledger_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _entry(decision: Mapping[str, Any]) -> dict[str, Any]:
    targets = list(decision["target_paths"])
    return {
        "ledger_id": _ledger_id(decision),
        "source_repo": decision["source_repo"],
        "source_path": decision["source_path"],
        "capability_name": decision["capability_name"],
        "capability_summary": decision["capability_summary"],
        "target_bindings": [
            {"path": path, "role": "primary" if index == 0 else "supporting", "required_for_main_path": True}
            for index, path in enumerate(targets)
        ],
        "migration_strategy": "direct_port",
        "main_path_status": "tested_main_path",
        "lifecycle": "productized",
        "owner_unit": OWNER_UNIT,
        "downstream_units": ["M1-S07A-02", "M1-07C", "M1-08", "M2-worker-pool-panel"],
        "dependencies": ["M1-S03D", "M1-S05A", "M1-S05B", "M1-S05C", "M1-S05D"],
        "source_evidence": [
            {
                "source_repo": decision["source_repo"],
                "source_path": decision["source_path"],
                "exists_in_workspace": True,
                "source_kind": "bounded_module_group",
                "reason": "Pinned source graph and concrete source files verified for M1-S07A-01.",
                "symbols": list(decision["source_symbols"]),
                "tags": [decision["source_role"], OWNER_UNIT],
            }
        ],
        "main_path": {
            "surfaces": [
                "task_api_physical_lease",
                "subagent_physical_mapping",
                "worker_lifecycle",
                "independent_edge_process",
                "dynamic_topology_custody",
            ],
            "event_types": list(decision["event_types"]),
            "api_routes": list(decision["api_routes"]),
            "control_commands": ["cancel", "drain", "wake", "stop"],
            "artifact_kinds": ["execution_receipt", "edge_artifact", "graph_snapshot"],
            "worker_runtime": decision["runtime_function"],
            "ui_panels": [],
        },
        "line_count_policy": "counts_as_runtime",
        "license_notice": {
            "source_repo": decision["source_repo"],
            "status": "recorded",
            "license_hint": "source license retained in repository notice inventory",
            "notice_path": "packages/scheduler/THIRD_PARTY_NOTICES.md",
            "notes": "Cropped and modified lifecycle mechanisms are maintained inside Zyra-owned modules.",
        },
        "runtime_entry": {
            "module": decision["runtime_module"],
            "function": decision["runtime_function"],
            "protocol": "zyra.worker-pool/v1",
            "health_check": TEST_COMMAND,
            "config_refs": targets,
        },
        "test_entries": [
            {
                "path": "tests/unit/test_worker_pool_foundation.py",
                "command": "python -m pytest -q tests/unit/test_worker_pool_foundation.py",
                "kind": "behavior",
                "expected_signal": "worker lifecycle, durable lease, cancellation, inbox, heartbeat and takeover mutate canonical state",
                "required": True,
            },
            {
                "path": "tests/integration/test_edge_worker_resource_pool.py",
                "command": "python -m pytest -q tests/integration/test_edge_worker_resource_pool.py",
                "kind": "integration",
                "expected_signal": "independent authenticated edge process executes, cancels and returns fenced artifact",
                "required": True,
            },
            {
                "path": "tests/integration/test_worker_pool_api_main_path.py",
                "command": "python -m pytest -q tests/integration/test_worker_pool_api_main_path.py",
                "kind": "integration",
                "expected_signal": "default task and subagent APIs acquire physical leases before TypeScript OMP-gated execution, settle receipts and fail closed when the gate is disabled",
                "required": True,
            },
            {
                "path": "packages/runtime/claude-runtime/test/omp-worker-control.test.ts",
                "command": "bun test packages/runtime/claude-runtime/test/omp-worker-control.test.ts",
                "kind": "behavior",
                "expected_signal": "original-language semaphore, bounded fanout, AsyncJob park/revive/cancel and physical dispatch gating mutate TypeScript runtime state",
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
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "source_symbols": list(decision["source_symbols"]),
            "source_callsites": list(decision["source_callsites"]),
            "source_tests": list(decision["source_tests"]),
            "state_owner": decision["state_owner"],
            "logical_task_owner": "M1-S03D TypeScript AgentTaskRuntime",
            "workspace_owner": "M1-S05A WorkspaceManagerRuntime",
            "gateway_owner": "M1-S05B SandboxGatewayRuntime",
            "event_owner": "M1-S05C RuntimeEventStore",
            "backend_owner": "M1-S05D BackendRegistry",
            "graph_owner": "Zyra GraphStateCustody and DynamicTopologyRuntime",
            "langgraph_runtime_dependency": False,
            "omp_runtime_dependency": False,
            "root_source_runtime_dependency": False,
            "openclaw_forward_excluded": True,
        },
    }


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _validate_decisions(decisions: Sequence[Mapping[str, Any]]) -> None:
    """Reject self-consistent ledger data that violates its migration contract."""

    for decision in decisions:
        source_language = str(decision.get("source_language") or "").strip().lower()
        target_language = str(decision.get("target_language") or "").strip().lower()
        migration_mode = str(decision.get("migration_mode") or "").strip().lower()
        if "same_language" in migration_mode and source_language != target_language:
            raise ValueError(
                "same-language source decision has mismatched custody: "
                f"{decision.get('source_repo')} {source_language}->{target_language}"
            )


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    _validate_decisions(DECISIONS)
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict) and isinstance(document.get("entries"), list):
        entries = document["entries"]
    else:
        raise ValueError("internalization ledger seed must be a list or contain entries")
    replacements = {item["ledger_id"]: item for item in (_entry(item) for item in DECISIONS)}
    output: list[dict[str, Any]] = []
    for item in entries:
        ledger_id = str(item.get("ledger_id") or "")
        if str(item.get("owner_unit") or "") == OWNER_UNIT and ledger_id not in replacements:
            continue
        output.append(replacements.pop(ledger_id, item))
    output.extend(replacements.values())
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    expected = (
        output
        if isinstance(document, list)
        else {**document, "entries": output, "summary": to_jsonable(InternalizationLedger(typed).summary())}
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
    arguments = parser.parse_args(argv)
    aligned, count = synchronize(arguments.ledger.resolve(), write=arguments.write or not arguments.check)
    print(f"worker_pool_foundation_source_ledger_aligned={str(aligned).lower()}")
    print(f"worker_pool_foundation_source_decision_count={count}")
    print(f"worker_pool_foundation_source_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={arguments.ledger.resolve()}")
    return 0 if aligned or not arguments.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
