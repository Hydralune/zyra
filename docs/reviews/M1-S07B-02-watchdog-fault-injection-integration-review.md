# M1-S07B-02 Watchdog 与故障注入集成批判式自审

> **2026-07-22 父级审查更正：** 本文保留原 slice 完成时的历史记录；其实现 target、默认主路径结论和有效代码数字已由父级审查修复并取代。最终实现 target 为 `383acfbe276420dbceec3f64f09829d38b99816c`，07B-02 有效 production 为 `7,059`，父级累计为 `15,710`。权威结论见 [`M1-07B-watchdog-fault-injection-parent-review-2026-07-22.md`](M1-07B-watchdog-fault-injection-parent-review-2026-07-22.md) 及 amended evidence JSON。

- slice: `M1-S07B-02`
- review date: `2026-07-22`
- decision commit: `b0609f788011684626c287975d3d4a58c01ab5b7`
- final implementation commit: `c6887b0a7a336500d13ad5ae2b02473140d45b5c`
- evidence commit: created by committing this review and its evidence record
- result: `pass`

## 1. 结论

本 slice 已把 07B-01 的 fault foundation 接入真实 task-scoped source session、Browser
Action 证据、Claude TypeScript execution/transport runtime、HTTP API、`/inject` 与
`/watchdog` 命令以及 07C-compatible handoff consumer port。默认路径没有增加第二个
classifier、event store、memory store、scheduler-health store、task owner 或 recovery
planner。

最终实现目标的保守有效生产代码为 `6,669` 行，高于 slice 门槛 `6,500`。从真实父级
基线 `6900e96dcb6dd73c22b803787afc30125fbe947c` 到最终实现提交重新计算的父级有效生产
代码为 `15,324` 行，高于 `M1-07B` 门槛 `15,000`；该结果由 blame/AST/token 审计重算，
没有把 07B-01 的报告数字做算术相加。

## 2. 冻结决策与勘误

生产改动前的冻结提交是 `b0609f7`。其中父级基线被误写为不存在的
`6900e96b...`；07B-01 evidence 和 Git 对象均证明真实基线是 `6900e96d...`。决策文档
在 evidence commit 中显式勘误，原提交历史保留。该勘误不改变 source role、owner、
阈值、范围或测试要求。

实现最初冻结为 `f999d46`。证据提交前的相邻 scheduler API 回归在 Windows 临时目录
清理阶段暴露 `api.fault-runtime.sqlite3` 句柄未释放；功能断言本身已通过，但资源泄漏
仍判为失败。`_recovery_task_view` 增加 `finally` 释放可重开的 fault-store connection
后，implementation amend 为唯一最终目标 `c6887b0`。最终全部测试和行数审计只针对
`c6887b0`。

## 3. 来源内化与模块边界

| 来源角色 | 上游机制 | Zyra-owned 落位 | 改造结果 |
|---|---|---|---|
| Browser Use primary | explicit attach/start/stop、lifecycle loss exemption、target crash/process/CDP evidence | `source_session.py`、`browser_integration.py` | 绑定 run/task/session/worker/browser 身份，generation fence，显式订阅与全部回调解除；04D 保留 browser evidence custody |
| OMP supplementary | tool abort/settlement、partial stream cursor、MCP timeout/close/reconnect、worker generation restart | `execution-supervisor.ts`、`provider-stream-supervisor.ts`、`mcp-supervisor.ts`、`worker-restart-supervisor.ts`、`integration-supervisor.ts` | 保留 TypeScript 原语言执行边界，向 Python canonical ingress 发 typed observation；不拥有 durable fault state |
| Zyra-owned integration | typed admission、effect verification、containment、handoff、requirement separation | `observation_port.py`、`effect_runtime.py`、`containment_runtime.py`、`handoff_runtime.py`、`requirement_control.py`、`integration.py` | 复用 07B-01 classifier/store/event writer、MemoryFabric、BackendRegistry health、TaskState 和 handoff outbox；不选择 recovery plan |
| Product main path | HTTP/command/session lifetime | `fault_runtime/api.py`、`apps/api/zyra_api/main.py`、`watchdog_control.py` | persistent runtime across requests, idle SQLite release, bind/observe/runtime-event/dispatch routes, shared `/inject` and `/watchdog` handler |

