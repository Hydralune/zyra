# M2-S01B-02 canonical state store projection and recovery preimplementation decision

Date: 2026-07-23

Authority:
`docs/milestones/M2-console-demo/slice-01b-02-canonical-state-store-projection-recovery.md`

Baseline commit: `8f6ea956286a6f84f7c20fc46225fa0b9b5ee9d5`

Parent-unit baseline commit:
`1f41ab17864dd789948aeebe3723540007ebaf2b`

This record is frozen before the first production-code change. It applies the
2026-07-22 effective-code/language/migration gate and fixes the source roles,
language path, canonical owners, and migration modes for this slice.

## Source, language, migration, and owner decision

| Role | Source repository and bounded path | Source language | Zyra target | Target language | Migration mode | Canonical owner after the slice | Preimplementation decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| primary | `opencode/packages/app/src/context/server-session.ts` | TypeScript | `apps/web/src/state/{store,reducer,transactions,optimistic,tombstones,orphans,retention}.ts` | TypeScript | `same_language_retained_control_flow_adapt` | The Zyra `CanonicalProjectionStore` is the single browser projection writer; backend M1 stores and event spine remain authoritative | Preserve generation fencing, in-flight freshness, deterministic identity merge, optimistic-to-confirmed reconciliation, partial-to-final accumulation, orphan-parent handling, removal markers, and protected cache eviction. Replace OpenCode session/message/part DTOs and Solid store ownership with normalized Zyra ingress events, immutable revision transactions, domain projections, and typed subscriptions. |
| primary | `opencode/packages/app/src/context/global-sync/event-reducer.ts`, `session-cache.ts`, `session-trim.ts` | TypeScript | `apps/web/src/state/{reducer,entity-index,retention,selectors}.ts` | TypeScript | `same_language_crop_and_adapt` | The same `CanonicalProjectionStore` owns all browser task/node/worker/tool/artifact/memory/scheduler/recovery/command/permission/overlay/session projections | Preserve ordered insert-or-reconcile, deletion cleanup, stable sorting, permission/question lifecycle, bounded retained sessions, protected active identities, and cache cleanup. Replace directory/project-specific branches with one normalized domain reducer and one selector registry. |
| primary | `opencode/packages/app/src/context/server-sync.tsx`, `child-store.ts` | TypeScript / TSX | `apps/web/src/state/{store,subscriptions,persistence}.ts` and `apps/web/src/app/runtime.ts` | TypeScript | `same_language_crop_and_adapt` | One workbench-scoped store owns projection lifecycle, persistence scheduling, task pinning, and observer fan-out | Preserve reference-counted lifecycle, owner-scoped cleanup, task/session pinning, bounded child eviction, and stable external-store snapshots. Do not migrate Solid, TanStack query state, project caches, or create per-panel stores. |
| supplementary | `OpenHands/openhands/app_server/event/event_service_base.py`, `event_router.py`, `sandbox/remote_sandbox_service.py` | Python | `apps/web/src/state/{history-fold,persistence,recovery}.ts` and recovery integration tests | TypeScript | `bounded_cross_language_behavior_port_exception` | No supplementary owner; the TypeScript `CanonicalProjectionStore` remains the only projection writer | Port only the independently testable behavior: stable ordered history pages, cursor continuation, load-before-fold, accept-only-missing identity, and one callback/effect per newly accepted event. This narrow protocol behavior supplement is required because the slice fixes the target canonical reducer language as TypeScript. It does not copy Python storage or control flow, establish a Python reducer, or replace the TypeScript primary. Equivalent behavior is proved by snapshot/live interleave, reconnect, replay, and duplicate-side-effect tests. |
| conformance_only | `OpenHands/frontend/src/stores/use-event-store.ts`, `utils/handle-event-for-ui.ts`, `hooks/use-websocket.ts` | TypeScript | `apps/web/test/**` and `tests/integration/**` | TypeScript / Python | `conformance_only` | No production owner | Verify id lookup, timestamp-stable late insertion, streaming partial-to-final reconciliation, action/observation replacement, reconnect cleanup, and no duplicate UI identity. These frontend stores are not migrated because doing so would create a second fact pipeline. |
| primary foundation | Existing Zyra `apps/web/src/events/ingress/**`, `apps/web/src/api/event-transport.ts`, `packages/runtime/runtime-event-spine`, and M1 canonical stores | TypeScript / Python | `apps/web/src/state/**`, `apps/web/src/app/runtime.ts`, and the default `createZyraApi().events` path | TypeScript / Python | `same_language_integrate_existing` | Runtime event spine owns durable sequence and causality; M1 domain stores own runtime state; the new browser store owns read-only projections | Consume ordered immutable `IngressBatch` values and their committed cursor/generation. Preserve backend global sequence, aggregate sequence, state mutation, correlation, causation, span, tool, artifact, checkpoint, and command identities without renumbering or inventing fallback events. |
| conformance_only | `claude-code-best/src/utils/messageQueueManager.ts`, `queueProcessor.ts`, `context/overlayContext.tsx`, `types/command.ts`, and permission request/result shapes | TypeScript / TSX | The single normalized reducer contract tests | TypeScript | `conformance_only` | No production owner | Verify priority/FIFO command projection, command lifecycle receipts, permission ask/resolve, local command result, and modal/non-modal overlay lifecycles. The Claude queue, permission runtime, hooks, and overlay store are not migrated into a parallel browser state owner. |
| conformance_only | Agent Framework AG-UI history/approval envelopes and `oh-my-pi` request-correlation/cancellation shapes | Python / TypeScript | Reducer and selector contract tests | TypeScript / Python | `conformance_only` | No production owner | Verify correlation continuity, approval status mapping, request cancellation targeting, and replay idempotency only. No second workflow, approval, RPC, or history store is migrated. |
| conformance_only | LangGraph checkpoint identity/lineage, pending-versus-committed writes, stable task identity, and exact-resume correlation as limited by `source-graphs/langgraph/source-graph.md` section 12 | Python | Snapshot/reconnect/schema-upgrade tests | TypeScript / Python | `conformance_only` | No production owner | Verify committed cursor monotonicity, stale snapshot rejection, pending versus committed visibility, and exact resume. Do not migrate StateGraph, channels, Pregel, stream controller, store, ToolNode, SDK, server, or deployment code. |
| excluded_forward_only | OpenClaw | N/A | none | none | `excluded_forward_only` | none | Do not read, restore, depend on, test against, or cite OpenClaw as an implementation or conformance source. |

