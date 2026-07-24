# M2-S02A-02 topology interaction and large-graph control self-review

Date: 2026-07-24

Authority:
`docs/milestones/M2-console-demo/slice-02a-02-topology-interaction-large-graph-control.md`

## Frozen commit interval

- slice baseline: `a2fa483a75ccae4c14bf9cd94aa0cda41346c28e`
- parent and numeric-stage baseline:
  `f7fff49be91fb0f797260c03ff9cca76c06c5974`
- preimplementation decision:
  `dde72a7bb36d6431d4138ff176617687241dc791`
- implementation commit:
  `6b15c5d8924a113553eac4d21922bc28239696dc`
- evidence commit: the commit containing this review, the audit script and the
  synchronized source ledger; its hash is recorded in
  `docs/milestones/execution-state.yaml`.

Production behavior and directly related tests are frozen in the implementation
commit. Evidence-only files do not contribute to effective production totals.

## Outcome

M2-S02A-02 and its parent M2-02A are complete.

The task-detail main path now embeds a topology workbench over the immutable
M2-S02A-01 projection. It provides:

- deterministic incremental layout that preserves existing node coordinates
  while nodes, edges, roles and capabilities are added, removed or replaced;
- a spatial index, viewport virtualization and hard render caps for a tested
  2,400-node and 3,100-plus-edge graph;
- multilevel clustering, level-of-detail expansion, minimap navigation, pan,
  zoom, pointer selection, selection boxes and keyboard traversal;
- query parsing, fuzzy search, graph/route/placement facets and filters;
- structure, route-density, route-churn, broadcast, placement, provider/model,
  privacy and policy-violation layers;
- node, edge, route, placement, checkpoint, branch, pending/committed write,
  conflict/rebase, interrupt/resume, next-task and requirement-change details;
- artifact, timeline and reversible-causation navigation intents;
- screen-reader rows, focus traversal, live announcements and non-canvas access
  to graph facts;
- typed `/change`, `/rewind` and `/resume` command submission, immutable
  receipts, idempotent replay, canonical-effect observation and stale
  projection fencing; and
- loading, empty, integrity-error, disconnected/reconnecting, denied and
  fail-closed disabled states.

The browser never writes canonical graph, checkpoint, permission, session or
recovery facts and never applies an optimistic topology mutation.

## Source role and language result

| Source | Role | Result |
| --- | --- | --- |
| Existing Zyra projection, typed client, command dispatcher, permission, session and recovery owners | `primary_implementation` | TypeScript/TSX interaction modules were integrated in the existing frontend language. Bounded Python owner glue preserves the existing control/recovery language and state custody. |
| OpenCode | `reference_only` | Dense session inspection, keyboard traversal and durable status presentation were checked; no store, client or runtime was migrated. |
| OpenHands | `reference_only` | Task hierarchy, detail/timeline navigation and failure-state presentation were checked; no Redux, router, session or sandbox owner was migrated. |
| LangGraph | `conformance_only` | Only checkpoint identity/lineage, pending/committed writes, interrupt/resume correlation and exact-resume behavior were tested. StateGraph, channel/reducer, Pregel, stream, Store, ToolNode, SDK/server/deploy own no production path. |

There is no supplementary implementation source and no cross-language port.
The two implementation languages retain their existing responsibilities.
OpenClaw remains `excluded_forward_only`: it has no source read, decision row,
runtime dependency, comparison test or restored path.

## Canonical owners and data flow

| State | Canonical owner | Slice behavior |
| --- | --- | --- |
| graph snapshot/delta/revision/commit | Python `GraphStateCustody` / `DynamicTopologyRuntime` | read-only projection input |
| route, resource and placement | existing scheduler/worker/provider owners | displayed, never rescored |
| permission decision and intervention ledger | Python `RuntimeControlDispatcher` and permission runtime | deterministic authorization and sealed denial |
| checkpoint, branch and exact resume | Python `RecoveryApplication` and existing stores | exact signed checkpoint selection and restore |
| session command lifecycle | existing command/session owners | canonical receipt and event persistence |
| browser graph facts | TypeScript `CanonicalProjectionStore` | remains the sole frontend fact writer |
| viewport, focus, selection, filters, layout and receipt display | TypeScript `TopologyWorkbenchController` | transient, revision-fenced view state only |

Read path:

`RuntimeEventSpine -> TaskEventTransport -> EventIngressCoordinator ->
CanonicalProjectionStore -> TopologyProjectionView -> view model/layout/spatial
index/controller -> virtualized workbench`.

Mutation path:

