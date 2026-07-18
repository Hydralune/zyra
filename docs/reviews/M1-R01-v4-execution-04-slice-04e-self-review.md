# M1-R01 E04-E 增量批判式自审

## 结论

`E04-E Agent / Control Source Recovery` 在实现提交
`60bfe4984a1b2d2ac1820446b7f532c50e6e2cee` 上通过增量验收。本结论只关闭
04E 的 `agent_subagent` 与 `isolation_control` 能力域，不完成 E04，不恢复
`M1-S05C-01`，也不替代 E04 专用独立终审。

## 上游源码恢复与 Zyra 接管

- `e04-source-015`：把 Claude `runAgent` 的 child-run lifecycle 接入冻结 target
  `runAgent`，并让 E03 `TaskExecutor` 删除重复的 child input 构造，统一调用该 owner。
  TypeScript 现在一次性接管 tool allow/deny、skill、MCP、permission ceiling、model、
  task/session/attempt/lease/lineage、context snapshot 和全部预算；child settlement 的
  task/lease 归属或资源使用不符时直接拒绝。
- `e04-source-016`：把 `resumeAgentBackground` 的所有权与恢复语义接入默认
  `TypeScriptAgentRuntime`。原 `queuedBackground` 内存 map 已删除；background supervisor
  只保存可重建的 claim projection，真实 prompt、context、revision 和 lease 只从
  `DurableTaskRegistry` 恢复，再进入同一个 `runAgent` owner。
- `e04-source-017`：把 `getOrCreateWorktree` 拆为 TypeScript 逻辑 owner 与 Python
  物理 effect。`IsolationRequestRuntime` 决定 create/reuse 策略并校验绝对隔离路径、
  `git-worktree` backend、created/reused disposition 和精确 HEAD；Python 不持有 lease、
  request journal 或恢复决策，只创建/安全复用 worktree 并回传事实。
- `e04-source-018`：把 `killAsyncAgent` 的 terminal-control 语义接入
  `TypeScriptControlRuntime` 与 `AgentControlHandler`。非终态 cancel/kill 需要 revision
  与 scope 权限，终态重放不重复物理效果，canonical task 状态仍由 E03 registry 持有。

逐 range 的 source fingerprint、candidate target fingerprint、裁剪分支和转换说明见
`docs/reviews/evidence/M1-R01-v4/execution-04/slice-04e/target-provenance-report.jsonl`。
G0 不可变清单未改写。

## 默认路径、状态 owner 与反回退

- agent 默认闭包为 `CodeWorkerApplication.runAgentControlPort ->`
  `E03AgentControlCoordinator -> AgentExecutionRuntime -> TaskExecutor -> runAgent ->`
  `AgentExecutionContext.runChild`。
- background 创建与恢复都从 `DurableTaskRegistry` 读取 canonical task；
  `AgentBackgroundSupervisor` 只作 claim/projection，不保留第二份 prompt/context 输入。
- isolation 默认闭包由 `E03AgentControlCoordinator` 生成 effect，再由 Python durable port
  执行 Git worktree；结果必须回到 `IsolationRequestRuntime` 通过 receipt 验证后才能提交。
- cancel/kill 的逻辑 decision、revision fence 和 terminal replay 都由 TypeScript 完成；
  Python 无 agent lifecycle 或 control fallback。
- `ZYRA_DISABLE_E04_AGENT_SOURCE_RUNTIME`、
  `ZYRA_DISABLE_E04_ISOLATION_SOURCE_RUNTIME` 以及 domain-07/08 mutation 都会切断真实默认
  路径并 fail closed。

## 实现中发现并关闭的真实缺口

- E03 `TaskExecutor` 曾复制 child input 构造而绕过冻结 `runAgent`；现在只保留一个
  run/resume owner，新增的 scope、context、budget 和 settlement 约束不会被旁路。
- `TypeScriptAgentRuntime` 曾用进程内 `queuedBackground` 保存 raw arguments/context，
  进程重启后会丢失并形成 shadow state。现在 background inline messages/turns 被拒绝，
  prompt/context references 进入 durable registry，重建无需内存 map。
- 内置 task definition 的预算时间戳固定在模块加载期，真实跨进程恢复会把新任务误判为
  已超时；现在 create 时用注入 clock 重置 consumption、started/deadline 并重算 digest，
  lease runtime 也使用同一 clock。
- worktree Python 回执此前无法区分 create/reuse，也没有 typed HEAD/backend 证明；现在
  TypeScript 会拒绝路径、backend、disposition 或 revision 不一致的回执。
- cancel/kill 过去只对同名终态幂等；现在所有终态都视为终局重放，避免重复 abort/kill
  物理效果。

## 对抗、失败与变异结果

- 04E 八个精确测试覆盖 scoped run、错误归属、failed resume、新 lease、跨 runtime
  background restore、create/reuse worktree、错误 HEAD、terminal idempotency 与 disable。
- `e04-mutation-domain-07` 和 `e04-mutation-domain-08` 变异后 TypeScript 仍可编译，
  但各自精确 killer `e04-agent-disable`、`e04-isolation-disable` 均失败；恢复后 8/8 通过。
- 恢复 SHA-256 分别为
  `a525892d57e2f94ac682ce8b44f9e0d6a7b6b859f9eda95963d645ad073ca493` 与
  `1c5f12690da9e1485f6658425fe17202260235f461e47cf12fba730c2c0b969d`，无 backup 残留。

## 动态可达性、断开即失败与依赖边界

- 新控制流由真实 `agent_spawn/list/cancel/kill/resume` 和 isolation prepare/effect/receipt
  路径触发，不依赖 import smoke、manifest 查询、固定 health 或预录轨迹。
- 若删除/绕过 `runAgent`，默认 child execution 的 disable killer 失败；若恢复 shadow
  queue，跨 runtime background restore 无法仅凭 registry 完成；若绕过
  `IsolationRequestRuntime`，错误 worktree HEAD 会被接受；若绕过 terminal decision，
  重复 cancel/kill 会再次产生物理效果。
- 当前 diff 未新增 npm/pip 依赖、动态 import、端口、Docker、外部服务或根目录来源仓库
  运行依赖。唯一子进程是既有 Python physical port 对项目自身 Git worktree 的受约束调用。

## 有效行数分桶

- production TypeScript/Python：新增 401 行、删除 109 行；承担唯一 run owner、durable
  background 输入、预算/lease clock、terminal decision、worktree request/receipt 与物理事实。
- test：新增 674 行、删除 6 行；覆盖八个精确行为、E03 receipt fixture 与真实临时 Git
  仓库的 create/reuse 回归。fixture 代码不计 production。
- validation tooling：新增 28 行；只承担可逆 domain-07/08 mutation operator，不计产品
  运行时。
- generated、data-as-code、vendor-like/source-pool、adapter-only、mock-only：0 行。

## 验证与未关闭事项

两个 TypeScript 工程 typecheck、04E 8 个精确测试、E03 346 个相邻行为测试、E02
14 个 default-path/custody 测试与 Python durable port 4 个测试均通过。G0 在最终实现
提交上复核为 18 个 source range、18 个 target、1619 个 Python owner symbol、13 个
mutation point，零异常。

八个语义域的跨域 E2E、全 13 mutation、双发行构建、cleanroom、依赖审计、有效行数
累计审计与 candidate gate 仍由 04F 强制执行。E04 terminal verdict 只能由专用独立审查
任务书在最终 target commit 上给出。