## Fixed implementation boundaries

- Backend state/event owners remain authoritative. The browser store contains
  deterministic read-only projections and cannot write runtime task, graph,
  worker, scheduler, memory, recovery, artifact, permission, or command truth.
- `CanonicalProjectionStore` is the only code path allowed to reduce normalized
  ingress into canonical UI projections. Panels, query hooks, modal components,
  feature views, and local shell stores may keep ephemeral presentation state
  but may only read canonical projection data through registered selectors.
- The default path is
  `createZyraApi().events -> TaskEventTransport -> EventIngressCoordinator ->
  CanonicalProjectionStore transaction -> entity indices -> typed selectors`.
  No fixture fallback, page-local history fold, direct event endpoint fetch, or
  second reconnect controller may mask a disabled store.
- Every accepted batch is applied as one immutable, revisioned transaction.
  Compare-and-swap base revision, task generation, committed cursor, and
  monotonic sequence fence stale transactions and stale restored snapshots.
- Replaying an already committed event is a no-op. An optimistic identity is
  replaced by its authoritative identity without double counting. Partial
  content remains bounded until a final event settles it. Tombstones retain a
  bounded deletion marker so late history cannot resurrect removed state.
  Orphans remain bounded until their parent arrives or retention expires them.
- Domain projection uses the event identity, state mutation domain/path,
  event type, intent, inline payload, metadata, and artifact references. The
  reducer must cover task, node, worker, tool, artifact, memory, scheduler,
  recovery, command, permission, overlay, and session domains in the same
  transaction and preserve unknown events in a bounded causal timeline.
