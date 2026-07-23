# M2-S02A-01 topology route and placement projection self-review

Date: 2026-07-24

Authority:
`docs/milestones/M2-console-demo/slice-02a-01-topology-route-placement-projection.md`

## Frozen commit interval

- `baseline_commit`:
  `f7fff49be91fb0f797260c03ff9cca76c06c5974`
- preimplementation decision commit:
  `870c52d6efe833bb501ac93ed2bd5e631464d7eb`
- `implementation_commit`:
  `7ea026f1de635197d84b3179d5494e96ca687577`
- `evidence_commit`:
  the commit containing this review, the effective-line auditor, and the
  synchronized source ledger; its authoritative hash is recorded in
  `docs/milestones/execution-state.yaml`.
- effective implementation interval:
  `f7fff49be91fb0f797260c03ff9cca76c06c5974..7ea026f1de635197d84b3179d5494e96ca687577`

The implementation commit contains production behavior and directly related
tests only. This review, ledger synchronization, and the line-count auditor are
evidence-only and do not contribute to the effective production total.

## Outcome

M2-S02A-01 is complete.

The existing `CanonicalProjectionStore` remains the sole browser projection
writer. The new TypeScript feature is a pure read-only derivation reached by
the existing topology panel selector. It projects:

- PlanNode, explicit and inferred edges, namespace/subgraph membership,
  separate graph/entity/commit/projection revisions, and open-world runtime
  node/edge/role/capability add/remove/replace mutations;
- event-provided top-k route candidates, accepted/selected status, score,
  rank, reasons, health, required capabilities, worker resources, and the
  distinction between fixed candidate selection and topology mutation;
- worker/backend/provider/model placement, model splits, device/local, edge,
  and cloud locations, privacy, budget, cost, latency, resource fit, and SLA
  violations;
- checkpoint lineage, pending and committed writes, interrupt/resume/next-task
  identity, branch-local read/write sets, deterministic order, conflicts,
  reject/rebase/replay/replan outcomes, and canonical visibility;
- requirement-change impact, affected/superseded/needs-revision nodes, local
  replan identity, resource/route decisions, and causal closure;
- roots, leaves, isolation, reachability, ancestry, depths, cycles, weak
  components, topological order, critical path, missing endpoints, effective
  steps, effective route transitions, and reverse evidence;
- duplicate, stale, gap, lag, readiness, source-evidence, ambiguity, lineage,
  write-visibility, and sensitive-field diagnostics.

The slice intentionally does not implement graph canvas rendering, viewport,
selection, inspector interaction, or operator mutation controls. Those remain
M2-S02A-02 responsibilities.

## Source-role and language decision result

| Source | Role | Language decision | Result |
| --- | --- | --- | --- |
| Existing Zyra normalized ingress, canonical state, causality, and selector chain | `primary_implementation` | TypeScript to TypeScript `same_language_component_integration` | Retained immutable transaction state, stale/duplicate fencing, reversible causality, selector dependency keys, and disable behavior. No second store, reducer, transport subscription, cursor, or persistence owner was added. |
| Existing Zyra graph custody, scheduler, worker-pool, symbolic requirement control, and checkpoint/recovery contracts | canonical input foundation | Existing Python control owners retained; TypeScript consumes JSON event facts | Real `DynamicTopologyRuntime`, `GraphStateCustody`, `ResourceScheduler`, and `apply_requirement_change` output drives the integration test. No Python runtime control flow was rewritten as frontend implementation. |
| Existing Zyra provider control plane | canonical input foundation | TypeScript contract integration | Provider/model/backend/cost/latency facts are projected; credential material, headers, base URLs, tokens, and secrets are removed. |
| OpenCode | `reference_only` | no production migration | Single synchronized state and hierarchy/event presentation are conformance references only. |
| OpenHands | `reference_only` | no production migration | Planner/task hierarchy is a presentation reference only. |
| oh-my-pi | `reference_only` | no production migration | Typed route/revision interaction is a reference only. |
| LangGraph | `conformance_only` | no production migration | Only checkpoint identity/lineage, pending/committed write, interrupt/resume, stable identity, and exact-resume semantics are checked. StateGraph/channel/Pregel/stream/Store/ToolNode/SDK/server/deploy have no owner or runtime path. |

