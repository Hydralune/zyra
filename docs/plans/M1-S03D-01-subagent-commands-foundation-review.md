# M1-S03D-01 subagent 与 commands foundation 复审记录

## 结论

`M1-S03D-01` 已完成，固定实现 HEAD 为 `12996fd`，基线为 `228fc1f`。本结论只关闭当前 foundation slice，不关闭父级 `M1-03D`；下一入口仍是 `slice-03d-02-subagent-commands-integration.md`。

当前实现把 Claude AgentTool/commands、opencode task/command、Hermes delegation 和 AgentScope child-agent 机制裁剪成 Zyra 自有逻辑状态、权限边界、持久队列和控制协议。没有新增 `vendor/**`、`vendor-runtimes/**`、外部 CLI、sidecar、Docker、端口服务或根目录 `../` 运行依赖。

## 目标覆盖矩阵

| 验收项 | 状态 | 生产证据 | 行为证据 | 阻断 |
| --- | --- | --- | --- | --- |
| `SubagentRuntime` 与逻辑 task lifecycle | 已完成 | `packages/workers/zyra_workers/subagents/runtime.py`、`lifecycle.py`、`task_store.py` | foreground 完成、background cancel、持久 handoff/transcript 测试 | 否 |
| Agent definition 与 child context | 已完成 | `definitions.py`、`context.py` | isolated context、唯一 child session、depth/cycle metadata | 否 |
| child tool/permission 单调收窄 | 已完成 | `tool_scope.py`、`code_worker_runtime.py`、`executor.py` | parent tool ceiling、unknown tool、bypass、cooperative cancel-before-side-effect | 否 |
| budget、result rollup、structured handoff | 已完成 | `budget.py`、`handoff.py`、`transcript.py` | 原子共享 reservation 防超卖、低熵 handoff、raw transcript 不注入 parent | 否 |
| background、parent cancel cascade、continuation | 已完成 | `control.py`、`continuation.py`、`runtime.py` | running child message、跨重启幂等 replay、parent cascade、execution_ref 不变 | 否 |
| logical isolation request/manifest/cleanup | 已完成 | `isolation.py` | shared-workspace M0 port；worktree/sandbox/remote 确定性拒绝；cleanup receipt | 否 |
| dynamic command registry | 已完成 | `packages/commands/zyra_commands/runtime/registry.py` | source precedence、alias collision、相同 reload 不提升 generation | 否 |
| durable control dispatcher 与 prompt queue | 已完成 | `dispatcher.py`、`store.py`、`prompt_queue.py` | received→validated→running→terminal、幂等、revision、now/next/later、target isolation | 否 |
| StructuredIO main path | 已完成 | `structured_io.py`、`POST /tasks/{id}/control-frames` | strict versioned envelope、ordered response、resolved persistence | 否 |
| `/btw` side question | 已完成 | `side_question.py`、API handler | tools=空、maxTurns=1、cache write=false、独立 transcript/usage、parent messages/replan 不变 | 否 |
| stateful command 语义 | 已完成 | `apps/api/zyra_api/main.py` | `/goal` 真实修改 TaskState；`/compact` 调 MemoryFabric；无 canonical owner 的 `/hooks`、`/clear`、`/rewind` fail closed | 否 |
| 禁用即失败 | 已完成 | 各 runtime 的 disabled gate | 分别断开 SubagentRuntime、lifecycle、isolation、registry、dispatcher 均使真实路径失败 | 否 |
| 8,500 行最低线 | 已完成 | 见分桶 | Git numstat | 否 |

## 主路径证据表

| 模块 | 来源机制 | Zyra runtime 入口 | 状态/event/artifact 接入 | 测试 |
| --- | --- | --- | --- | --- |
| logical subagent aggregate | Claude AgentTool/runAgent/LocalAgentTask；opencode Task | `SubagentRuntime.spawn`、`POST /tasks/{id}/subagents` | `SubagentTaskStore`、SUBAGENT_* events、structured handoff artifact refs | `test_subagent_foreground_lifecycle_has_structured_handoff` |
| CodeWorker child dispatch | Claude runAgent/tool scope | `CodeWorkerSubagentExecutionPort.execute` | root Task ID 保持；logical child ID 写 metadata；restricted ToolRegistry 同时供模型与 executor | child scope/cancel tests；既有 CodeWorker/Skill/MCP suites |
| logical control | SendMessage/background task | `SubagentControlRuntime.execute`、`POST .../subagents/{child}/{action}` | durable `controls.json`、pending structured messages、cascade cancel | `test_background_subagent_cancel_is_durable_and_cooperative` |
| command control | Claude commands/controlSchemas；opencode command metadata | `RuntimeControlDispatcher.submit`、`POST /tasks/{id}/commands` | `ControlRequestStore`、COMMAND_* events、SQLite TaskState checkpoint | dispatcher/API integration tests |
| structured transport | Claude structuredIO/remoteIO | `StructuredControlIO.handle`、`POST /tasks/{id}/control-frames` | versioned envelope、dedupe、resolved response store | `test_commands_use_durable_dispatch_and_goal_is_real_mutation` |
| side question | Claude btw/sideQuestion/forkedAgent | `SideQuestionRuntime.ask`、`/btw` | immutable context snapshot、独立 JSONL transcript、SIDE_QUESTION events | tool-free and tool-violation tests |
| compact/goal/artifact | Zyra canonical owners | `/compact`、`/goal`、`/team-onboarding` | MemoryFabric、TaskState、LocalArtifactStore | API command suite |

