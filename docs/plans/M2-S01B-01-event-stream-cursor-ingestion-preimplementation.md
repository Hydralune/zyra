# M2-S01B-01 event stream and cursor ingestion preimplementation decision

Date: 2026-07-23

Authority:
`docs/milestones/M2-console-demo/slice-01b-01-event-stream-cursor-ingestion.md`

Baseline commit: `1f41ab17864dd789948aeebe3723540007ebaf2b`

Parent-unit baseline commit:
`1f41ab17864dd789948aeebe3723540007ebaf2b`

This record is frozen before the first production-code change. It applies the
2026-07-22 effective-code/language/migration gate and fixes the source roles,
language path, canonical owners, and migration modes for this slice.

## Source, language, migration, and owner decision

| Role | Source repository and bounded path | Source language | Zyra target | Target language | Migration mode | Canonical owner after the slice | Preimplementation decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| primary | `opencode/packages/app/src/context/server-sdk.tsx` | TypeScript / TSX | `apps/web/src/events/ingress/{coalescing,delivery,reconnect,supervisor,transport}.ts` | TypeScript | `same_language_crop_and_adapt` | Zyra browser event-ingress supervisor owns only live connection, retry generation, heartbeat, batching, and bounded delivery state | Preserve the single live-stream generation, abort fencing, reconnect loop, heartbeat timeout, frame-yield behavior, last-subscriber shutdown, and adjacent-update coalescing. Replace Solid context, source SDK events, directory emitters, and OpenCode event names with the Zyra cursor protocol and runtime-event envelope. |
| primary | `opencode/packages/app/src/context/server-session.ts` | TypeScript / TSX | `apps/web/src/events/ingress/{snapshot-barrier,ordered-buffer,identity-window,partial-assembly,tombstones,pressure}.ts` | TypeScript | `same_language_retained_control_flow_adapt` | Zyra ingress coordinator owns transient snapshot/live merge state only; M1 stores remain canonical and M2-S01B-02 will own UI projections | Preserve event-during-request freshness, generation fencing, in-flight deduplication, deterministic identity merge, partial/delta retention across retry, orphan handling, removal markers, and bounded cache pressure. Replace session/message/part ownership with generic normalized event receipts and do not migrate the canonical frontend session store. |
| primary | `opencode/packages/app/src/context/server-sync.tsx`, `global-sync/event-reducer.ts`, `global-sync/session-cache.ts` | TypeScript / TSX | `apps/web/src/events/ingress/{subscription-registry,scope,diagnostics}.ts` and `apps/web/src/api/event-transport.ts` | TypeScript | `same_language_crop_and_adapt` | One Zyra `TaskEventTransport` facade owns browser subscription union and transient diagnostics | Preserve normalized subscription scopes, reference-counted start/stop, bounded cache eviction, and stream-to-consumer isolation. Do not migrate OpenCode reducers or create a second UI projection owner. |
| supplementary | `OpenHands/frontend/src/api/event-service/event-service.api.ts`, `event-service.types.ts` | TypeScript | `apps/web/src/events/ingress/{http-source,long-poll-source,history-source}.ts` | TypeScript | `same_language_bounded_integration` | Zyra ingress transport sources own HTTP request progress only | Preserve bounded paging, explicit sort direction, count/limit validation, missing-event handling, and bounded parallel history fetch. Replace Axios, OpenHands event DTOs, and conversation state with the existing Zyra typed client and cursor contract. |
| supplementary | `OpenHands/openhands/app_server/event/event_service_base.py`, `event_router.py`, `event_store/event_store_service.py` | Python | `apps/api/zyra_api/event_stream_ingress.py`, `apps/api/zyra_api/main.py` | Python | `same_language_crop_and_adapt` | Existing TypeScript runtime-event spine remains canonical; the Python API facade owns snapshot/delta/transport negotiation only | Preserve cursor-bounded query construction, stable order, explicit count/page limits, not-found/error mapping, and transport-facing response metadata. The facade must query the existing runtime-event bridge and may not persist, renumber, reduce, or mutate canonical events. |
| primary foundation | Existing Zyra `packages/runtime/runtime-event-spine`, `packages/runtime/zyra_runtime/runtime_events`, `packages/core/typed-api-client`, and `apps/web/src/api/**` | TypeScript / Python | Event ingress protocol, API facade, and default `createZyraApi()` event path | TypeScript / Python | `same_language_integrate_existing` | Runtime event spine owns canonical event sequence and causality; typed client owns request/auth/version behavior | Reuse canonical `globalSequence`, aggregate sequence, correlation, causation, state delta, artifact refs, and identity. Extend the existing typed API path; no direct browser `fetch`, alternate auth path, second event log, or fabricated event fallback is allowed. |
| conformance_only | LangGraph checkpoint identity/lineage, pending-versus-committed writes, stable task identity, and exact-resume correlation as limited by `source-graphs/langgraph/source-graph.md` section 12 | Python | `apps/web/test/**`, `tests/integration/**` | TypeScript / Python | `conformance_only` | No production owner | Verify committed cursor monotonicity, exact resume after a disconnect, and no promotion of a pending/gapped event. Do not migrate StateGraph, channels, Pregel, stream controller, store, ToolNode, SDK, server, or deployment code. |
| conformance_only | Agent Framework AG-UI history/approval envelopes and `oh-my-pi` request-correlation/cancellation behaviors already bounded by M2-S01A | Python / TypeScript | Event-ingress conformance tests | TypeScript / Python | `conformance_only` | No production owner | Compare correlation continuity, cancel targeting, schema rejection, and replay/resume behavior only. No second stream controller, workflow store, or RPC runtime is migrated. |
| excluded_forward_only | OpenClaw | N/A | none | none | `excluded_forward_only` | none | Do not read, restore, depend on, test against, or cite OpenClaw as an implementation or conformance source. |

