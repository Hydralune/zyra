# M1-S07C-02 recovery planner / routing / memory integration preimplementation decision

- slice: `M1-S07C-02`
- parent unit: `M1-07C`
- decision status: `frozen_before_production_change`
- slice baseline commit: `fabe347207bc3d2d144ad68c029cb288f09e73a8`
- parent baseline commit: `aa494526ed7f32b177f9a10fa3a93d8a684e0264`
- decision date: `2026-07-22`
- implementation commit: to be frozen after production code and directly related behavior tests
- evidence commit: to be created after the implementation commit from review/evidence/ledger files only
- minimum conservative effective production: `7,000` lines for this slice and `15,000` lines for the parent
- mandatory gate: `docs/milestones/effective-code-language-migration-gate-2026-07-22.md`

No production file was changed after the protected `M1-S07C-01` evidence commit and before this record. Slice accounting will use `fabe347207bc3d2d144ad68c029cb288f09e73a8..implementation`; parent accounting will use `aa494526ed7f32b177f9a10fa3a93d8a684e0264..implementation`. This decision and the later evidence commit are excluded from production totals.

## 1. Frozen source, language, migration and owner decisions

| State domain / role | Selected source mechanisms | Source language | Target language | Migration mode | Zyra target | Canonical owner after integration |
|---|---|---|---|---|---|---|
| primary: recovery integration policy, action progression and durable continuation | Zyra-owned `M1-S07C-01` classifier, decision runtime, action runtime, route runtime, checkpoint bridge, store and memory feedback, integrated with existing 02B/02D/03A/03B/05C/05D/06C/07A/07B public owners | Python | Python | `zyra_owned_implementation` | `packages/scheduler/zyra_scheduler/recovery_runtime/**`, `apps/api/zyra_api/**` | `RecoveryDecisionRuntime` remains the sole policy owner; `RecoveryPlanStore` remains the sole recovery plan/action/checkpoint/route/feedback owner; the integration runtime only sequences their public operations |
| primary: real post-recovery dispatch and execution proof | Zyra task state, graph custody, worker lease, backend lease, provider route, permission queue, compact/memory and event APIs already productized in Zyra | Python | Python | `zyra_owned_integration` | recovery integration modules plus the existing API process assembly | each existing domain owner keeps canonical mutable state; 07C stores only causally linked request and applied receipt references |
| primary semantic source: exact resume | LangGraph checkpoint identity/lineage, pending versus committed writes, stable task ids, atomic commit, interrupt/resume correlation and exact-resume conformance | Python | Python | `conformance_only`; no LangGraph runtime code or dependency enters production | exact-recovery orchestration and conformance tests | `RecoveryPlanStore`, `CheckpointCommitRuntime`, `CheckpointResumeBridge` and `DeterministicCommitRuntime`; no StateGraph, channel, Pregel, Store, ToolNode or stream owner |
| supplementary: provider retry/credential rotation, partial-stream replay fences and durable task/worktree continuation receipts | Oh My Pi `packages/ai/src/auth-retry.ts`, coding-agent loop/session/task/worktree mechanisms selected in the parent source graph | TypeScript | TypeScript | `cropped_migration`; bounded receipt classification, replay eligibility and continuation normalization only | `packages/runtime/claude-runtime/src/recovery/**` | TypeScript owns only process-local normalized receipts and fences; it cannot apply a route, persist a recovery plan or resume a canonical owner |
| conformance: permission ask/deny and pending approval | Claude Code batch 03 plus the already productized 03A control plane | TypeScript source graph; Zyra production boundary is Python/TypeScript | no migration quota | `conformance_only` | ingress mapping and behavior tests | PermissionControlPlane remains canonical; recovery cannot retry a denied or pending dangerous tool |
| conformance: prompt-too-long compact/restore | Claude Code batch 04 plus existing 02D/06C compact and memory owners | TypeScript source graph; Zyra production boundary is Python | no migration quota | `conformance_only` | exact-recovery/continuation tests | session lifecycle, compact runtime and MemoryFabric remain canonical |
| conformance: MCP auth, reconnect, breaker and elicitation | Claude Code batch 05 plus existing 03B MCP event/control path | TypeScript source graph; Zyra production boundary is Python/TypeScript | no migration quota | `conformance_only` | typed ingress and recovery integration tests | MCP client/control owner remains canonical; recovery stores only linked action receipts |
| conformance: subagent failure and resume | Claude Code batch 07 plus existing SkillTool/AgentTool/07A boundaries | TypeScript source graph; Zyra production boundary is Python/TypeScript | no migration quota | `conformance_only` | typed ingress, route and memory tests | subagent/task/worker owners remain canonical |
| conformance: API retry, partial stream and fallback | Claude Code batch 08 and existing CodeWorker query/watchdog receipts | TypeScript | no new migration quota beyond the OMP supplement above | `conformance_only` | receipt ingress and end-to-end tests | query loop and provider control plane retain their owners |
| conformance only | Agent Framework workflow/checkpoint and AG-UI approval/history; AgentScope selected lifecycle/workspace behavior; opencode durable-session/provider behavior; Hermes typed protocol behavior | mixed | none | `conformance_only` or `reference_only` | tests/review only | no production owner or line quota |
| excluded | OpenClaw | none | none | `excluded_forward_only` | none | none |

