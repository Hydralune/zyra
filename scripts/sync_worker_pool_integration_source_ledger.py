from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
for package_path in (ROOT / "packages" / "core", ROOT / "packages" / "integrations"):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

import sync_worker_pool_foundation_source_ledger as foundation  # noqa: E402
from zyra_integrations import InternalizationLedger, InternalizationLedgerEntry  # noqa: E402
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402


OWNER_UNIT = "M1-S07A-02"
DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
TEST_COMMAND = (
    "python -m pytest -q tests/integration/test_worker_pool_integration_runtime.py "
    "tests/integration/test_worker_pool_api_main_path.py; "
    "bun test packages/runtime/claude-runtime/test/omp-worker-control.test.ts"
)


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "ledger_id": "ledger_07a02_agentscope_worker_integration",
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
            "persist control intent before cancel/wakeup side effects",
            "single-flight session admission and active-run recovery",
            "inbox-before-wakeup ordering and restart-safe dispatch",
            "canonical lease recheck immediately before physical execution",
            "graceful stop convergence after the final in-flight lease",
        ],
        "source_tests": [
            "tests/app/service_wakeup_dispatcher_test.py",
            "tests/app/service_cancel_dispatcher_test.py",
            "tests/app/service_inbox_middleware_test.py",
        ],
        "capability_name": "worker_pool_admission_renewal_control_checkpoint_projection_and_recovery_handoff",
        "capability_summary": (
            "AgentScope lifecycle ordering is productized into one Zyra SQLite transaction domain "
            "for capacity admission, progress-coupled renewal, durable cancel/drain/wake, exact "
            "restart correlation, M2 cursor projection and 07C lifecycle evidence."
        ),
        "target_paths": [
            "packages/scheduler/zyra_scheduler/worker_pool/integration.py",
            "packages/scheduler/zyra_scheduler/worker_pool/integration_store.py",
            "packages/scheduler/zyra_scheduler/worker_pool/admission.py",
            "packages/scheduler/zyra_scheduler/worker_pool/application.py",
            "packages/scheduler/zyra_scheduler/worker_pool/leases.py",
            "packages/scheduler/zyra_scheduler/worker_pool/renewal.py",
            "packages/scheduler/zyra_scheduler/worker_pool/control.py",
            "packages/scheduler/zyra_scheduler/worker_pool/execution_gate.py",
            "packages/scheduler/zyra_scheduler/worker_pool/inbox.py",
            "packages/scheduler/zyra_scheduler/worker_pool/lifecycle.py",
            "packages/scheduler/zyra_scheduler/worker_pool/store.py",
            "packages/scheduler/zyra_scheduler/worker_pool/checkpoint.py",
            "packages/scheduler/zyra_scheduler/worker_pool/invariants.py",
            "packages/scheduler/zyra_scheduler/worker_pool/projection.py",
            "packages/scheduler/zyra_scheduler/worker_pool/recovery_handoff.py",
            "packages/scheduler/zyra_scheduler/worker_pool/health_bridge.py",
            "packages/scheduler/zyra_scheduler/worker_pool/scheduler_bridge.py",
            "apps/api/zyra_api/worker_pool_api.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "primary_implementation",
        "migration_mode": "cropped_same_language_migration_with_zyra_cas_lease_graph_and_checkpoint_ownership",
        "runtime_module": "zyra_scheduler.worker_pool",
        "runtime_function": "WorkerPoolIntegrationRuntime",
        "event_types": [
            "worker_dispatch_started",
            "worker_dispatch_progress",
            "worker_typed_yield",
            "worker_dispatch_terminal",
            "control_command",
            "worker_health",
        ],
        "api_routes": [
            "GET /worker-pool/integration",
            "GET /worker-pool/handoff",
            "GET /worker-pool/recovery-handoff",
            "POST /worker-pool/checkpoints/{run_id}",
            "POST /worker-pool/health/sweep",
            "POST /tasks/{task_id}/worker-pool-control",
            "POST /tasks/{task_id}/cancel",
            "POST /tasks/{task_id}/subagents",
        ],
        "state_owner": (
            "WorkerPoolStore and WorkerPoolIntegrationRepository own physical attempt, lease, "
            "binding, renewal, control, yield and checkpoint durability; TypeScript 03D owns the "
            "logical task and GraphStateCustody owns immutable topology revisions"
        ),
        "rationale": (
            "The integration preserves AgentScope's control-before-effect and wakeup ordering but "
            "replaces process registries and bus globals with Zyra CAS rows, lease fences, typed "
            "foreign refs, exact checkpoint correlation and canonical journal projection."
        ),
    },
    {
        "ledger_id": "ledger_07a02_omp_session_runtime",
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_path": (
            "packages/coding-agent/src/task/index.ts;"
            "packages/coding-agent/src/task/executor.ts;"
            "packages/coding-agent/src/task/parallel.ts;"
            "packages/coding-agent/src/task/provider-concurrency.ts;"
            "packages/coding-agent/src/async/job-manager.ts;packages/roboomp/src/**"
        ),
        "source_language": "typescript",
        "target_language": "typescript",
        "source_symbols": [
            "TaskTool.execute",
            "Semaphore",
            "mapWithConcurrencyLimit",
            "AsyncJobManager",
            "park/revive",
            "restart requeue",
        ],
        "source_callsites": [
            "shared session capacity across foreground and background jobs",
            "park releases a permit and revive reacquires it",
            "typed yield exactly once before terminal settlement",
            "process restart rehydrates only checksummed Python physical projections",
        ],
        "source_tests": [
            "packages/coding-agent/test/task/task-batch.test.ts",
            "packages/coding-agent/test/async-job-manager.test.ts",
            "roboomp queue claim/restart tests recorded in source graph batch-06",
        ],
        "capability_name": "omp_shared_session_admission_park_revive_typed_yield_and_edge_execution_projection",
        "capability_summary": (
            "OMP semaphore, foreground/background, park/revive and AsyncJob mechanics remain in "
            "TypeScript as a process-local supplement that gates real E03 execution and rehydrates "
            "from the Python canonical lease projection without becoming a second store."
        ),
        "target_paths": [
            "packages/runtime/claude-runtime/src/omp-worker-control/session-runtime.ts",
            "packages/runtime/claude-runtime/src/omp-worker-control/dispatch-runtime.ts",
            "packages/runtime/claude-runtime/src/omp-worker-control/contracts.ts",
            "packages/workers/zyra_workers/edge_pool/integration.py",
            "apps/api/zyra_api/main.py",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "cropped_same_language_session_control_with_read_only_python_lease_projection",
        "runtime_module": "@zyra/claude-runtime/omp-worker-control",
        "runtime_function": "OmpSessionAdmissionRuntime",
        "event_types": ["worker_dispatch_progress", "worker_typed_yield", "artifact_written"],
        "api_routes": ["POST /tasks/{task_id}/subagents", "POST /tasks/{task_id}/subagents/fanout"],
        "state_owner": (
            "TypeScript OmpSessionAdmissionRuntime owns only process-local permits and projections; "
            "Python WorkerPoolStore remains the sole durable physical owner and edge-only routing "
            "cannot fall back to a local OMP process"
        ),
        "rationale": (
            "The original-language supplement fills shared session concurrency, promotion, "
            "park/revive and typed-yield mechanics while all durable lease, fence, checkpoint and "
            "recovery decisions remain in the AgentScope-derived Python primary."
        ),
    },
)


