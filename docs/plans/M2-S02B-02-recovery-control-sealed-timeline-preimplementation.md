# M2-S02B-02 recovery control and sealed timeline preimplementation decision

## 1. Slice identity and immutable baseline

- Slice: `M2-S02B-02`
- Parent: `M2-02B timeline and worker state`
- Numeric stage: `M2-02 console event, topology and timeline`
- Authority entry:
  `../../../docs/milestones/M2-console-demo/slice-02b-02-recovery-control-sealed-timeline-integration.md`
- Parent authority:
  `../../../docs/milestones/M2-console-demo/unit-02b-timeline-worker-state.md`
- Baseline Zyra commit:
  `46be71eb4ced2da38f5b05fce24907da7ffc8f6c`
- Numeric-stage baseline:
  `f7fff49be91fb0f797260c03ff9cca76c06c5974`
- Baseline worktree: clean
- Decision commit: pending until this document is committed.
- Implementation commit: pending; it must be later than the decision commit and
  contain production code plus directly related behavior tests.
- Aggregate-review fix commit: optional and only allowed before the evidence
  commit if the M2-02 aggregate review finds a real defect.
- Evidence commit: pending; it must be later than the final implementation
  target and contain the exact-commit auditor, source ledger update, slice
  self-review, M2-02 aggregate review and machine-readable evidence.

The authority state protects all slices through `M2-S02B-01`. This decision
does not reopen their state owners or rewrite their evidence. It extends the
read-only worker causal timeline with one interactive control surface and one
large-history presentation boundary.

## 2. Completion boundary

The slice is complete only when the real task-detail route supports all of the
following against canonical runtime state:

1. named interactive operators can submit `kill`, `steer`, `retry`,
   `reassign` and exact-checkpoint `resume`;
2. every request has deterministic identity, idempotency, timeout, expected
   revision and target-owner validation;
3. the response and later canonical events reconcile into
   `pending`, `denied`, `applied` or `failed` receipts;
4. the timeline does not own approval state and never converts a pending
   permission request into an optimistic success;
5. sealed autonomous mode rejects every human mutation, records one
   intervention attempt per idempotent request, keeps
   `human_intervention_count=0`, and invokes deterministic deny/replan without
   waiting for a user;
6. worker kill fences/cancels the real worker lease, reassign changes the real
   worker/lease route, retry changes the M1 recovery/continuation state, steer
   changes the real graph route, and resume uses the existing exact checkpoint
   owner;
7. a late receipt, owner loss, stale revision, request race, network timeout,
   disconnect and reconnect remain distinguishable;
8. closing the timeline viewer detaches presentation listeners only; it does
   not cancel a durable control request or stop the runtime;
9. histories with thousands of real effective events use a bounded virtual
   window, time/causal folding, indexed search, critical-path focus, goal-drift
   analysis and compact/checkpoint/placement/effective-step overlays;
10. `M2-02B` and the `M2-02A/M2-02B` numeric stage pass their cumulative
    effective-code, behavior, source, dependency, build and cleanroom review.

This slice will not add a second event store, approval queue, recovery policy,
worker store, scheduler, checkpoint store, topology owner or frontend-only
worker state.

## 3. Source-role and language decision

