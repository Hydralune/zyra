# M1-S07A-01 Worker Lifecycle / Resource Pool Foundation Review

Date: 2026-07-21

Review level: slice closeout with risk-matched exact-commit cleanroom. Parent `M1-07A` remains
incomplete; its integration slice `M1-S07A-02` is the next authority entry.

Baseline: `f2db58c4ab4726f5d3ec2878c12114a3a2dc0e9b`

Implementation and final cleanroom target:
`83251b198abe88e6b848f1095b6faed1005cd39d`

## 1. Conclusion

**通过。** The slice internalizes one durable physical worker lifecycle instead of adding another
scheduler score or backend-health projection. `WorkerPoolStore` owns worker instances, immutable
capability manifests, physical attempts, fenced leases, heartbeats, telemetry, execution receipts,
inbox/wakeup envelopes and cancellation receipts. It is reached by the default task API and the
existing 03D subagent path; 03D remains the logical task owner.

`GraphStateCustody` separately owns immutable topology snapshots and branch deltas. It supports
runtime node/edge/role/capability mutation with explicit base revision, read/write sets,
causation, idempotency and deterministic serialize/rebase/replan receipts. Physical attempt and
lease identifiers are references on graph nodes, not a second logical task or checkpoint owner.

The exact detached cleanroom ran a real authenticated edge child process over localhost TCP, with
registration, capability attestation, heartbeat/telemetry, lease fencing, artifact return, real
cancellation and fail-closed disabled-connector behavior. The final risk-matched suite passed `66`
tests plus `4` subtests. Conservative production-only effective code is `9,240` lines, above the
slice minimum of `9,000`. Parent `M1-07A` is not claimed complete until `M1-S07A-02` supplies the
remaining integration and cumulative `15,000`-line closeout.

## 2. Findings fixed during implementation

- SQLite indexes initially referred to pre-normalization timestamp names. The schema now indexes
  the canonical `updated_at`/identifier fields and is exercised through fresh and reopened stores.
- Transaction helpers initially passed the context-manager wrapper rather than its connection.
  All multi-record attempt/lease/receipt changes now execute through the actual transaction.
- Edge protocol startup omitted the protocol-version import and the server stopped before sending
  its final response. Version negotiation is explicit and graceful stop acknowledges before exit.
- Inbox requeue initially updated the delivery without producing a new wakeup. Requeue now writes a
  durable wakeup envelope; restart recovery reclaims expired claims.
- Worker event projection initially attempted unsupported top-level causal fields. Causation and
  worker/attempt/lease references now live in the existing event payload contract.
- Graph node removal was initially treated as an exception-only path. A blocked dependency removal
  now returns a deterministic conflict receipt, leaving the immutable head unchanged.
- API pending tasks originally used a short fixed lease and a constant attempt idempotency key.
  Resume now fences an expired lease, increments only the physical attempt, preserves the 03D task
  ID and refreshes the local process heartbeat before placement.
- Stale disjoint branches were applied correctly but labelled as ordinary commits. A requested
  rebase is now explicit in the receipt even when the read/write sets are conflict-free.
- Static reachability did not expose routes assembled by the HTTP dispatcher. The colocated
  `ZYRA_DYNAMIC_API_ROUTES` contract lists every implemented worker-pool route for ledger audit.

## 3. Acceptance matrix

| Requirement | Runtime and behavior evidence | Status |
|---|---|---|
| One physical lifecycle owner | `WorkerLifecycleRuntime` and `WorkerPoolStore` persist register/start/idle/busy/park/wake/drain/stop/lost/failed transitions | PASS |
| Durable attempt and lease split | Every scheduling decision creates a physical `TaskAttempt` and fenced `WorkerLease`; logical task ID remains the existing 03D ID | PASS |
| Heartbeat, telemetry and lost recovery | Monotonic heartbeat records drive health sweep, lost-worker transition, lease expiry and recovery request | PASS |
| Cancel/wakeup/drain semantic effect | Cancellation fences leases and changes attempt state; durable inbox claims requeue/ack/recover; drain rejects new leases | PASS |
| Capability composition | Worker, backend/gateway and 03D request constraints form one manifest/requirement with signed attestation | PASS |
| Dynamic topology | Nodes, edges, roles and capabilities can be added, removed and replaced during the run, outside a precompiled graph | PASS |
| Immutable graph commit | Snapshot + branch delta + read/write set + causation + idempotency use atomic head CAS and explicit conflict receipts | PASS |
| State custody separation | 03D task, 05A workspace, 05B/05C backend/gateway, 05D artifacts, 07A lease and graph custody have unique writers | PASS |
| Real edge pool | Independent child PID and localhost TCP endpoint perform auth, attestation, heartbeat, execution, artifact and cancel | PASS |
| No local fallback | Disabled/unavailable edge connector raises a deterministic `no local fallback` error | PASS |
| API and subagent reachability | Default `POST /tasks`, task run/cancel, worker-pool routes and subagent spawn/cancel mutate physical pool state | PASS |
| Restart behavior | Reopened SQLite stores restore workers, attempts, leases, receipts, graph head and pending inbox state | PASS |
| Disconnect/disable effect | Removing worker-pool acquisition makes task creation fail with `fallback=false`; disabling edge prevents execution | PASS |
| Clean submission boundary | Exact cleanroom scan found no root source path, `../` source dependency, npm link, editable path or external Docker context | PASS |
| Production minimum | Conservative `9,240 >= 9,000` | PASS |