def _entry(decision: Mapping[str, Any]) -> dict[str, Any]:
    entry = foundation._entry(decision)
    entry["owner_unit"] = OWNER_UNIT
    entry["downstream_units"] = ["M1-S07B-01", "M1-S07C", "M2-worker-pool-panel"]
    entry["dependencies"] = ["M1-S03D", "M1-S05A", "M1-S05B", "M1-S05C", "M1-S05D", "M1-S06C", "M1-S07A-01"]
    entry["runtime_entry"]["health_check"] = TEST_COMMAND
    entry["test_entries"] = [
        {
            "path": "tests/integration/test_worker_pool_integration_runtime.py",
            "command": "python -m pytest -q tests/integration/test_worker_pool_integration_runtime.py",
            "kind": "integration",
            "expected_signal": "capacity, renewal, cancel, drain/wake, exact restart, single yield and real edge failover mutate canonical state",
            "required": True,
        },
        {
            "path": "tests/integration/test_worker_pool_api_main_path.py",
            "command": "python -m pytest -q tests/integration/test_worker_pool_api_main_path.py",
            "kind": "main_path",
            "expected_signal": "task/subagent APIs use integrated physical admission and durable control without fallback",
            "required": True,
        },
        {
            "path": "packages/runtime/claude-runtime/test/omp-worker-control.test.ts",
            "command": "bun test packages/runtime/claude-runtime/test/omp-worker-control.test.ts",
            "kind": "original_language_behavior",
            "expected_signal": "shared session concurrency, park/revive, typed yield, restart rehydration and disable failure execute in TypeScript",
            "required": True,
        },
    ]
    entry["main_path"]["surfaces"] = [
        "scheduler_capacity_admission",
        "task_and_subagent_api",
        "durable_worker_control",
        "canonical_pre_execution_lease_gate",
        "graceful_stop_after_drain",
        "real_edge_process_failover",
        "m2_worker_state_handoff",
        "m1_07c_recovery_handoff",
    ]
    entry["main_path"]["control_commands"] = [
        "cancel",
        "drain",
        "wake",
        "stop",
        "park",
        "revive",
    ]
    entry["source_evidence"][0]["reason"] = "Pinned source graph and concrete source files verified for M1-S07A-02."
    entry["source_evidence"][0]["tags"] = [decision["source_role"], OWNER_UNIT]
    entry["notes"] = entry["notes"].replace("M1-S07A-01", OWNER_UNIT)
    entry["metadata"]["owner_unit"] = OWNER_UNIT
    entry["metadata"]["openclaw_forward_excluded"] = True
    return entry


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    foundation._validate_decisions(DECISIONS)
    document = json.loads(path.read_text(encoding="utf-8"))
    entries = document if isinstance(document, list) else document.get("entries")
    if not isinstance(entries, list):
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
    print(f"worker_pool_integration_source_ledger_aligned={str(aligned).lower()}")
    print(f"worker_pool_integration_source_decision_count={count}")
    print(f"worker_pool_integration_source_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={arguments.ledger.resolve()}")
    return 0 if aligned or not arguments.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
