# M1-S04C-02 Action Registry / Permission Integration 增量批判式自审

## 1. 结论

- Slice：`M1-S04C-02`
- 基线提交：`33de078e73352bc9b51d35addf802b145e4ee4a4`
- 实现提交：`b5e3a676acf1f907885d680764e32feadda8e504`
- 自审结论：**当前 integration slice 完成，父级 `M1-04C` 收口完成，不阻断进入 `M1-S04D-01`。**
- 默认路径：未指定 legacy backend 的 `BrowserWorker` 现在只能经 `BrowserActionApplication` 执行 productized plan；04C 失败不会回退到旧 actor/action loop。
- 高风险升级：已触发并完成匹配验证。当前改动改变默认 browser action、permission 与 fallback 路径，因此执行了实际 Chrome 主路径、03A/04A/04B 邻接回归、ledger 全套和精确提交 clean-copy 测试；未无差别重复全仓长套件。

## 2. 当前 slice 目标覆盖矩阵

| 当前 slice 目标 | 状态 | 生产证据 | 行为证据 | 阻断 |
| --- | --- | --- | --- | --- |
| 04C registry/security 进入默认 BrowserWorker | 完成 | `browser_worker.py` → `BrowserActionApplication.execute_plan` | 默认 plan、unknown/bypass、disabled/fallback 测试 | 否 |
| 全计划先准入、先安全预检、先权限裁决，再允许任一 side effect | 完成 | `plan_adapter.py`、`application.py`、`gateway.py` | 第二步未知时第一步 CDP=0；任一步 deny 时整个 plan side effect=0 | 否 |
| action/tool-use identity 稳定且 ASK 可续跑 | 完成 | `integration_models.py`、`continuation_runtime.py` | pending 保持 live session，审批后同一 action id 只执行一次，replay 被拒绝 | 否 |
| 04A session-bound CDP、04B selector/semantic state、03A one-use grant 合并为唯一 dispatch fence | 完成 | `session_adapter.py`、`semantic_probe.py`、`executor.py` | stale/target/session/grant 变化均在 dispatch 前失败 | 否 |
| network redirect/DNS、download、clipboard、screenshot/PDF 等完整 handler | 完成 | `session_adapter.py`、`download_runtime.py`、`executor.py` | redirect/rebinding、oversize、native download quarantine、artifact 投影测试 | 否 |
| deadline/cancel 与运行中 control | 完成 | `deadline_runtime.py`、`control_runtime.py` | exact active action 可取消；跨 run/task/session/action 拒绝；command replay 幂等，body conflict 拒绝 | 否 |
| plan/action/permission/artifact/final-result 因果链 | 完成 | `event_writer.py`、`event_port.py` | 每 action 恰一条 final result；pending partial 与 terminal 分离；cause/action/tool-call id 对齐 | 否 |
| API ASK continuation 与 idempotency | 完成 | `apps/api/zyra_api/main.py` | pending 返回 202；同 endpoint + 同 idempotency key 审批后继续；pending 不被 terminal cache 覆盖 | 否 |
| source ledger 与根来源仓库独立 | 完成 | ledger seed、sync script | `--check`：10 decisions、7 productized、owner `M1-04C` | 否 |
| 真实 productized Chrome lane | 完成 | `smoke_browser_session_productization_live.py` | 2 actions、2 allow、0 deny、0 human、3 artifacts、owner-loss/cleanup 均成功 | 否 |

## 3. 父级 `M1-04C` 累计收口

| 父级能力 | 04C-01 foundation | 04C-02 integration | 累计结论 |
| --- | --- | --- | --- |
| typed registry/schema/risk/security policies | registry、strict schema、risk/network/file/secret/form/selector receipts | 默认 plan adapter 使用同一 registry digest 与 receipts | 完成 |
| permission owner | 03A bridge、exact grant、ASK/deny/replay | session-owned continuation WAL、API 202/resume、all-actions barrier | 完成，未建立第二套 permission store |
| browser side effects | typed CDP executor/fence | 04A session transport、全部 handlers、deadline/control | 完成，无 blind JS/legacy fallback |
| event/artifact | action result/projector | causal writer、partial/final、native download/artifact handoff | 完成 |
| live/runtime productization | foundation live lane | 默认 productized Chrome lane、owner-loss 与 cleanup | 完成 |
| 最低有效 production | 保守 `9,944` | 保守 `6,559` | **`16,503 / 15,000`，完成** |

## 4. 来源角色、裁剪与目标落位

