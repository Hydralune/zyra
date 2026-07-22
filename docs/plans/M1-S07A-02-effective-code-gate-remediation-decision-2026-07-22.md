# M1-S07A-02 effective-code gate remediation decision (2026-07-22)

## Decision boundary

- Frozen original slice baseline: `8981599da7312f2bfba7b3303498efebb8339e39`.
- Frozen original implementation: `16f24689c1817a1f74fec9afb451d24b97983670`.
- Frozen original evidence: `66b3c42da106a799b65eea3e17a7efcdff0b5f2b`.
- Remediation audit commits: `72732e2167772b306dfd1921912995b48389ed99` and `2bdaa4acd860e62eda705f2cb248cc7483a8a2aa`.
- Prospective remediation baseline: `2bdaa4acd860e62eda705f2cb248cc7483a8a2aa`.
- This document is committed before any remediation production change. It does not rewrite the original evidence or claim that the following decisions existed before the frozen implementation.

The effective-code audit reports `14,646` parent-scope effective production lines against the `15,000` minimum. A line-count shortfall alone does not authorize production work. The remediation may proceed only because the code audit below found four independently testable gaps in the already-required worker lifecycle semantics.

## Observed runtime gaps

### 1. Canonical execution authorization is not rechecked at the immediate run boundary

`_acquire_subagent_physical_dispatch` acquires and starts a lease before returning a signed projection. `_run_typescript_agent_request`, however, enters workspace acquisition and `CodeWorkerRuntime.run` without reloading the binding, lease, attempt, worker generation, manifest, or pending durable control from `WorkerPoolStore`. An `agent_resume` request does not carry the original physical projection at all. A cancel, stop, replacement generation, or manifest change between admission and execution can therefore reach the TypeScript runtime before a later fence check rejects only the final commit.

Required repair: add a Python, canonical-store-backed execution gate and invoke it immediately before `CodeWorkerRuntime.run` for `Agent` and `agent_resume`. The gate must fail closed when disabled, reject unsigned/forged or stale projections, require an active started attempt, respect cancel/stop controls, and permit an existing draining attempt only to finish its already-admitted work. It must not create a second lease or logical task owner.

### 2. Graceful stop does not converge after the last lease settles

`WorkerLifecycleRuntime.stop(force=False)` changes a busy worker to `DRAINING`. Lease completion/cancel/expiry/fence calls `mark_idle_if_unleased`, which intentionally acts only on `BUSY`; there is no durable stop intent and no transition from the drained worker to `STOPPED`. The API can report that stop was applied while the worker remains draining forever.

Required repair: persist stop intent in the existing canonical worker record, drain existing leases/bindings, and deterministically finish `DRAINING -> STOPPING -> STOPPED` after the final lease reaches a terminal state. No new store or state owner is allowed.

### 3. Parent cancellation does not settle child physical leases

The parent task cancel path sends `agent_cancel` to every live TypeScript child, but the 07A control command targets only the parent task id. Child bindings use the child task id. Consequently, a successfully cancelled logical child may retain an active physical lease and consume resource-pool capacity.

Required repair: after each successful logical child cancel, route a child-scoped durable 07A cancel through `WorkerControlRuntime`. A failed physical cancellation must be reported and must not be hidden by the logical result.

### 4. Wake control can acknowledge work without dispatching it

`WorkerControlRuntime` currently calls `dispatch_wakeups` with `on_wakeup=lambda ...: None` and an always-inactive session predicate. The store claim is not scoped to the requested worker. This can mark unrelated wakeups dispatched while no execution owner was invoked.

Required repair: introduce an explicit wake dispatch port, claim only the target worker's wakeups, and acknowledge a wakeup only after the port accepts it. If pending work exists but the port is disabled or absent, the control must fail closed and leave the wakeup recoverable. A worker with no queued wakeup may still transition from parked to idle.

## Prospective source, language, and ownership decision

