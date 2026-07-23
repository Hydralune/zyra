# M2-S01B-01 Event Stream Cursor Ingestion Review

Date: 2026-07-23
Verdict: `PASS`

## 1. Frozen commit boundary

- `baseline_commit`:
  `1f41ab17864dd789948aeebe3723540007ebaf2b`
- `parent_baseline_commit`:
  `1f41ab17864dd789948aeebe3723540007ebaf2b`
- `preimplementation_decision_commit`:
  `93aa6af991082fda8b07cdf240aa233014a0bdce`
- `implementation_commit`:
  `6cd3f3fc259a5d39e96f81c79917ce3159d46efe`
- `evidence_commit`: `this_commit`
- line-count interval:
  `1f41ab17864dd789948aeebe3723540007ebaf2b..6cd3f3fc259a5d39e96f81c79917ce3159d46efe`

The prospective source/language decision was committed before production
changes. Production code and directly related tests are frozen in the
implementation commit. This review, its JSON evidence, exact line auditor,
ledger synchronizer and ledger updates are evidence-only and cannot inflate
the implementation interval.

## 2. Source, language and migration decision

The prospective decision is
`docs/plans/M2-S01B-01-event-stream-cursor-ingestion-preimplementation.md`.
No source-role reassignment, cross-language exception or canonical-owner
transfer was used.

| Role | Source / pinned commit | Source language | Target language | Effective production | Responsibility |
| --- | --- | --- | --- | ---: | --- |
| primary | OpenCode `adf178a6b95c61506ddaadaf4dd062badb4a8fda`, `server-sdk.tsx`, `server-session.ts`, `server-sync.tsx` and bounded global-sync files | TypeScript / TSX | TypeScript | 7,616 | generation/session lifecycle, snapshot/live merge, cursor/order/dedupe/gap/assembly/pressure, subscription union and diagnostics |
| supplementary | OpenHands `c105a82387898e744423c8831d412e26495b38a9`, frontend event-service API/types | TypeScript | TypeScript | 742 | typed event source, bounded page/wait validation and long-poll fallback |
| supplementary | same OpenHands commit, server event service/router/store | Python | Python | 1,067 | read-only signed cursor, captured snapshot, delta wait and finite SSE facade |
| existing Zyra foundation | M2-S01A-01 typed transport | TypeScript | TypeScript | 175 | streaming response lifecycle through the sole browser request/auth/version/cancellation boundary |
| conformance | narrow LangGraph exact-resume, AG-UI history envelope and OMP correlation/cancellation | Python / TypeScript | tests only | 0 | committed cursor monotonicity, exact reconnect and pending/gap rejection |
| excluded | OpenClaw | N/A | none | 0 | forward exclusion preserved |

The prospective record used the obsolete OpenHands spelling
`event_store/event_store_service.py`; at the pinned commit the bounded module
is `openhands/app_server/event/event_store.py`. The evidence ledger records the
real path. Repository, commit, source language, role, migration mode and owner
are unchanged; this is a path-resolution correction, not a postimplementation
source waiver.

OpenCode Solid contexts, SDK event names, global reducers and canonical session
cache were removed. OpenHands Axios/conversation DTOs and server event-store
ownership were removed. The retained mechanisms were decomposed into Zyra
cursor, barrier, buffer, assembly, retry, source and diagnostic modules and
connected to Zyra schemas, errors, typed transport and runtime-event spine.

## 3. Default path and state custody

The production path is:

`createZyraApi().events -> TaskEventTransport -> EventIngressCoordinator ->
TaskApiEventIngressSource -> FetchApiTransport -> EventIngressApiFacade ->
RuntimeEventSpineBridge`.

The public event entry remains the existing `createZyraApi().events` facade.
There is no second browser client, direct `fetch`, alternate authentication
path, alternate event log or fixture fallback.

| State | Canonical owner | Slice ownership |
| --- | --- | --- |
| durable event identity, global/aggregate sequence, causation, correlation, state delta and artifacts | TypeScript `RuntimeEventSpine` | read and validate only |
| durable event persistence | existing runtime-event spine / event log | no write path |
| cursor tokens and snapshot/delta protocol | Python `EventIngressApiFacade` | signed, task/filter-scoped, expiring read cursor; no event mutation |
| request/auth/version/cancellation | existing `FetchApiTransport` | one added streaming response lifecycle |
| generation, transport session and retry | browser `EventIngressCoordinator` | transient and bounded |
| snapshot/live merge, ordered buffer, gap and identity windows | browser ingress modules | transient and bounded |
| partial/final, orphan and tombstone bookkeeping | `PartialEventAssembler` | transient delivery preparation only |
| subscriber union and observer isolation | `TaskEventTransport` / `IngressSubscriptionRegistry` | browser subscription state only |
| task/run/message/permission/artifact UI projection | reserved for M2-S01B-02 | not implemented by this slice |

