# M2-S02A-01 topology, route, and placement projection preimplementation decision

Date: 2026-07-24

Authority:
`docs/milestones/M2-console-demo/slice-02a-01-topology-route-placement-projection.md`

Baseline commit:
`f7fff49be91fb0f797260c03ff9cca76c06c5974`

Parent-unit baseline commit:
`f7fff49be91fb0f797260c03ff9cca76c06c5974`

M2-02 numeric-stage baseline commit:
`f7fff49be91fb0f797260c03ff9cca76c06c5974`

This record is frozen before the first production-code change. It applies the
2026-07-22 effective-code/language/migration gate and fixes the source roles,
language path, canonical owners, projection cache boundary, and migration
modes for this slice.

## Source, language, migration, and owner decision

| Role | Source repository and bounded path | Source language | Zyra target | Target language | Migration mode | Canonical owner after the slice | Preimplementation decision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| primary implementation foundation | Existing Zyra `apps/web/src/events/ingress/**` and `apps/web/src/state/{store,reducer,projectors,selectors,causality,panel-selectors}.ts` | TypeScript | `apps/web/src/features/topology/projection/**` and the existing state selector export boundary | TypeScript | `same_language_component_integration` | `CanonicalProjectionStore` remains the single browser projection writer; the topology feature is a pure, read-only derivation and owns no persistent state | Retain normalized event identity, immutable revision snapshots, stale/duplicate fencing, reversible causality, selector dependency keys, and store disable behavior. Compose a topology-specific read model from the existing node, worker, scheduler, recovery, mutation, tombstone, cursor, runtime, and causal tables. Do not add a second reducer, store, event subscription, persistence schema, reconnect controller, or page-local fact cache. |
| canonical input foundation | Existing Zyra `packages/orchestration/zyra_orchestration/graph_custody/{models,runtime}.py`, `packages/symbolic/zyra_symbolic/{topology,control}.py`, `packages/scheduler/zyra_scheduler/{models,scheduler}.py`, `packages/scheduler/zyra_scheduler/worker_pool/{models,integration_models,scheduler_bridge,projection}.py`, and `packages/scheduler/zyra_scheduler/recovery_runtime/contracts.py` | Python | Existing runtime event spine and normalized ingress attributes consumed by `apps/web/src/features/topology/projection/**` | TypeScript consumer over the existing JSON event contract | `contract_integrate_existing` | `GraphStateCustody`, `DynamicTopologyRuntime`, `ResourceScheduler`, worker-pool owners, and checkpoint/recovery stores remain authoritative | Consume existing graph snapshots/deltas/commit receipts, PlanNode changes, route candidates, worker capacity, resource decisions, requirement-change effects, checkpoint lineage, pending/committed writes, branch deltas, conflicts, rebases, interrupts, and route-layer changes. No Python control flow is rewritten or claimed as frontend production implementation; the feature interprets already emitted facts only. |
| canonical input foundation | Existing Zyra `packages/runtime/provider-control-plane/src/{contracts,routing,control-plane}.ts` | TypeScript | Provider/model and cost/latency portions of `apps/web/src/features/topology/projection/**` | TypeScript | `same_language_contract_integration` | Provider catalog, route lease, credential, dispatch, and failover owners remain in the provider control plane | Interpret provider/model route leases and provider control events as placement evidence. Never expose credential material, base URLs, request headers, or secret references in the topology view. |
| reference_only | OpenCode `adf178a6b95c61506ddaadaf4dd062badb4a8fda`, especially `packages/tui/src/routes/session/index.tsx` and its session/timeline/subagent state consumption | TypeScript / TSX | Topology projection conformance tests and later graph-view interaction contract | TypeScript | `reference_only` | No production owner | Check that one route/session view can consume a single synchronized state source and expose status, hierarchy, and event relationships without introducing a parallel reducer. No OpenCode UI state, Solid store, command runtime, or session cache is migrated in this slice. |
| reference_only | OpenHands `c105a82387898e744423c8831d412e26495b38a9`, `frontend/src/routes/planner-tab.tsx`, `task-list-tab.tsx`, conversation event/history surfaces | TypeScript / TSX | Topology projection conformance tests and later graph-view input contract | TypeScript | `reference_only` | No production owner | Check task/planner hierarchy and event-backed state presentation only. No OpenHands router, Redux store, conversation owner, sandbox service, or backend is migrated. |
| reference_only | oh-my-pi `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`, typed route/provider/state and TUI interaction patterns | TypeScript / TSX | Route, placement, and revision conformance tests | TypeScript | `reference_only` | No production owner | Check typed route identities, immutable lease changes, request correlation, and concise state drill-down. No OMP RPC, TUI, provider owner, or agent loop is migrated. |
| conformance_only | LangGraph `5931a5f0b313feff24e2516a586c55601b868ac1`, limited by `source-graphs/langgraph/source-graph.md` section 12 to checkpoint identity/lineage, pending versus committed writes, stable task identity, interrupt/resume, and exact-resume semantics | Python | Checkpoint, branch, conflict, rebase, and exact-resume projection tests | TypeScript / Python | `conformance_only` | No production owner | Verify that pending and committed writes remain visibly distinct; conflicts are rejected/rebased/replanned explicitly; interrupt/resume correlation and checkpoint lineage remain reversible. Do not migrate StateGraph, channels, reducers, Pregel, stream controller, Store, ToolNode, SDK, server, or deployment code. |
| excluded_forward_only | OpenClaw | N/A | none | none | `excluded_forward_only` | none | Do not read, restore, depend on, test against, cite, or create a ledger/source-role entry for OpenClaw. Historical protected files are not modified. |

