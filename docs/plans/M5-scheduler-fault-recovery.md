# M5 Scheduler, Fault Injection, And Recovery

M5 is the first backend-heavy internalization pass for edge/local/cloud resource scheduling and runtime fault recovery. It builds on M2 worker runtimes, M3 symbolic control, and M4 memory/trajectory rather than replacing them with a standalone scheduler.

## Source-To-Target Ledger

| Source | Internalized capability | Zyra target paths | Runtime path |
| --- | --- | --- | --- |
| `OpenHands` | workspace gateway, runtime event envelope, backend health shape | `packages/scheduler/zyra_scheduler/backends.py`, `pool.py`, `apps/api/zyra_api/main.py` | dispatch envelope, scheduler health API |
| `openclaw` | active-memory recovery, fault injection, trajectory-preserving recovery route | `packages/scheduler/zyra_scheduler/recovery.py`, `scheduler.py`, `packages/symbolic/zyra_symbolic/control.py` | `/inject`, recovery node, event log |
| `agentscope` | worker manifest registry, capability/tool/resource matching | `packages/scheduler/zyra_scheduler/pool.py`, `scheduler.py` | `WorkerManifest`, `WorkerPool`, `ResourceScheduler` |
| `browser-use` | browser worker profile, simulated edge backend, browser failure classification | `packages/scheduler/zyra_scheduler/watchdog.py`, `pool.py`, `packages/workers/zyra_workers/browser_worker.py` | `BrowserWorker` route and watchdog signals |
| `agent-framework` | middleware-style routing signals, compact-aware recovery context | `packages/scheduler/zyra_scheduler/recovery.py`, `scheduler.py`, `packages/memory/zyra_memory/fabric.py` | scheduler signals, compact/trajectory memory |
| `claude-code-best` | permission-governed code runtime, command-controlled runtime, watchdog-friendly tool metadata | `packages/runtime/zyra_runtime/permissions.py`, `packages/runtime/zyra_runtime/workers.py`, `packages/scheduler/zyra_scheduler/*`, `packages/commands/zyra_commands/registry.py` | `CodeWorkerRuntime`, `/scheduler`, `/doctor`, `/usage` |

The same ledger is available at runtime through `zyra_scheduler.source_to_target_ledger()` and `GET /scheduler/manifests`.

## Implemented Runtime Surface

- `packages/scheduler/zyra_scheduler` now contains `WorkerManifest`, `WorkerPool`, `ResourceScheduler`, `WorkerBackendGateway`, `RuntimeWatchdog`, and `RecoveryPlanner`.
- `TopologyRouter` uses `ResourceScheduler` when the scheduler package is available. The selected runtime worker still remains `CodeWorkerRuntime` or `BrowserWorker`, but the chosen manifest/backend/location/model split is recorded in the node and decision metadata.
- Route stages emit `topology_route` plus `resource_decision` events.
- Execute stages attach dispatch envelope metadata to `WorkerRequest`, including manifest id, backend, location, sandbox, gateway, and model split.
- Worker runtime exceptions or failed worker results are classified by `RuntimeWatchdog` and passed to `RecoveryPlanner`.
- `/change` and `/inject` now produce resource decision evidence when scheduler is available; `/inject` also records `recovery_planned` events and `metadata.recovery_plans`.
- `MemoryFabric` preserves and summarizes `resource_decision`, `recovery_planned`, and `worker_health` events, so failures, requirement changes, compact history, and trajectory records can influence later scheduling.
- API endpoints expose scheduler state: `GET /scheduler/manifests`, `GET /scheduler/health`, `GET /tasks/{task_id}/scheduler`, and `GET /tasks/{task_id}/recovery`.
- Slash command surface includes `/scheduler`; `/agents`, `/doctor`, `/usage`, `/change`, and `/inject` return scheduler/recovery context.
- Web console has an M5 Scheduler panel connected to real scheduler and recovery APIs.

## Verification

Primary command:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m5.py
```

Focused tests:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.unit.test_scheduler
.\.venv\Scripts\python.exe -m unittest tests.scenarios.test_m5_scheduler_fault_recovery
.\.venv\Scripts\python.exe -m unittest tests.integration.test_scheduler_api
```

Regression checks:

```powershell
.\.venv\Scripts\python.exe scripts\verify_m4.py
.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## Critical Self-Check

- M5 is not only a `WorkerManifest` dataclass: the scheduler now mutates route/execute node metadata, worker selection, event log records, API responses, command results, and Web data.
- Fault injection no longer stops at `node_failed`; it creates a recovery node, a resource-aware route, and a recovery plan.
- Memory-to-routing is present in the first version: `ResourceScheduler` consumes failure injections, requirement changes, compact counts, trajectory mentions, event history, and persisted memory records.
- The current implementation is still a first backend integration pass. It does not yet provide real remote cloud workers, real Docker sandbox isolation, or full Claude Code AgentTool/MCP/SkillTool deep execution. Those remain M6/M7/second-stage debt and should not be misread as complete.
