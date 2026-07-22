# M1-S07B-02 Watchdog / Fault Injection Integration Preimplementation Decision

- slice: `M1-S07B-02`
- parent unit: `M1-07B`
- decision status: `frozen_before_production_change`
- baseline commit: `805d31985df5f549745ac2fd2b21d4bfc6236dd6`
- parent effective-code baseline: `6900e96b2413fa12f66b082dd3c47a3c0174c480`
- decision date: `2026-07-22`
- implementation commit: pending
- evidence commit: pending

## 1. Scope and non-negotiable ownership

This slice integrates the already-landed `M1-S07B-01` watchdog/fault foundation into the default runtime path. It does not create a second classifier, event store, memory store, scheduler-health store, task state owner, or recovery planner.

The canonical ownership remains:

| State / decision domain | Canonical owner | This slice's authority |
|---|---|---|
| structured observation classification | `WatchdogSignalClassifier` | feed typed observations; no free-text identity inference |
| durable observation, signal, injection, projection and handoff state | `FaultStateStore` | extend integration state only through that store |
| canonical event projection | `FaultSignalEventWriter` and the M1-05C event store | invoke and verify causality |
| failure memory | `MemoryFabric.refresh_task_memory` through `FaultSignalEventWriter` | prove real mutation and recovery input |
| worker/backend health | scheduler health writer / `BackendRegistry` | prove route-health mutation; do not replace owner |
| requirement change | `ConstraintKeeper` / `TopologyRouter` and `/change` control path | keep strictly outside fault counters, pressure, health and failure memory |
| recovery planning | future `M1-07C RecoveryPlanner` | deliver, lease, acknowledge or release typed handoffs only; do not select plans |
| browser process/CDP evidence | M1-04D browser process/session runtime | attach an observer to the evidence; do not own browser process lifecycle |

`RuntimeWatchdog`, `WatchdogSignalClassifier`, `FaultInjectionRuntime`, `SameRunFaultInjector`, `FaultSignalEventWriter`, and `WatchdogRecoveryBridge` remain mandatory main-path components.

## 2. Fixed source selection and role limits

| Source | Fixed revision | Language retained | Role | Selected mechanisms | Explicit exclusions |
|---|---|---|---|---|---|
| `browser-use` | `18484f23ac96bb955259a1c54530a7d265dfffdb` | Python | primary for browser observer lifecycle | `BaseWatchdog.attach_to_session`, lifecycle-event exemption during CDP loss, explicit stop/cancel cleanup, target crash listener, process-death and CDP responsiveness observation | no Browser Use state store, event bus, agent loop, browser launcher or recovery owner |
| `oh-my-pi` | `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca` | TypeScript | supplementary for cross-runtime fault signals | turn/tool abort fences, partial-stream terminalization without duplicate tool effect, MCP request timeout/abort cleanup, transport close notification, reconnect single-flight and crash-storm breaker, durable-job process restart signal | no OMP session store, CLI/TUI, provider catalog, complete MCP manager or second canonical classifier |
| Claude Code best, Agent Framework, LangGraph, Hermes Agent, opencode | revisions already frozen in parent/source graph | no production migration for this slice | conformance/reference only | taxonomy and event-shape checks | no production control flow and no code quota |
| OpenClaw | excluded forward-only | none | excluded | none | no reading, migration, dependency, source graph or evidence obligation |

Browser Use is the sole primary implementation source for the browser observer subdomain. OMP is supplementary and is limited to cross-runtime execution/transport signals. The general cross-domain state machine and classifier remain Zyra-owned.

## 3. Source-to-target migration map

| Source mechanism | Zyra target | Mode | Runtime responsibility |
|---|---|---|---|
| Browser Use attach/detach and lifecycle-event behavior | `packages/scheduler/zyra_scheduler/fault_runtime/source_session.py` | cropped same-language migration | bind active sources to run/session/worker/browser identities; attach, heartbeat, restart, detach and fence stale generations |
| Browser Use target crash, CDP disconnect and process health observation | `packages/scheduler/zyra_scheduler/fault_runtime/browser_integration.py` plus existing `browser_source.py` | same-language adaptation | consume real BrowserActionRuntime evidence and submit structured browser observations |
| OMP per-turn/per-tool abort and completed-effect preservation | `packages/runtime/claude-runtime/src/watchdog/execution-supervisor.ts` | cropped same-language migration | abort overdue work, preserve completed side effects, mark skipped siblings and emit one typed observation |
| OMP stream interruption handling | `packages/runtime/claude-runtime/src/watchdog/provider-stream-supervisor.ts` | cropped same-language migration | fence partial stream revisions and prevent retry from repeating committed tool effects |
| OMP MCP timeout/close/reconnect breaker | `packages/runtime/claude-runtime/src/watchdog/mcp-supervisor.ts` | cropped same-language migration | request deadline, abort cleanup, single-flight reconnect, crash-window breaker and structured signal emission |
| OMP durable worker restart/requeue signals | `packages/runtime/claude-runtime/src/watchdog/worker-restart-supervisor.ts` | cropped same-language migration | generation fencing, restart budget and durable-job handback signal |
| cross-runtime signal admission | existing `runtime_event_adapter.py`, extended by `integration.py` | protocol adaptation | TypeScript emits process-local structured observations; Python owns durable classification and projection |
| same-run effects and causal verification | `packages/scheduler/zyra_scheduler/fault_runtime/effect_runtime.py` | Zyra-owned integration | verify event, memory, health and task projections before a handoff can be considered ready |
| 07C-compatible delivery | `packages/scheduler/zyra_scheduler/fault_runtime/handoff_runtime.py` | Zyra-owned integration | lease/dispatch/ack/release handoffs to a consumer port without choosing a recovery plan |
| requirement-change separation | `packages/scheduler/zyra_scheduler/fault_runtime/requirement_control.py` | Zyra-owned integration | route requirement facts to the established replan path and prove fault state is unchanged |
| default path and commands | `packages/scheduler/zyra_scheduler/fault_runtime/integration.py`, `packages/commands/zyra_commands/watchdog.py`, `apps/api/zyra_api/main.py` | product integration | session bind, observation ingestion, fault injection, handoff pump and task snapshot through real API/command paths |

