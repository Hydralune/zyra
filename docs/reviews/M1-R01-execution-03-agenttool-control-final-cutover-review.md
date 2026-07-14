# M1-R01 Execution-03 AgentTool/Control Final Cutover Review

日期：2026-07-14

基线：`a873f8eaba7f033f045bb9ef8ec0a481dbd40116`

结论：PASS。Agent definition/scope/fork/child QueryEngine/background/fanout/fanin/resume/cancel/message 与 CodeWorker-local control 已由 TypeScript 取得唯一默认逻辑 owner；Python 只保留 durable CAS、workspace/isolation physical port、side effect、event/artifact/checkpoint projection。没有 `vendor/**`、`vendor-runtimes/**`、source-pool 或根来源仓库运行依赖；下一入口为根执行状态指定的 `M1-S05B-01`。

## 1. 最终 owner 裁决

| Capability | Canonical owner after execution-03 | Python retained responsibility | Forbidden fallback |
| --- | --- | --- | --- |
| Agent definition、tool/permission ceiling、budget/depth/cycle、context fork | TypeScript | durable record 与 workspace request 校验 | Python `ParentScopeBuilder`/`SubagentRuntime` 不得回到默认 CodeWorker/API |
| child QueryEngine、foreground/background、fanout/fanin、result commit decision | TypeScript | `SubagentTaskStore` revisioned persistence、physical isolation manifest | Python `CodeWorkerSubagentExecutionPort`/`LogicalFanoutRuntime` 不得执行 canonical child loop |
| status、cancel、resume、message、idempotent replay | TypeScript | load/list/CAS/message persistence、late-result fence | API 不得直接调用 Python durable port 充当逻辑 mutation runtime |
| CodeWorker-local context/model/compact/clear/cancel/resume command state | TypeScript | API/session durable projection与已有全局 task control | Python command facade 不得覆盖 TypeScript QueryEngine-local state |
| builtin tool side effect、workspace、artifact、event/checkpoint | Python physical ports | canonical physical responsibility | 不得借 physical owner 重新取得 Agent policy/loop owner |

## 2. Per-file disposition

| Path or group | Disposition | Evidence |
| --- | --- | --- |
| `packages/runtime/claude-runtime/src/agents/**` | canonical TypeScript production module | definition registry、monotonic scope、context fork、lifecycle、child run、durable hydrate、resume/cancel/message、fanout/fanin、late-result rejection |
| `packages/runtime/claude-runtime/src/control/**` | canonical TypeScript CodeWorker-local control module | revision/idempotency、model/compact/clear/cancel/resume、self-checksummed restore |
| `capabilities.ts`、`capability-host.ts`、`stdio.ts` | canonical Agent registry/execution wiring | legacy Python Agent tools filtered；child `ClaudeRuntimeCore` 在 TS 内运行；background 在退出前由 TS drain |
| `protocol.ts`、`contracts.ts` | narrow cross-language durable protocol | `agent.mutate/agent.mutate.result`；不把逻辑决策交给 Python |
| `packages/workers/zyra_workers/subagents/typescript_port.py` | Python durable/physical port, not logical runtime | run/parent/session authority、revision CAS、shared state-root lock、workspace containment、terminal/result persistence |
| `packages/workers/zyra_workers/subagents/task_store.py` | existing durable store with explicit refresh | 跨 port 读取原子替换后的最新 durable state |
| `packages/workers/zyra_workers/typescript_claude_runtime.py` | adapter-only narrow host | JSONL mutation transport；child session 事件隔离为 `agent_child_query_session`；不得计作 TypeScript 内化源码 |
| `apps/api/zyra_api/main.py` | TypeScript Agent API routing and durable projection | spawn/fanout/cancel/message/resume 重新进入 TS tool；真实 task workspace/MCP/edit/isolation ports；一次性 permission custody |
| `scripts/verify_typescript_runtime_custody.py` | audit-only, not production line credit | 拒绝 Python default constructor、API direct mutation bypass、缺失 TS Agent/control/hydrate/child-event/shared-CAS marker、来源路径依赖 |
| legacy `subagents/agent_tool.py`、`runtime.py`、`dispatch.py`、`fanout.py`、`parent_scope.py` | retained non-default compatibility/conformance code | 默认 CodeWorker/API 无构造调用；不得作为 fallback 或有效新增代码；删除条件见第 8 节 |
| `vendor/**`、`vendor-runtimes/**`、source-pool/runtime-sources | rejected | diff 与 cleanroom 均为零 |

