# M1-S03D-02 subagent 与 commands integration 复审

## 结论

本切片实现提交为 `6bce2f01208fc7a6675f5104b42acf3dc088ff80`，相对基线
`0ecd643e4e2d0bd0afed8d968ea7a289bee32463` 新增生产代码 10,605 行、
删除 157 行，新增测试 748 行，`vendor/**` 与 `vendor-runtimes/**` 为 0。
生产代码超过本切片 8,500 行最低线；03D-01 与 03D-02 合计满足父级
17,000 行最低线。

切片目标判定为完成。该结论只关闭 03D 的逻辑 subagent、typed yield、
background coordination 和 command/control integration，不关闭 05A 的物理
worktree/sandbox，也不关闭 07A 的 worker/lease/placement owner。

## 目标覆盖矩阵

| 验收项 | 状态 | 证据 | 阻断 |
| --- | --- | --- | --- |
| AgentTool 进入真实 CodeWorker registry | 完成 | `agent_tool.py`、`code_worker_runtime.py`、API signed-scope 测试 | 否 |
| child session/tool/permission/MCP/skill 单调收窄 | 完成 | `parent_scope.py`、`integration.py`、`session_assembly.py`；扩权拒绝测试 | 否 |
| 同 parent 两 child fan-out/progress/typed-yield/fan-in | 完成 | `fanout.py`、`typed_yield.py`、`subagent_yield.py`；真实 API fanout 测试 | 否 |
| foreground-to-background promotion | 完成 | durable fanout record、detached handle、promotion 行为测试 | 否 |
| late result、double cancel、restart fence | 完成 | `execution_receipts.py`；late quarantine、幂等 cancel、unsafe restart 测试 | 否 |
| evicted transcript resume 且不重复副作用 | 完成 | `resume_capsule.py`；签名 capsule materialization 测试 | 否 |
| parent-child structured delivery，禁止 sibling/free chat | 完成 | `delivery.py`；拓扑违规测试 | 否 |
| dynamic command registry | 完成 | `source_coordinator.py`；API 启动/`GET /commands` refresh；last-good 测试 | 否 |
| structured API/headless control | 完成 | `control_hub.py`、`POST /tasks/{id}/control-frames` | 否 |
| command 真实语义效果 | 完成 | `/clear` checkpoint-before-reset、`/compact`、`/goal`、`/btw`；MCP/permission/model/plugin/subagent 只读操作直连 canonical owner，未授权 mutation fail closed | 否 |
| side question 不污染主会话 | 完成（继承并回归） | 03D-01 `SideQuestionRuntime` 行为测试 | 否 |
| 物理 worktree/sandbox | deferred | 只交付 signed isolation request/manifest/cleanup contract；owner=M1-05A | 否，本切片明确边界 |
| physical worker/lease | deferred | logical receipt 明确排除 `worker_id/lease_id/capacity/heartbeat`；owner=M1-07A | 否，本切片明确边界 |

## 主路径证据

| 来源机制 | Zyra 落位 | 生产入口 | 状态/event 接入 | 行为测试 |
| --- | --- | --- | --- | --- |
| Claude Code `AgentTool`/subagent lifecycle | `subagents/agent_tool.py`、`integration.py`、`session_assembly.py` | CodeWorker `Agent`/`Task` tool；`POST /tasks/{id}/subagents` | `SubagentTaskStore`、transcript、execution receipt、task lifecycle events | signed ceiling、cross-task rejection、API spawn |
| OMP TaskTool/executor | `fanout.py`、`typed_yield.py`、`subagent_yield.py` | `POST /tasks/{id}/subagents/fanout` | `FanoutStore`、`TypedYieldStore`、causal aggregate/delivery | 两 child 真实 CodeWorker fanout/fan-in |
| OMP async job manager | `fanout.py` | foreground request promotion | durable group/handle/progress；无 global singleton | promotion 后完成测试 |
| OMP agent registry/RPC | `delivery.py`、`control_hub.py` | control frames、parent-child delivery | durable edge/inbox/frame；correlation/idempotency | sibling denial、frame recovery |
| Claude/OpenCode command registry | `source_coordinator.py`、`owner_handlers.py` | API startup、`GET /commands`、`/help` | atomic generation、last-good、refresh events | malformed reload 保留 last-good |
| Claude session commands | `session_control.py`、API owner callbacks | `/clear` | SQLite task state、session transaction store、checkpoint/event | API `/clear` epoch mutation |

