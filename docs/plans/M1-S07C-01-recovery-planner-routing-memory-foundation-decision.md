# M1-S07C-01 recovery planner / routing / memory foundation preimplementation decision

- slice: `M1-S07C-01`
- parent unit: `M1-07C`
- decision status: `frozen_before_production_change`
- baseline commit: `aa494526ed7f32b177f9a10fa3a93d8a684e0264`
- decision date: `2026-07-22`
- implementation commit: to be frozen after production code and direct behavior tests only
- evidence commit: to be created after the implementation commit from review/evidence/ledger files only
- minimum conservative effective production: `8,000` lines
- mandatory gate: `docs/milestones/effective-code-language-migration-gate-2026-07-22.md`

No production file was changed before this record. Effective-code accounting will use the baseline above through the final implementation commit; the preimplementation and evidence commits are not counted as production.

## 1. Source, language, migration and canonical-owner freeze

| Source role / state domain | Source repository and selected mechanisms | Source language | Target language | Migration mode | Zyra target | Canonical owner after this slice |
|---|---|---|---|---|---|---|
| primary: recovery policy and plan execution | Zyra-owned deterministic policy over the existing 02B/02D/03A/05C/05D/07A/07B public receipts | Python | Python | `zyra_owned_implementation` | `packages/scheduler/zyra_scheduler/recovery_runtime/**` | `RecoveryDecisionRuntime` owns deterministic action selection; `RecoveryPlanStore` owns recovery plans, attempts, receipts and outcomes |
| primary semantic source: exact-resume contract | LangGraph `libs/checkpoint/langgraph/checkpoint/base/**`, checkpoint tuple/pending-write tests and conformance specs, restricted to identity/lineage/pending-versus-committed writes/stable task id/atomic put/interrupt-resume correlation | Python | Python | `conformance_only`; Zyra-owned implementation of the selected contract, with no LangGraph production package or graph runner copied | `packages/scheduler/zyra_scheduler/recovery_runtime/checkpoint_*.py`, `delta_*.py`, focused conformance tests | `RecoveryPlanStore` owns recovery checkpoint rows; `CheckpointResumeBridge` owns validation and resume preparation; 07A and 07C owners retain their pre-existing state domains |
| supplementary: retry/fallback classification and receipt normalization | Oh My Pi `docs/non-compaction-retry-policy.md`, `packages/ai/src/auth-retry.ts`, selected provider error/fallback helpers | TypeScript | TypeScript | `cropped_migration`, generalized into typed candidates/receipts without copying OMP provider/session state | `packages/runtime/claude-runtime/src/recovery/**` | TypeScript runtime owns only process-local candidate/receipt construction; Python `RecoveryDecisionRuntime` remains the only applied-policy owner |
| supplementary: append-only resume/fork and task/worktree outcome receipts | Oh My Pi `packages/coding-agent/src/session/session-manager.ts`, `task/executor.ts`, `task/worktree.ts` | TypeScript | TypeScript | `cropped_migration`, retaining replay-safe receipt, branch ancestry, task terminal state and merge-side-effect fences | `packages/runtime/claude-runtime/src/recovery/**` | TypeScript runtime owns only receipt validation and normalization; it does not own session, task, workspace, checkpoint or recovery persistence |
| conformance: permission/compact/MCP/subagent/API failure semantics | Claude Code source graph batches 03/04/05/07/08 and the already internalized Zyra runtimes | TypeScript source graph; existing Zyra Python/TypeScript contracts | no new source migration quota | `conformance_only` | classifier and behavior tests | existing domain owners remain canonical; recovery consumes typed receipts only |
| conformance: durable workflow/checkpoint behavior | Agent Framework workflow/checkpoint and approval/history behavior | Python/C# source facts as applicable | no production migration | `conformance_only` | focused tests only | no Agent Framework state store or workflow runtime enters production |
| excluded | OpenClaw | none | none | `excluded_forward_only` | none | none |