There is no supplementary production implementation source in this slice.
Consequently there is no second primary control flow, no supplementary owner,
and no cross-language implementation exception. The non-zero production quota
is fulfilled by original-language TypeScript behavior integrated with the
existing TypeScript projection path.

## Fixed state, event, and cache boundaries

- The default path is:
  `M1 canonical event -> TaskEventTransport -> EventIngressCoordinator ->
  CanonicalProjectionStore transaction -> topology selector -> immutable
  topology graph view input`.
- Backend task, graph, edge, scheduler, worker, route, provider, model,
  placement, resource, checkpoint, branch, recovery, requirement, and artifact
  stores remain authoritative. The browser cannot write any of those facts.
- `CanonicalProjectionStore` remains the only browser projection state owner.
  The new topology feature may allocate local maps/sets while evaluating one
  selector call, but must return a deeply immutable value and retain no
  cross-revision mutable entity cache.
- Selector memoization remains owned by the existing selector registry and is
  keyed by canonical state dependency changes. A topology projector cannot
  subscribe to transport, persist a cursor, acknowledge an event, invent a
  revision, or mutate a canonical projection entity.
- Every projected node, edge, route, placement, checkpoint, branch, conflict,
  requirement change, and diagnostic must retain reversible references to the
  canonical event, span/correlation/causation, mutation, checkpoint, graph
  revision, and source entity identities that actually exist.
- Sensitive provider fields are excluded from the read model. Credential IDs,
  credential fingerprints, secret references, headers, base URLs, and raw
  request defaults are never returned by topology selectors.

## Fixed semantic boundaries

- Open-world dynamic topology means a canonical event proves that a node,
  edge, role, or capability was added, removed, or replaced at runtime. Picking
  a different candidate inside a fixed candidate set is a route decision, not
  a topology mutation.
- Graph revisions, entity revisions, commit revisions, projection revisions,
  cursor generation, and global event sequence are distinct and must not be
  collapsed into one counter.
- Branch-local pending writes never become canonical graph state until a
  committed receipt says they are visible. Conflicted/rejected/replan-required
  branch outcomes remain visible and cannot be colored as successful commits.
- Completion-order variation cannot decide canonical ordering. Stable event
  sequence, commit revision, graph revision, branch identity, and entity
  identity provide deterministic tie-breaks.
- Candidate workers preserve acceptance, score, rank, reasons, capabilities,
  required resources, capacity/allocated/available vectors, health, and
  selected status. Top-k means the event-provided ranked candidates, not a
  frontend rescore.
- Placement keeps device/local, edge, and cloud distinct and projects backend,
  provider, model, privacy, budget, latency, cost, and SLA evidence without
  recomputing scheduler policy.
- Requirement changes project the source change, affected/superseded nodes,
  newly created replan node, `needs_revision`, local-replan route, and causal
  link to the resulting graph/resource decisions.
- Disabling the topology projector must fail visibly. Falling back to the old
  shallow topology panel, fixtures, scheduler summaries, or direct API reads is
  forbidden.