- Causal lookup is reversible by event, correlation, causation, span,
  parent-span, tool call, artifact, failure, recovery, state mutation, task,
  run, session, node, worker, checkpoint, and control command identities.
- Persistence stores a versioned projection snapshot plus committed ingress
  cursor. Restore validates checksum, task scope, schema version, generation,
  cursor, and revision, runs explicit migrations, and rejects stale or corrupt
  snapshots before reconnect. Cursor persistence happens only after a reducer
  transaction commits.
- Closing a browser subscription persists and detaches only browser projection
  observers. It does not issue a task cancel/stop control command. A reopened
  workbench restores the last committed snapshot/cursor and converges with
  snapshot plus delta without duplicate projection changes or effects.
- Bounded retention is explicit for events, tombstones, orphans, optimistic
  records, mutations, diagnostics, causal index entries, tasks, and inactive
  sessions. Active, pending permission, running worker, unresolved command,
  recovery, and pinned task identities are protected from ordinary eviction.
- Selectors expose stable snapshots and dependency keys. Subscribers are
  notified only when the selected dependency changes; a task update may not
  invalidate unrelated task, artifact, permission, or overlay selectors.
- Disable behavior fails visibly: disabled reduction rejects new batches;
  disabled persistence prevents restore/cursor commit; disabled selectors
  cannot return a fabricated projection. Existing polling, shell state, or API
  query caches may not silently substitute for any of these paths.

## Language exception and equivalence obligation

The primary source and target reducer/store control flow are both TypeScript,
so non-zero original-language TypeScript production code is mandatory. No
primary cross-language rewrite exception is requested.

The slice authority declares the OpenHands history/WebSocket fold supplement
as Python while fixing the canonical UI reducer target as TypeScript. The
bounded supplementary exception above therefore ports only protocol behavior,
not a Python runtime or storage implementation. Its independent benefit is
history/live reconciliation with accept-only-missing idempotency. The primary
store remains complete without a second owner, and the supplement is accepted
only if real snapshot/reconnect tests demonstrate:

1. history and live events interleave in deterministic canonical order;
2. an existing event is not folded or side-effected twice;
3. a reconnect continues from the committed cursor and accepts only missing
   identities;
4. browser close/reopen restores the projection without stopping the backend
   task; and
5. disabling projection recovery makes those tests fail or visibly reject.

## Effective-code and aggregate-review obligations

The slice minimum is 7,500 effective production lines. The parent M2-01B
minimum is 15,000 effective production lines. This final parent slice will
recompute the complete parent range
`1f41ab17864dd789948aeebe3723540007ebaf2b..implementation_commit`
directly rather than summing child self-reports.

Final evidence will bucket every changed file into raw additions, runtime
behavior, UI behavior, UI presentation, types/declarations, schema/data,
adapter-only glue, generated content, tests/mocks/fixtures, docs/comments,
vendor-like/source-pool material, and effective production. Files above 500
raw additions, files contributing over 20 percent of effective code, and files
with more than 30 percent excluded content require a concentration audit.

Because this slice closes M2-01B and the M2-01 numeric stage, its implementation
commit must also pass one cleanroom validation of the complete applicable
frontend/backend tests, web typecheck/build, source-to-target audit, external
path/dependency audit, state-owner audit, and M2-01A/01B adjacent regressions
using the same final target commit.

## Commit boundary

- `baseline_commit`:
  `8f6ea956286a6f84f7c20fc46225fa0b9b5ee9d5`
- `parent_baseline_commit`:
  `1f41ab17864dd789948aeebe3723540007ebaf2b`
- This decision record is intentionally committed before production code.
- `implementation_commit` will be created only after production code and its
  directly related tests pass, but before the completion report, ledger
  updates, and execution-state update.
- The slice implementation range is
  `baseline_commit..implementation_commit`; documentation is excluded from
  effective production counts.
- The parent implementation range is
  `parent_baseline_commit..implementation_commit` and will be audited directly,
  not by summing child-slice reports.