删除或禁用 `LogicalFanoutRuntime`/`TypedYieldStore` 会使两 child fanout 测试失败；
禁用 `SubagentRuntime`、dispatcher、prompt queue 或 lifecycle owner 会使 03D-01
disconnect 测试失败；删除 `SubagentYield` 会令真实 API fanout 以
“explicit typed yield missing”失败。不存在普通函数 summary fallback。

## Source-to-target 裁决

| 来源 | 路径/机制 | 裁决 | 说明 |
| --- | --- | --- | --- |
| claude-code-best | AgentTool、subagent lifecycle、background task | active | 拆为 Zyra task/scope/session/receipt/delivery 模块，不运行上游 CLI |
| claude-code-best | TUI/CLI command registry、session commands、StructuredIO | active | 拆为 registry coordinator、dispatcher owner、control hub 与 API route |
| claude-code-best | worktree isolation | contract-only/deferred | 03D 仅持有逻辑 isolation request；物理 owner=M1-05A |
| oh-my-pi | `task/index.ts` fanout/semaphore/promotion/progress | active | Zyra-owned durable fanout state machine |
| oh-my-pi | `task/executor.ts` scope/budget/yield/park-revive | active | signed parent scope、execution receipts、typed yield、resume capsule |
| oh-my-pi | async job manager / agent registry | adapter | 只取 coordination 语义；无 `AgentRegistry.global()` 运行依赖 |
| oh-my-pi | RPC progress/events | adapter | 映射为 structured control/event envelope；不启动 `omp --mode rpc` |
| oh-my-pi | PAL/worktree | contract-only/deferred | 物理实现由 05A/07A 接管 |
| oh-my-pi | 整仓、JSONL、sidecar | reference-only/excluded | 未迁入、未计行数、非运行依赖 |

## 状态 custody

- logical task/transcript：`SubagentTaskStore` 与 `SubagentTranscriptStore`。
- signed parent/child authority：`ParentScopeStore`，服务端签名，child 只能取交集。
- execution exactly-once fence：`ExecutionReceiptStore`，保存 attempt token digest、phase、late-result quarantine。
- fanout/progress/fan-in：`FanoutStore` 与 `TypedYieldStore`。
- parent-child message：`DurableDeliveryStore`，只允许 topology-bound route。
- resume：`ResumeCapsuleStore`，commit-before-evict，带 transcript leaf digest。
- command request/frame/source：`ControlRequestStore`、`ControlFrameStore`、`CommandRegistryCoordinator`。
- canonical task/session mutation：SQLite task store；03D transaction store只保留前后 owner receipt，不建立第二 session truth。
- permission：03A `ToolPermissionRuntime`；内部 `SubagentYield` 仍经 risk policy、精确 session rule和一次性 execution grant，不旁路权限。
- MCP/skill/model：分别由 03B/03C/session metadata owner 持有；03D command handler只调用 owner，未授权 mutation 确定性失败。

## 测试与验证

通过：

```text
.venv\Scripts\python.exe -m unittest tests.unit.test_commands_skills tests.unit.test_subagent_commands_foundation tests.integration.test_subagent_commands_foundation_api tests.unit.test_subagent_commands_integration tests.integration.test_subagent_commands_integration_api -v
33 tests, OK

.venv\Scripts\python.exe -m compileall -q packages/commands/zyra_commands packages/workers/zyra_workers/subagents packages/workers/zyra_workers/code_worker_runtime.py packages/runtime/zyra_runtime/claude_query_engine_runtime.py packages/runtime/zyra_runtime/executor.py apps/api/zyra_api/main.py
OK

clean archive of 6bce2f0, no root source repositories/.git/cache/vendor:
29 tests, OK
```