`operator -> TaskApi.controlCommand -> POST /tasks/{task_id}/commands ->
ControlCommandRegistry/RuntimeControlDispatcher -> permission ->
session/recovery owner -> persisted event/checkpoint -> ingress refresh ->
observed canonical effect`.

The controller rejects out-of-order projections and accepts a control receipt
only as lifecycle evidence. It waits for a later canonical projection before
reporting the graph/checkpoint effect.

## Dynamic reachability and semantic effect

- `task-detail.tsx` mounts `TopologyWorkbench` from the production topology
  panel, so the code is reached through the real task UI, not an import smoke.
- Layout, clustering, spatial queries and virtualization change rendered node
  and edge sets for zoom, viewport and open-world revisions.
- Search, facets and layer selection change the graph subset and annotations.
- Selecting graph evidence produces task, artifact, timeline and causation jump
  intents consumed by existing task-detail regions.
- `/change` reaches the real symbolic requirement-change path and emits a
  canonical mutation event.
- `/rewind` and `/resume` select an exact persisted checkpoint, preserve its
  signature/owner/version reference and emit recovery topology-route events.
- Sealed competition mode denies operator mutation deterministically, records
  one intervention for an idempotently replayed request and keeps
  `human_intervention_count=0`.
- Disabling the view/controller throws `TOPOLOGY_VIEW_DISABLED`; disabling or
  lacking a checkpoint in the backend fails closed instead of returning a
  fabricated success.

## Behavior and failure-path evidence

`apps/web/test/topology-interaction.test.ts` has six tests and 84 assertions:

- an actual 2,400-node graph with more than 3,100 edges, bounded visible entity
  counts, spatial queries, LOD clusters and minimap projection;
- stable coordinates over add/remove/replace revisions and controller restart;
- rejection of stale/out-of-order projection revisions;
- search, facets, layers, selection, evidence navigation, checkpoint lineage
  and branch/write/conflict details;
- pointer, keyboard, screen-reader and selection-box interaction; and
- loading/reconnect/error/disable behavior plus typed `/change`, `/rewind` and
  `/resume` receipt mapping and sealed denial.

`tests/integration/test_topology_interaction_control.py` uses the live HTTP API
and proves:

- a real `/change` command reaches the requirement-change owner;
- two real recovery checkpoints are committed;
- `/rewind` and `/resume` target exact canonical checkpoints and preserve
  checkpoint identity;
- sealed `/change` is denied, idempotent replay does not double-count the
  intervention, and the human-intervention count remains zero; and
- recovery emits topology-route facts consumed by the projection path.

The adjacent command regression specifically proves that `/rewind latest`
without a canonical checkpoint fails at permission authorization. The fix is
inside the already allocated permission/recovery owner boundary.

## Verification

Passed on the frozen implementation target:

```text
node_modules/.bin/bun.exe test ./apps/web/test/topology-interaction.test.ts
6 pass, 0 fail, 84 expect() calls

node_modules/.bin/bun.exe test ./apps/web/test
85 pass, 0 fail, 450 expect() calls

node_modules/.bin/bun.exe run --cwd apps/web build
TypeScript check passed; 136 modules bundled.

.venv/Scripts/python.exe -m pytest -q \
  tests/integration/test_topology_interaction_control.py
1 passed

.venv/Scripts/python.exe -m pytest -q \
  tests/integration/test_api_control_commands.py::ApiControlCommandTests::test_change_command_creates_requirement_change_event \
  tests/integration/test_api_control_commands.py::ApiControlCommandTests::test_clear_uses_canonical_session_owner_and_rewind_fails_closed \
  tests/integration/test_recovery_planner_routing_memory_api.py::RecoveryPlannerRoutingMemoryApiTests::test_real_api_recovery_mutates_task_persists_memory_and_disables_fail_closed \
  tests/integration/test_topology_interaction_control.py
4 passed
```

Pytest emitted only its pre-existing inability to write `.pytest_cache`; no
test depended on that cache.

The Web dev server and API started successfully, but the connected browser
runtime reported that no browser instance was available. Therefore viewport
screenshots and manual click-through are not claimed as passed. They are a
named external-environment verification item for the M2-02 numeric-stage
aggregate after M2-S02B-02; deterministic layout, interaction, accessibility,
typecheck and production-bundle evidence passed in this slice.

## Exact effective-line accounting

The auditor uses exact added-line sets from Git. TypeScript compiler AST
classification excludes imports, declarations and static contracts. Tests,
fixtures, docs, CSS/static presentation, DTO/type-only content, generated or
bounded Python owner glue, thin API adapters, ledger data and source-pool
material receive no effective-production credit.

Current slice (`a2fa483...6c28e..6b15c5d...96dc`):