There is no supplementary production source and no cross-language
implementation exception. OpenClaw remains forward-excluded and has no source
entry, source read, runtime path, test obligation, or restored workspace.

## Canonical owner and cache custody

| State | Canonical owner | Projection behavior |
| --- | --- | --- |
| task/run/session/events | existing backend M1 stores and RuntimeEventSpine | read from normalized ingress facts |
| graph snapshot, node, edge, delta, revision, commit | `GraphStateCustody` / `DynamicTopologyRuntime` | immutable read model only |
| route, resource decision, worker capacity, placement | `ResourceScheduler`, worker-pool and provider control owners | preserves event-provided candidates and selection; does not rescore |
| checkpoint, pending/committed writes, branch outcome | existing M1 checkpoint/recovery stores | separates visibility and lineage; does not commit |
| requirement change and local replan | symbolic requirement-control owner | projects causal impact only |
| browser canonical projection | `CanonicalProjectionStore` | remains sole browser writer |
| topology selector result | no persistent owner | one-call local maps/sets; deeply frozen result; no cross-revision entity cache |
| selector memoization | existing selector registry | dependency-keyed invalidation |

The default call chain is:

`RuntimeEventSpine -> TaskEventTransport -> EventIngressCoordinator ->
CanonicalProjectionStore transaction -> selectTopologyProjection /
buildTopologyProjection -> selectTopologyPanel -> task detail`.

`apps/web/src/state/panel-selectors.ts` now delegates the existing topology
panel to the richer projector. There is no fallback to the former shallow
node-only derivation.

## Dynamic reachability and semantic effect

- A normalized topology/scheduler/recovery/requirement event is reduced by the
  real `CanonicalProjectionStore`, then read by the topology selector.
- `selectTopologyPanel` invokes `buildTopologyProjection`, so the production
  task-detail topology path reaches the new module rather than an import-only
  smoke path.
- Runtime add/remove/replace node/edge/role/capability mutations alter node,
  edge, mutation, namespace, graph-analysis, and effective-step results.
- Event-provided candidate selection alters route, placement, node route
  references, route-change metrics, and reverse evidence without changing the
  canonical scheduler decision.
- Pending checkpoint writes remain pending and are excluded from committed
  writes. A conflicted branch remains non-visible until a committed/rebased
  outcome exists.
- A requirement change alters affected/superseded/revision/replan and route
  relationships and keeps its causation chain.
- A sensitive `api_key` test value is absent from projected attributes and
  increments the redaction diagnostic.

Disconnect/disable proof:

- `buildTopologyProjection(..., {disabled: true})` and a disabled
  `TopologyProjectionEngine` throw `TOPOLOGY_PROJECTION_DISABLED`.
- Removing the projector breaks the state export and the default topology
  panel import; disabling it breaks the explicit behavior tests.
- Changing the real scheduler input from a code task to a browser/URL task
  changes the backend selected manifest, worker, backend, and location, and
  changes the projected route plus the actual graph node
  `backend_route_ref`.
- No fallback fabricates a route, placement, graph commit, or successful
  readiness state when the tested module is disabled.

## Behavior and failure-path evidence

`apps/web/test/topology-projection.test.ts` proves:

- graph snapshot, PlanNode/edge/namespace/subgraph, roots and reachability;
- open-world add/remove/replace node and edge plus role/capability mutation;
- open-world mutation versus fixed candidate-route selection;
- ranked top-k candidate, resource/capability fit, model split, provider/model,
  device/edge/cloud, privacy/cost/latency/budget/SLA;
- pending versus committed writes, lineage, interrupt/resume/next-task;
- branch conflict, deterministic order, snake/camel alias input, rebase and
  canonical visibility;
- requirement change, superseded/needs-revision/local-replan/route causation
  and reverse evidence;
- duplicate, stale, lag/gap diagnostics and fail-closed disable;
- sensitive-field removal; and
- delegation from the existing topology panel selector.

`tests/integration/test_topology_route_placement_projection.py` proves:

- real `DynamicTopologyRuntime`/`GraphStateCustody` commits create the
  snapshots and mutation deltas consumed by the browser projection;
- real `ResourceScheduler` chooses `local-code-worker` for a code task and
  `edge-browser-worker` for a browser/URL task;
- those backend decisions change worker, backend, location, model/placement,
  and the real graph node `backend_route_ref`;