父级命令已执行但未全绿：

```text
.venv\Scripts\python.exe scripts/verify_m2.py
FAIL before 03D checks: SkillRevision has no attribute vendor_paths
```

这是既有 03C verifier/model 漂移：`scripts/verify_m2.py:173` 仍读取已删除的
`SkillRevision.vendor_paths`。本切片未修改该 verifier 或 03C model。全量
`unittest discover -s tests` 也受仓库既有 PYTHONPATH loader、live browser、旧
tool-count、ledger/baseline 和 scheduler expectation 失败影响；03D 定向套件与
干净目录套件均全绿。该全仓门禁债务必须由对应 owner 修复，但不掩盖本切片结果。

## 有效行数分桶

| 桶 | 新增行 | 是否计最低线 | 说明 |
| --- | ---: | --- | --- |
| production | 10,605 | 是 | apps/packages 下真实 state machine、owner、API/worker 集成 |
| test | 748 | 否 | 行为、失败、恢复、并发、API 测试 |
| generated | 0 | 否 | 无 |
| data/docs/runtime-assets | 0 | 否 | 本实现提交无清单/seed/skill 文本 |
| vendor/source-pool | 0 | 否 | 无新增 |
| adapter-only | 0 计入 | 否 | source audit 与 RPC/PAL contract 不作为最低线依据 |
| mock/fixture | 0 计入 | 否 | fake child仅用于并发状态机单测；真实 API test执行 CodeWorker child |

命令：

```text
git diff --numstat 0ecd643e4e2d0bd0afed8d968ea7a289bee32463 6bce2f01208fc7a6675f5104b42acf3dc088ff80 -- apps packages skills scripts
git diff --numstat 0ecd643e4e2d0bd0afed8d968ea7a289bee32463 6bce2f01208fc7a6675f5104b42acf3dc088ff80 -- tests
git diff --numstat 0ecd643e4e2d0bd0afed8d968ea7a289bee32463 6bce2f01208fc7a6675f5104b42acf3dc088ff80 -- vendor vendor-runtimes
```

## Requirement 影响

- `REQ-TOPO-01` / `SCORE-ORG`：增加真实 logical hierarchy、同 parent fanout、typed fan-in 与稀疏 parent-child route 证据；不宣称动态 top-k/静态拓扑对照已关闭。
- `REQ-COMM-01`：typed yield、禁止 sibling/free-chat、artifact/evidence refs提供低熵通信基础；指标 owner仍为 04B/05C/08。
- `REQ-TRACE-01`：subagent/task/tool/yield/control correlation 进入 canonical event/control stream；UI owner仍为 M2。
- `SCORE-COMPAT`：child model allowlist 与 session assembly已存在；多真实 provider 验证仍由 05D/07A/M3 完成。
- `REQ-CLOSE-01`、`REQ-FAULT-01` 未因本切片提升状态；本切片只提供 receipt/cancel/restart/recovery input 基础。

## 残留债务与父级收束

1. `verify_m2.py` 的 03C `vendor_paths` 漂移是全仓门禁债务，需由 03C/验证脚本 owner修复。
2. `/branch`、`/rewind`、`/resume` 的 transaction/state-owner机制已实现，但 API 默认只激活 `/clear`；其余命令在 canonical checkpoint owner未提供精确目标时 fail closed。
3. `/mcp`、`/permissions`、`/model`、`/hooks` 的只读 owner路径已接通；mutation必须持有对应 owner的精确授权，03D不伪造 grant。
4. worktree/sandbox 与 physical worker/lease严格留给 05A/07A，不能用03D logical receipt冒充。
5. 本切片没有完成 live 2,000-step、真实端边云、多模型或赛题最终UI证据，不能据此关闭阶段赛题门禁。

上述 2-4 是文档明确的 owner/权限边界，不阻断 03D logical integration；第1项阻断
全仓 `verify_m2.py` 绿灯但不是本切片行为回归。父级 03D 可收束完成并将下一入口交给
M1-04A-01。
