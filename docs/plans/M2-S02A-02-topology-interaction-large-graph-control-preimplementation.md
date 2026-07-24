# M2-S02A-02 topology interaction and large-graph control preimplementation decision

## 1. Authority and immutable baseline

- Slice: `M2-S02A-02`
- Parent: `M2-02A`
- Zyra baseline: `a2fa483a75ccae4c14bf9cd94aa0cda41346c28e`
- Parent and numeric-stage baseline: `f7fff49be91fb0f797260c03ff9cca76c06c5974`
- Protected predecessor: `M2-S02A-01`
- Slice minimum: `6,500` effective production TypeScript/TSX behavior lines.
- Parent minimum: `13,000` effective production TypeScript/TSX behavior lines, recomputed from
  `f7fff49be91fb0f797260c03ff9cca76c06c5974` to the eventual current-slice
  implementation commit.
- Parent closeout occurs in this slice. The M2-02 numeric-stage aggregate remains
  pending until `M2-S02B-02`.

The current worktree was clean at the declared baseline. The slice does not
rewrite protected `M2-S02A-01` projection behavior and does not move graph,
route, placement, checkpoint, permission, or recovery canonical ownership.

## 2. Source-role decision

| Role | Source | Source language | Target | Target language | Migration mode | State owner | Decision |
|---|---|---|---|---|---|---|---|
| primary foundation | Zyra `apps/web/src/features/topology/projection/**` from `M2-S02A-01` | TypeScript | `apps/web/src/features/topology/view/**` and task-detail main path | TypeScript / TSX | `same_language_integrate_existing` | M2-01B canonical projection store remains the only frontend fact writer | Consume immutable `TopologyProjectionView`; add only transient viewport, filter, selection, focus, layout cache and receipt display state. |
| primary control transport | Zyra M2-01A typed client plus M1 `ControlCommandRegistry`, `RuntimeControlDispatcher`, permission runtime and recovery/session owners | TypeScript / Python | typed endpoint extension, topology control client, receipt projection and API owner glue | TypeScript / Python | `same_language_integrate_existing` plus bounded owner glue | Backend M1 permission/recovery/session/graph owners remain canonical | All update, change, checkpoint-resume and time-travel actions submit typed `/tasks/{task_id}/commands` mutations. UI never applies optimistic canonical graph/checkpoint changes. |
| reference_only | OpenCode graph/session/task UI patterns selected by the parent source graph | TypeScript / TSX | interaction and failure-state review only | none | `reference_only` | none | Check dense inspection, keyboard navigation and durable receipt visibility; migrate no store, client or graph runtime. |
| reference_only | OpenHands task/session visualization patterns selected by the parent source graph | TypeScript / TSX | panel hierarchy and operator-state review only | none | `reference_only` | none | Check selection/detail/timeline jumps and loading/error/reconnect affordances; migrate no canonical session store or event client. |
| conformance_only | LangGraph checkpoint identity/lineage, pending-versus-committed writes, atomic commit, stable task id and exact-resume narrow contract | Python | TypeScript behavior tests and backend integration tests | TypeScript / Python | `conformance_only` | none | Validate identity, lineage, write visibility, interrupt/resume correlation and exact checkpoint targeting only. No StateGraph, Pregel, channel/reducer, ToolNode, SDK, server or deployment code enters production. |
| excluded_forward_only | OpenClaw | none | none | none | `excluded_forward_only` | none | No source read, comparison, ledger row, migration, dependency, package, process or path is introduced. |

No supplementary implementation source is required. No cross-language port
exception is claimed: frontend interaction is implemented in TypeScript/TSX;
the bounded Python edits only connect already-owned M1 command/session/recovery
behavior to the existing API route.

## 3. Planned Zyra-owned modules

The view package is split by runtime responsibility instead of by upstream
directory shape:

1. `model` converts the immutable `TopologyProjectionView` into renderable
   graph records without becoming another projection store.
2. `layout` maintains deterministic incremental coordinates, pins stable nodes,
   lays out open-world additions and removes stale cache entries.
3. `spatial` indexes graph bounds and returns only viewport-visible entities.
4. `cluster` computes level-of-detail groups and expansion state.
5. `filter` and `search` derive node/edge/route/placement subsets with stable
   match ranking.
6. `layers` derives route density, churn, broadcast, placement, provider/model,
   privacy and policy-violation overlays.
7. `navigation` translates selection and evidence into task/detail, artifact,
   timeline and causation jump intents.