The structured contract is controlling. The only original-language supplementary production quota is bounded Oh My Pi TypeScript; therefore the implementation must contain non-zero conservative TypeScript production. LangGraph and all other conformance/reference sources create no migration quota.

## 2. Frozen integration architecture

The implementation will extend, rather than replace, the `M1-S07C-01` path:

`typed domain receipt -> RecoverySignalClassifier -> RecoveryDecisionRuntime -> RecoveryPlanStore -> RecoveryActionRuntime -> canonical owner receipt -> execution continuation/dispatch proof -> RoutingMemoryFeedback -> next context and route selection`.

The production decomposition is frozen as follows:

1. typed ingress for permission, MCP, compact/API, worker, backend/provider, subagent, worktree and durable-task receipts, with source-specific validation and no free-text identity inference;
2. multi-owner state fusion that requires at least three real state families and records their revision/digest receipts without copying owner state;
3. exact recovery orchestration for committed side effects, pending requests, in-flight messages, response correlation, topology revisions and restart continuation;
4. explicit graph, worker, backend and provider/model route-layer execution with single-layer isolation by default and policy-owned cross-layer escalation;
5. a durable recovery continuation queue derived from `RecoveryPlanStore` plans/outcomes/receipts, including lease, idempotency and restart rehydration;
6. applied-action verification that proves a later task, graph, worker, backend, provider, permission, compact, MCP or context projection changed before feedback is accepted;
7. a causal trace linking failure span, signal, checkpoint, side-effect fence, plan, action receipt, route mutation, continuation and memory update within the same run/task;
8. a dependency/disable control plane that fails closed when checkpoint, worker, backend, provider, graph, side-effect-fence or OMP receipt integration is disconnected;
9. candidate routing that consumes `RoutingMemoryFeedback` to alter later worker/backend/provider/model order while preserving owner lease acquisition;
10. API routes for typed recovery observations, plan driving/restart continuation, causal inspection and component-state inspection through the existing `zyra_api` process;
11. an Oh My Pi-derived TypeScript supplement for credential rotation, partial-stream replay safety, MCP breaker/reconnect, worktree dirty/conflict and durable background-task continuation receipts, connected to the existing CodeWorker recovery receipt path.

No integration module may independently select an action with ad-hoc `if/else`, accept an LLM action as authoritative, mutate graph/worker/backend/provider state, or create a shadow checkpoint/memory store. It must call the frozen `RecoverySignalClassifier`, `RecoveryDecisionRuntime`, `CheckpointResumeBridge`, `RoutingMemoryFeedback` and `RecoveryPlanStore` path.

## 3. State custody and transaction freeze

| State/fact | Canonical owner | Integration rule |
|---|---|---|
| recovery signal, plan, action cursor, action receipt, outcome, route decision, checkpoint, side-effect fence and recovery feedback | `RecoveryPlanStore` | all durable recovery facts use its SQLite transaction and idempotency boundaries; restart derives work from its plans and receipts |
| recovery action selection | `RecoveryDecisionRuntime` | integration may add validated context/evidence but cannot insert a selected action |
| exact resume validation and canonical owner resume | `CheckpointResumeBridge` | signature, topology, owner refs, completed steps, pending requests and fences are validated before owner calls |
| task/node execution state | `SQLiteStore` / TaskState | integration records immutable before/after refs and dispatch receipt ids only |
| graph/topology | `GraphStateCustody` and 07A topology owner | graph replan/mutation uses the owner port; no graph state is stored in 07C payloads beyond version refs |
| worker attempt/lease | 07A worker pool | worker reroute must acquire a successor lease; no process-local worker map is canonical |
| backend/workspace | 05D BackendRegistry/workspace owner | switch-backend must acquire a canonical backend lease and keep other route layers unchanged unless escalation is explicit |
| provider/model/credential/transport | 05D ProviderControlPlane and credential store | provider/model fallback must return a route lease/checksum/version receipt; no credential material enters recovery persistence |
| permission | 03A PermissionControlPlane | ask creates or links a real pending request; denial blocks tool continuation; sealed mode cannot wait for a human |
| compact/session/memory | 02B/02D session and compact owners plus 06C MemoryFabric | recovery records compact/resume receipts and context digest; it does not mirror messages or memory records |
| MCP | 03B MCP client/control events | auth/reconnect/breaker receipts are linked; the integration does not become an MCP client |
| runtime event trace | 05C canonical event store | causal recovery projections reference real spans, calls, artifacts, leases and state mutations |
| TypeScript OMP supplement | process-local typed receipt runtime | no durable owner; Python admits the receipt through the classifier and store path |

