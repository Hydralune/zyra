# M2-S02B-01 worker causal timeline projection preimplementation decision

## 1. Slice identity and immutable baseline

- Slice: `M2-S02B-01`
- Parent: `M2-02B timeline and worker state`
- Authority entry:
  `../..\..\docs\milestones\M2-console-demo\slice-02b-01-worker-causal-timeline-projection.md`
- Parent authority:
  `../..\..\docs\milestones\M2-console-demo\unit-02b-timeline-worker-state.md`
- Baseline Zyra commit:
  `0980c52e1a0624a06501ccd0c39a723e624c95e3`
- Baseline worktree: clean
- Decision commit: pending until this document is committed
- Implementation commit: pending; it must be a later commit than the decision
  commit and contain only production code and behavior tests for this slice.
- Evidence commit: pending; it must be a later commit than the implementation
  commit and contain the self-review, source ledger update, exact line auditor
  and machine-readable evidence.

The authoritative execution state identifies this slice as the only current
entry and protects all work through `M2-S02A-02`. This decision does not reopen
or rewrite any protected runtime, canonical projection, topology, permission,
scheduler, recovery, or artifact owner.

## 2. Product boundary and completion claim

This slice will add a read-only, revisioned worker causal timeline derived from
the canonical M2-01B projection selectors. It will not create a second event
store, worker state machine, lease owner, failure registry, recovery planner,
topology store, artifact store, or command path.

The completed slice must expose, from the real task-detail route:

1. worker lifecycle phases `queued`, `admitted`, `starting`, `running`,
   `waiting-tool`, `waiting-policy`, `recovering`, `completed`, `failed` and
   `cancelled`;
2. one normalized row stream that joins span, tool call, artifact, failure,
   recovery, route/placement and state-mutation evidence;
3. recovery chain and critical-path derivation without inventing canonical
   facts;
4. worker/status/type/time filters whose hidden-result summaries preserve
   causal gaps and ancestor/descendant evidence;
5. bidirectional linkage to topology entities and drill-down targets for
   artifacts, tools, permissions, failures, recoveries and causal events;
6. deterministic late-event, partial-stream, reconnect/restart, lease
   replacement, background park/revive and duplicate-recovery behavior;
7. stable row identity and bounded visible-window projection suitable for large
   histories;
8. an explicit disabled projector path that makes the dedicated timeline fail
   closed while leaving the M2-01B canonical state owner untouched.

`M2-S02B-02` remains the owner of recovery/operator interaction controls. This
slice may display canonical commands and recovery facts, but it will not submit
restart, retry, cancel, reroute, approval or recovery commands.

## 3. Source-role and language decision