## Fixed implementation boundaries

- The TypeScript runtime-event spine remains the canonical event owner. Its
  durable `globalSequence`, aggregate sequence, event identity, causation,
  correlation, state mutation, and artifact references are not renumbered or
  reinterpreted by the Python API or browser.
- `apps/api/zyra_api/event_stream_ingress.py` is a read-only protocol facade.
  It may validate cursor tokens, create a consistent snapshot boundary, page
  committed deltas, wait for a high watermark, and advertise supported
  transports. It may not create an event store, reducer, delivery queue, or
  write fallback.
- `apps/web/src/events/ingress/**` owns transient subscription, transport,
  cursor, buffering, gap, duplicate, partial/final, orphan/tombstone,
  backpressure, retry, and diagnostic state. It does not own the canonical UI
  task/run/message/permission/artifact projections assigned to M2-S01B-02.
- The public browser entry remains `createZyraApi().events`. Existing callers
  continue to use `TaskEventTransport`; the implementation behind it becomes
  the new ingress coordinator rather than a second public stream controller.
- A connection starts its live delta subscription before requesting the
  snapshot. Live frames observed while the snapshot is in flight are retained,
  and the snapshot plus interleaved frames are committed in deterministic
  canonical sequence without allowing an older snapshot to overwrite a newer
  live identity.
- Every accepted frame carries a schema version, cursor generation, monotonic
  global sequence, event identity, and normalized bindings for task/run/session,
  span/parent-span, tool call, artifact, checkpoint, correlation, causation, and
  state mutation when present. Unknown versions and cross-task bindings fail
  closed and trigger a bounded resync rather than local fabrication.
- Duplicate or stale frames are acknowledged diagnostically but are not
  redelivered. A forward sequence gap pauses committed delivery, requests a
  bounded catch-up page, and reconnects from the last committed cursor if the
  gap cannot be healed.
- Partial events may be coalesced only for the same event/part identity and
  generation. A final event settles its partial chain. Tombstones remove only
  their declared transient identity; unmatched child events remain bounded
  orphans until their parent arrives or the orphan policy expires them.
- Buffers have explicit item and byte ceilings, reserved capacity for effective
  and terminal events, watermarks, coalescing rules for declared non-effective
  updates, and deterministic overload failure. No unbounded array, map, retry
  queue, subscription set, or diagnostics journal is allowed.
- Transport negotiation prefers server-advertised WebSocket or SSE when the
  runtime supports them and retains long polling as a protocol-equivalent
  fallback. All transports share the same frame validator, cursor ledger, gap
  recovery, cancellation, and retry budget; transport changes do not reset the
  committed cursor.
- Closing or hiding a browser stops only that browser subscription. It does not
  cancel the backend task. Reopening with the last committed cursor converges
  with a fresh snapshot and does not duplicate committed events.
- Disabling the ingress coordinator, cursor ledger, snapshot barrier, or
  bounded buffer must make the corresponding real behavior fail or visibly
  degrade. No old polling loop or fixture replay may mask the failure.
- The slice does not implement the M2-S01B-02 UI reducer/store and does not add
  presentation panels. Consumers receive ordered immutable ingress batches and
  explicit status/diagnostic snapshots for the next slice.

## Language and effective-code obligations

The primary source and target event-ingress control flow are TypeScript, so
non-zero original-language TypeScript production code is mandatory. The
OpenHands supplementary frontend path is adapted in TypeScript, and its bounded
server-side pagination/error behavior is adapted in Python. No cross-language
rewrite exception is requested.

The slice minimum is 7,500 effective production lines. The parent M2-01B
minimum is 15,000 effective production lines and will be recomputed directly
at its final slice. Final evidence will bucket every changed file into runtime
behavior, excluded types/declarations, schema/DTO/data, adapter-only glue,
tests/mocks/fixtures, docs/comments, generated content, UI
behavior/presentation, and vendor-like/source-pool material. Files above 500
raw lines, files contributing over 20% of effective code, and files with an
exclusion bucket over 30% require a separate concentration audit.

Production volume alone does not close the slice. The implementation must pass
build/typecheck plus real snapshot/delta server and browser-ingress tests for
subscribe-before-snapshot interleaving, deterministic order, gaps, duplicates,
stale cursors, partial/final assembly, tombstones/orphans, bounded
backpressure, schema/version rejection, reconnect/exact resume, cancellation,
transport fallback, last-subscriber shutdown, and disable-path behavior.

## Commit boundary

- `baseline_commit`:
  `1f41ab17864dd789948aeebe3723540007ebaf2b`
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
  `parent_baseline_commit..parent-final-implementation-commit` and will be
  audited directly, not by summing child-slice reports.