## 3. 对抗审查发现与修复

1. API 最初仍构造 Python `SubagentRuntime`、`CodeWorkerSubagentExecutionPort`、fanout 与 parent scope。已删除默认构造链；spawn/fanout 统一进入 TypeScript `Agent` tool。
2. 首次 Agent API 挂起后没有完整 custody 重试路径，且 `step_id` 未固定 permission 使用的 tool identity。已回传一次性 session envelope、接收 bearer custody，并用稳定 `request_id` 同时绑定 `tool_call_id`；审批后 exact grant 被消费一次。
3. 新 Node 进程只认识内存 task，无法 status/cancel/resume/message 或跨进程幂等 replay。已增加 durable `load`、definition digest 校验、scope/context hydrate 和 terminal result 恢复；同 task/idempotency 不再二次 dispatch。
4. Python durable port 的 load/mutation 曾未在操作前校验 caller authority。已在任何非 create/list 操作前强制匹配 `run_id + parent_task_id + parent_session_id`，错误 session 失败关闭。
5. 多个 port 实例可能读取陈旧 `tasks.json`，API 列表看不到 QueryEngine 刚提交的 child。已增加显式 store refresh，并让 API 只通过 refresh-aware projection 读取。
6. cancel 与 late completion 的两个 port 实例曾可能在 revision 检查之间竞态覆盖。已按 resolved state root 共享进程内 `RLock`，每次 mutation 先 refresh 再 CAS；stale completion 被拒绝且 durable status 保持 cancelled。
7. child QueryEngine 事件曾写入 parent `query_session` 域，导致 parent lifecycle 出现 identity mismatch。已投影为 `agent_child_query_session`/`agent_child_tool_result`，保留因果 trace 但不污染父会话预算与验收。
8. TypeScript control state 曾附加在 session snapshot 外且没有自身完整性保护。已加入独立 checksum，篡改 model/compact/cancel state 会在 restore 前失败。
9. 旧 API/cleanroom 测试把未审批 `file_write` 当成默认成功。已将非权限主题测试改为预置文件上的只读 plan；专门权限测试保留写操作并验证 `409 permission_suspended` 与零副作用，没有新增 allow bypass。
10. API control 的 cancel/message 曾直接调用 Python `runtime.handle`。已改为 `agent_cancel/agent_message/agent_resume` TypeScript tool；Python 只接收通过 TS 后的 durable mutation。

## 4. 行为、失败路径与动态可达性证据

- TypeScript runtime 全套：`24 passed`。覆盖 child QueryEngine、depth failure、background drain、cross-process hydrate/cancel、stable fanout/fanin、terminal replay、exact resume correlation、control revision/idempotency/checksum、permission、protocol、budget/compact/restore、skills。
- Python/API 相邻回归：`66 passed, 2 subtests passed`。覆盖 durable authority/workspace escape/stale revision/shared-lock late result、spawn/fanout permission suspend、exact approval/retry、两个 child durable completion、child event domain、API cross-process resume、CodeWorker/API/control 与 clean productized runtime。
- Custody audit：`typescript runtime custody: PASS`。
- Python syntax：`py_compile` 覆盖 API、durable port/store、narrow host 与 audit script。
- 断开即失败：Agent host port 缺失时 TS 抛出 `agent durable/physical host port is unavailable`；未知/错误 parent session 无法 load；revision stale 无法 cancel/complete；control snapshot 篡改无法 restore；custody audit 会拒绝 Python owner 回流。
- 动态主路径：真实 `/tasks/{id}/subagents/fanout` 首次在 task 创建前挂起，审批后进入 TS Agent fanout，两个 child `ClaudeRuntimeCore` 完成并提交 durable record；随后 `/resume` 再次独立挂起、审批、hydrate、恢复并提升 revision。

未运行全仓无差别测试。原因：本 remediation execution 已运行所有改动文件和相邻 Agent、permission、CodeWorker、API、control、cleanroom 路径；无关长耗时套件按分层规则留给数字阶段/里程碑聚合审查。没有跳过本次 owner 转移所需的真实行为、失败路径或 cleanroom。

当前环境未提供 `tsc`、`bun` 或 `pnpm`，没有伪称执行静态 TypeScript compiler。TS 验证使用 Node 22 `--experimental-strip-types` 对全部 24 个 executable tests 直接加载正式 `.ts` 模块；该工具链限制已显式记录，不以此跳过行为门禁。