| 来源角色 | 来源机制 | Zyra 目标 | 集成结果 |
| --- | --- | --- | --- |
| primary：browser-use | action service/actor、security/watchdog、download/browser state | `browser_action/{application,plan_adapter,session_adapter,download_runtime,semantic_probe}.py` | 拆为 Zyra plan/session/receipt/event/artifact 模块；不保留 actor public bypass 或上游 runtime owner |
| supplementary：claude-code-best | tool loop identity、permission continuation、result budget | `continuation_runtime.py`、`event_writer.py`、03A action gate binding | action id=tool-use id；ASK WAL/exact resume；bounded result externalization；03A 仍是 canonical permission owner |
| supplementary：oh-my-pi | typed hook/preflight、all-sections-first、hash-bound control | `plan_adapter.py`、`control_runtime.py` | 全计划静态准入、deny-first、command/body fingerprint；不引入 dynamic/yolo path |
| conformance/reference | opencode、OpenClaw、AgentScope | tests/ledger decision | 只验证 approval/policy 语义，不获得第二套 runtime/store |

最终运行不依赖 `../browser-use`、`../claude-code-best`、`../opencode`、vendor/source-pool、npm link、pip editable source path、sidecar、MCP server、插件、Docker、本地辅助端口或动态 import。实际 Chrome 是 Zyra 04A 自有 session runtime 的受控外部进程，不是新增黑箱决策 owner。

## 5. 主路径与动态可达性

| 模块 | 真实入口 | owner 接入 | 断开即失败证据 |
| --- | --- | --- | --- |
| `BrowserActionApplication` | `BrowserWorkerRuntime.run` 默认 productized branch | 04A session + 04B context + 03A gate | disabled application 明确失败，legacy fallback=false |
| `BrowserActionPlanAdapter` | `execute_plan` 的第一阶段 | registry/schema/selector target | unknown、bypass、oversize、elapsed deadline 使整个 plan CDP=0 |
| continuation | worker/API 同一 request id + idempotency key | `PermissionStateStore` + session-owned WAL | pending 不执行；批准后一次；replay/foreign binding 失败 |
| session transport | gateway executor | exact browser session/target/CDP generation | session/target/response size/cancel 失败均不跨 lease 执行 |
| control | `browser_action_control` WorkerRequest | deadline active-action registry | exact cancel 改变执行状态；控制命令不能授权或续跑 permission |
| event/artifact writer | plan admission 至 terminal result | canonical `EventRecord`/`LocalArtifactStore` | writer/port disabled 时主路径 fail closed；不是仅日志 ACK |
| API continuation | task worker POST | pending/terminal idempotency 分区 | 202 envelope 包含 checkpoint/request/tool-use ids，terminal 才进入 final cache |

## 6. 零副作用与失败矩阵

| 条件 | 结果 | 语义证据 |
| --- | --- | --- |
| plan 中任一 unknown action、reserved permission state 或 bypass metadata | 整体拒绝 | 所有 action `side_effect_count=0`，无 final success |
| sealed 高风险/未知 action | deterministic deny + recovery | `human_intervention_count=0`，不回退 legacy |
| interactive ASK | pending + 202 | live browser session 保留，pending 前 CDP=0；审批后 exact once |
| stale selector/semantic/target/CDP generation | dispatch 前失败 | 04B identity 重新验证，不信任模型 selector/geometry |
| DNS set 变化、redirect 越界、literal/private 未授权 | network fail closed | Fetch continue/navigation=0；receipt epoch 不改变 DNS identity，地址集合改变会拒绝 |
| request/response oversize、deadline elapsed、exact cancel | typed failure | action grant 不消费或 dispatch 被取消 |
| native download 路径/identity/quarantine 失败 | artifact 不发布 | owned download ledger 和 canonical artifact handoff fail closed |
| action/control replay 或相同 command id 不同 body | replay/conflict | 已完成 action 不重放，控制原记录不被覆盖 |
| 04C application/CDP/event port disabled | explicit failure | `browser_action_fallback_allowed=false` |

## 7. 状态 custody 与恢复

| 状态 | canonical owner | 04C-02 责任 |
| --- | --- | --- |
| browser process/session/lease/target/CDP | M1-04A | 只消费 session runtime，绑定 generation，保留 owner-loss resume capsule |
| browser message/selector/semantic revision | M1-04B | begin/finish turn、resolve/revalidate，写回 context receipt |
| permission mode/rule/request/decision/grant/custody | M1-03A `PermissionStateStore` | 只提供 action material、消费 one-use grant、保存 continuation reference |
| pending continuation | 04C session-owned WAL + 03A request | payload/checkpoint 与 run/task/session/action/tool-use 指纹绑定，terminal 后 tombstone |
| deadline/cancel/control | 04C in-process runtime | 活跃 action 的短期 execution state；不接管 task/session canonical history |
| event/artifact | canonical event log / `LocalArtifactStore` | 写 causal projections、bounded externalization、download quarantine handoff |
| API idempotency | 既有 task checkpoint | pending 与 terminal 分区；不持久化 custody token 或 secret material |

## 8. 因果与语义效果

每个 plan 先生成 `plan-admitted`，每个 step 的 `tool_call_id == action_id == permission_tool_use_id`。preflight、permission decision/grant、action transition、artifact handoff 和 `tool_result` 都携带同一 identity 与 cause event。pending 只能产生 partial result；每个被接纳 action 最终恰有一条 terminal result。计划任一步在 barrier 前失败时，其余已获 grant 的 action标为 skipped 且不消费 side effect。测试不仅断言 event 存在，还断言 CDP 调用次数、session 存活、artifact 数量、grant 消费与 replay 行为真实改变。