8. `interaction` owns pan, zoom, pointer, hover, selection, keyboard focus,
   minimap viewport and screen-reader traversal.
9. `controls` validates local-update/time-travel inputs, submits typed control
   commands and exposes immutable pending/denied/failed/committed receipts.
10. React components render a virtualized accessible SVG graph, toolbar,
    minimap, inspector, checkpoint lineage and control receipts in the real task
    detail route.

The typed API change adds an endpoint contract and normalizer for the existing
`POST /tasks/{task_id}/commands` route. It is transport glue and is excluded
from effective-line credit.

## 4. Owner and data-flow boundary

```text
RuntimeEventSpine
  -> TaskEventTransport
  -> EventIngressCoordinator
  -> CanonicalProjectionStore (only frontend fact writer)
  -> selectTopologyProjection
  -> topology view model / layout / spatial index / local interaction state
  -> virtualized accessible SVG + inspector/minimap/layers

operator action
  -> TaskApi.controlCommand (typed M2-01A transport)
  -> POST /tasks/{task_id}/commands
  -> ControlCommandRegistry / RuntimeControlDispatcher
  -> M1 deterministic permission authorization
  -> canonical graph/session/recovery owner
  -> event/checkpoint persistence
  -> event ingress and canonical projection refresh
  -> immutable receipt + observed canonical effect in the UI
```

Selection, hover, focus, pan, zoom, expanded clusters, filters, search text and
layout coordinates are transient browser state. Node/edge existence, route,
placement, checkpoint, branch, write visibility, conflict, recovery and policy
facts remain canonical backend projections.

## 5. Effective-code plan

The audit will use the 2026-07-22 language/migration gate and assign every
changed file to one of:

- `production_behavior`
- `tests`
- `presentation`
- `types_interfaces`
- `adapter_only`
- `docs`
- `ledger_data`
- `generated`
- `mock_fixture`

Only executable TypeScript/TSX production behavior after exclusions counts.
Interface/type declarations, generated/API types, CSS, JSX-only presentation,
tests, fixtures, docs, ledger rows and thin transport adapters do not count.
Files over 500 raw lines, files contributing more than 20% of the slice total,
or files with more than 30% exclusions receive an individual audit note.

The planned production-behavior budget is:

| Responsibility | Minimum behavior lines |
|---|---:|
| graph model, filters, search and navigation | 1,200 |
| stable incremental layout, clustering, spatial index and virtualization | 2,000 |
| viewport, pointer, keyboard, minimap and accessibility interaction | 1,300 |
| topology layers and dense-graph diagnostics | 900 |
| control validation, submission, receipt and sealed-mode behavior | 700 |
| React orchestration behavior after presentation exclusions | 400 |
| Total planned | 6,500 |

## 6. Required behavioral evidence

The implementation and adjacent regressions must demonstrate:

- at least 2,000 nodes with viewport virtualization, bounded rendered entities,
  LOD clustering, search, filters, minimap and deterministic incremental layout;
- stable coordinates across out-of-order and rapid canonical revisions, plus
  open-world node/edge add/remove/replace behavior without a compile-time graph;
- node/edge hover, selection, keyboard traversal and screen-reader summaries;
- details, route, placement, checkpoint lineage, pending/committed write,
  conflict/rebase, interrupt, next-task and requirement-change inspection;
- artifact, timeline and causation jump intents derived from canonical evidence;
- route-density, route-churn, broadcast, placement, provider/model, privacy and
  policy-violation layers;
- loading, empty, integrity-error, disconnected/reconnecting and recovery states;
- typed local update and time-travel commands with idempotency, visible receipt
  phases and no optimistic canonical mutation;
- backend proof that allowed control changes a real checkpoint/recovery/route
  projection, while sealed mode deterministically denies the operator action
  and increments the intervention ledger;
- restart persistence and exact identity/lineage correlation through the
  existing backend stores;
- disabling the topology view package breaks the task topology behavior test,
  while disabling the backend control path fails closed rather than fabricating
  a successful UI result.

## 7. Validation scope

This ordinary slice runs current and adjacent Web tests, focused backend command
and topology integration tests, Web typecheck/build, source-ledger strict owner
audit, dependency/path scan and effective-line audit. Parent closeout recomputes
M2-02A cumulative behavior and owner consistency. Full M2-02 cleanroom,
repository-wide ledger audit and independent aggregate review remain assigned to
the M2-02A/02B numeric-stage closeout after `M2-S02B-02`, unless an actual
high-risk trigger appears during implementation.

