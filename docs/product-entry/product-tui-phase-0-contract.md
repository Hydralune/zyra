# Product TUI Phase 0：边界、契约与真实样本

状态：Phase 0 原型已实现；尚未替换默认 `zyra` 入口。

## 1. 冻结边界

- Foundation 当时只消费 `zyra.ui-event/v1`；当前产品 reducer 已由 ADR-002 升级为 `zyra.ui-event/v2`，仍不直接渲染 `runtime.*`。
- 当前 `SessionProjection`、`LineTranscriptRenderer` 和 `commands/interactive.ts` 保持原样，继续承担开发者事件观察职责。
- REST、SSE、snapshot、cursor、control、permission custody、daemon 和 terminal node 继续作为共享基础设施。
- canonical task state 始终属于后端；投影器不创建第二套任务真相。
- 本阶段没有更改 `zyra run` JSONL、退出码或现有命令分发。
- 唯一产品参考为本地 `G:\agent-zoo\codex`。借鉴其 transcript、working status、composer/footer 与快照测试方式，不引入 Codex App Server 协议。

实现入口：

- `apps/cli/src/presentation/events.ts`：`ZyraUiEvent/v1` 类型。
- `apps/cli/src/presentation/projector.ts`：canonical snapshot / raw ingress 到产品事件的纯投影。
- `apps/cli/src/presentation/renderer.ts`：不含 ANSI 的 40～240 列渲染原型和 UI state reducer。
- `apps/cli/test/fixtures/product-tui-real-task.v1.json`：真实物理任务的脱敏 fixture。
- `apps/cli/test/snapshots/product-tui-{80,120}.snap`：首批 golden snapshots。
- `scripts/product_tui_smoke.ts`：真实 daemon 提交与终端最终回答验收入口。

## 2. `ZyraUiEvent/v1`

每个事件包含：

- Foundation schema：`zyra.ui-event/v1`；当前 schema 与准入规则见 `docs/product-tui/adr-002-product-presentation-admission.md`；
- 稳定 `eventId`，可以在 replay、重复帧和恢复后去重；
- 产品语义 `type`；
- 可获得时使用 canonical `occurredAt`，不使用本地当前时间伪造顺序。

第一版事件域包括 session、user/assistant message、activity、tool、permission、workspace change、subagent、task terminal 和 transport recovery。`task.cancelled` 被单独建模，避免把用户取消误报为失败。

`assistant.message.completed.source` 明确区分：

- `stream`：确实从显式 assistant presentation text 重建；
- `canonical_final_answer`：真实后端没有文本正文时，从 task `metadata.final_answer` 进行诚实的最终回答回退。

回退不会生成伪造的 delta。

## 3. canonical 来源矩阵

| 产品事实 | 当前 authoritative 来源 | Phase 0 行为 | 已知缺口 |
|---|---|---|---|
| session/task identity | normalized `TaskProjection` | `session.started` | 无 |
| 用户输入 | `TaskProjection.userGoal` | `user.message` | 多轮消息仍需稳定 message history |
| 执行活动 | `TaskProjection.planNodes` | 按 node identity 投影 activity；UI 聚合显示 | title 仍偏内部，需要后端提供产品 label |
| 助手文本流 | `runtime.text.started/delta/ended` 的明确 presentation text | 只有正文存在才投影；digest/byte count 被忽略 | 当前 mapper 只稳定提供 `stream_id`、`delta_bytes` 和 digest |
| 最终回答 | `TaskProjection.metadata.final_answer` | completed task 时回退为完整 assistant message | 需要与未来 streamed completion 做一致性检查 |
| 工具状态 | typed `runtime.tool.called/progress/succeeded/failed/cancelled` | 只显示 tool name、百分比和结果状态 | 当前真实样本没有这些事件；安全输入/结果摘要仍缺契约 |
| 权限请求 | permission custody API 的 request snapshot | canonical snapshot 覆盖 event fallback；只允许 allow/deny | typed event 只有 permission/action digest，无法展示完整目标和风险 |
| 文件变更 | `metadata.delivery` 的 created/modified/deleted paths | `workspace.changed`；绝对路径先去根显示 | rename、diff、测试结果仍缺独立 canonical 来源 |
| 子代理状态 | typed `runtime.subagent.*` | 聚合为 stable agent/status，不展示拓扑 | 当前真实样本未产生 typed subagent 事件 |
| 任务终态 | `TaskProjection.status/terminal` 与 canonical outcome | completed/failed/cancelled 独立投影 | 不依赖 raw task terminal event |
| transport | typed client/SSE recovery state | reconnecting/recovered | 完整在线 event loop 留给 Phase 2/3 |