删除或禁用上述模块会直接使对应测试失败；失败依赖的是 Zyra module，而不是 vendor/sidecar 的缺失。

## Source-to-target 裁决

逐项机器可读裁决位于：

- `packages/workers/zyra_workers/subagents/source_audit.py`
- `packages/commands/zyra_commands/runtime/source_audit.py`

摘要：

| 来源 | disposition | 目标与说明 |
| --- | --- | --- |
| Claude `AgentTool.tsx`、`runAgent.ts`、agent definitions/context、fork/resume、LocalAgentTask、SendMessage、task/session disk output | active / Zyra module migrated | 拆入 runtime/definitions/context/lifecycle/continuation/task_store/transcript；使用 Zyra schema、event 和错误模型 |
| Claude agentMemory | adapter | 03D 只携带 memory refs；实际 skill memory/outcome restore 由 M1-06C 接管 |
| Claude RemoteAgentTask | deferred | 真实 remote dispatch/lease 由 M1-07A/05D；03D 不伪造 remote worker |
| Claude worktree | contract-only | 03D 交付 isolation request/manifest/cleanup；物理实现由 M1-05A |
| Claude commands、btw、prompt queue、controlSchemas、structuredIO/remoteIO/print | active / adapter | 拆入 registry/dispatcher/queue/side_question/structured_io 与 API 主路径 |
| opencode Task/background/command/session prompt | adapter / reference-only | 采用 typed metadata、child session 和 background 语义；拒绝进程内 background registry 成为持久 owner |
| Hermes delegation/async | adapter | 采用 depth、tool intersection、completion/re-entry；不调用其 subprocess runtime |
| AgentScope child-agent/HITL | adapter / deferred | agent template/provenance 进入 registry；resident inbox/wakeup 留给 07A/05C |

`legacy_vendor_debt` 没有被用作完成状态。M1-01B pilot、root source repos、manifest/inventory 都未进入 runtime 依赖或有效行数。

## 状态责任

| 状态 | 当前 owner | 恢复方式 |
| --- | --- | --- |
| root TaskState/checkpoint | `SQLiteStore` | task checkpoint |
| logical child task、parent-child、dispatch/execution ref、handoff、recovery | `SubagentTaskStore` | atomic JSON restore |
| child sidechain | `SubagentTranscriptStore` | digest/sequence validated JSONL replay |
| shared usage reservation | `SubagentBudgetReservationStore` | atomic JSON reservation/settlement |
| logical subagent operator command | `SubagentControlRuntime` | durable response/idempotency replay |
| permission rules/requests/mode | M1-03A `PermissionStateStore` | 03D 只保存派生 digest/rule IDs/denials，不复制 exact grant |
| command request/queue | `ControlRequestStore`、`PromptQueueRuntime` | atomic JSON lifecycle restore |
| root canonical events | `SQLiteStore` / EventRecord | SUBAGENT_*、COMMAND_*、SIDE_QUESTION |
| physical worker/lease/capacity/heartbeat | 不属于 03D；M1-07A | 本 slice store 明确禁止这些字段 |
| worktree/sandbox physical lifecycle | 不属于 03D；M1-05A | 当前 logical port 对物理 kind fail closed |

## 有效行数分桶

基线 `228fc1f` 到实现 HEAD `12996fd`：

| 桶 | added | deleted | 是否计入 8,500 |
| --- | ---: | ---: | --- |
| apps/packages production 总量 | 9,029 | 30 | 待扣除 source-audit bookkeeping |
| source-audit/source-map 实现 | 296 | 0 | 否；只作来源裁决证据 |
| 有效 production | 8,733 | 30 | 是；净 8,703，超过 8,500 |
| tests | 以最终 evidence commit 的 numstat 为准 | 29+ | 否 |
| docs | 本复审文档 | 0 | 否 |
| generated/data/runtime-assets | 0 | 0 | 否 |
| vendor/vendor-runtimes/source-pool | 0 | 0 | 否，且触发失败线 |