## 4. Main paths and semantic effects

```text
POST /tasks
  -> existing 03D logical TaskState + 05A workspace
  -> GraphStateCustody immutable task topology
  -> WorkerPoolStore physical attempt + fenced lease
  -> graph node binds attempt/lease/backend refs
  -> existing task graph / CodeWorker runtime
  -> execution receipt + canonical worker events + artifact refs

03D subagent dispatch
  -> existing logical subagent task id/request
  -> capability requirement
  -> physical attempt + lease (no copied logical task)
  -> cancel maps back to the same attempt/lease

edge registration
  -> hidden independent Python child + authenticated localhost TCP
  -> attested capability manifest + heartbeat/telemetry
  -> fenced execution request
  -> gateway receipt/artifact or real cancellation
```

`POST /tasks/{id}/run` calls `ensure_task_lease`. An active lease is reused idempotently; an expired
lease is first made terminal and fenced, then a successor physical attempt is allocated. No
committed graph delta is replayed. `POST /tasks/{id}/cancel` coordinates logical graph cancellation,
backend cancellation and physical lease cancellation.

## 5. Internalization and source decisions

| Role | Pinned source | Migration and target | Unique owner |
|---|---|---|---|
| Primary implementation | AgentScope `b6698c5dbaa1aa916925e27402767f45e2405fa4` | Cropped Python lifecycle/inbox/wakeup semantics adapted to durable Zyra SQLite/CAS/event schemas | `WorkerPoolStore` + lifecycle/inbox runtimes |
| Supplementary implementation | Oh My Pi `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | Bounded attempt/drain/claim/external-worker protocol adaptation; no OMP process/store dependency | attempt/lease manager and Zyra edge connector |
| Zyra-owned implementation | No upstream production owner | Immutable graph custody and runtime dynamic topology | `GraphStateStore` / `GraphStateCustody` |
| Conformance only | LangGraph pinned source graph | Exact-resume/checkpoint identity semantics only; no StateGraph/Pregel/channel/store production owner | none |
| Reference only | Claude task/background patterns | Mapping check only; existing 03D task runtime remains canonical | none |
| Forward excluded | OpenClaw | No source read, migration, test duty or runtime dependency | none |

AgentScope globals/bus/process managers and OMP JSONL/global registry/roboomp SQLite are not runtime
dependencies. The mature lifecycle ordering is decomposed into Zyra modules for schema, lifecycle,
capability, lease, heartbeat, inbox, cancellation, recovery and API composition. The edge process is
not an opaque upstream sidecar: its protocol, server, connector and state integration are all Zyra
source in `packages/workers/zyra_workers/edge_pool` and are tested from a clean checkout.

## 6. State custody

| State | System of record / writer | Restore boundary |
|---|---|---|
| Logical task/subagent identity | Existing M1-S03D `TaskState` / TypeScript agent task runtime | Existing task checkpoint; 07A stores the ID as a foreign reference |
| Worker instance and generation | `WorkerPoolStore.workers` via `WorkerLifecycleRuntime` | SQLite reopen plus process generation/identity checks |
| Capability manifest/attestation | `WorkerPoolStore.capability_manifests` / attestation table | Digest and HMAC verification before placement |
| Physical attempt and lease | `WorkerPoolStore.task_attempts` / `worker_leases` | Transactional CAS, fence epoch/token and idempotency key |
| Heartbeat/telemetry | `WorkerHeartbeatRuntime` append records | Monotonic sequence and manifest/generation match |
| Execution/cancel receipt | `WorkerPoolStore.execution_receipts` / cancellations | Idempotency fence and artifact/event references |
| Inbox/wakeup claim | `WorkerInboxRuntime` durable envelopes | Claim deadline recovery and requeue wakeup |
| Topology snapshots/deltas | `GraphStateStore` | Signed immutable revision, branch delta and atomic head CAS |
| Workspace | Existing M1-S05A owner | Reference only |
| Backend/gateway route | Existing M1-S05B/05C owners | Manifest/route reference only |
| Artifact | Existing M1-S05D artifact store | Artifact ID/receipt reference only |

No LLM decides lifecycle transitions, capability admission, lease fencing, conflict handling,
cancellation, health loss or restore validity.

## 7. Effective line review

Range `f2db58c..83251b1`:

- whole commit: `11,841` added / `9` deleted;
- raw production candidate: `9,999` added;
- production blank/comment-only lines excluded conservatively: `759`;
- conservative effective production: **`9,240`**;
- tests: `756`, excluded;
- source-ledger synchronization helper: `346`, excluded as ordinary helper;
- ledger seed/data: `436` added / `8` deleted, excluded;
- export-only `__init__.py` glue: `290`, excluded;
- third-party notice: `14`, excluded as documentation;
- generated, vendor/source-pool, opaque bundle, mock/fixture-only, data-as-code, dead/unreachable and
  adapter-only code counted as production: `0`;
- minimum: `9,000`; shortfall: `0`.

The repository line-count tool reports `raw_added=11,841`, `effective_added=11,391`,
`excluded_added=450`, `shortfall=0`. It includes tests/tooling in its effective surface, so the more
conservative production-only `9,240` is the acceptance count.

## 8. Verification

Main worktree:

- slice behavior suite: `13 passed in 12.73s`;
- adjacent scheduler/backend/sandbox/API suite: `53 passed`, `4 subtests passed`;
- compileall for all new production modules: pass;
- ledger synchronization: aligned, `2` source decisions;
- current production dependency/path scan: `0` forbidden matches;
- `git diff --check`: pass.

Exact detached cleanroom `83251b198abe88e6b848f1095b6faed1005cd39d`:

- `npx --yes bun@1.2.15 install --frozen-lockfile`: pass, `16` locked packages;
- isolated pytest basetemp inside the workspace; cache provider and bytecode writes disabled;
- combined slice + adjacent suite: `66 passed`, `4 subtests passed in 87.17s`;
- real edge child PID differed from parent and communicated over authenticated localhost TCP;
- `zyra_scheduler.worker_pool`, `zyra_orchestration.graph_custody`, `zyra_workers.edge_pool` and
  `zyra_api.worker_pool_api` import origins all asserted inside the detached worktree;
- ledger synchronization: aligned, `2` decisions;
- line-count gate: pass (`11,391` tool-effective, minimum `9,000`);
- current production forbidden dependency scan: `0` matches;
- detached `git status --short`: empty after install/tests.

This slice changed public state owners and added a child process/local port, so exact-commit
cleanroom was run under the high-risk rule. The full repository suite and global cross-unit
ledger/source-to-target audit remain assigned to the M1-07 numeric-stage aggregate review; the
risk-matched adjacent suites above cover the touched scheduler, backend, sandbox, task graph and
API surfaces.

## 9. Baseline ledger debt

The existing test
`test_audit_treats_planned_unmaterialized_targets_as_warnings_not_failure` remains red because the
seed contains protected historical productized entries whose old target paths no longer exist.
The identical failure was reproduced in a detached baseline worktree at
`f2db58c4ab4726f5d3ec2878c12114a3a2dc0e9b`. Current 07A entries add no audit finding, synchronize
2/2 and pass their behavior/reachability contracts. This slice does not rewrite protected earlier
ledger history; classification and repair belong to the next applicable global ledger audit.

## 10. Disconnect and failure evidence

- Disconnecting worker acquisition from default task creation yields
  `worker_pool_acquisition_failed` with `fallback=false`; task execution is not silently routed
  through the old static worker projection.
- Draining a worker prevents new leases while existing leases remain visible until settled.
- Lost heartbeat expires/fences the old lease; stale fence tokens cannot start or commit receipts.
- Recovery allocates a strictly larger attempt number and preserves capability/location/resource
  requirements. An edge-only requirement without another edge worker requests replan rather than
  using local fallback.
- Graph write/write conflicts leave the head unchanged and produce serialize/rebase/replan
  receipts; cyclic topology changes are rejected before snapshot commit.
- Disabling the edge connector prevents process startup and execution; cancellation actually
  interrupts the child's wait job.

These tests establish semantic effects. They do not merely assert schema presence, health text,
source inventory or fixed replay fixtures.

## 11. Residual work and competition boundary

`M1-S07A-02` must integrate the foundation with full admission/capacity, exact checkpoint recovery,
long-running resource renewal and the parent cumulative minimum. `M1-07C` still owns scheduler
recovery policy, while later M1/M3 gates own real local/isolated-edge/cloud dispatch combinations,
fault matrices and final cleanroom/package evidence.

This slice advances physical worker scale, dynamic topology and fault evidence, but does not claim
to close the two cross-domain live runs, 2,000 effective canonical transitions, cloud/model
compatibility, final low-entropy comparison or submission score gates.

## 12. Commit and state boundary

- Baseline: `f2db58c4ab4726f5d3ec2878c12114a3a2dc0e9b`.
- Implementation and final cleanroom target: `83251b198abe88e6b848f1095b6faed1005cd39d`.
- Review/evidence commit: created after this report.
- Root `G:/agent-zoo/docs/milestones/execution-state.yaml` is outside the Zyra Git repository and
  is updated only after the review/evidence commit exists.

## 13. Next step

Execute `M1-S07A-02` from its authority document. Parent `M1-07A` and numeric stage `M1-07` remain
open; no slice-local blocker remains.