| Capability chain | Source role | Repository and commit | Exact source paths / symbols | Source language | Zyra target | Target language | Migration mode | Owner after integration |
|---|---|---|---|---|---|---|---|---|
| Durable session control interaction, abort-before-revert ordering, request-completion UI update, stable timeline row/window behavior | primary | `opencode@adf178a6b95c61506ddaadaf4dd062badb4a8fda` | `packages/app/src/pages/session/use-session-commands.tsx` (`runCommand`, `undo`, `redo`); `packages/app/src/context/permission.tsx`; `packages/app/src/pages/session/timeline/{model.ts,row-reconciliation.ts,message-timeline.tsx}` | TypeScript/TSX | `apps/web/src/features/timeline/control/**`, `apps/web/src/features/timeline/scale/**`, timeline view | TypeScript/TSX | `cropped_migration` and `same_language_component_integration` | M1 `RuntimeControlDispatcher`, permission runtime and canonical owner handlers remain authoritative; the timeline owns only request/view orchestration |
| Stop/resume pending, rollback, refetch and live status behavior | supplementary | `OpenHands@c105a82387898e744423c8831d412e26495b38a9` | `frontend/src/hooks/mutation/use-unified-stop-conversation.ts`; `use-unified-start-conversation.ts`; `use-v1-resume-conversation.ts`; `frontend/src/components/features/controls/agent-status.tsx`; `frontend/src/hooks/use-sandbox-recovery.ts` | TypeScript/TSX | control receipt reconciliation, connection recovery and control panel state | TypeScript/TSX | `cropped_migration` | canonical backend receipts remain truth; no optimistic worker mutation |
| Action error, consecutive failure, connection recovery, bounded retry and pause/resume state vocabulary | supplementary | `browser-use@18484f23ac96bb955259a1c54530a7d265dfffdb` | `browser_use/agent/views.py` (`AgentState`, `ActionResult`, `AgentHistoryList.errors`); `browser_use/agent/service.py` (`_check_stop_or_pause`, reconnection recovery, consecutive-failure reset, `pause`, `resume`) | Python | event-to-receipt status semantics and large-history recovery overlays only | TypeScript | `semantic_port/production` under section 4 | M1 browser/action runtime remains owner; timeline displays canonical facts |
| Workflow/checkpoint lifecycle and AG-UI approval/history | conformance only | Agent Framework pinned source facts already recorded by the project source graph | workflow/checkpoint and AG-UI approval/history contracts | Python/.NET | focused behavior tests | TypeScript/Python tests | `conformance_only` | no production owner |
| Lifecycle/control negative examples | reference only | OMP facts already recorded in protected source analysis | retry/lifecycle/control contracts | TypeScript | sealed/failure tests only | tests | `reference_only` | no production owner and no second control runtime |
| LangGraph narrow exact-resume semantics | conformance only | `langgraph@5931a5f0b313feff24e2516a586c55601b868ac1` | protected source-graph section 12: checkpoint identity, pending/committed writes, stable task id and interrupt/resume correlation | Python/TypeScript | exact-resume and stale/race tests | tests | `conformance_only` | M1 recovery/checkpoint runtime remains owner |
| OpenClaw | excluded forward only | none | none | none | none | none | `excluded_forward_only` | none |

The production result must contain non-zero primary original-language
TypeScript/TSX migration and non-zero supplementary TypeScript/TSX migration.
No runtime or build path may reference a workspace source repository.

## 4. Bounded browser-use cross-language exception

No Python browser-use source will be translated into a server control runtime.
The only cross-language production work is a bounded TypeScript semantic port
of already-canonical action state:

- an action error remains failed until a canonical retry/recovery fact appears;
- a reconnect receipt may supersede a connection-loss presentation without
  erasing the original failure;
- consecutive failures and bounded retry attempts remain visible;
- pause/resume state is displayed but does not become a frontend worker owner;
- a missing action result stays pending/partial.

This exception is necessary because the target is the browser-resident
timeline, M1 already owns browser execution and event production, and adding a
Python presentation sidecar would duplicate state. The exception excludes the
browser controller, DOM state, tool execution, retry loop, persistence and
watchdogs. Its target lines will be reported as `semantic_port/production`,
not original-language migration.

## 5. Canonical owner and command path

The request path is:

```text
TaskDetail timeline action
  -> TypeScript RecoveryControlRuntime
  -> M2-01A TaskApi.controlCommand
  -> POST /tasks/{task_id}/commands
  -> M1 RuntimeControlDispatcher
  -> M1 permission_authorize / sealed policy
  -> existing canonical owner
  -> canonical command / worker / recovery / route events
  -> M2-01B CanonicalProjectionStore
  -> timeline receipt observer
```

Command mapping:

| UI action | Command | M1 state owner/effect |
|---|---|---|
| `kill` | `/kill` | `WorkerControlRuntime` cancels/fences the task lease and, for a worker target, stops/drains the real worker |
| `steer` | `/steer` | `RecoveryApplication` classifies a `CONTROL_RUNTIME:requirement_changed` signal and commits through `GraphStateCustody` |
| `retry` | `/retry` | `RecoveryApplication` classifies a bounded retryable signal and applies through the existing QueryEngine/recovery continuation owners |
| `reassign` | `/reassign` | `RecoveryApplication` classifies worker loss and acquires a successor through `WorkerPoolFoundationRuntime` |
| `resume` | `/resume` | existing `SessionControlRuntime` and `RecoveryApplication.resume_checkpoint` exact-resume path |

The dispatcher remains the only admission/idempotency/queue lifecycle owner.
The permission runtime remains the only permission owner. The timeline may
cache immutable response receipts for presentation, but it must not invent,
approve or mutate permission requests.

## 6. Stale, ownership and race contract

Every mutation request carries:

- `request_id`, `command_id` and an idempotency key derived from the complete
  normalized request;
- task/run identity;
- named actor identity;
- optional expected session/projection revision;
- expected worker, lease, attempt, checkpoint, graph or node identity as
  applicable;
- a bounded reason/instruction and timeout.

Before mutation, the backend handler validates the target against canonical
task metadata and the relevant M1 store. A mismatched task/run, replaced
worker, fenced lease, missing checkpoint, stale revision or foreign graph
target fails closed. The dispatcher store makes the same idempotency key replay
the same response and rejects changed content. Concurrent controls use the
existing session mutation guard; a losing request is queued or receives a
revision conflict, never an optimistic success.

The frontend keeps an append-only view ledger keyed by command id and
idempotency key. A direct response is provisional until canonical evidence is
observed. A late response cannot overwrite a newer terminal receipt unless it
contains a higher canonical revision and consistent owner evidence.

## 7. Sealed autonomous contract

For every mutating timeline command in sealed autonomous mode:

1. `RuntimeControlDispatcher.permission_authorize` returns false before the
   action handler runs;
2. one operator-intervention ledger entry is appended for the request id;
3. idempotent replay does not increment the ledger again;
4. `operator_intervention_attempt_count` increases while
   `human_intervention_count` remains exactly zero;
5. the denial creates a typed permission-denied recovery signal in
   `SEALED_AUTONOMOUS` mode;
6. `RecoveryDecisionRuntime` chooses deterministic graph replan/abort policy;
7. no permission prompt, approval wait or frontend retry loop is opened;
8. failure to apply the safe replan is recorded as fail-closed and returned
   without hanging or applying the human command.

The timeline displays the canonical denial and automatic recovery result. It
does not own or recompute the sealed decision.

## 8. Large-history projection boundary

The existing causal projector remains the fact owner. New scale modules derive
only presentation indexes:

- a measured-height virtualizer with binary-search range selection, anchor
  restoration and bounded overscan;
- temporal buckets and causal folds that preserve boundary event ids,
  hidden counts and critical-path crossings;
- a token/prefix/trigram search index over safe row fields;
- goal-drift epochs derived from canonical requirement/goal mutations;
- effective-step classification that excludes heartbeat, repaint, replay and
  no-op facts while retaining real state/control/recovery mutations;
- compact, checkpoint, placement and worker-replacement overlays;
- critical-path focus that never recomputes the path from filtered rows;
- reconnect snapshots that can reconcile late rows without resetting scroll
  or selected identity.

Virtualization and folding never change canonical ordering or completeness.
Search/fold/overlay state is transient and disappears with the viewer.

## 9. Target module plan

Production targets:

- `apps/web/src/features/timeline/control/contracts.ts`
  - request/receipt/owner/connection contracts;
- `validation.ts`
  - bounded values, target ownership and request normalization;
- `commands.ts`
  - the five action-to-command mappings;
- `receipts.ts`
  - safe response parsing and canonical phase mapping;
- `sealed.ts`
  - sealed presentation policy and intervention facts, not authorization;
- `ledger.ts`
  - immutable receipt reconciliation, idempotency and stale/late rules;
- `observation.ts`
  - command/event/recovery/worker evidence matching;