| Capability | Source path and role | Source language | Target | Target language | Migration mode | Canonical owner |
|---|---|---|---|---|---|---|
| wake serialization, busy-session handling, accepted-before-ack dispatch | `agentscope/src/agentscope/app/_manager/_wakeup_dispatcher.py`, primary | Python | `packages/scheduler/zyra_scheduler/worker_pool/execution_gate.py`, `control.py`, `inbox.py`, `store.py` | Python | `cropped_migration` + `same_language_module_integration` | `WorkerPoolStore` for durable queue/lease; `WorkerControlRuntime` for control application |
| cancel propagation and holder self-selection | `agentscope/src/agentscope/app/_manager/_cancel_dispatcher.py`, primary | Python | worker-pool execution gate/control and API parent-cancel integration | Python | `cropped_migration` + `same_language_module_integration` | logical task remains TypeScript E03; physical lease remains `WorkerPoolStore` |
| session semaphore / park-revive execution guard | `oh-my-pi/src/agent/task/executor.ts` and the already-internalized `omp-worker-control/session-runtime.ts`, supplementary | TypeScript | existing TypeScript OMP runtime plus the language-neutral signed physical projection contract | TypeScript (existing); Python only for canonical lease validation | `native_extract` already present; no new cross-language port | TypeScript owns process-local session execution; Python owns physical lease authorization |
| graceful drain/stop convergence | AgentScope lifecycle/manager semantics, primary | Python | `WorkerLifecycleRuntime`, lease settlement, and durable control | Python | `same_language_module_integration` | `WorkerPoolStore` / `WorkerLifecycleRuntime` |

There is no cross-language primary migration in this remediation. AgentScope-derived production remains Python. OMP-derived production remains TypeScript. The Python execution gate does not port OMP control flow; it validates the shared language-neutral lease projection at the canonical physical-state boundary.

## Target files and effective-code treatment

Planned production files are limited to `apps/api/zyra_api/main.py` and `packages/scheduler/zyra_scheduler/worker_pool/**`. Runtime branches, state transitions, validation, error handling, and integration calls are production candidates. Protocol declarations, dataclass fields, `__init__.py` exports, comments/docstrings, tests, and this plan remain excluded under the effective-code gate.

No generated code, source-pool copy, vendor directory, manifest/data-as-code, or external dependency is authorized. No root source repository may become a runtime path dependency.

## Required behavior and adversarial tests

1. Lease-before-execution: spy on `CodeWorkerRuntime.run`; a cancelled, expired, forged, wrong-generation, or gate-disabled physical projection must be rejected before the spy is called. A valid projection must be authorized immediately before the call.
2. Resume: a parked/revived logical child must resolve its current physical binding and fail before TypeScript execution if the lease is cancelled or missing.
3. Mutation: mutate `lease_id`, `worker_id`, `fence_epoch`, manifest digest, projection digest, and binding id independently; every mutation must fail closed.
4. Graceful stop: stop a busy worker, finish its current attempt, and assert the worker becomes `STOPPED` and cannot accept a new lease. Restart between stop request and lease settlement must preserve the outcome.
5. Parent cancel: cancel a parent with live children and assert both the TypeScript child state and every child physical lease/binding are terminal; capacity is released.
6. Wake: a target worker's accepted wake invokes the dispatch port exactly once; another worker's wake remains queued; absent/disabled port does not acknowledge queued work. A port failure remains retryable across a new runtime instance.
7. Disable tests: `ZYRA_WORKER_EXECUTION_GATE_DISABLED=1` and wake-port disable must block the claimed behavior with no fallback.
8. Run the existing worker-pool/API/OMP adjacent regression suites and the effective-code audit at the final remediation target.

## Completion rule

The remediation is complete only after separate implementation and evidence commits exist, all required behavior tests pass, a new per-file effective-code report covers the frozen parent implementation plus the prospective remediation implementation commit(s), and the parent total is at least `15,000`. If valid runtime work still leaves the total below the threshold, the unit remains blocked; no filler may be added.
