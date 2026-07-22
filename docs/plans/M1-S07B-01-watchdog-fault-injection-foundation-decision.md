# M1-S07B-01 实现前语言、迁移与状态 owner 冻结记录

- slice: `M1-S07B-01`
- 记录日期: `2026-07-22`
- 实现前 Zyra baseline commit: `6900e96dcb6dd73c22b803787afc30125fbe947c`
- browser-use source revision: `18484f23ac96bb955259a1c54530a7d265dfffdb`
- oh-my-pi source revision: `c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`
- 当前 slice 最低有效生产代码: `8,500` 行
- 强制门禁: `docs/milestones/effective-code-language-migration-gate-2026-07-22.md`

本记录在第一处生产代码修改之前冻结。实现提交只包含生产代码和直接行为测试；证据、自审、账本与执行状态在实现提交之后另作 evidence commit。有效行数统计固定为本记录所列 baseline 到实现提交的 diff。

## 1. source / language / migration / owner 冻结表

| source role | source repo / modules | source language | target language | migration mode | Zyra target | canonical owner after migration |
|---|---|---|---|---|---|---|
| primary: browser observer lifecycle | `browser-use/browser/watchdog_base.py`; `browser_use/browser/watchdogs/local_browser_watchdog.py`; `browser_use/browser/session.py::attach_all_watchdogs`; upstream `CrashWatchdog` only as a rejected contrast | Python | Python | cropped same-language migration plus integration with the already productized 04D detector | `packages/scheduler/zyra_scheduler/fault_runtime/browser_observer.py`; `observer_registry.py`; 04D `BrowserCrashDetector` bridge | `fault_runtime.BrowserWatchdogObserver` owns 07B attachment/revision intake; 04D `BrowserCrashDetector` keeps browser process/CDP evidence ownership |
| primary: cross-runtime fault classification | source-role decision is Zyra-owned; Claude/opencode/LangGraph/Agent Framework/Hermes taxonomy is conformance only | Python | Python | Zyra-owned implementation, no upstream control-flow copy | `packages/scheduler/zyra_scheduler/fault_runtime/classifier.py`; `fault_runtime.py`; `state_store.py` | `FaultStateStore` owns classified signal, injection and observer lifecycle journals; `WatchdogSignalClassifier` owns deterministic classification policy |
| supplementary: bounded emission guard | `oh-my-pi/packages/coding-agent/src/advisor/emission-guard.ts` | TypeScript | TypeScript | cropped same-language migration, generalized from advice notes to structured runtime signals | `packages/runtime/claude-runtime/src/watchdog/emission-guard.ts` | `RuntimeSignalEmissionGuard` owns only dedupe/noise/rate-limit state inside the TypeScript observer runtime |
| supplementary: MCP/process crash and reconnect signals | `oh-my-pi/packages/coding-agent/src/mcp/transports/stdio.ts`; `packages/coding-agent/src/mcp/manager.ts`; `packages/coding-agent/src/mcp/timeout.ts` | TypeScript | TypeScript | cropped same-language migration; preserve deadline cleanup, close rejection, epoch fencing and reconnect-storm breaker semantics | `packages/runtime/claude-runtime/src/watchdog/deadline-observer.ts`; `process-observer.ts`; `reconnect-observer.ts`; `observer-runtime.ts` | TypeScript observer runtime owns process/MCP observation state only; it does not own MCP connection state or recovery planning |
| supplementary: provider failure signals | `oh-my-pi/packages/ai/src/error/retryable.ts`; `rate-limit.ts`; `provider.ts`; `classes.ts`; `utils/provider-response.ts` | TypeScript | TypeScript | cropped same-language migration using structured status/error-kind first; text is allowed only for non-identity taxonomy refinement | `packages/runtime/claude-runtime/src/watchdog/provider-observer.ts`; `classification.ts` | TypeScript observer runtime owns provider signal classification only; provider route/credential state remains with the existing provider runtime |
| supplementary: tool/permission/workspace/schema observation | existing Zyra-owned 03A/05A/05C/05D/06A runtime receipts and exceptions | Python | Python | same-language integration, no owner transfer | `packages/scheduler/zyra_scheduler/fault_runtime/tool_observer.py`; `permission_observer.py`; `workspace_observer.py`; `schema_observer.py` | Existing domain stores remain canonical; observers own only structured observation cursors and emitted fault evidence |

## 2. 明确排除与成熟度裁决

