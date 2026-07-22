# M1-S07A-02 effective-code gate remediation completion (2026-07-22)

## Verdict

`PASS` for the 2026-07-22 in-flight completion gate and effective-code/language migration gate.

The original implementation and evidence remain frozen. The earlier remediation audit correctly reopened the unit because the parent range contained only `14,646` effective production lines. A prospective decision was committed before new production work. The remediation implements four independently required worker-lifecycle behaviors, adds `896` effective production lines, and brings the current parent range to `15,497 / 15,000`.

This report supersedes only the remediation blocker status. It does not rewrite the conclusions or claimed decision time of the frozen original review/evidence.

## Commit boundary

| Boundary | Commit | Meaning |
|---|---|---|
| Parent baseline | `f2db58c4ab4726f5d3ec2878c12114a3a2dc0e9b` | Start of M1-07A parent accounting |
| Slice baseline | `8981599da7312f2bfba7b3303498efebb8339e39` | Start of frozen M1-S07A-02 implementation accounting |
| Frozen original implementation | `16f24689c1817a1f74fec9afb451d24b97983670` | Original implementation target; unchanged |
| Frozen original evidence | `66b3c42da106a799b65eea3e17a7efcdff0b5f2b` | Original evidence; unchanged |
| Remediation blocker audit | `72732e2167772b306dfd1921912995b48389ed99` | Added audit/disable evidence |
| Remediation blocker evidence | `2bdaa4acd860e62eda705f2cb248cc7483a8a2aa` | Recorded `14,646` parent blocker |
| Prospective remediation decision | `b4bd15832fc3729e451e438bae23fbbfaa063f06` | Source/language/owner/test decision committed before production |
| Remediation implementation | `2cf2bfa718d1c22d06cf4ef48119d21408dd3a0c` | Production and behavior-test target |
| Completion audit tool | `9186225`, `b6a9060` | Separate nonproduction audit tooling |

The current parent blame allowlist is exactly `83251b198abe88e6b848f1095b6faed1005cd39d`, `150389567046f0cf43205b2e649e74f8543421ae`, `16f24689c1817a1f74fec9afb451d24b97983670`, and `2cf2bfa718d1c22d06cf4ef48119d21408dd3a0c`. Evidence, audit, unrelated remediation, and decision commits do not contribute effective production.

## Effective-code result

| Range | Raw additions | Effective production | Required | Result |
|---|---:|---:|---:|---|
| Frozen slice `8981599..16f2468` | 10,352 | 6,078 | 6,000 | PASS, retained as frozen history |
| Prospective remediation `b4bd158..2cf2bfa` | 1,317 | 896 | capability-driven increment | PASS |
| Current parent `f2db58c..2cf2bfa`, implementation-blame allowlist | 35,150 | 15,497 | 15,000 | PASS by 497 |

Current parent exclusions are: type/interface `704`, schema/DTO/data `3,832`, adapter-only `174`, generated `0`, tests/mock/fixture `2,810`, nonproduction tooling `885`, docs/comments/blank `1,544`, and unrelated/evidence lines `9,704`. These exclusions total `19,653`; none is counted in the `15,497` production result.

The machine evidence contains the complete per-file table for the frozen slice, remediation range, and current parent range:

- `docs/reviews/evidence/M1-S07A-02-effective-code-gate-remediation-completion-2026-07-22.json`

## Remediation per-file buckets

| File | Raw | Effective production | Type | Schema/data | Adapter | Generated | Test | Tooling | Docs/blank |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `apps/api/zyra_api/main.py` | 148 | 139 | 0 | 0 | 0 | 0 | 0 | 0 | 9 |
| `apps/api/zyra_api/worker_pool_api.py` | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `packages/integrations/zyra_integrations/data/internalization_ledger_seed.json` | 31 | 0 | 0 | 31 | 0 | 0 | 0 | 0 | 0 |
| `packages/scheduler/zyra_scheduler/worker_pool/__init__.py` | 9 | 0 | 9 | 0 | 0 | 0 | 0 | 0 | 0 |
| `packages/scheduler/zyra_scheduler/worker_pool/control.py` | 73 | 68 | 3 | 0 | 0 | 0 | 0 | 0 | 2 |
| `packages/scheduler/zyra_scheduler/worker_pool/execution_gate.py` | 514 | 486 | 1 | 1 | 0 | 0 | 0 | 0 | 26 |
| `packages/scheduler/zyra_scheduler/worker_pool/inbox.py` | 84 | 82 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |
| `packages/scheduler/zyra_scheduler/worker_pool/integration.py` | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `packages/scheduler/zyra_scheduler/worker_pool/lifecycle.py` | 82 | 73 | 0 | 0 | 0 | 0 | 0 | 0 | 9 |
| `packages/scheduler/zyra_scheduler/worker_pool/store.py` | 44 | 41 | 0 | 0 | 0 | 0 | 0 | 0 | 3 |
| `scripts/sync_worker_pool_integration_source_ledger.py` | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 0 |
| `tests/integration/test_worker_pool_api_main_path.py` | 95 | 0 | 0 | 0 | 0 | 0 | 95 | 0 | 0 |
| `tests/integration/test_worker_pool_integration_runtime.py` | 222 | 0 | 0 | 0 | 0 | 0 | 222 | 0 | 0 |
| **Total** | **1,317** | **896** | **13** | **32** | **0** | **0** | **317** | **8** | **51** |

## Source role and language custody