The Python facade has explicit `canonicalWriteAllowed: false` response metadata
and only calls the existing runtime-event bridge query. Browser unsubscribe
closes the browser coordinator and transport session but does not call backend
task cancellation.

## 4. Implemented behavior and failure semantics

The implementation provides:

- signed opaque cursors scoped to task, filter digest, cursor kind, generation,
  snapshot boundary and expiry;
- capability negotiation for SSE, WebSocket descriptors and protocol-equivalent
  long polling;
- live-subscribe-before-snapshot ordering, captured snapshot pagination and
  deterministic merge of interleaved live frames;
- explicit predecessor chains, monotonic committed cursor, per-frame cursors,
  duplicate/stale suppression and conflict rejection;
- bounded catch-up for a forward gap and reconnect/resync from the last
  committed cursor;
- normalized full runtime-event identity including task/run/session,
  span/parent-span, tool call, checkpoint, artifacts, correlation, causation
  and state mutation;
- partial/final chain settlement, bounded orphan release and explicit
  tombstone targeting;
- item/byte ceilings, high/critical watermarks and reserved effective/terminal
  capacity;
- bounded reconnect attempts, exponential delay, transport cooldown,
  heartbeat timeout and cursor generation fencing;
- fragmented SSE decoding, WebSocket client implementation and long-poll
  fallback through one validator/cursor/buffer path;
- status, receipts, diagnostics, audits and visible disable failures.

Unknown schema versions, cross-task frames, digest changes, cursor tampering,
cursor scope mismatch, cursor regression, duplicate conflicts, buffer overflow,
unhealed gaps, transport exhaustion and disabled modules fail closed. No event
is fabricated to preserve liveness.

## 5. Adversarial findings fixed before implementation freeze

| Finding | Severity | Evidence | Fix |
| --- | --- | --- | --- |
| Finite SSE declared keep-alive without chunked transfer or Content-Length, so browser EOF/reconnect was nondeterministic. | high | real Bun/browser probe intermittently stopped at generation 1 | finite SSE declares `Connection: close`; the embedded handler closes after its terminal frame |
| Multi-frame snapshots started gap healing before the first buffered predecessor was committed. | high | real survivor task reached committed sequence with zero observer batches | a whole page is buffered, then drained in predecessor order; only a remaining gap starts catch-up |
| Heartbeat timeout aborted the subscription-lifetime controller, so the run loop treated transport failure as user cancellation. | high | aggregate real-API run intermittently skipped reconnect | watchdog closes only the active session; the outer cursor/reconnect lifecycle remains alive |

The multi-frame regression now uses a jump sequence `0 -> 20 -> 21 -> 22` and
asserts three delivered events, zero resync attempt and a clean coordinator
audit. The real browser probe proves natural finite-SSE reconnect, signed cursor
resume, long-poll reopen and last-subscriber shutdown against an isolated real
API/store.

## 6. Verification

All current-slice and directly affected commands below are green at the frozen
implementation:

| Command | Result |
| --- | --- |
| typed-client and Web TypeScript typecheck | PASS |
| `bun test ./apps/web/test/event-ingress.test.ts` | PASS; 16 tests, 68 assertions |
| `bun test ./packages/core/typed-api-client/test/transport.test.ts` | PASS; 15 tests, 35 assertions |
| `bun run --cwd apps/web build` | PASS; 89 modules, 1,559,679-byte JS, 27,789-byte CSS |
| three directly affected Python integration files | PASS; 9 tests in 53.81 s |
| new snapshot/delta/browser integration file | PASS; 2 tests in 17.08 s |
| repeated real browser reconnect probe | PASS; 1 test in 9.42 s |
| Python compile of the facade and API handler | PASS |
| exact effective-line auditor | PASS; 9,600 effective against 7,500 |
| slice ledger synchronizer | PASS; 5 aligned entries |