- `runtime.ts`
  - durable submission orchestration, timeout, reconnect and listener lifecycle;
- `transport.ts`
  - the thin M2-01A client binding;
- `apps/web/src/features/timeline/scale/**`
  - virtual window, folds, search, drift, overlays and scale coordinator;
- `apps/web/src/features/timeline/view/**`
  - control panel, receipt list and large-history row viewport;
- `apps/web/src/features/timeline/projection/**`
  - only narrowly necessary canonical fact readers/classifiers;
- `packages/commands/zyra_commands/runtime/registry.py`
  - descriptors for the four new commands; existing `/resume` is retained;
- `apps/api/zyra_api/main.py`
  - canonical-owner handlers and sealed deny/replan integration.

The implementation may extend existing files when ownership is already clear.
It must not add a standalone control API, approval database, polling daemon or
server-side timeline projector.

## 10. Verification plan

Focused verification must prove:

1. TypeScript command construction, validation, idempotency and receipt parsing;
2. pending/denied/applied/failed and late/stale/race reconciliation;
3. timeout and disconnect/reconnect without frontend state mutation;
4. viewer close leaves the durable request running;
5. real HTTP controls change canonical session/worker/lease/graph/recovery state;
6. sealed denial increments the operator ledger once, leaves human count zero,
   applies deterministic replan or fail-closed, and never waits for approval;
7. ownership loss and stale revision reject before the effect;
8. disabling the timeline control binding makes the control behavior fail;
9. thousands of effective events remain searchable and bounded by the virtual
   window;
10. causal folds preserve boundary evidence and critical-path crossings;
11. goal drift and compact/checkpoint/placement/effective-step overlays are
    derived from real canonical facts;
12. rapid events, long recovery chains and late receipts keep stable identity;
13. existing M2-01B, topology and M2-S02B-01 timeline regressions remain green.

The numeric-stage aggregate will run, against one final target commit:

- all web tests and production build/typecheck;
- applicable M2-02 Python integration tests;
- exact source/dependency/owner audits;
- a clean Git-object export with a frozen dependency install;
- source-to-target ledger verification;
- cumulative M2-02A/M2-02B effective-line review;
- the common critical review taskbook.

## 11. Effective-code gate

The slice floor is `6,000` conservative effective production lines. The
M2-02B parent floor is `12,000`; `M2-S02B-01` contributed `6,836`, so the
parent residual is `5,164`, but this slice must independently reach `6,000`.

The exact Git-object-bound auditor will compare the baseline commit with the
final implementation target and classify every changed file into:

- executable production behavior;
- active UI behavior;
- type/interface/declare lines;
- repeated schema/DTO or generated wire/client lines;
- imports/exports, comments and blanks;
- JSX/CSS/SVG/static presentation;
- adapter-only code;
- tests/fixtures/probes;
- docs/scripts/audit tooling;
- generated/data/vendor/source-pool content.

Only executable production and conservative active UI behavior count. Tests,
docs, comments, type-only declarations, generated/data content, ledgers,
fixtures, static presentation and thin adapters do not. Every production file
over 500 raw added lines, every file contributing more than 20% of effective
production, and every file with more than 30% exclusions receives individual
review.

The M2-02B cumulative report will add exact slice counts rather than raw parent
numstat. The M2-02 numeric-stage report will separately show raw diff,
effective production, tests, docs, scripts, data and exclusions.

## 12. Blocker rules

The slice fails if any of these remain true:

- a control only changes React state;
- a frontend receipt is treated as canonical approval or worker state;
- sealed mode can apply a human command or wait on an approval;
- a retry/reassign bypasses M1 recovery policy or worker custody;
- `/resume` accepts a foreign or stale checkpoint;
- request timeout cancels or ambiguously replays a durable backend effect;
- viewer unmount stops the runtime;
- thousands of events render without a bounded virtual range;
- folded/search results lose causal boundary evidence;
- source-language primary TypeScript migration is zero;
- the slice effective floor, parent floor or numeric-stage aggregate review is
  not met;
- a root source repository, cache or editable/link dependency is needed at
  runtime, build time or test time.