| Current-parent source role | Language | Raw | Effective production | Test | Adapter | Excluded/other |
|---|---|---:|---:|---:|---:|---:|
| AgentScope primary same-language | Python | 13,974 | 10,294 | 0 | 0 | 3,680 |
| OMP supplementary same-language | TypeScript | 1,659 | 1,325 | 0 | 0 | 334 |
| OMP protocol adapter | Python | 222 | 0 | 0 | 174 | 48 |
| Zyra AgentScope/OMP integration | Python | 1,387 | 1,284 | 0 | 0 | 103 |
| Zyra supporting runtime | Python | 4,264 | 2,513 | 0 | 0 | 1,751 |
| Zyra supporting runtime | TypeScript | 5,420 | 81 | 0 | 0 | 5,339 |
| Mixed verification | Python | 2,764 | 0 | 2,389 | 0 | 375 |
| Mixed verification | TypeScript | 1,079 | 0 | 421 | 0 | 658 |

Additional data, docs, lock, and remediation-tooling roles have zero effective production and remain enumerated in the JSON evidence.

AgentScope primary remains Python and the remediation production is a same-language Python module integration. OMP session/semaphore/park-revive production remains TypeScript and continues to execute in the original language. The Python edge integration remains explicitly excluded as `adapter_only` (`174` lines). There is no cross-language primary exception and no generated production.

## Runtime behavior repaired

1. `WorkerExecutionGateRuntime` now reloads binding, lease, attempt, worker generation, manifest, health, and pending durable controls from `WorkerPoolStore` at the last boundary before `CodeWorkerRuntime.run`. It rejects missing, terminal, expired, cancelled, fenced, wrong-generation, wrong-manifest, unsigned, or authority-mismatched projections.
2. `Agent` fanout validates every unique child binding. `agent_resume` resolves the current child binding from canonical storage; a parked physical binding is revived before TypeScript resume and then re-authorized.
3. Graceful stop persists `stop_after_drain` in the existing canonical worker record, marks current leases/bindings draining, survives runtime reconstruction, and converges to `STOPPED` when the final lease settles.
4. Parent cancel now fences child physical bindings even when permission suspension occurred after physical admission but before the TypeScript logical child record committed.
5. Wake claims are scoped to the requested worker. A wakeup becomes dispatched only after an explicit execution port accepts it. A disabled/missing port leaves queued work recoverable; stale wake claims can be requeued after restart.

No second logical task, lease, worker, graph, or checkpoint store was added. TypeScript E03 remains the logical task owner; `WorkerPoolStore` remains the physical lease/worker owner; `GraphStateCustody` remains the topology owner.

## Lease-before-execution evidence

The real subagent path is now:

`POST /tasks/{parent}/subagents -> _acquire_subagent_physical_dispatch -> scheduler.admit -> WorkerPoolStore lease commit -> integration.start -> signed projection -> _run_typescript_agent_request -> _authorize_typescript_agent_physical_execution -> WorkerExecutionGateRuntime canonical reload -> CodeWorkerRuntime.run -> TypeScript OmpWorkerDispatchRuntime -> child operation`.

`test_subagent_api_admits_through_typescript_omp_gate_before_child_execution` asserts the canonical journal order:

`attempt_started < worker_execution_authorized < execution_receipt_committed`.

The execution authorization does not expose the fence token to TypeScript. Final yield/receipt commit still requires the canonical Python fence.

## Disable and mutation evidence

- `ZYRA_WORKER_LEASE_STORE_DISABLED=1`: the API returns conflict before `_run_typescript_agent_request`; TypeScript execution count remains zero and no child lease exists.
- `ZYRA_WORKER_EXECUTION_GATE_DISABLED=1`: physical admission exists, but `CodeWorkerRuntime.run` count remains zero; the already-started attempt is settled as failed rather than executed.
- `ZYRA_OMP_WORKER_CONTROL_DISABLED=1`: the TypeScript OMP runtime fails closed and its operation counter remains unchanged.
- `ZYRA_WORKER_WAKE_DISPATCH_DISABLED=1`: queued wake work remains queued and the parked target worker is not falsely reported as resumed.
- Re-signed mutations of `lease_id`, `worker_id`, `fence_epoch`, `manifest_digest`, and `integration_binding_id` all fail against canonical binding state.
- The existing TypeScript tests reject tampered projections, duplicate concurrent execution, wrong logical-task binding, invalid restart rehydration, and disable mutation.
- Existing integration tests retain the atomic rollback proof for binding insertion failure, stale fence rejection after takeover, graph/lease owner disable matrix, and edge-only no-fallback behavior.

## Verification results

| Command/scope | Result |
|---|---|
| `pytest test_worker_pool_foundation.py test_worker_pool_integration_runtime.py test_worker_pool_api_main_path.py -q` | `26 passed` |
| edge worker + runtime event spine + source-language custody | `12 passed` |
| final focused stop/order/source-language rerun | `7 passed` |
| new execution-gate/stop/wake/parent-cancel selection | `5 passed` |
| `bun test .../omp-worker-control.test.ts` | `11 passed` |
| `tsc -p packages/runtime/claude-runtime/tsconfig.json --noEmit` | PASS |
| Python `compileall` / audit-script `py_compile` | PASS |
| source-ledger synchronization `--write` then `--check` | PASS |
| completion audit `--summary-only --fail-on-gate` and full JSON emission | PASS; no blockers |
| incremental external path/dependency scan | no root-source relative path, npm link, editable path, vendor/source-pool, or external build-context match |

The all-repository suite and complete cleanroom remain assigned to the M1-07 numeric-stage aggregation/exit layer under the documented verification-frequency rule. No current change introduces a high-risk external dependency, process, port, or canonical-owner transfer that would require an immediate unrelated full-repository rerun.

## Final state recommendation

M1-S07A-02 and parent M1-07A may return to completed status at implementation target `2cf2bfa718d1c22d06cf4ef48119d21408dd3a0c`, with this completion evidence committed separately. The next executable entry may advance to M1-S07B-01. The earlier remediation blocker report remains historical evidence of why this additional prospective work was required.