## 9. 有效行数分桶

`git diff --numstat 33de078 b5e3a67` 的保守分桶：

| 桶 | 新增 | 删除 | 是否计 production |
| --- | ---: | ---: | --- |
| 10 个新增 `browser_action` production modules | 5,969 | 0 | 是 |
| 既有 API/runtime/worker/security modules 的生产改造 | 590 | 54 | 是 |
| 保守有效 production | **6,559** | **54** | **是，超过 slice 6,000** |
| package export `__init__.py` | 91 | 0 | 否 |
| tests | 511 | 19 | 否 |
| verification/ledger scripts | 102 | 25 | 否 |
| ledger JSON data | 410 | 134 | 否 |
| generated/mock-only/fixture-only/data-as-code | 0 | 0 | 否 |
| vendor/vendor-runtimes/source-pool | 0 | 0 | 否 |

04C-01 保守 `9,944` + 04C-02 保守 `6,559` = 父级 **`16,503`**，超过 `15,000`。测试、脚本、ledger data、exports 均未抵扣生产下限。

## 10. 验证记录

最终实现提交上的直接验证：

1. 04C/04A/API 组合：36 passed、5 subtests，48.66s。
2. 03A permission + BrowserWorker：99 passed、281 subtests，102.34s。
3. 04B message foundation/integration：11 passed、10 subtests，67.36s。
4. internalization ledger 全套：67 passed；3 个既有 pytest collection warnings，97.43s。
5. 实际 Chrome productized smoke：2 actions completed、2 permission allows、0 denies、0 human interventions、3 artifacts；owner-loss resume 与 cleanup 均成功。
6. 精确提交 clean-copy（`git archive b5e3a67`，临时目录，`PYTHONDONTWRITEBYTECODE=1`）：14 passed，26.83s；覆盖 integration unit、exact ASK、default rejection、control、network/fallback、API。
7. ledger `--check`：aligned；10 decisions、7 productized、owner `M1-04C`。
8. `compileall`、`git diff --check`、vendor diff、增量依赖/路径审计：通过；vendor diff 为 0。

实现期间发现并修复：

- 严格 schema 最初拒绝 04B 历史 `capture_trace(marker=...)`；最终只增加 bounded optional `marker`，未放宽 `additionalProperties=false`。
- bypass 扫描最初把 04A 受独立 session policy 约束的顶层 Windows Chrome sandbox authority 误判为 permission bypass；最终仅豁免精确顶层 runtime flag，嵌套/step 注入仍拒绝。
- 真实 Chrome smoke 揭示旧脚本 `interactive + permission_headless=true` 被 03A 正确收敛为 sealed；脚本改为显式 interactive session authorization，sealed deterministic deny 仍由测试覆盖。

未运行项：未重复全仓所有长期套件。当前高风险项已由匹配的 03A/04A/04B/API/Chrome/clean-copy/ledger 验证覆盖；完整全仓 cleanroom、跨 04A-04D ledger audit 与独立复审仍由 `M1-04` 数字阶段聚合审查强制执行。

## 11. 批判式剩余风险

1. Windows Chrome 真实 lane 需要显式 sandbox bypass authority；它由 04A session runtime 独立约束且 smoke 明示 trusted isolated lane。04C 只允许精确顶层 runtime flag，不允许其嵌套或 action-step 注入。聚合审查应继续覆盖非 Windows sandbox lane。
2. 真实 Chrome smoke 覆盖 navigation、target read、artifact externalization、owner-loss 与 cleanup；selector/OOPIF/redirect/download 的穷举语义主要由 recording CDP/Fetch + 真实 owner integration tests 覆盖，04D watchdog live 场景应追加中途 target loss/download stall。
3. continuation WAL 是 session-owned、有界且与 03A request 绑定；跨进程并发压力和长期 tombstone 压缩不属于本 slice，需在后续 fault/watchdog 与阶段聚合中验证。
4. legacy backend 仍可由调用方显式选择用于历史兼容，但默认 backend 不会进入 legacy。M1/M2 聚合应继续审计正式 benchmark/config 未显式选择 legacy。

## 12. 完成判定

- 当前 slice 行为、失败路径、动态可达性、断开即失败：完成。
- 当前 slice 最低有效 production：`6,559 / 6,000`，完成。
- 父级最低有效 production：`16,503 / 15,000`，完成。
- 默认主路径、ASK/API continuation、control、event/artifact、真实 Chrome：完成。
- 根来源仓库、vendor/source-pool、黑箱 runtime 依赖：未引入。
- 下一入口：`docs/milestones/M1-runtime-memory-scheduler-fault/slice-04d-01-watchdogs-history-artifacts-foundation.md`。
