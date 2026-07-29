# P2-S01-02 LoopX state bridge review

## Verdict

P2-S01-02 passes after implementation commit
`68adc01deb95c3767b2800a4dcb94bd441193abb` and risk-fix commit
`c35fa796757e2029c1c5ae87629ea8fe2163ca3e`.

The slice adds a workspace-local, durable and replayable bridge from successful
Zyra `GraphStateCustody` commits into the private state of the pinned LoopX
0.2.4 runtime. It does not activate `phase2_strongest_v1` and does not start
P2-S01-03 or any later Phase 2 slice.

## Bridge and owner contract

The public bridge command and receipt contracts are versioned. Each command
carries a mapping version, canonical commit receipt digest, causation,
correlation and stable idempotency key. Only committed, rebased or replayed
canonical graph receipts are admitted. Persisted receipt and command digests
are revalidated before dispatch.

The mapping is deliberately bounded:

- goal maps objective and requirement-revision references, not a Zyra task;
- todo maps continuation hints, not canonical graph nodes;
- claim is private LoopX occupancy and never creates or changes a worker lease;
- quota is a LoopX private slot view and never changes Zyra execution budget;
- history contains verified references and never replaces the event spine;
- interaction is admitted only after Zyra validation and permission gates.

`GraphStateCustody`, `WorkerLeaseManager`, permission runtime,
`ResourceScheduler`, the TypeScript runtime event spine and
`LocalArtifactStore` retain their canonical ownership.

## Durable outbox and restart behavior

The outbox is a workspace-bound SQLite store under
`{workspace}/.zyra/loopx/state/bridge/outbox.sqlite3`, with WAL journaling,
FULL synchronization, monotonic sequence numbers, message leases, apply
receipts, retry scheduling, dead letters and explicit replay.

The dispatcher performs at-least-once delivery. LoopX event identifiers are
derived from the bridge idempotency key and mapping identity, so a replay does
not duplicate goal, todo, claim, quota, history or interaction mutations.

A process crash was injected after real LoopX apply and before outbox ack. The
record remained inflight with one quota-spend event. A restarted dispatcher
recovered the record, replayed it, returned `idempotent_replay`, acknowledged
attempt two and left the LoopX event count and single quota spend unchanged.

## Real LoopX state and continuation constraint

The adapter loads only the package module root named by the project-local
P2-S01-01 install receipt and verifies that the imported module originates
inside that installation. It uses LoopX's real append-only event store, state
projection, active-state renderer and interaction contract builder.

Private goal registry, event log, `ACTIVE_GOAL_STATE.md` and bridge projection
stay under the fixed workspace state root. Quota exhaustion, rejected spend
and rejected interaction prevent continuation. Each of validation, permission,
lease and budget failure was independently tested and produced zero LoopX
quota spend.

## Windows single writer

The writer uses cross-process exclusive file creation, a monotonically
increasing fencing epoch, a random fencing token and PID plus process-create
time identity. It does not rely on an in-memory lock or the upstream
POSIX-only `fcntl` helper, and it never steals a lock merely because a
heartbeat is old.

Two spawned Windows processes produced exactly one owner and one
`loopx_writer_conflict`. A deliberately crashed owner exited with code 17;
its confirmed-dead lock was taken over with a greater epoch and different
token, and the old fence was rejected.

High-risk review found that the initial low-level APIs trusted a live fence
without independently checking its workspace. The risk-fix commit binds both
outbox and runtime adapter operations to the fence owner's workspace identity.
Cross-workspace fences now fail closed with
`loopx_writer_workspace_mismatch`.

## Observability and failure routes

Every configured sync publication writes a full
`zyra.loopx-sync-status/v1` receipt through `LocalArtifactStore` and a compact
`runtime.audit.finding` through the canonical TypeScript event spine. The event
contains the sync status, outbox sequence, canonical commit and causation
references, validation receipt, claim conflict, quota exhaustion, degraded
reason and a canonical artifact reference.

A disabled bridge leaves the outbox pending and creates no LoopX private
state. A missing pinned runtime yields `sync_degraded`, then a retry or
dead-letter according to policy. Neither path alters the already committed
canonical graph or reports LoopX as active.

## Verification

- bridge, restart, Windows fencing, adjacent worker runtime and owner registry:
  `39 passed in 52.26s`;
- Phase 2 policy contracts: `valid=true`;
- internalization ledger: `audit_ok=true`, zero errors and blockers;
- compileall: passed;
- `git diff --check`: passed.

The slice's suggested adjacent filename
`tests/integration/test_worker_lease_runtime.py` does not exist in the current
repository. The actual adjacent worker and owner suites used were
`test_worker_pool_integration_runtime.py` and
`test_phase2_owner_registry.py`.

## Effective change buckets

| Bucket | Raw additions | Treatment |
|---|---:|---|
| production | 2,776 lines | durable contracts, outbox, dispatcher, runtime adapter, event/artifact publication and single writer |
| adapter-only | 379 lines | deterministic Zyra-to-LoopX state mapping |
| test | 709 lines | real package/state, process, crash, restart and owner behavior |
| runtime-assets | 0 bytes | reuses the frozen P2-S01-01 LoopX package |
| data/generated/mock-fixture | 0 lines | no credit |

The test helper installs the real pinned package into fresh temporary
workspaces. The acceptance tests do not use a mock bridge, recorded success
trace or shared residual state.

## Evidence

- `docs/reviews/evidence/P2-S01-02/bridge-runtime-trace.json`
- `docs/reviews/evidence/P2-S01-02/risk-audit.json`
- `docs/reviews/evidence/P2-S01-02/verification-summary.json`

## P2-S01-03 handoff

The next slice may consume the stable `zyra_integrations.loopx.bridge` API,
`zyra.loopx-sync-status/v1` artifact schema, replayable
`zyra.loopx-bridge-command/v1` messages, typed error codes, fixed workspace
state locator and apply-before-ack restart contract. It must continue to treat
LoopX claim and quota as private supplementary state.

## Repository boundary

The user-selected slice and Phase 2 planning documents remain under
`G:\agent-zoo\docs`, outside the Zyra Git repository. They were not modified
and will not be included by the Zyra evidence commit.