`models.py` 中的 schema 不是重复 DTO：其构造期 invariants、digest、safe/private serialization、transition identity 被 task store、dispatcher、API 与行为测试共同消费。`CodeWorkerSubagentExecutionPort` 也不是薄黑箱 adapter：它接管 root/child identity、exact registry、cooperative cancellation、permission/context conversion、usage/error/event rollup；核心决策不委托上游 CLI 或 sidecar。

## 验证记录

| 命令 | 结果 |
| --- | --- |
| `python -m unittest tests.unit.test_subagent_commands_foundation -v` | 15/15 通过 |
| `python -m unittest tests.integration.test_subagent_commands_foundation_api tests.unit.test_subagent_commands_foundation -v` | 最终 17/17 通过 |
| `python -m unittest tests.integration.test_api_control_commands -v` | 22/22 通过；旧 event-only `/goal` 测试已改为真实 state mutation，clear/rewind 改为 fail-closed 断言 |
| CodeWorker + SkillTool + MCP 相关 30 项套件 | 29 通过；1 项仍固定断言旧 `tool_registry_active_count=9`，实际 03C 主路径为 12，与本 slice 参数改动无关 |
| cleanroom：只复制所需 Zyra packages 与核心 unit test 后运行 | 15/15 通过；无 root source repo/vendor/cache/SQLite 残留 |
| `python -m unittest discover -s tests` | 已运行但非全绿；包含测试发现时 `zyra_skills` 未进入 sys.path 的既有 import-order errors、既有 clean-productization 与 ledger 审计失败；定向 03D 与受影响 API suites 通过 |
| `python scripts/verify_m2.py` | 非零：既有脚本访问已不存在的 `SkillRevision.vendor_paths`，在进入本 slice 路径前失败 |
| `git diff --check` | 通过 |
| `git diff --numstat 228fc1f 12996fd -- vendor vendor-runtimes` | 空 |

## 批判式复审发现与修复

1. 发现 fork selector 在 path/domain identity 缺失时可能放行；改为缺字段即拒绝，并加语义测试。
2. 发现 child scope 初版只按 runtime parent registry，不尊重请求中的 parent tool ceiling；改为先对 parent registry 做 declared-parent 交集。
3. 发现 registry 相同 source reload 会因 generation 被纳入比较而误增 generation；改为比较 generation-neutral digest。
4. 发现 `Future.cancel` 不足以阻断已运行工具；在 `ToolExecutor` 副作用边界增加 cooperative cancellation check。
5. 发现旧 `/goal`、`/hooks`、`/plan` event-only ACK 会制造伪成功；`/goal` 改为 TaskState mutation，无 owner 的命令拒绝。
6. 发现 StructuredControlIO 最初只有导出、没有生产可达性；补充 `/control-frames` 主路径和集成测试。
7. 发现 continuation/control 最初未组装进 `SubagentRuntime`；补充 durable `SubagentControlRuntime`、running-child message、跨重启幂等 response 和 API action path。

这些问题均已修复后重新运行定向测试。

## 残留债务与下游 contract

非阻断于当前 slice、但阻断父级 M1-03D 完成的事项：

- `slice-03d-02`：补齐更完整的 interactive permission ask、MCP mutation、plugin reload、session interrupt/resume/control surface；不得回退到 event-only ACK。
- M1-05A：实现 worktree/sandbox physical lifecycle，消费 isolation request/manifest/cleanup contract。
- M1-05C：把当前 durable event/control stores 投影到 canonical EventStore/MessageBus，不复制 task truth。
- M1-07A/07C：增加 WorkerInstance/lease/capacity/heartbeat 与 task-worker-lease 投影；不得把这些字段写回 03D task aggregate。
- M1-06C：消费 agent memory refs 并拥有 skill memory/outcome restore。
- M2-04A/B：消费已存在的 command/control/subagent protocol；前端不能成为后端状态 owner。

当前 slice 触达 `REQ-TOPO-01`、`REQ-COMM-01`、`REQ-TRACE-01` 以及 `SCORE-ORG`、`SCORE-COMPAT` 的基础设施证据，但没有关闭任何比赛硬门禁：尚无两个 live 跨域任务、2,000 canonical transitions、真实端边云 dispatch、多模型矩阵或 M3 冻结证据，因此 requirement matrix 状态不提升。