- browser-use upstream `CrashWatchdog` is `source_inactive`: `BrowserSession.attach_all_watchdogs()` leaves it commented out. It will not be described as an active source implementation and will not be attached by Zyra.
- Browser-use attached lifecycle and Zyra 04D `BrowserCrashDetector` are `active_real`. Disabling the 07B bridge must stop cross-runtime browser fault capture; injection may not substitute for it.
- oh-my-pi advisor mutation/advice is not a fault and cannot mutate the primary run; only its deterministic emission-guard mechanism is supplementary.
- oh-my-pi MCP/process/provider observation is `active_real` only where installed in the TypeScript query runtime or a real transport observer. Unwired mechanisms are reported as `experimental`, never silently counted as active.
- `RequirementChanged` stays in the 03D/05C control domain. It is excluded from fault counters, backoff, backend-health mutation, failure memory, injections and recovery handoff.
- OpenClaw is `excluded_forward_only`; no source reading, code, path, dependency, test or provenance is introduced by this slice.

## 3. 状态 custody 与事务边界

| state domain | canonical owner | 07B write rule |
|---|---|---|
| observer descriptors, attach/start/stop revisions and observation cursors | `FaultStateStore` | compare-and-swap revision; one lifecycle owner per observer; disable changes state before callbacks stop |
| classified watchdog signals and dedupe fingerprints | `FaultStateStore` | idempotent append keyed by signal id/fingerprint and structured observation identity |
| injected faults and same-run progress | `FaultStateStore` | injection id + idempotency key fence; transition journal; no free-text identity inference |
| task/checkpoint metadata | existing `SQLiteStore` / `TaskState` | only public projection and last-known receipt; not a second fault journal |
| canonical runtime event stream | 05C `RuntimeEventSqliteStore` through `RuntimeEventSpineBridge` | append real `WORKER_HEALTH` or `FAILURE_INJECTED` events; no shadow event DB |
| memory | existing `SQLiteStore`/`MemoryFabric` and memory index | ingest persisted fault event; no 07B memory store |
| scheduler/backend health | 05D `BackendRegistryStore` through `BackendRegistryHealthAdapter` | mutate only an explicitly bound route; never infer route/worker from summary text |
| browser process/CDP evidence | 04D `BrowserCrashDetector` | 07B consumes typed `WatchdogSignal`; it does not copy detector state |
| permission, workspace, tool, provider and MCP domain state | their existing canonical runtimes | 07B receives receipts/snapshots/errors and stores only observation references |
| recovery plan | future 07C owner | 07B writes a versioned `WatchdogRecoveryBridge` handoff/intention; it does not select a recovery plan |

## 4. 预定生产模块与真实主路径

The implementation is expected to create a `zyra_scheduler.fault_runtime` package containing structured identities/contracts, observer lifecycle, deterministic classifiers, deadline/process/browser/permission/workspace/schema observers, fault injection catalog and same-run state machine, durable state store, 05C event writer, MemoryFabric and 05D health sinks, 07C handoff bridge, and a composed `RuntimeWatchdog`/`FaultInjectionRuntime` application.

The API route `POST /tasks/{task_id}/faults/inject` and the existing `/inject` command will enter that composed application. At least one injected fault will be committed into the same run, persisted to 05C, ingested by memory, projected to scheduler health when an explicit route exists, and either continue the current run or emit the versioned 07C handoff. The TypeScript query runtime will attach its observer runtime to real tool/provider/process outcomes and emit compatible `tool_failure_signal` frames into the existing 05C ingress.

## 5. 验证与计数冻结

- implementation commit diff base: `6900e96dcb6dd73c22b803787afc30125fbe947c`
- intermediate implementation commit: `db27e57892cbb62eec4e783f73394c0a6a5d54df`（在 evidence 前的保守逐文件审查中未达到有效 production 门禁，未被冻结为最终 target）
- final implementation commit: `242038c842a02b3311e2452dad3cb86a2fa864e6`
- evidence commit: 本决策记录与 review/evidence/ledger 一并提交后生成；不参与 implementation 统计
- conservative effective production: `8,866 / 8,500`（Python `8,299`；TypeScript `567`）
- Python original-language production must be non-zero for browser-use primary migration.
- TypeScript original-language production must be non-zero for oh-my-pi supplementary migration.
- Tests must prove structured identity binding, no free-text critical-ref inference, observer disable semantics, injection idempotency, same-run progression, 05C/memory/05D writes, browser observer capture, TypeScript observer behavior, `/inject` command and HTTP API reachability, and `RequirementChanged` exclusion.