The structured language contract in the slice is controlling: LangGraph is a narrow semantic/conformance source and produces no migrated-line quota; the recovery planner remains Python; the only retained-language supplementary migration is Oh My Pi TypeScript, whose conservative original-language effective production must be non-zero.

## 2. Explicit non-owners and excluded control flow

- LangGraph `StateGraph`, channel/reducer machinery, Pregel planner/runner, stream controller, Store, ToolNode/prebuilt agent, SDK/server/deploy and dynamic graph compiler are excluded. They cannot write a Zyra checkpoint or select a route.
- Agent Framework is conformance only and cannot become a second workflow/checkpoint owner.
- Oh My Pi `AgentSession`, `SessionManager`, provider registry, TaskTool store, worktree owner and RoboOmp queue are not copied as state stores. Only bounded TypeScript normalization and replay-fence mechanisms are retained.
- LLM/advisor/model suggestions may be recorded as candidates or explanations. They cannot write checkpoint state, bypass permission, acquire a worker/backend/provider lease, or select the applied action.
- Requirement change is a control reason for graph replan. It never enters failure retry counters, failure memory, backend health penalty or retry backoff.

## 3. State custody and transaction boundaries

| State domain | Canonical owner | 07C-01 rule |
|---|---|---|
| recovery signals, plans, candidate evaluations, action attempts, receipts and outcomes | `RecoveryPlanStore` | versioned SQLite rows with optimistic revision and idempotency fences; one applied decision per signal/revision |
| recovery checkpoint manifest, committed refs, pending writes, in-flight messages and resume correlation | `RecoveryPlanStore` | atomic SQLite transaction; versioned JSON allowlist only; pending and committed rows are separate; corrupt or unknown versions fail closed |
| branch-local delta journal and write-set conflicts | `RecoveryPlanStore` plus `DeterministicCommitRuntime` | immutable copy-on-write values, explicit read/write sets, deterministic ordering, no shared mutable aliases, no pending delta in canonical snapshots |
| graph/topology state and graph route | existing 07A `GraphStateStore` / `GraphStateCustody` and 07C applied graph-route receipt | 07C requests and records a graph route change; it does not replace the graph-state owner |
| worker attempt and lease | 07A worker pool | 07C requests a new worker lease through a port and stores only the returned receipt/reference |
| execution backend and workspace | 05D `BackendRegistry` / workspace owner | 07C requests a backend/workspace change and stores before/after refs plus the owner receipt |
| provider/model/credential/transport route | 05D ProviderControlPlane | 07C requests a provider route change and stores only the immutable route receipt |
| session and compact restore | 02B session lifecycle and 02D compact runtime | `CheckpointResumeBridge` validates and calls their public resume/compact ports; it cannot mirror session state |
| permission request/decision | 03A permission store/runtime | ask-permission creates/links a real pending request; sealed autonomous mode deterministically denies and replans rather than waiting |
| fault evidence and handoff | 07B `FaultStateStore` / `WatchdogRecoveryBridge` | 07C consumes/acknowledges the leased handoff; it does not rewrite fault state or classifications |
| memory | existing `MemoryFabric` / `SQLiteStore` | `RoutingMemoryFeedback` writes typed recovery outcome facts through the existing owner and reads them for later route context |
| canonical runtime events | 05C runtime event store | recovery emits causally linked decision/action/checkpoint/feedback events through the existing event port, never a shadow event database |

## 4. Frozen production decomposition

The Python package `zyra_scheduler.recovery_runtime` will contain real, mutually bounded runtime modules for:

1. typed recovery signal admission and deterministic classification;
2. recovery policy budgets, candidate generation, eligibility, ranking and decision explanation;
3. versioned checkpoint manifests, strict JSON codec, atomic persistence and integrity verification;
4. branch-local delta journal, read/write-set conflict detection and deterministic commit;
5. side-effect and processed-response replay fencing;
6. `CheckpointResumeBridge` orchestration across session, compact, worker lease and topology signatures;
7. layered route decisions and owner-specific route request/receipt ports;
8. durable recovery plans, action attempts, outcomes, leases and query projections;
9. concrete action execution for retry, reroute, degrade model, switch backend, replan, ask permission and checkpoint resume;
10. routing/memory feedback that changes subsequent route policy context;
11. a composed foundation application and API service reachable from the existing application process;
12. 07B handoff consumption and 05C event projection without taking either owner's state.