- the Bun probe sends those actual outputs through normalized ingress and the
  real `CanonicalProjectionStore`;
- projected route and graph facts exactly match the backend decision; and
- real `apply_requirement_change` creates an affected-node and replan fact
  that remains visible in the projection.

## Verification commands

Passed:

```text
node_modules/.bin/tsc.exe -p apps/web/tsconfig.json

node_modules/.bin/bun.exe test ./apps/web/test
79 pass, 0 fail, 366 expect() calls

.venv/Scripts/python.exe -m pytest \
  tests/integration/test_topology_route_placement_projection.py \
  tests/unit/test_dynamic_graph_custody.py \
  tests/unit/test_scheduler.py \
  tests/unit/test_symbolic_control.py -q
14 passed

node_modules/.bin/bun.exe run --cwd apps/web build
TypeScript check and browser production bundle passed.

node_modules/.bin/bun.exe scripts/audit_m2_s02a_01_effective_lines.mjs --summary
effective_production=6658
minimum=6500
line_count_ok=true

.venv/Scripts/python.exe scripts/verify_source_language_custody.py \
  --evidence docs/reviews/evidence/M2-S02A-01-topology-route-placement-projection.json \
  --base f7fff49be91fb0f797260c03ff9cca76c06c5974 \
  --target 7ea026f1de635197d84b3179d5494e96ca687577
ok=true; actual_production_languages=[typescript]; raw TypeScript custody additions=7660
```

The Web suite includes all M2-S01B-02 canonical projection/store tests, so the
adjacent ingress, transaction, causality, persistence, restore, retention,
stale/duplicate, disable, and downstream-panel regression boundary passed.

## Exact effective-line accounting

Method:

- exact added line numbers from
  `git diff --unified=0 baseline_commit implementation_commit`;
- TypeScript compiler AST removes imports, interfaces, type aliases,
  export-only declarations, declaration-only signatures, and the static schema
  constant in `contracts.ts`;
- the TypeScript scanner removes comments and blank lines;
- all tests/probes, Python integration tests, docs, schema-only content,
  generated/data material, adapter-only content, and vendor/source-pool
  material receive zero production credit.

Totals:

| Bucket | Added lines |
| --- | ---: |
| raw additions | 9,146 |
| production runtime | 6,658 |
| UI behavior | 0 |
| UI presentation | 0 |
| type declarations | 957 |
| schema/DTO/data | 1 |
| adapter-only | 0 |
| generated | 0 |
| test/mock/fixture/probe | 1,292 |
| docs/comments/blank | 238 |
| vendor/source-pool | 0 |
| effective production | **6,658** |
| required minimum | **6,500** |

Per production file:

| File | Raw additions | Effective production | Main responsibility |
| --- | ---: | ---: | --- |
| `analysis.ts` | 482 | 460 | graph indices, SCC/cycles, components, order, path, metrics |
| `changes.ts` | 457 | 417 | requirement impact and causal linking |
| `checkpoints.ts` | 1,087 | 1,026 | checkpoint writes, lineage, branch conflict/rebase/order |
| `contracts.ts` | 595 | 14 | executable error only; 578 declaration lines excluded |
| `diagnostics.ts` | 217 | 195 | ready/gap/stale/evidence/visibility diagnostics |
| `edges.ts` | 546 | 496 | explicit, inferred, mutation-backed edge projection |
| `evidence.ts` | 484 | 460 | reversible causality and entity evidence |
| `index.ts` | 2 | 0 | export-only |
| `nodes.ts` | 960 | 909 | node/snapshot/mutation/namespace relationships |
| `placements.ts` | 800 | 748 | placement, resources, split, privacy and SLA |
| `projector.ts` | 297 | 271 | pure assembly, guard, selector and engine |
| `record-reader.ts` | 829 | 809 | canonical record collection and safe extraction |
| `routes.ts` | 873 | 821 | route source/candidate/top-k/change projection |
| `state/index.ts` | 1 | 0 | export-only |
| `state/panel-selectors.ts` | 33 | 32 | default topology-panel delegation |

Original-language TypeScript contributes all 6,658 effective production lines.
Cross-language implementation contributes zero. Python is integration-test
evidence only. There is no exemption, deferred language quota, adapter credit,
or production credit from schemas, tests, docs, ledger, source inventory, or
source-pool material.

