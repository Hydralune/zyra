# M2-S03B-03 Causal Trace Cross-view Integration Pre-implementation Decision

Status: frozen before production changes

Decision date: 2026-07-25

Baseline commit: `ca1d1b1eee0ef04451cc619ddc2b3f944f30f99e`
Slice: `docs/milestones/M2-console-demo/slice-03b-03-causal-trace-cross-view-integration.md`

## 1. Canonical ownership and integration boundary

- `CanonicalProjectionStore`, its immutable projection state, and the M2-S01B
  causal event indexes remain the only browser-side owners of committed task,
  run, worker, tool, permission, artifact, checkpoint, placement, fault, and
  recovery facts.
- The trace feature builds a disposable derived index from canonical selectors.
  It may keep filter, fold, viewport, selected span, navigation history, and
  pinned-report-reference state in memory. It must not persist a second event
  history, replay store, checkpoint store, or causal truth.
- Timeline, topology, terminal, browser, artifact, and diff views remain owners
  of their own view-local interaction state. Cross-view navigation uses typed
  entity and correlation identifiers and emits a bounded focus request; it never
  changes task execution, permission, placement, recovery, or lifecycle state.
- Missing, partial, late, orphaned, duplicate, and quarantined records remain
  visible as diagnostics. The trace index never invents a join by comparing
  summaries, labels, terminal text, URLs, filenames, or other display strings.
- Closing or disabling the trace viewer releases only derived indexes and local
  subscriptions. The canonical run and every worker/session continue unchanged.

## 2. Source, language, and migration decision