The full Web test command was also sampled. All 31 current event-ingress and
typed-transport tests passed, as did 30 protected M2-S01A workbench tests. One
protected `CommandQueue` test failed because same-millisecond enqueue timestamps
fall through to random UUID lexical order (`two, one` instead of FIFO
`one, two`). The current slice changes no command/queue file and its tests do
not share that state. Per the protected-slice and slice-incremental rules, the
unrelated prior-slice flake is recorded for the M2-01 aggregate review rather
than modifying M2-S01A-02 here.

`ruff` is not installed in the project virtual environment; no Ruff result is
claimed. Python compilation and real API behavior tests are green.

## 7. Effective-code audit

`scripts/audit_m2_s01b_01_effective_lines.mjs` uses exact Git added-line sets.
TypeScript compiler AST classification excludes imports, interfaces, type
aliases, export-only declarations, overload signatures and static schema
constants. Python AST classification excludes imports, docstrings, enum bodies,
module constants and DTO fields. Route/client glue is conservatively
adapter-only. Tests, fixtures, docs, presentation, generated and
vendor/source-pool content receive zero credit.

| File | Raw | Runtime | Type | Schema | Adapter | Test | Docs | Effective |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `apps/api/zyra_api/event_stream_ingress.py` | 1,221 | 1,067 | 20 | 49 | 0 | 0 | 85 | 1,067 |
| `apps/api/zyra_api/main.py` | 157 | 0 | 0 | 0 | 157 | 0 | 0 | 0 |
| `apps/web/src/api/client.ts` | 41 | 0 | 0 | 0 | 41 | 0 | 0 | 0 |
| `apps/web/src/api/event-transport.ts` | 248 | 196 | 41 | 0 | 0 | 0 | 11 | 196 |
| `apps/web/src/api/task-api.ts` | 216 | 0 | 0 | 0 | 216 | 0 | 0 | 0 |
| `apps/web/src/events/ingress/assembly.ts` | 878 | 802 | 64 | 0 | 0 | 0 | 12 | 802 |
| `apps/web/src/events/ingress/contracts.ts` | 830 | 328 | 482 | 7 | 0 | 0 | 13 | 328 |
| `apps/web/src/events/ingress/coordinator.ts` | 1,105 | 1,003 | 81 | 0 | 0 | 0 | 21 | 1,003 |
| `apps/web/src/events/ingress/cursor-ledger.ts` | 754 | 691 | 42 | 0 | 0 | 0 | 21 | 691 |
| `apps/web/src/events/ingress/diagnostics.ts` | 358 | 341 | 13 | 0 | 0 | 0 | 4 | 341 |
| `apps/web/src/events/ingress/errors.ts` | 341 | 306 | 22 | 0 | 0 | 0 | 13 | 306 |
| `apps/web/src/events/ingress/gap-recovery.ts` | 495 | 443 | 38 | 0 | 0 | 0 | 14 | 443 |
| `apps/web/src/events/ingress/identity-window.ts` | 441 | 391 | 33 | 0 | 0 | 0 | 17 | 391 |
| `apps/web/src/events/ingress/index.ts` | 14 | 0 | 14 | 0 | 0 | 0 | 0 | 0 |
| `apps/web/src/events/ingress/ordered-buffer.ts` | 674 | 610 | 48 | 0 | 0 | 0 | 16 | 610 |
| `apps/web/src/events/ingress/reconnect.ts` | 434 | 375 | 33 | 0 | 0 | 0 | 26 | 375 |
| `apps/web/src/events/ingress/snapshot-barrier.ts` | 681 | 635 | 33 | 0 | 0 | 0 | 13 | 635 |
| `apps/web/src/events/ingress/subscriptions.ts` | 403 | 337 | 44 | 0 | 0 | 0 | 22 | 337 |
| `apps/web/src/events/ingress/transport-sources.ts` | 815 | 742 | 52 | 2 | 0 | 0 | 19 | 742 |
| `apps/web/src/events/ingress/validator.ts` | 1,208 | 1,158 | 43 | 5 | 0 | 0 | 2 | 1,158 |
| `apps/web/test/event-ingress-probe.ts` | 262 | 0 | 0 | 0 | 0 | 262 | 0 | 0 |
| `apps/web/test/event-ingress.test.ts` | 721 | 0 | 0 | 0 | 0 | 721 | 0 | 0 |
| prospective decision document | 125 | 0 | 0 | 0 | 0 | 0 | 125 | 0 |
| typed-client constants | 8 | 0 | 0 | 8 | 0 | 0 | 0 | 0 |
| typed-client normalizer registration | 9 | 0 | 0 | 0 | 9 | 0 | 0 | 0 |
| typed-client protocol declarations | 84 | 0 | 0 | 84 | 0 | 0 | 0 | 0 |
| `packages/core/typed-api-client/src/transport.ts` | 189 | 175 | 12 | 0 | 0 | 0 | 2 | 175 |
| Python integration test | 265 | 0 | 0 | 0 | 0 | 265 | 0 | 0 |
| **Total** | **12,977** | **9,600** | **1,115** | **155** | **423** | **1,248** | **436** | **9,600** |