The TypeScript `claude-runtime/src/recovery` module will retain only Oh My Pi-derived, process-local mechanisms: structured retry/fallback reason classification, bounded backoff/cooldown candidates, partial-output and tool-effect replay fences, append-only resume/fork receipts, task terminal receipts, worktree merge receipts and a normalized envelope emitted to the Python owner. It will be used by the existing query/watchdog runtime path, not kept as an isolated sample.

## 5. Frozen main-path behavior

The required path is:

`typed runtime/fault/control receipt -> RecoverySignalClassifier -> persisted RecoveryPlan -> RecoveryDecisionRuntime -> concrete owner-specific action -> Checkpoint/LayeredRoute receipt -> RoutingMemoryFeedback -> changed subsequent decision context/route -> 05C event projection`.

At minimum:

- permission pending/ask selects `ask_permission`, while sealed autonomous deny selects deterministic replan/deny and never retries a dangerous tool;
- prompt-too-long selects compact then checkpoint resume, preserving correlation and bypassing completed steps;
- stream stall may retry within budget and then fall back/degrade with partial-output/side-effect fencing;
- backend unavailable requests a new 05D backend lease;
- subagent failure requests a new 07A worker route or graph replan;
- MCP auth required enters auth/control and never generic transport retry;
- requirement change selects graph replan with a control reason and leaves fault counters unchanged;
- completed response IDs and committed side effects are never replayed;
- memory feedback measurably changes a subsequent candidate score or route context.

The API integration will expose task-scoped recovery signal submission, plan inspection, action application, checkpoint commit/resume and outcome feedback through the existing `zyra_api` process. Tests must show that disconnecting the classifier, decision runtime, checkpoint bridge, concrete route port or memory feedback changes or breaks this real path.

## 6. Validation and counting freeze

The implementation commit will contain production code plus directly related Python and TypeScript behavior tests only. Focused validation will cover:

- the six required signal families and every required action family;
- atomic checkpoint save, corrupt/unknown/path-escape/pickle rejection, pending/committed separation, topology/signature mismatch, processed-response and external-effect replay fences;
- branch-local copy isolation, write-set conflicts, deterministic commits and concurrent/reentrant idempotency;
- route layer ownership, before/after refs and applied receipts;
- 07B handoff, 07A lease, 05D backend/provider, 02B/02D session/compact, 03A permission, MemoryFabric and 05C boundaries through production ports or existing real stores;
- API dynamic reachability, restart persistence, failure paths and disable/disconnect behavior;
- TypeScript recovery receipt behavior and its active query/watchdog hook;
- adjacent graph custody, worker pool, watchdog, backend, memory and API regression tests;
- dependency/path scans proving no `../` source repository, npm link, editable install, external build context, cache, persisted test database or generated artifact is required.

The evidence commit will include a per-file conservative line bucket, source/language summary, manual review for every file over the gate's large-file thresholds, target coverage matrix, main-path evidence, excluded-line reasons, dependency audit, test transcript, internalization ledger updates and final blocker decision. `git diff --numstat` is only the input to that audit and cannot itself prove the `8,000`-line threshold.

## 7. Commit order

1. baseline: `aa494526ed7f32b177f9a10fa3a93d8a684e0264`;
2. this preimplementation decision commit;
3. implementation commit: production plus direct behavior tests only;
4. evidence commit: review/evidence/ledger synchronization only;
5. root `docs/milestones/execution-state.yaml` update outside the Zyra Git repository after the evidence commit exists.

The final evidence record will fill the implementation hash and name the evidence commit creation boundary without amending or squashing this preimplementation decision.