The final implementation may consolidate a planned file when ownership stays identical, but it may not change source roles or move a canonical owner without a new explicit decision.

## 4. Public behavior frozen for implementation

1. A task-scoped fault integration session binds `run_id`, `task_id`, `session_id`, `worker_id`, and the relevant `tool_call_id`, `backend_id`, `browser_session_id`, `provider_id`, `workspace_id`, or `mcp_server_id` before accepting an observation.
2. Active sources support attach/start, heartbeat, generation-fenced restart, stop/detach and disable. Disabling a real observer removes its capture path; fault injection remains a separate `injection-only` source and cannot masquerade as real capture.
3. At least tool deadline, worker heartbeat loss, browser process/CDP disconnect, provider transport failure and MCP disconnect are reachable through typed source methods. No identity is extracted from summary/error text.
4. A source observation enters `WatchdogObserverRegistry`, is classified by `WatchdogSignalClassifier`, is durably written by `FaultSignalEventWriter`, updates MemoryFabric and scheduler health where applicable, and creates a `WatchdogRecoveryBridge` handoff when disposition requires it.
5. A 07C consumer port receives a claimed `RecoveryHandoff` in the same run. The bridge records claim/ack/release causality, but this slice neither chooses nor executes a recovery plan.
6. Fault injection is reachable from the existing `/inject` command and `/tasks/{task_id}/faults/inject` API. The newly integrated session route exposes bind, observe, tick and handoff-delivery operations without introducing a second state store.
7. OMP-derived execution supervision demonstrates at least: tool deadline abort; MCP disconnect circuit breaker; provider partial-stream retry without duplicate committed effect; and durable worker generation restart/requeue signaling.
8. `RequirementChanged` is sent to the established `ConstraintKeeper`/`TopologyRouter` replan path. It cannot increment fault pressure/count/backoff, mutate scheduler health, or write failure memory.
9. Every durable or external effect is fenced before execution. Retries and restarts return the original receipt or continue from durable cursor state rather than duplicate effects.

## 5. Failure and restart semantics

- stale source generations are rejected before observation submission;
- observer source restart uses the existing lifecycle supervisor and durable observer epoch;
- a TypeScript process restart may replay source events, but `source_signal_id`, observation identity and `FaultStateStore` fences prevent duplicate canonical signals;
- an incomplete projection is retried through `FaultSignalEventWriter.retry_incomplete` before the handoff is dispatched;
- a handoff lease expires or is explicitly released before another consumer may claim it;
- a disabled observer cannot silently fall back to polling, injection, fixed events or free-text scanning;
- requirement changes remain control facts even when their human-readable text contains words such as crash, timeout or failure.

## 6. Language and effective-code gate

The source language contract is frozen:

- Browser Use-derived observer control remains Python;
- OMP-derived execution, provider, MCP and process supervision remains TypeScript;
- both feed the Python-owned cross-runtime classifier and durable state store;
- same-language source-derived effective production code must be non-zero in both Python and TypeScript.

The slice implementation must add at least `6,500` lines of effective production code after excluding generated/data/docs/tests/vendor-like/source-pool, DTO/interface-only, adapter-only, mock/fixture and unreachable sample code. Per-file buckets and large-file manual reviews are required. Parent closeout recomputes cumulative effective production from `6900e96b2413fa12f66b082dd3c47a3c0174c480` through the final implementation commit and must reach at least `15,000`, without adding the previous slice's reported number as a separate arithmetic shortcut.

## 7. Verification and commit boundary

Required focused verification:

- Python behavior tests covering active lifecycle, five observation categories, disable/restart, same-run event/memory/scheduler mutations, 07C handoff delivery, injection API/command and requirement-change isolation;
- TypeScript behavior tests covering tool deadline abort, MCP breaker, partial-stream idempotency and worker generation restart;
- existing `M1-S07B-01` foundation tests and adjacent API/control/scheduler/memory regressions;
- clean dependency/path checks proving no runtime dependency on sibling source repositories, caches or source pools;
- source-ledger sync/check, per-file effective-code bucket report and parent cumulative audit.

Commit order is frozen as:

1. baseline `805d31985df5f549745ac2fd2b21d4bfc6236dd6`;
2. this preimplementation decision commit;
3. implementation commit containing production code and directly related tests only;
4. evidence commit containing review, evidence and ledger synchronization;
5. root `docs/milestones/execution-state.yaml` update outside the Zyra Git repository.

No production file was changed before this decision document was created.