## 4. Frozen semantic acceptance paths

The implementation and direct tests must demonstrate all of the following through production APIs or runtime entry points:

- permission denied/pending creates an ask/blocked path and never dispatches the denied tool;
- prompt-too-long compacts canonical context, resumes the exact checkpoint and produces a subsequent turn/context mutation;
- exhausted API/provider retry changes model/provider or backend through a canonical route owner;
- worker loss creates a new 07A attempt/lease while graph/backend/provider layers remain stable;
- backend failure creates a new backend/workspace lease while worker/provider remain stable unless escalation is explicitly recorded;
- provider 429 or revoked credential rotates a provider/model/credential route lease while graph/worker/backend remain stable;
- requirement change mutates the graph revision as a control replan and does not increment fault retry history;
- subagent failure produces routing memory and replan/reroute evidence that changes a later route decision;
- a crash after a real side effect never repeats the committed effect or processed response;
- pending permission/MCP requests and in-flight messages resume exactly; corrupt encoding, path escape, signature/topology mismatch and unsafe payloads fail closed;
- branch aliasing/random completion order cannot pollute snapshots and conflicting writes explicitly rebase, serialize or replan;
- feedback is rejected until an applied owner receipt and changed downstream projection exist;
- restart can continue a planned, applying or waiting recovery from `RecoveryPlanStore` plus canonical owner refs without copied mutable owner state.

The disable matrix will independently disconnect checkpoint restore, side-effect fences, graph router, worker router, backend router, provider control plane, memory feedback and the TypeScript receipt normalizer. Each affected semantic path must fail visibly; legacy fallback, fixture replay, OMP CLI/RPC or another route layer cannot mask the failure.

## 5. Counting and validation freeze

The slice target is at least `7,000` conservative effective production lines. The implementation is expected to consist primarily of Python recovery integration/runtime code plus non-zero TypeScript OMP supplementary production. Type/interface/Protocol-only lines, DTO/schema boilerplate, adapters without decisions or state effects, generated/data/source-pool code, tests, docs, comments, exports and ordinary helper scripts are excluded. The parent cumulative audit must add the already frozen `8,429` effective production lines from `M1-S07C-01` only after checking overlap and then recompute from the parent baseline.

Direct validation will cover real API reachability, non-fixture input, restart persistence, concurrency/reentrancy, four isolated route layers, explicit escalation, exact resume, applied receipt verification, causal trace, feedback effect, OMP scenarios and every disable switch. Adjacent 02D/03A/03B/05D/06C/07A/07B and API regression tests will run in proportion to the current diff. Because this is the final parent slice and it changes the default recovery assembly, the parent closeout will also perform the applicable parent cumulative review, dependency/path scan, source-to-target audit and focused clean-state replay. Broader M1-07 digital-stage cleanroom and full-suite work remains assigned to the later aggregate review unless a concrete cross-slice regression triggers it here.

Every new file above 500 raw lines, every file contributing more than 20 percent of effective production, and every file with more than 30 percent excluded lines will receive an individual large-file review. Final evidence will contain per-file raw additions, production-runtime, UI, type/interface, schema/DTO, adapter-only, generated/data, test/docs and conservative effective buckets; a language/role summary; main-path and disable evidence; test transcript; dependency audit; parent cumulative count; residual debt; and an explicit blocker decision.

## 6. Commit order

1. protected slice baseline: `fabe347207bc3d2d144ad68c029cb288f09e73a8`;
2. this implementation decision commit;
3. implementation commit: production code plus directly related behavior tests only;
4. evidence commit: critical review, machine evidence, ledger/source-to-target synchronization and other documentation only;
5. root `docs/milestones/execution-state.yaml` update after the Zyra evidence commit exists; the root is not the Zyra Git repository and cannot be included in the evidence commit.

Neither implementation nor evidence commits will be amended or squashed after their hashes are frozen.