## 5. Source-free cleanroom

最终 cleanroom：`C:\Users\libin\AppData\Local\Temp\zyra-r01-execution-03-final-cleanroom-35001ce3d1634467997816f346e766f5`

只复制 Zyra 正式 `packages/**`、必要 `apps/code-worker/**`、API module、custody script 与当前 durable-port test；复制时明确排除 `vendor`、`vendor-runtimes`、source-pool、runtime-sources、`node_modules`、`__pycache__` 与 `.cache`。没有复制 `../claude-code-best` 或其它根来源仓库。

- cleanroom TypeScript：`24 passed`。
- cleanroom Python durable port：`2 passed`。
- cleanroom custody audit：`PASS`。
- 初次 Python collection 因隔离目录没有安装元数据且未设置 `PYTHONPATH` 而失败；同一 cleanroom 显式绑定复制后的 formal package roots 后通过。该失败发生在 import collection 前，不是行为失败，且没有借用根来源仓库修复。

## 6. Diff buckets

基于 working diff、相对 `a873f8eaba7f033f045bb9ef8ec0a481dbd40116`；三份任务开始前已有的脏计划文档不在本次暂存、分桶或 commit 中：

| Bucket | Added | Deleted | Counting decision |
| --- | ---: | ---: | --- |
| TypeScript production Agent/control/runtime wiring | 2,110 | 25 | 有效正式实现 |
| Python durable/API/physical-state production | 751 | 333 | 有效 Zyra state/API/routing 实现；不取得 Agent logical owner |
| Python adapter-only narrow host | 49 | 3 | 单独报告，不用作 TypeScript 深度内化行数证明 |
| Tests | 943 | 115 | 行为与对抗证据，不计 production 下限 |
| Package/config and custody audit | 90 | 1 | 配置/审计，不计 production 下限 |
| Generated/data/docs/vendor-like/source-pool | 0 | 0 | 无 |

production source 合计为 `2,910` additions / `361` deletions，其中 adapter-only 已单独隔离。没有用 tests、audit、配置、数据、ledger、manifest 或 vendor-like 内容抵扣正式实现。

## 7. One-vote veto audit

- 默认 CodeWorker/API 不构造 `AgentToolRuntime`、`SubagentRuntime`、`CodeWorkerSubagentExecutionPort`、`LogicalFanoutRuntime` 或 `ParentScopeBuilder`。
- API 不通过 `get_typescript_agent_port().handle(...)` 绕过 TypeScript control tool。
- TypeScript runtime 不引用 `../claude-code-best`、`vendor/claude-code-best` 或 `vendor-runtimes/claude-code-runtime`。
- 没有新增 npm/pip、MCP server、端口服务、Docker、动态 import 或来源仓库运行依赖。
- permission ask/approve 没有被弱化；Agent 与 resume 都必须独立取得 exact grant。
- `vendor/**` 与 `vendor-runtimes/**` 增量为零。

## 8. Legacy Python disposition and deletion condition

旧 Python Agent/subagent 文件继续存在只为历史测试、旧 import surface 和 conformance 对照；它们不是 fallback，也不计本 execution 有效新增代码。物理删除必须满足：

1. 后续数字阶段聚合审查确认仓内 production call graph 仍无构造或调用。
2. 依赖旧 `zyra_workers.__init__` export 的历史测试已迁移到显式 legacy fixture 或删除。
3. source graph/ledger 中引用旧类名的历史事实已保留为历史索引，不要求运行模块继续存在。
4. 删除不改变 `SubagentTaskRecord`、durable store、workspace/isolation physical port 等仍由 Python承担的正式责任。

在这些条件满足前允许保留文件，但任何后续 slice 都不得把它们迁入 vendor、重新设为默认 owner、silent fallback 或用其行数证明 TypeScript 内化。

## 9. Remaining boundary

- process-bound background 由 TypeScript 先返回 queued result，再在 stdio runtime 退出前 drain，避免孤儿 Node 进程与并发 JSONL reader；它不是 Python child loop，也不伪称独立 daemon。跨进程 task identity、cancel fence 与 resume 已 durable 化。
- 更长生命周期的 scheduler-managed detached execution 如后续需要，应复用当前 TS logical task contract 与 Python physical lease/worker ports，不得恢复 Python Agent decision loop。
- execution-03 已关闭 M1-R01 三份 remediation 的代码切换；后续恢复 `execution-state.yaml` 指定的正式 M1 slice，不新增第四份 remediation 切片。