## 4. raw event 映射与过滤

产品投影允许解释 raw event 的模块只有 projector；renderer 和 state reducer 不包含任何 `runtime.*` 分支。

| raw 类别 | 产品结果 |
|---|---|
| `runtime.text.*` | 仅在存在显式正文时生成 assistant message |
| `runtime.tool.*` | tool started/updated/completed/failed；不透传参数或 digest |
| `runtime.permission.requested/pending/allowed/denied` | permission requested/resolved；canonical custody snapshot 优先 |
| `runtime.subagent.*` | 聚合 subagent status |
| task snapshot `planNodes` | activity state |
| task snapshot `metadata.delivery` | workspace changes |
| task snapshot terminal status | task completed/failed/cancelled |
| `runtime.agent.message` | 默认丢弃；它不是 assistant answer 契约 |
| node/lease/route/backend dispatch/audit/artifact/system notice | 默认丢弃，保留给 Developer/Web |
| `runtime.node.failed` | 默认丢弃；不能单独升级为 task failure |

投影对输入帧先按 sequence 排序，再按 event identity 去重。相同 snapshot、乱序输入和重复 replay 会生成相同产品事件序列。Phase 1 仍需补齐 generation/gap/snapshot replacement 的在线状态机测试。

## 5. assistant text 与 final answer 一致性

当前 runtime event catalog 声明了 `runtime.text.started/delta/ended`，但 `source-mapper.ts` 的稳定 inline 契约只包含 stream identity、segment、delta byte count 和 content digest。digest 不能还原正文，也不能作为正文显示。

2026-08-31 的真实物理简单任务包含 144 个 ingress frames，其中：

- 57 个 `runtime.agent.message`；
- 25 个 audit finding；
- 21 个 backend dispatch requested；
- 19 个 node updated；
- 12 个 artifact committed；
- 1 个 `runtime.node.failed`，原因为旧 lease 被 resume dispatch supersede；
- 0 个 `runtime.text.*`；
- 0 个 raw `runtime.task.completed`。

task snapshot 本身为 `completed`，且 `metadata.final_answer` 有完整回答。因此 Phase 0 的正确策略是：

1. 不把 `runtime.agent.message` 当回答；
2. 不把 node failure 当 task failure；
3. 不从 summary/digest 猜文本；
4. 用 canonical task status 决定终态；
5. 用 `metadata.final_answer` 生成 `source=canonical_final_answer` 的完整消息。

未来物理 runtime 提供显式正文后，projector 可以生成真实 delta；若 streamed completion 与 canonical final answer 不一致，canonical final answer 必须仍被保留并触发一致性诊断，不能静默覆盖事实。诊断事件将在 Phase 1 定义。

## 6. fixture 与脱敏

fixture 来自本机 daemon 的真实 task/event-ingress 读取，不是为测试合成的任务结果。为使它可提交：

- 所有 task/run/session/node/event/artifact/workspace identity 已替换；
- 删除 digest、receipt、provider metadata、physical location 和能力凭证；
- 保留真实 goal、final answer、plan node 产品字段、event type 统计和证明过滤行为的代表帧；
- 明确记录未观察到的 text/tool/permission typed events。

fixture 不能作为“真实 daemon 验收”替代品。`product-tui:smoke` 才是 Phase 0 的真实入口。

## 7. 验证命令

协议和 golden tests：

```powershell
Set-Location G:\agent-zoo\zyra
.\node_modules\.bin\bun.exe test .\apps\cli\test\product-presentation.test.ts
.\node_modules\.bin\bun.exe run typecheck:cli
```

真实 daemon smoke（会创建并执行一个真实任务）：

```powershell
Set-Location G:\agent-zoo\zyra
.\node_modules\.bin\bun.exe run build:cli
.\node_modules\.bin\bun.exe run product-tui:smoke -- --width=80 --goal="测试，收到请回复"
```

通过条件：终端包含 canonical 完整回答，不包含 `runtime.*`，进程退出码为 0。

## 8. Phase 1 入口条件

在默认入口切换前仍需完成：

- 为 assistant 正文增加真正稳定、持久与 resume 可重建的 presentation contract；
- 将 permission custody snapshot 接入实时 projector，而不是只接受函数输入；
- 定义安全 tool input/result summary；
- 补齐 rename/diff/test result 来源；
- 增加 generation、cursor gap、snapshot replacement 与实时/离线一致性测试；
- 定义 streamed completion 与 final answer divergence diagnostic。