| Source role | Source repository and exact paths | Source language | Zyra target | Target language | Migration mode | Counted production boundary |
| --- | --- | --- | --- | --- | --- | --- |
| primary semantic source | `zyra@ca1d1b1eee0ef04451cc619ddc2b3f944f30f99e`: `apps/web/src/state/contracts.ts`, `apps/web/src/state/selectors.ts`, `apps/web/src/state/panel-selectors.ts`, `apps/web/src/features/timeline/projection/{event-reader,causal-graph,critical-path,drilldown}.ts` | TypeScript | `apps/web/src/features/trace/**` and the task-detail main-path mount | TypeScript/TSX | `same_language_component_integration` | Canonical admission, entity/span indexing, deterministic typed joins, critical-path computation, diagnostics, filters, folds, virtualization, local pins, report references, and connected cross-view behavior count. Canonical state declarations and repeated DTOs do not count. |
| supplementary implementation source | `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`: `packages/coding-agent/src/modes/rpc/rpc-types.ts`, `packages/coding-agent/src/modes/rpc/rpc-client.ts`, `packages/coding-agent/src/modes/rpc/rpc-subagents.ts`, `packages/coding-agent/src/task/yield-assembly.ts`, `packages/coding-agent/src/tools/bash-pty-selection.ts` | TypeScript | `apps/web/src/features/trace/{correlation,reconciliation,navigation}/**` | TypeScript | `cropped_migration` and `same_language_component_integration` | Optional request IDs, partial/final settlement, stale/late fencing, reconnect epochs, parent tool/subagent ownership, yield provenance, and PTY selection correlation count after Zyra identity and canonical-event integration. Generic RPC transport, session files, agent loop, and OMP task runtime do not migrate. |
| inherited view mechanism, no new quota | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda`, `browser-use@18484f23ac96bb955259a1c54530a7d265dfffdb`, and `OpenHands@c105a82387898e744423c8831d412e26495b38a9` mechanisms already accepted in M2-S03A/M2-S03B-01/02 | TypeScript/TSX and Python | existing `features/{terminal,browser,artifacts,diff-review,timeline,topology}/**`; this slice adds typed navigation consumers only | TypeScript/TSX over existing typed protocols | prior accepted migrations; `same_language_component_integration` for the new navigation surface | Only new Zyra typed focus resolution, availability checks, reverse navigation, and failure reporting count here. Previously counted viewer/runtime code and browser Python semantics are not recounted. |
| conformance only | Hermes auth/replay behavior identified by the slice contract | Python | trace integration and reconnect tests | TypeScript/Python tests only | `conformance_only` | No production line quota and no replay/session owner. |
| excluded forward only | OpenClaw | n/a | none | n/a | `excluded_forward_only` | No reading, migration, comparison, dependency, or code quota. |

## 3. Language and cross-language finding

The new production implementation is TypeScript/TSX because both the canonical
projection owner and the allocated console surface are TypeScript. The primary
and active supplementary sources therefore require non-zero same-language
production implementation. No new cross-language semantic port is authorized in
this slice.

Browser records that originated in the Python BrowserWorker runtime are consumed
only through the already accepted typed browser/canonical projections. This
slice does not re-port browser-use models, execute Python browser logic in the
web client, or claim those Python lines again. Python remains the authoritative
browser execution/artifact source language through the existing worker path.

## 4. Planned Zyra modules

- `apps/web/src/features/trace/index/**`: canonical record admission, span and
  entity indexes, deterministic edges, hierarchy, reverse indexes, quarantine,
  late reconciliation, and completeness diagnostics.
- `apps/web/src/features/trace/analysis/**`: critical path, contribution and
  latency attribution, permission/compact/placement/fault/MCP/skill/subagent
  semantic classification, search, filter, fold, and aggregate summaries.
- `apps/web/src/features/trace/navigation/**`: typed cross-view destinations,
  availability resolution, terminal/browser/artifact/timeline/topology focus,
  reverse navigation, and final-report references.
- `apps/web/src/features/trace/view/**`: task-bound virtualized trace workbench
  with bounded local controller state, keyboard/accessibility behavior, and
  explicit missing/late/orphan/quarantine presentation.
- Direct tests under `apps/web/test/trace-*.test.ts` and integration coverage
  that proves real canonical-event reachability, typed correlation, disable
  behavior, reconnect, large-run scale, and viewer-close isolation.

## 5. Required behavioral evidence

- One canonical task/run chain traverses task, worker, tool, PTY or browser
  action, artifact, mutation/checkpoint, and recovery facts using exact IDs in
  both directions.
- Permission, compact/checkpoint, placement/provider retry, fault/recovery, MCP
  reconnect, skill execution, subagent spawn/yield/finalization, and partial/final
  settlement remain separately classifiable and inspectable.
- Late artifact and final events reconcile without duplicate nodes; an orphan or
  identity conflict is quarantined instead of guessed into a chain.
- Search, filters, folds, critical path, and virtual windows remain deterministic
  for a large trace and do not require retaining duplicate canonical payloads.
- Terminal, browser, artifact/diff, timeline, and topology navigation succeeds
  only when a typed destination can be resolved, and reverse navigation returns
  to the exact trace entity/span.
- Disabling the index/join module makes claimed cross-view trace behavior fail
  explicitly. Closing the viewer cannot stop or mutate the underlying task.

## 6. Effective-code accounting intent

The implementation threshold is at least 5,500 effective production lines in
`baseline..implementation`. Executable admission, indexing, reconciliation,
graph algorithms, typed correlation, navigation resolution, virtualization,
controller behavior, and connected UI behavior may count. Tests, docs, comments,
generated output, interfaces/type-only declarations, schema-shaped DTOs,
fixtures/mocks, presentation-only styling, protocol-only adapters, and ledger
content are excluded.

M2-03B closes in this slice, so the evidence stage will directly recompute the
parent from `cc92c129ead234a82cf23dad5a1c32e3bf35f06f` to the final implementation
commit. The M2-03 numeric-stage aggregate will directly recompute from
`1833319acdb8a09fac3438b12356da0e9c78d6bb`, run the applicable cleanroom,
source-to-target, API/UI/worker regression and build checks, and record a
separate critical review against the same final implementation commit.
