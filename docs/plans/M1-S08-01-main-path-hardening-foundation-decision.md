# M1-S08-01 main-path hardening foundation — preimplementation decision

Date: 2026-07-22

Status: frozen before production implementation

## Commit boundaries

- `baseline_commit`: `44da53ae718334c6cefe87276638a2035019f1fc`
- `implementation_commit`: pending; will be the commit containing production runtime and direct tests, before evidence/state closeout
- `evidence_commit`: pending; will contain the slice review, machine-readable evidence and final effective-line audit

The baseline is the verified M1-S07C aggregate-review head. Documentation in this decision commit is excluded from effective production code. No production file for M1-S08-01 was changed before this decision was frozen.

## Planning sufficiency decision

The 9,000 effective production-line floor is supported by real M1 completion responsibilities that are currently absent as executable product capabilities. The work is not authorized as a new implementation of any 02A–07C runtime. It is a productized audit/evaluation layer that consumes those owners and makes their integration, disable behavior and competition-gate evidence executable through a stable API and CLI.

The current `zyra_evaluation` package contains a legacy M2 scenario helper and a small trace completeness scorer. It does not implement role-aware source completion, default-path dependency auditing, state custody validation, disable probes, LangGraph boundary checks, long-horizon canonical transition accounting, low-entropy comparisons, sealed-autonomy validation, real-tier/provider maturity, or an M1 cross-module scenario report. Those are distinct production responsibilities assigned by unit 08 and are sufficient to meet the slice floor without generated data, duplicated DTOs, mocks, source pools, or filler.

## Source/language/migration decision

This slice has `migration_mode=audit_and_hardening_only`. It does not obtain migration credit for new upstream control flow. Each row below audits a previously adjudicated mechanism in its existing owner language. The new hardening runtime is Zyra-owned Python because the existing product evaluation package and HTTP API are Python; it observes and challenges the owners, but never becomes their canonical state owner.

| Audited mechanism | Source role | Source repository/path | Source language | Existing Zyra target / language | M1-S08-01 target / language | Migration mode | Canonical owner retained |
| --- | --- | --- | --- | --- | --- | --- | --- |
| QueryEngine/session/tool loop/permission/MCP/skills/subagent/compact/API | primary | `claude-code-best/src/query*`, `src/services/**`, `src/tools/**`, `src/tasks/**`, `src/commands/**` | TypeScript/TSX | `packages/runtime/claude-runtime`, `apps/code-worker`, `packages/integrations/claude-mcp` / TypeScript | `packages/evaluation/zyra_evaluation/m1_hardening/**` / Python | audit and disable only | Claude-derived TypeScript runtimes |
| durable runtime event spine | primary | `opencode` session/event mechanisms selected by 05C | TypeScript | `packages/runtime/runtime-event-spine` / TypeScript | same hardening package / Python consumer | audit and conformance only | TypeScript RuntimeEventSpine |
| provider catalog/retry/fallback/wire adapters | primary/supplementary by 05D role table | `opencode` provider control plus retained Claude/OMP supplements | TypeScript | `packages/runtime/provider-control-plane` / TypeScript | provider maturity gate / Python consumer | audit and disable only | TypeScript ProviderControlPlane |
| retrieval/index algorithms | primary/supplementary | AgentScope retrieval/workspace plus selected Oh My Pi mechanisms | Python, TypeScript | `packages/memory`, `packages/code_index`, `packages/memory/retrieval-algorithms` / Python, TypeScript | memory/index/cross-cutting gates / Python consumer | audit and disable only | existing MemoryIndex/CodeIndex/retrieval owners |
| memory curator | primary/supplementary | Hermes curator and selected Oh My Pi memory mechanisms | Python, TypeScript | `packages/memory/zyra_memory`, `packages/memory/curator-state-machine` / Python, TypeScript | curator custody/disable gates / Python consumer | audit and disable only | existing curator decision/commit owners |
| skill/procedure memory and compact restore | primary/supplementary | Claude skill-memory/compact plus Hermes/OMP supplements | TypeScript, Python | `packages/memory/skill-memory-runtime`, `packages/runtime/claude-runtime` / TypeScript | skill/restore scenario and disable gates / Python consumer | audit and disable only | existing skill-memory and compact owners |
| physical worker/resource pool/dynamic topology | primary/supplementary | AgentScope worker lifecycle plus OMP task supplement; Zyra topology | Python, TypeScript | `packages/scheduler/zyra_scheduler/worker_pool`, `packages/orchestration/zyra_orchestration/graph_custody` / Python | tier/lease/topology gates / Python consumer | audit and adversarial conformance only | WorkerPoolStore and GraphStateCustody |
| watchdog/fault injection | primary/supplementary | browser-use watchdog plus OMP supplement | Python, TypeScript | `packages/scheduler/zyra_scheduler/fault_runtime`, browser worker integrations / Python | watchdog/fault disable and scenario gates / Python consumer | audit and disable only | existing fault/watchdog owners |
| recovery planner/routing/memory | Zyra primary with narrow LangGraph semantics and OMP supplement | Zyra; LangGraph checkpoint semantics; selected OMP routing | Python, TypeScript | `packages/scheduler/zyra_scheduler/recovery_runtime` / Python | recovery/lineage/boundary gates / Python consumer | audit and adversarial conformance only | RecoveryApplication/RecoveryPlanStore/GraphStateCustody |
| checkpoint identity/lineage/pending writes/exact resume | narrow primary semantic source | `langgraph/libs/checkpoint/**`, selected `_loop/_algo/_checkpoint` semantics | Python | Zyra graph/recovery stores / Python | `LangGraphBoundaryGate` / Python | conformance only; no LangGraph runtime dependency | Zyra stores and recovery runtime |
| StateGraph/Pregel/channels/reducers/ToolNode/stream/SDK/server | reference/rejected/deferred | LangGraph framework/runtime surface | Python/TypeScript | none on default path | negative dependency and behavior gate / Python | negative conformance only | no production owner granted |
| TaskTool/PAL/provider/memory/Hashline candidates | supplementary/conformance/reference as individually adjudicated | `oh-my-pi` selected mechanisms | TypeScript/Rust | existing Zyra task/provider/memory/patch owners / TypeScript, Rust, Python | OMP clean-room and disable probes / Python consumer | audit and disable only | existing Zyra owners |