没有迁入 Browser Use event bus、browser launcher、state store 或 agent loop；没有迁入 OMP
session store、CLI/TUI、provider catalog 或完整 MCP manager。Claude Code best、Agent Framework、
LangGraph、Hermes 和 opencode 只保留 conformance/reference；OpenClaw 保持
`excluded_forward_only`。

## 4. Canonical state custody 与恢复边界

| 状态域 | owner | 本 slice 的行为 |
|---|---|---|
| source lifecycle/process-local callback | `RuntimeSourceSessionManager` / TypeScript supervisors | attach、heartbeat、generation restart、detach、disable、cursor resume |
| observation/signal/injection/projection/handoff journal | `FaultStateStore` | 唯一 durable fault owner；event/source IDs 与 generation/revision 防重复 |
| classification | `WatchdogSignalClassifier` | 只接受 typed refs；不从 summary/error 文本提取身份 |
| canonical event | M1-05C store via `FaultSignalEventWriter` | signal effect 前写入并校验 causality |
| failure memory | `MemoryFabric` | 真实 signal 才写 failure memory；requirement change 与 injection health 被隔离 |
| scheduler health | existing backend registry owner | 真实 worker/provider/MCP/browser health signal 更新；不建影子 health store |
| task/checkpoint | existing `SQLiteStore` / `TaskState` | containment 在同一 run 取消、fence 或标记 task effect |
| recovery plan | `M1-07C` | 本 slice 只 claim/dispatch/ack/release typed handoff |
| requirement change | `ConstraintKeeper` / `TopologyRouter` | 走既有 replan；fault count/pressure/health/failure memory 均保持不变 |

## 5. 动态可达性、语义效果与断开即失败

- `POST /tasks/{task_id}/faults/sources/bind` 创建持久 source session；stale generation 在
  observation admission 前被拒绝。
- `POST .../sources/observe`、`.../observations` 和 `.../runtime-events` 进入同一个
  `WatchdogFaultIntegrationRuntime`，产生 classifier signal、canonical event、memory/health
  projection、same-run containment 和 durable handoff。
- `POST .../handoffs/dispatch` 把已 claim 的 handoff 交给 consumer port 并记录 ack/release；
  删除 `SameRunHandoffDispatcher` 会使 end-to-end causal-chain test 失败。
- `/inject` 与 `/watchdog` 共享同一 persistent runtime；HTTP 多请求测试证明 bind 与 observe
  不依赖单请求内存。断开 command runtime 会使 command mutation test 失败。
- browser observer disable 会解除 9 类 callback；随后真实 browser event 不再被捕获，且
  不启动 injection/polling/free-text fallback。
- actual spawned child process termination 与 MCP disconnect 测试证明不是 fixture-only event
  replay。
- tool deadline 测试要求 abort callback 被真实调用、late result 被拒绝、已提交 effect 不
  重放；provider/MCP/worker TypeScript 测试分别验证 partial resume、breaker/single-flight 和
  durable job exactly-once requeue。
- `RequirementChanged` 中即使含 crash/timeout/failure 文本，也只生成 replan，不产生 fault
  signal、pressure、scheduler-health 或 failure-memory mutation。

## 6. 有效行数逐文件分桶

列顺序为：raw、production、type、schema/data、adapter、test、docs/blank、effective。
审计工具本身位于 evidence commit，标记为 nonproduction，不进入 implementation range。