| Bucket | Lines |
| --- | ---: |
| raw additions / deletions | 11,848 / 35 |
| production runtime | 6,814 |
| UI behavior | 555 |
| UI presentation excluded | 1,444 |
| type declarations excluded | 1,141 |
| adapter-only excluded | 153 |
| generated/bounded Python glue excluded | 235 |
| tests/fixtures excluded | 1,160 |
| docs/comments/blank excluded | 346 |
| vendor/source-pool | 0 |
| effective production | **7,369** |
| slice minimum | **6,500** |

Parent M2-02A (`f7fff49...974..6b15c5d...96dc`):

| Bucket | Lines |
| --- | ---: |
| raw additions / deletions | 23,030 / 140 |
| production runtime | 13,472 |
| UI behavior | 555 |
| UI presentation excluded | 1,444 |
| type declarations excluded | 2,098 |
| schema/DTO/data excluded | 979 |
| adapter-only excluded | 153 |
| generated excluded | 949 |
| tests/fixtures excluded | 2,452 |
| docs/comments/blank excluded | 928 |
| vendor/source-pool | 0 |
| effective production | **14,027** |
| parent minimum | **13,000** |

No file contributes more than 20 percent of the slice effective total.
Original-language TypeScript/TSX provides all credited behavior. Python owner
glue, transport adapters and declaration/presentation volume receive zero
credit.

## Large-file and exclusion audit

Files over 500 raw added lines:

- `clustering.ts` (546/493), `controller.ts` (560/514), `controls.ts`
  (665/635), `filtering.ts` (667/633), `geometry.ts` (552/492), `interaction.ts`
  (1,090/945), `layout.ts` (1,057/966) and `model.ts` (728/690) contain
  executable clustering, orchestration, receipt, query, geometry, input,
  incremental-layout and graph-model behavior. Each is reached by the
  workbench tests and changes observable results when disconnected.
- `contracts.ts` (613/0) is intentionally type-only and fully excluded.
- `styles.css` (563/0) is presentation-only and fully excluded.
- `topology-interaction.test.ts` (841/0) is behavior evidence and fully
  excluded from production credit.

Production candidates with more than 30 percent exclusion:

- `graph-canvas.tsx` (424/163), `inspector.tsx` (438/148), `minimap.tsx`
  (96/41), `toolbar.tsx` (251/46) and `topology-workbench.tsx` (278/157)
  count only event/orchestration behavior; JSX and static presentation are
  excluded.
- `main.py` (226/0) and `owner_handlers.py` (9/0) are bounded existing-owner
  glue and intentionally excluded from the TypeScript slice quota.
- `task-api.ts`, typed-client `constants.ts`, `normalizers.ts` and
  `protocol.ts` total 153 adapter-only lines and receive zero credit.
- `task-detail.tsx`, `index.ts`, the preimplementation document, CSS, and both
  test files are presentation/export/docs/test buckets and receive zero credit.

This classification prevents types, adapters, backend glue, CSS and tests from
masking the behavioral quota.

## Dependency, path and risk audit

The frozen implementation diff adds no:

- parent-source runtime path, npm link, pip editable parent path or external
  Docker build context;
- package or lockfile dependency;
- production subprocess, local port, MCP server, dynamic package import or
  cache/state dependency; or
- credential, token, header or provider base-URL exposure.

The slice-scoped synchronizer validates four source decisions and all target
paths. The repository-wide ledger verifier is not represented as green: it
still reports three pre-existing findings in unchanged
`packages/evaluation/zyra_evaluation/m1_hardening/long_horizon_runtime.py`
for historical `../OpenHands`, `../browser-use` and `../claude-code-best`
strings, plus its existing warnings. That protected file is unchanged in this
slice and no new finding is introduced.

No high-risk upgrade trigger occurred: there is no incompatible public
schema/event/persistence migration, canonical-owner transfer, global default
policy change, new external dependency/process/port, or cross-slice regression.
The API extension and recovery/permission fixes remain within owners allocated
to this slice. Full cleanroom, full-repository tests, complete M2-02
source/ledger aggregation and independent aggregate review remain due after
M2-S02B-02.

## Parent closure and handoff

M2-02A now cumulatively meets its 13,000-line minimum with 14,027 effective
behavior lines. M2-S02A-01 remains the immutable projection/fact boundary;
M2-S02A-02 adds transient view interaction and typed control without adding a
second graph, scheduler, checkpoint, permission or projection owner.

The implementation advances `REQ-TOPO-01`, `REQ-EDGE-01`, `REQ-TRACE-01`,
`SCORE-ORG` and `SCORE-UX`; it does not claim the full competition requirements
closed. The next execution entry is `M2-S02B-01`.