UI behavior/presentation, generated and vendor-like/source-pool additions are
all zero. The 9,600 effective production lines exceed the 7,500 minimum by
2,100. The M2-01B parent remains open; its 15,000-line minimum will be
recomputed directly at the parent-final implementation commit rather than by
summing child reports.

## 8. Concentration audit

No file contributes more than 20% of effective production. The largest is
`validator.ts` at 1,158 / 9,600 = 12.1%.

Files above 500 added lines have bounded, distinct responsibilities:

- `event_stream_ingress.py`: signed cursor, task query, page boundary, delta
  wait and finite SSE protocol facade;
- `assembly.ts`: partial/final settlement, orphan release, tombstone targeting
  and bounded expiry;
- `contracts.ts`: shared protocol types plus executable normalization helpers;
- `coordinator.ts`: the single generation/session composition root;
- `cursor-ledger.ts`: monotonic cursor observation/commit/audit;
- `ordered-buffer.ts`: predecessor-ordered bounded queue and reserved capacity;
- `snapshot-barrier.ts`: subscribe-before-snapshot retention and merge;
- `transport-sources.ts`: SSE parser/session, WebSocket client and long-poll
  source;
- `validator.ts`: strict untrusted envelope/page/capability normalization;
- `event-ingress.test.ts`: one directly related behavior suite; all 721 lines
  receive zero production credit.

`contracts.ts` is the only file with an exclusion bucket above 30%: 482
interface/type lines plus 7 static schema lines, 60.5% of raw additions, receive
zero credit. Its 328 credited lines are executable capacity/retry/filter,
priority, identity and freeze helpers exercised by the ingress tests.

## 9. Anti-fake-internalization and dependency review

- Default-path reachability is proven by a Bun browser client against a real
  embedded API, isolated SQLite store and TypeScript runtime-event spine.
- Removing `EventIngressCoordinator`, `EventIngressApiFacade`, the cursor
  ledger, snapshot barrier or ordered buffer breaks a directly related unit or
  integration behavior; disabling those modules fails visibly.
- Python, TypeScript and browser state all use Zyra task/event/cursor/error
  structures. No upstream directory shape, source pool, manifest scan or fixed
  contract response is used as completion evidence.
- Runtime source scan found no current-slice parent-repository path, npm link,
  editable parent path, external Docker context, dynamic import or OpenClaw
  reference.
- Direct browser `fetch` remains confined to the existing
  `FetchApiTransport`; the new source uses `TaskApi`.
- No dependency, lockfile, subprocess, port, MCP server or external service was
  added.
- This is an additive owner assigned by the current slice. It does not make an
  incompatible schema/persistence change, move a canonical owner or change a
  global permission/scheduler/recovery/compact policy. No high-risk escalation
  is triggered; full cleanroom remains mandatory at the M2-01 aggregate layer.

The general ledger verifier still reports the same three historical global
forbidden-relative-source findings in
`packages/evaluation/zyra_evaluation/m1_hardening/long_horizon_runtime.py`.
They are outside this diff. The five current slice entries validate and
synchronize with zero slice finding or error.

## 10. Scope closure

This slice advances ordered canonical event trace, reconnect/exact-resume,
real browser transport and bounded recovery evidence. It does not claim the
M2-S01B-02 UI reducer/store, panels, the M2-01B parent line gate, parent
cleanroom, dynamic-topology evidence or an M2 exit gate.

Result: `M2-S01B-01` satisfies its prospective source/language decision,
default-path, state-custody, behavior, failure, dependency, evidence and
7,500-line gates at 9,600 effective production lines. The next slice is
`M2-S01B-02`.