| 文件 | raw | prod | type | schema | adapter | test | docs | effective |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `apps/api/zyra_api/main.py` | 158 | 152 | 0 | 0 | 0 | 0 | 6 | 152 |
| `docs/plans/M1-S07B-02-watchdog-fault-injection-integration-decision.md` | 111 | 0 | 0 | 111 | 0 | 0 | 0 | 0 |
| `packages/commands/zyra_commands/runtime/__init__.py` | 4 | 0 | 4 | 0 | 0 | 0 | 0 | 0 |
| `packages/commands/zyra_commands/runtime/registry.py` | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 1 |
| `packages/commands/zyra_commands/runtime/watchdog_control.py` | 327 | 271 | 1 | 24 | 0 | 0 | 31 | 271 |
| `packages/runtime/claude-runtime/src/stdio.ts` | 17 | 16 | 0 | 0 | 0 | 0 | 1 | 16 |
| `packages/runtime/claude-runtime/src/watchdog/execution-supervisor.ts` | 579 | 443 | 89 | 0 | 0 | 0 | 47 | 443 |
| `packages/runtime/claude-runtime/src/watchdog/index.ts` | 5 | 0 | 5 | 0 | 0 | 0 | 0 | 0 |
| `packages/runtime/claude-runtime/src/watchdog/integration-supervisor.ts` | 351 | 292 | 29 | 0 | 0 | 0 | 30 | 292 |
| `packages/runtime/claude-runtime/src/watchdog/mcp-supervisor.ts` | 595 | 476 | 75 | 0 | 0 | 0 | 44 | 476 |
| `packages/runtime/claude-runtime/src/watchdog/provider-stream-supervisor.ts` | 517 | 382 | 95 | 0 | 0 | 0 | 40 | 382 |
| `packages/runtime/claude-runtime/src/watchdog/runtime.ts` | 22 | 21 | 0 | 0 | 0 | 0 | 1 | 21 |
| `packages/runtime/claude-runtime/src/watchdog/worker-restart-supervisor.ts` | 580 | 453 | 83 | 0 | 0 | 0 | 44 | 453 |
| `packages/runtime/claude-runtime/test/watchdog-integration.test.ts` | 282 | 0 | 0 | 0 | 0 | 282 | 0 | 0 |
| `packages/scheduler/zyra_scheduler/fault_runtime/__init__.py` | 81 | 0 | 81 | 0 | 0 | 0 | 0 | 0 |
| `packages/scheduler/zyra_scheduler/fault_runtime/api.py` | 47 | 46 | 0 | 0 | 0 | 0 | 1 | 46 |
| `packages/scheduler/zyra_scheduler/fault_runtime/browser_integration.py` | 554 | 412 | 4 | 89 | 0 | 0 | 49 | 412 |
| `packages/scheduler/zyra_scheduler/fault_runtime/containment_runtime.py` | 653 | 497 | 1 | 107 | 0 | 0 | 48 | 497 |
| `packages/scheduler/zyra_scheduler/fault_runtime/effect_runtime.py` | 460 | 348 | 1 | 70 | 0 | 0 | 41 | 348 |
| `packages/scheduler/zyra_scheduler/fault_runtime/handoff_runtime.py` | 555 | 448 | 10 | 38 | 0 | 0 | 59 | 448 |
| `packages/scheduler/zyra_scheduler/fault_runtime/integration.py` | 785 | 699 | 1 | 32 | 0 | 0 | 53 | 699 |
| `packages/scheduler/zyra_scheduler/fault_runtime/observation_port.py` | 673 | 534 | 4 | 57 | 0 | 0 | 78 | 534 |
| `packages/scheduler/zyra_scheduler/fault_runtime/requirement_control.py` | 275 | 209 | 1 | 34 | 0 | 0 | 31 | 209 |
| `packages/scheduler/zyra_scheduler/fault_runtime/runtime.py` | 46 | 44 | 0 | 0 | 0 | 0 | 2 | 44 |
| `packages/scheduler/zyra_scheduler/fault_runtime/source_session.py` | 1,048 | 897 | 6 | 80 | 0 | 0 | 65 | 897 |
| `packages/scheduler/zyra_scheduler/fault_runtime/state_store.py` | 33 | 28 | 0 | 0 | 0 | 0 | 5 | 28 |
| `tests/integration/test_watchdog_fault_injection_integration.py` | 795 | 0 | 0 | 0 | 0 | 795 | 0 | 0 |
| **总计** | **9,554** | **6,669** | **490** | **642** | **0** | **1,077** | **676** | **6,669** |

按 source role/language：Browser Use primary Python `412`；OMP supplementary TypeScript
`2,083`；Zyra API Python `152`；Zyra command Python `272`；Zyra fault integration Python
`3,750`。两种同语言来源实现均非零。

## 7. 大文件人工复审