## New Zyra-owned module responsibilities

The hardening runtime will be split by behavior, not by source repository:

- `coverage`: role/maturity separation, completion scoring, production-entry/state-owner/scenario/disable/limitation evidence, and M2-owned deferrals.
- `dependency`: default-path import/process/path/package/link/cache/build-context inspection with explicit clean-room findings.
- `custody`: state-family ownership, persistence, restore, event correlation, ambiguity and duplicate-owner checks.
- `disable`: registered probes, dependency ordering, fail-closed execution, fallback detection, semantic-difference checks and evidence persistence.
- `langgraph`: forbidden-default-path scanner plus runtime topology/alias/determinism/conflict/pending-write/side-effect/CodeWorker-cohesion probes against Zyra owners.
- `progress`: before/after revision, causation and semantic-mutation accounting; explicit exclusion of heartbeat/log/replay/no-op/projector duplicates.
- `entropy`: targeted envelope budgets, quantiles and baselines for static route, full broadcast and full-text inline.
- `autonomy`: sealed low-risk allow/high-risk deny/recovery validation and zero-human/manual-resume/state-edit/unresolved-approval enforcement.
- `tiers` and `providers`: real local/edge/cloud handshake/heartbeat/route/lease/artifact validation and two distinct real wire-dialect maturity validation without loopback promotion.
- `cross_cutting`: Patch/Git, deny friction, secret/prompt-injection and code-index semantic-effect gates.
- `scenario`: cross-module scenario registry, evidence ingestion, ordered stage evaluation, disable evidence and event/state mutation correlation.
- `service`, `reporting`, `store`, `cli`: stable product API/CLI, atomic report persistence and machine-readable/Markdown rendering.

These modules can read and invoke public owner ports. They may not write canonical session, permission, memory, event, graph, worker, provider, recovery or artifact state directly. Scenario mutations occur only through an injected real API/control port.

## Main-path and API decision

Production reachability will be:

```text
GET/POST /hardening/m1/**
  -> M1HardeningApi
  -> M1HardeningService
  -> project/default-path inspection + real task/event/state evidence
  -> role/dependency/custody/disable/boundary/competition gates
  -> atomic hardening report artifact
```

The product CLI invokes the same service or an already-running Zyra API. The first foundation scenario will use a real task created through `/tasks`, a real CodeWorker permission suspension/recovery signal, a real control-command session mutation, real task/event projections, and a registered disable probe. No fixture replay or fixed health response is accepted as scenario success.

## State custody and event boundary

Hardening report state is derivative evidence owned only by `M1HardeningReportStore`. It is not canonical runtime state. Canonical owners remain:

- session/query/tool/permission/MCP/skill/subagent/compact: TypeScript Claude-derived stores/runtimes;
- runtime events: TypeScript RuntimeEventSpine;
- logical task projection: existing task store;
- artifacts: existing artifact store/task artifact references;
- graph/topology: GraphStateCustody;
- worker/lease: WorkerPoolStore;
- provider/backend: ProviderControlPlane and BackendRegistry owners;
- memory/index/curator/skill memory: their existing stores;
- fault/recovery: existing fault and recovery stores.

Hardening evidence records source references, revisions and causation IDs. It never repairs missing canonical state silently.

## Validation plan

Direct tests will cover:

1. role/maturity completion scoring and inactive-source handling;
2. forbidden path/process/package/link/cache dependency detection;
3. custody ambiguity, missing restore and causal-event failures;
4. disable probe expected failure, unexpected success, fallback masking, timeout and dependency ordering;
5. LangGraph forbidden imports and all required dynamic graph/immutability/determinism/conflict/pending-write/fence/cohesion probes;
6. progress inclusion/exclusion and 1,000/2,000 threshold semantics;
7. low-entropy budgets and three mandated baselines;
8. sealed autonomy and zero-human invariants;
9. real-tier and provider maturity without simulator/loopback promotion;
10. Patch/Git, deny, secret/injection and code-index checks;
11. one real API cross-module scenario, report artifact persistence, dynamic reachability and disable-causes-failure behavior.

The slice will run targeted tests and adjacent API/runtime regressions. Full repository tests, full cleanroom, live local/edge/cloud dispatch, live two-provider execution and the 2,000-transition sealed benchmark remain mandatory at M1-08 integration / milestone exit unless a concrete high-risk change in this slice triggers earlier escalation.

## Explicit non-goals

- no new QueryEngine, tool loop, permission evaluator, MCP client, compact runtime, event bus, provider plane, memory store, scheduler, worker runtime or recovery planner;
- no LangGraph package/runtime dependency;
- no simulated edge/cloud or loopback provider promoted to `active_real`;
- no source graph, ledger, manifest, generated data or documentation counted as production implementation;
- no change to any canonical owner, transaction, lease, idempotency, checkpoint or restore semantics.