## Large-file and high-exclusion audit

Files with more than 500 raw additions were audited individually:

- `checkpoints.ts` — executable parsing, merge, visibility, conflict, alias,
  deterministic-order, lineage, cycle, rebase and evidence behavior. Disabling
  it removes checkpoint/branch/conflict projection and fails the branch tests.
- `contracts.ts` — deliberately excluded: 578 declaration lines and one schema
  constant receive zero credit; only the executable fail-closed error class is
  counted. Its exclusion ratio is expected and does not mask production quota.
- `edges.ts` — executable snapshot/dependency/parent/namespace/mutation edge
  derivation and revision merge. Disconnecting it removes graph connectivity
  and fails open-world and panel tests.
- `nodes.ts` — executable canonical node/snapshot/delta interpretation,
  relationships, namespace construction, mutation kinds, open-world flags and
  missing-dependency behavior. Disconnecting it removes the graph projection.
- `placements.ts` — executable location/backend/provider/model/resource/SLA
  projection, split handling and placement changes. Disconnecting it fails
  placement and scheduler integration assertions.
- `record-reader.ts` — executable canonical table/tombstone collection,
  bounded traversal, alias conversion, resource math, redaction, stable
  identity and deep immutability. It owns no state and cannot acknowledge or
  rewrite events.
- `routes.ts` — executable route source extraction, candidate acceptance,
  top-k order, selected-candidate preservation, resource fit and route-change
  classification. Disconnecting it fails both direct and real scheduler tests.

No single production file contributes more than 20 percent of the 6,658
effective total. `contracts.ts` is the only production candidate with more
than 30 percent excluded content, and it contributes only the confirmed
executable error class. No schema/DTO volume is used to pass the line gate.

## Dependency, path, process, and clean-state audit

The current diff adds:

- no runtime dependency on `../opencode`, `../OpenHands`, `../oh-my-pi`,
  `../langgraph`, `../claude-code-best`, or any deleted/excluded source;
- no npm link, pip editable parent path, external Docker context, dynamic
  package, new subprocess, local port, external service, or runtime cache;
- no new package or lockfile entry;
- no browser-side persistent projection store or reconnect loop; and
- no credential, request-header, base-URL, secret, or token exposure.

The Python-to-Bun subprocess exists only in
`tests/integration/test_topology_route_placement_projection.py`; it invokes the
repository-pinned Bun executable and is not reachable from production.

The repository-wide `verify_internalization_ledger.py --json` observation is
not recorded as a pass: it reports three existing boundary findings in
`packages/evaluation/zyra_evaluation/m1_hardening/long_horizon_runtime.py`.
That protected file has no diff in the frozen implementation interval and the
findings refer to pre-existing source-name strings, not a dependency introduced
by this slice. The slice-scoped deterministic synchronizer successfully
deserializes all ledger entries, validates five owned decisions, and confirms
every target at the frozen implementation commit. The broader findings remain
outside this ordinary slice and are not silently represented as green.

This ordinary first sibling does not trigger an incompatible public
schema/event/persistence change, state-owner transfer, global policy change,
external production dependency, process/port, or cross-slice regression. Full
cleanroom, full repository tests, numeric-stage source/ledger aggregation, and
parent cumulative line accounting are therefore intentionally deferred to the
M2-02 numeric-stage aggregate review after M2-S02A-02. The implementation and
tests themselves do not depend on dirty caches, generated output, or parent
source repositories.

## Source ledger and forward handoff

`scripts/sync_m2_topology_projection_source_ledger.py` records five decisions:

1. existing Zyra TypeScript projection chain as the only primary
   implementation foundation;
2. OpenCode as `reference_only`;
3. OpenHands as `reference_only`;
4. oh-my-pi as `reference_only`; and
5. LangGraph as narrow `conformance_only`.

Reference/conformance entries use `not_selected`,
`excluded_inventory_only`, and contribute no production lines. All required
production targets exist at the implementation commit. The ledger records no
parent-source runtime dependency.

M2-S02A-02 must reuse this immutable projection as graph-view input. It may add
layout, canvas, selection, drill-down, and interaction behavior, but it must
not introduce a second reducer/state owner, recompute scheduler policy, write
canonical graph state from the browser, or collapse graph/commit/projection
revisions.