| 文件 | raw/effective | 可执行责任 | 断开或失败证据 |
|---|---:|---|---|
| `source_session.py` | 1048/897 | source bind/attach/heartbeat/restart/detach、generation fence、durable cursor | stale generation、disable、restart 和 HTTP persistence tests |
| `integration.py` | 785/699 | composition、typed source admission、tick、snapshot、handoff dispatch | causal-chain 与 HTTP multi-request tests |
| `observation_port.py` | 673/534 | tool/worker/browser/provider/MCP/permission/schema/workspace/subagent typed ingress | five-category、requirement rejection、duplicate ID tests |
| `containment_runtime.py` | 653/497 | observer-only abort/fence/terminate/disconnect effect；injection external no-op | actual subprocess terminate、MCP disconnect、injection isolation tests |
| `handoff_runtime.py` | 555/448 | same-run claim/dispatch/ack/release causality，不选 plan | acknowledged 07C consumer test |
| `browser_integration.py` | 554/412 | explicit 9-event subscribe/unsubscribe and generation fence | disable removes callbacks and blocks real capture |
| `execution-supervisor.ts` | 579/443 | tool deadline abort、sibling skip、late-result and effect fence | Node deadline/idempotency test |
| `mcp-supervisor.ts` | 595/476 | timeout abort、disconnect、single-flight reconnect、breaker | Node MCP timeout/breaker test |
| `provider-stream-supervisor.ts` | 517/382 | partial cursor resume and committed-effect fence | Node partial-stream no-replay test |
| `worker-restart-supervisor.ts` | 580/453 | heartbeat/process generation、restart budget、checkpointed requeue | Node exactly-once requeue test |
| `test_watchdog_fault_injection_integration.py` | 795/0 | 8 direct behavior tests | test-only，零生产代码计数 |

这些文件均在默认 API/command/runtime 路径可达；没有 inventory-only、manifest-only、固定
health response 或未调用 sample。大量类型、schema、测试和注释已排除，未用文件 raw 行数
冒充有效代码。

## 8. 验证结果

- direct + foundation + requirement isolation：`30 passed in 26.56s`。
- adjacent scheduler/fault/runtime-event/worker-pool/browser regression：
  `31 passed in 210.66s`。
- TypeScript watchdog suites：Node `7 passed`；覆盖 4 条新 integration behavior 与 3 条
  07B-01 watchdog behavior。
- TypeScript `tsc --noEmit`：pass。
- Python `compileall`：pass。
- source ledger `--write`/`--check`：aligned，恰好 2 条 `M1-S07B-02` decision。
- ledger test-quality：2 entries、6 test entries、0 error、0 warning。
- ledger reachability：2 entries、0 unreachable、0 current-unit finding；global trust gate 仍受
  保护的历史 ledger audit errors 阻断。
- `git diff --check`：pass。
- forbidden sibling-repo/cache/vendor/source-pool dependency scan：0 match。
- dependency/lockfile/vendor-like changed paths：0。
- placeholder/TODO/NotImplemented scan：0 match。

额外 `ruff` 检查未执行，因为项目 venv 未安装 `ruff`；这不替代已经通过的 compileall、
TypeScript typecheck 与行为测试。未为安装可选 lint 工具新增依赖。

## 9. 已知非阻断项与升级层级

1. 全局 ledger verifier 仍报告受保护早期单元的 `TARGET_PATH_NOT_FOUND`、历史 remediation
   source path 等错误；本 slice 的 2 条 entry 为 0 unreachable、0 unit finding，未追溯改写
   已完成单元。
2. full cleanroom 未在本 ordinary parent-closeout slice 重复执行。当前 diff 没有新增外部依赖、
   子进程服务、端口、Docker、动态 import、owner transfer 或不兼容公共 schema；完整 cleanroom
   仍由 M1-07 数字阶段聚合审查承担。
3. 没有声称关闭两个跨领域 live tasks、2,000 canonical transitions、sealed zero-human、动态图
   低熵、真实 local/edge/cloud dispatch、多模型或最终交付冻结门禁。

## 10. 父级收口与下一入口

`M1-07B` 的 foundation 与 integration 现已共同覆盖 observer lifecycle、typed classification、
durable state/injection/projection、real API/command reachability、same-run containment、restart
idempotency 和 07C handoff boundary。父级累计有效生产代码 `15,324` 达标。

review/evidence commit 与根目录 execution state 更新完成后，下一入口是
`M1-S07C-01`。07C 接管 recovery plan 的选择与执行；07B 不越界。