| Capability chain | Source role | Source repository and commit | Exact source path(s) | Upstream language | Zyra target | Target language | Migration mode | Canonical owner after integration |
|---|---|---|---|---|---|---|---|---|
| Timeline row model, stable row identity, incremental row reconciliation, active/visible row projection and bounded virtual window | primary | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` | `packages/app/src/pages/session/timeline/{model.ts,projection.ts,rows.ts,timeline-row.ts,row-reconciliation.ts,virtual-items.ts,message-timeline.tsx}` | TypeScript/TSX | `apps/web/src/features/timeline/projection/**`, `apps/web/src/features/timeline/view/**` | TypeScript/TSX | `cropped_migration` and `same_language_component_integration` | M2-01B canonical projection remains state owner; timeline projector owns only derived rows and transient view state |
| Status precedence, action/observation replacement, late action-result reconciliation and deduplicated UI event fold | supplementary | `OpenHands@c105a82387898e744423c8831d412e26495b38a9` | `frontend/src/utils/status.ts`, `frontend/src/utils/handle-event-for-ui.ts`, `frontend/src/hooks/use-filtered-events.ts` | TypeScript/TSX | `apps/web/src/features/timeline/projection/{phase-machine.ts,event-reconciliation.ts,filtering.ts}` | TypeScript | `cropped_migration` | timeline-derived phase and row reconciliation only |
| Browser action/result/state grouping into one inspectable step, step error extraction and history summaries | supplementary | `browser-use@18484f23ac96bb955259a1c54530a7d265dfffdb` | `browser_use/agent/views.py` (`AgentHistory`, `AgentHistoryList.errors`, `action_history`, `agent_steps`) | Python | `apps/web/src/features/timeline/projection/browser-step-adapter.ts` | TypeScript | `semantic_port/production` under the exception in section 4 | timeline-derived browser step only; browser runtime and history owners remain M1 |
| Background task/job status and park/revive lineage | supplementary | `oh-my-pi@c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | `packages/coding-agent/src/registry/agent-lifecycle.ts`, `packages/coding-agent/src/tools/job.ts` | TypeScript | `apps/web/src/features/timeline/projection/{background-lifecycle.ts,recovery-chain.ts}` | TypeScript | `cropped_migration` | M1 worker/background owners remain canonical; timeline displays derived lifecycle epochs |
| Lifecycle/status vocabulary and terminal/error priority | conformance only | `agent-framework@d50698bb797710bfd1ebf34eb621c905a4009b2d` | workflow/executor lifecycle contracts referenced by the source graph | Python/.NET | behavior tests only | TypeScript tests | `conformance_only` | no production owner |
| Interrupt/resume correlation, stable task identity, pending/committed write distinction and exact-resume lineage | conformance only | `langgraph@5931a5f0b313feff24e2516a586c55601b868ac1` | narrow checkpoint/interrupt contracts recorded in `source-graphs/langgraph/source-graph.md` section 12 | Python/TypeScript | `apps/web/test/worker-causal-timeline.test.ts` | TypeScript tests | `conformance_only` | M1 recovery/checkpoint owners remain canonical |
| Historical runtime sources including OpenClaw | excluded forward only | none | none | none | none | none | `excluded_forward_only` | none |

The production implementation must contain non-zero original-language primary
TypeScript/TSX migration and non-zero TypeScript supplementary migration. It
must not depend at runtime or build time on any `../opencode`,
`../OpenHands`, `../browser-use`, `../oh-my-pi`, `../agent-framework` or
`../langgraph` path.

## 4. Cross-language exception for browser-use

The only authorized cross-language production item is the narrow
`browser-use` history-step mechanism in
`browser_use/agent/views.py`. Direct Python migration is not appropriate for
this slice because:

1. the authoritative target is the browser-resident TypeScript/React timeline;
2. the M1 browser runtime already emits the canonical action, result, artifact
   and state events consumed by M2-01B;
3. adding a Python sidecar or browser-history process would create an external
   runtime dependency and a second presentation state owner;
4. the port is a pure derived projection over already-normalized canonical
   events, not a port of the browser controller, DOM owner, action executor,
   retry loop or history persistence.

The bounded port must preserve these observable semantics:

- actions, result evidence and browser state sharing a step identity appear in
  one stable step group;
- missing results remain partial rather than falsely successful;
- action errors are attached to the corresponding step and affect presentation
  status;
- multiple actions in one step retain deterministic order;
- state/artifact evidence remains inspectable without copying sensitive values.

The target must be dynamically reachable through the real task-detail timeline.
Dedicated tests must prove action/result/state grouping, missing-result
behavior, error behavior and disabled-projector failure. The implementation
will be classified as `semantic_port/production`, never as upstream
original-language migration, and will not transfer browser runtime state
custody.

## 5. Existing Zyra owners and read path

The projector will consume `CanonicalProjectionState` and its established
selectors/indexes:

- `selectEventsForTask`
- `selectWorkersForTask`
- `selectToolsForWorker`
- `selectRecoveriesForTask`
- `selectMutationsForTask`
- `selectEventsForSpan`
- `selectEventsForTool`
- `selectEventsForArtifact`
- `selectEventsForFailure`
- `selectEventsForRecovery`
- `selectEventsForCorrelation`
- `selectEventsCausedBy`

The implementation may add one rich `selectWorkerCausalTimeline` selector in
the new feature boundary. It must not change event ingestion, the canonical
reducer, persistence, cursor semantics, worker lifecycle semantics, lease
transactions, failure ownership, recovery idempotency, scheduler decisions or
topology commit semantics.

The data flow is:

```text
real runtime event stream
  -> M2-01A typed client / ingress
  -> M2-01B CanonicalProjectionStore
  -> immutable CanonicalProjectionState and causal indexes
  -> M2-S02B-01 timeline projector
  -> transient filter/window controller
  -> task-detail timeline UI
```

## 6. Target module plan

The expected production boundary is:

- `apps/web/src/features/timeline/projection/contracts.ts`
  - timeline contracts, phase vocabulary and option/result types;
- `event-reader.ts`
  - canonical event normalization and safe attribute access;
- `event-classification.ts`
  - event type/domain classification without substring-only lifecycle guesses;
- `phase-machine.ts`
  - deterministic lifecycle priority and interval generation;
- `event-reconciliation.ts`
  - stable identity, duplicate/late replacement, partial facts and revision
    reconciliation;
- `causal-graph.ts`
  - explicit/implicit edge construction, missing-edge diagnostics and closure;
- `worker-epochs.ts`
  - worker/lease epochs and replacement lineage;
- `tool-artifact-join.ts`
  - tool, permission, artifact and mutation joins;
- `recovery-chain.ts`
  - failure/recovery attempt grouping and idempotent duplicate collapse;
- `background-lifecycle.ts`
  - park/revive and background job lineage;
- `browser-step-adapter.ts`
  - the bounded browser-use semantic port;
- `critical-path.ts`
  - causal longest-path/critical-chain derivation with cycle guards;
- `filtering.ts`
  - worker/status/type/time filtering plus hidden-causality summaries;
- `drilldown.ts`
  - topology/artifact/tool/failure/recovery target descriptors;
- `windowing.ts`
  - stable bounded row-window selection;
- `projector.ts` and `index.ts`
  - the single rich selector/projector entry;
- `apps/web/src/features/timeline/view/**`
  - transient controller and React presentation;
- `apps/web/src/components/tasks/task-detail.tsx`
  - real route mount and replacement of the shallow recent-event list.

All projection modules must be reachable from the single projector or view
entry. No disconnected helper inventory or source-shaped mirror is authorized.

## 7. Correctness and safety decisions

### 7.1 Ordering and late events

Canonical ordering is `(sequence, aggregateSequence, committedAt, eventId)`.
Rows use stable semantic keys and are reconciled by key so late arrivals update
the original row rather than create a new narrative. A lower-sequence event
that is genuinely new may be inserted into its canonical position. A duplicate
event or duplicate recovery attempt is collapsed by canonical identity and its
diagnostic count is exposed.

### 7.2 Partial streams and reconnect

The projector does not assume every causation, tool result, artifact, failure
or recovery target is present. Missing relations create partial/gap facts.
Reconnect and snapshot restore are read from canonical cursor/runtime
diagnostics; the timeline does not synthesize completion.

### 7.3 Lease and worker replacement

Worker rows are grouped into epochs by worker identity, lease, route and
placement. Replacement produces an explicit relation between old and new
epochs. A replacement worker never rewrites the terminal state or history of
the previous worker.

### 7.4 Recovery idempotency

Recovery chains are keyed by canonical recovery/failure identity and attempt.
Repeated terminal recovery events update the same chain node. Reused
correlation IDs alone are insufficient to collapse distinct attempts.

### 7.5 Filtering

Filtering is presentation-only. Hidden ancestors/descendants become compact
gap summaries containing counts, range and boundary event IDs. Critical-path
and chain computation always runs against the full task projection, never the
filtered subset.

### 7.6 Sensitive data

The timeline reads the canonical safe projection only. It may display digests,
stable IDs, normalized summaries and permitted attributes. It must not recover
raw tool arguments, API keys, credentials, DOM values or unredacted runtime
payloads.

## 8. Verification plan

The implementation commit must be verified with:

1. TypeScript typecheck for the typed API client and web application;
2. focused Bun tests for lifecycle phases, causal joins, critical path,
   filtering/gaps, drill-down, windowing, late/partial events, restart/lease
   replacement, background revive and duplicate recovery;
3. existing canonical projection and topology adjacent regressions;
4. one Python integration test that produces real M1 worker-pool,
   watchdog/fault and recovery events, passes them through the real M2 ingress
   and canonical store, and asserts the TypeScript timeline result;
5. main-path reachability assertions from `TaskDetail`;
6. disabled-projector assertions;
7. forbidden dependency/path scan.

The behavior suite must use real committed runtime events for at least one
worker failure and recovery scenario. Pure fixture replay, log parsing or
static component snapshots are insufficient.

## 9. Effective-code and evidence gate

The slice floor is `6,000` conservative effective production lines. The parent
floor is `12,000` across `M2-S02B-01` and `M2-S02B-02`; only this slice is
closed here.

Before the evidence commit, an exact Git-object-bound auditor will compare the
baseline commit with the implementation commit and report, per file:

- raw additions/deletions;
- executable projection/runtime logic;
- active UI behavior;
- type/interface-only lines;
- schema/DTO lines;
- import/export-only lines;
- comments and blank lines;
- adapter-only lines;
- generated/data/vendor-like/source-pool lines;
- tests, fixtures, probes and audit tooling;
- conservative effective production lines;
- language, source role and migration mode.

Tests, docs, comments, CSS-only presentation, generated/data code,
source-ledger rows, manifests, fixtures, probes, thin adapters, inactive
samples, import/export glue and type/interface-only declarations do not count
toward the production floor.

Every production file above `500` raw added lines, every file contributing more
than `20%` of effective production and every file with more than `30%` excluded
lines requires an individual review. The evidence commit will also contain:

- source-to-target and owner-custody matrix;
- original-language migration summary;
- cross-language exception verification;
- dynamic reachability and disable evidence;
- semantic-effect and failure-path evidence;
- dependency/path audit;
- exact test transcript;
- parent residual line target;
- critical self-review and blocker decision.

The baseline decision commit itself does not claim completion and does not count
toward effective production.