## Planned production modules

The production implementation is decomposed under
`apps/web/src/features/topology/projection/**`:

- canonical record readers and safe nested-value extraction;
- node, namespace, dependency, subgraph, and open-world mutation projection;
- explicit edge and inferred dependency-edge projection;
- route candidate, top-k selection, route-change, and route metric projection;
- worker/resource capacity and capability fit projection;
- provider/model split and device/edge/cloud placement projection;
- privacy, budget, latency, cost, and SLA assessment;
- checkpoint lineage, pending/committed write, interrupt, resume, and next-task
  projection;
- branch delta, conflict, reject, rebase, replan, and deterministic commit
  projection;
- requirement-change impact and local-replan causation projection;
- graph algorithms for roots/leaves, reachability, cycles, components,
  critical path, namespace groups, missing references, and change sets;
- reverse causality and source-evidence indexing;
- readiness, gap/stale/duplicate diagnostics and effective-step/route metrics;
- pure projection assembly, deep immutability, disable/mutation guard, and
  selector integration.

The first slice does not implement graph canvas rendering, viewport/zoom,
selection control, node/edge interaction, inspector UI, operator control, or
backend mutation commands. Those remain in `M2-S02A-02`.

## Verification obligations

Direct and adjacent verification must prove:

1. real `DynamicTopologyRuntime`/`GraphStateCustody` output creates, removes,
   and replaces runtime node/edge/role/capability facts not present in an
   initial graph;
2. real `TopologyRouter`/`ResourceScheduler` output changes candidates,
   selection, device/edge/cloud placement, model split, resource evidence, and
   effective route metrics;
3. real requirement-change handling supersedes affected nodes, adds a local
   replan node, changes route input, and remains reversibly tied to the source
   change;
4. checkpoint and branch events keep pending/committed writes, lineage,
   conflicts, rejects, rebases, interrupts, resumes, and next tasks distinct;
5. stale generation, duplicate identity, sequence gaps, out-of-order input,
   aliased nested payloads, completion-order permutation, and projector disable
   have explicit outcomes;
6. changing scheduler input changes both the backend execution route and the
   projected graph route; disabling/removing the route projection fails the
   projection assertion rather than changing presentation color only;
7. the M2-01B ingress/store/causality tests and web production build remain
   green; and
8. dependency/path audit finds no source-repository runtime path, npm link,
   editable parent path, external Docker context, dynamic dependency, new
   subprocess, local port, or cache requirement.

This ordinary first slice targets directly affected and adjacent tests within
the 30-minute wall-clock budget. It does not close the M2-02A parent or numeric
stage, so aggregate cleanroom and full parent line accounting are deferred to
the final sibling slice unless a high-risk trigger is discovered.

## Effective-code and audit obligations

The slice minimum is 6,500 effective production lines. Type declarations,
DTO/schema-only content, generated code, tests, fixtures, docs/comments,
presentation-only JSX/CSS, adapter-only glue, source maps, ledgers, manifests,
and vendor/source-pool material receive zero production credit.

Final evidence will:

- compute exact added lines from
  `baseline_commit..implementation_commit`;
- report per-file raw additions and runtime/UI-behavior effective additions;
- summarize retained original-language TypeScript production, cross-language
  implementation code, and exempt glue;
- audit every production file over 500 added lines, every file contributing
  more than 20 percent of effective production, and every production
  candidate with over 30 percent excluded content;
- record main-path reachability, canonical owners, event causality,
  disable/mutation behavior, external dependency/path findings, and unrun
  aggregate checks with their mandatory later layer.

## Commit boundary

- `baseline_commit`:
  `f7fff49be91fb0f797260c03ff9cca76c06c5974`
- `parent_baseline_commit`:
  `f7fff49be91fb0f797260c03ff9cca76c06c5974`
- `numeric_stage_baseline_commit`:
  `f7fff49be91fb0f797260c03ff9cca76c06c5974`
- This decision record is intentionally committed before production code.
- `implementation_commit` will contain production code and directly related
  behavior tests only. Completion review, line auditor, source-to-target
  evidence, ledger changes, and execution-state changes are evidence-only and
  occur after the implementation commit.
- The effective implementation interval is
  `baseline_commit..implementation_commit`; documentation is excluded from
  effective production counts.
