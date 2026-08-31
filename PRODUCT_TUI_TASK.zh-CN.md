# Zyra 产品级 TUI 重构任务

## 1. 文档状态

- 状态：待实施
- 任务类型：产品入口重构 / 新终端应用
- 项目根目录：`G:\agent-zoo\zyra`
- 主要目标平台：Windows PowerShell；设计与协议不得阻止后续支持 Linux 和 macOS
- 参考产品：Codex CLI、Claude Code
- 参考原则：借鉴产品交互和终端工程经验，不复制 Codex App Server 协议，不把 Codex TUI 整体移植进 Zyra

## 2. 背景

Zyra 当前 CLI 已经能够启动真实任务、连接本地 daemon、消费 canonical SSE 事件、处理 cursor/snapshot 恢复、提交控制命令和权限决定，并注册本地 terminal node。

但是，当前交互界面以运行时事件为中心。它把 `runtime.node.updated`、`runtime.agent.message`、`runtime.audit.finding`、artifact ID、revision 和底层调度状态直接作为主要输出。这适合开发、测试、审计和比赛阶段验证，却不适合作为普通用户的默认入口。

当前实现不应被继续美化为产品 TUI。增加颜色、边框、spinner 或 Markdown 不能改变其事件观察器的本质。产品入口需要以用户对话、可理解的工作进度、权限请求、文件变更和最终回答为中心，重新建立状态模型和渲染层。

## 3. 已确认的产品决策

1. 默认 `zyra` 启动全新的面向用户 TUI。
2. 当前原始事件终端保留为显式开发者模式，例如 `zyra dev` 或 `zyra events`；最终命令名在实现阶段冻结。
3. 产品 TUI 采用 greenfield 方式实现，不以当前 `SessionProjection`、`LineTranscriptRenderer` 或交互循环为设计基础。
4. 复用的是已经验证的后端通信与控制管道，而不是当前 CLI 的交互设计。
5. 不把 Codex TUI 整体复制或 fork 到 Zyra，也不要求 Zyra 模拟完整 Codex App Server 协议。
6. 产品 TUI 不直接解释和渲染全部原始运行时事件。
7. CLI 与 Web 使用同一 canonical task、session、permission、artifact 和 workspace 事实，不建立第二套 Agent runtime 或任务状态。
8. 当前非交互 JSONL、daemon、scenario、Web launcher 等自动化命令保持兼容，除非后续有单独批准的破坏性变更。

## 4. 任务目标

实现一套可以直接交给最终用户使用的 Zyra 终端产品，使用户能够：

- 在清晰的对话界面中输入自然语言任务；
- 看到流式或增量的助手回答；
- 看到经过压缩和解释的执行进度，而不是底层事件洪流；
- 在终端中审查并处理权限请求；
- 查看工具调用、文件变更、测试结果和任务终态；
- 中断、追加、重定向、继续或取消任务；
- 在进程退出或网络断开后恢复 canonical 会话；
- 从 CLI 跳转到同一任务的 Web 看板查看完整拓扑、事件和证据；
- 在需要排障时显式进入开发者事件模式。

## 5. 非目标

本任务不包括：

- 重写 Zyra 调度器、Worker Pool、Runtime、Memory、Artifact 或 Web 后端；
- 让产品 TUI 理解所有内部拓扑、租约、审计和恢复实现细节；
- 逐一复制 Codex CLI 的全部功能、命令和视觉细节；
- 实现 Codex App Server 兼容层；
- 直接暴露模型私有推理过程或内部 chain-of-thought；
- 删除当前开发者事件终端；
- 改变 `zyra run` 的机器可读 JSONL 契约；
- 在首个版本中追求插件市场、主题市场或远程协作等非核心能力。

## 6. 核心架构

### 6.1 目标数据流

```text
Zyra Runtime / Scheduler / Worker / Permission
                      │
                      │ canonical task API + raw runtime events
                      ▼
          Zyra Client / Transport Infrastructure
                      │
                      │ REST、SSE、snapshot、cursor、control
                      ▼
              Presentation Projection
                      │
                      │ versioned ZyraUiEvent
                      ▼
               Product TUI State Store
                      │
                      ▼
          Chat / Activity / Tool / Approval / Diff UI

raw runtime events ───────────────────────────────► Developer CLI
canonical task ──────────────────────────────────► Web Dashboard
```

### 6.2 边界原则

- Transport 负责可靠收发，不决定用户看到什么。
- Presentation Projection 负责把内部事实转换为稳定、低噪声的产品语义。
- TUI 只消费产品语义，不按 `runtime.*` 前缀临时猜测展示方式。
- Developer CLI 可以继续消费完整原始事件流。
- 后端增加新内部事件时，不应迫使产品 TUI修改。
- 影响用户行为的 presentation schema 必须版本化并有兼容测试。

## 7. Presentation Protocol

### 7.1 必须新增稳定的用户级事件模型

建议的第一版事件集合如下；命名可以调整，但职责不能退化为原始事件透传：

```ts
type ZyraUiEvent =
  | { type: "session.started"; sessionId: string; taskId?: string }
  | { type: "user.message"; messageId: string; text: string }
  | { type: "assistant.message.started"; messageId: string }
  | { type: "assistant.message.delta"; messageId: string; text: string }
  | { type: "assistant.message.completed"; messageId: string; text: string }
  | { type: "activity.started"; activityId: string; label: string }
  | { type: "activity.updated"; activityId: string; label: string }
  | { type: "activity.completed"; activityId: string; outcome?: string }
  | { type: "tool.started"; toolCallId: string; name: string; summary: string }
  | { type: "tool.updated"; toolCallId: string; summary: string }
  | { type: "tool.completed"; toolCallId: string; summary: string }
  | { type: "tool.failed"; toolCallId: string; message: string }
  | { type: "permission.requested"; request: UiPermissionRequest }
  | { type: "permission.resolved"; requestId: string; decision: string }
  | { type: "workspace.changed"; changes: readonly UiFileChange[] }
  | { type: "subagent.updated"; agentId: string; label: string; status: string }
  | { type: "task.completed"; taskId: string; finalAnswer: string }
  | { type: "task.failed"; taskId: string; message: string; recovery?: string }
  | { type: "transport.reconnecting"; attempt: number }
  | { type: "transport.recovered" }
```

### 7.2 投影规则

- 多个内部事件可以合并为一个 `activity.*`。
- 无用户价值的 heartbeat、lease、route、audit 和 projector 维护事件默认不进入产品流。
- 内部失败不等于任务失败。只有影响用户任务或需要用户行动时才显示错误。
- `runtime.agent.message` 不能无条件视为助手回答，因为其中包含系统通知、拓扑消息和 Worker 状态。
- 产品回答必须使用明确的消息角色、message ID 和正文，而不是从 summary 或 digest 猜测。
- 若真实后端暂未产生文本 delta，任务完成时必须从 canonical `metadata.final_answer` 获取最终回答作为兼容回退。
- 回退只保证最终文本，不得伪造成实时流式输出。
- Presentation Projection 必须幂等，能够在 snapshot、delta、SSE 重连和重复帧下重建相同 UI 状态。
- 用户可见错误必须提供可操作说明；原始错误码可作为折叠详情保留。

### 7.3 后端契约缺口

在实现 TUI 前必须核实并补齐：

- 助手正文的稳定来源；
- `assistant.message.started/delta/completed` 的真实产生路径；
- 工具名称、用户级输入摘要和结果摘要；
- 权限请求的动作、目标、风险、有效期和允许的决策；
- 文件变更的 canonical 路径、类型和 diff 来源；
- task failure 与内部 node/lease failure 的严重性区分；
- resume 后消息与活动的确定性重建；
- final answer 与流式 message completion 的一致性校验。

## 8. 产品 TUI 功能范围

### 8.1 首屏与会话

- 显示 Zyra 名称、版本、当前 workspace、连接状态和当前模式。
- 支持无初始 prompt 启动，也支持 `zyra "<goal>"` 直接提交任务。
- 明确区分新会话、恢复会话和已完成任务。
- daemon 自动启动或连接失败时给出清晰、可执行的提示。

### 8.2 输入区

- 支持单行和多行输入；快捷键必须在界面中可发现。
- 支持粘贴保护、草稿、历史、外部编辑器和取消当前输入。
- 任务运行期间允许追加、排队、重定向或中断，具体行为映射到现有 canonical command API。
- `Ctrl+C`、`Esc`、EOF 和退出命令的行为必须明确且可测试。

### 8.3 对话与流式输出

- 用户输入和助手回复作为一级内容显示。
- Markdown、代码块、列表和链接在常见终端宽度下正确换行。
- 流式文本不得闪烁、重复、乱序或因 resize 损坏。
- 任务结束后必须在终端显示完整最终回答。
- 不显示内部 chain-of-thought；可显示经过整理的工作进度和简要说明。

### 8.4 执行活动

- 默认显示少量、稳定、可理解的活动，例如“正在检查项目”“正在运行测试”。
- 工具调用默认展示名称和安全摘要，不直接倾倒完整参数或大型输出。
- 活动可折叠或展开，详细视图仍需脱敏。
- 多代理协作以聚合状态显示；完整拓扑和证据优先交给 Web 看板。

### 8.5 权限

- 权限请求必须打断到可见位置，不能被日志淹没。
- 显示动作、目标、原因、风险、有效期和作用域。
- 支持由后端允许的“仅本次允许”“会话内允许”“拒绝”等决策。
- 所有决策通过现有 permission custody 和 canonical receipt 提交。
- custody 缺失、过期或校验失败时 fail closed，并提供恢复方法。

### 8.6 文件变更与结果

- 显示新增、修改、删除和重命名文件的摘要。
- 支持查看可读 diff；超大 diff 必须有界并可转到 Web 或本地文件查看。
- 显示测试命令、通过/失败和未运行项。
- 最终摘要包括任务状态、变更文件、验证结果和必要的恢复提示。

### 8.7 会话控制与恢复

- 支持 resume、cancel、continue、interrupt、redirect 和队列操作。
- SSE 断线时显示简洁的重连状态，不重复历史内容。
- cursor/generation 不可恢复时使用 canonical snapshot 重建。
- 退出 TUI 不隐式取消仍在运行的远端任务，除非用户明确选择取消。

### 8.8 Web 协同

- TUI 可以打开当前 task 对应的 Web 路由。
- Web 用于完整拓扑、原始事件、artifact、审计、证据和复杂治理。
- CLI 和 Web 显示的任务终态、最终回答和权限决定必须一致。

## 9. 命令面与兼容策略

目标命令面：

```text
zyra                              product TUI
zyra "<goal>"                     product TUI with initial goal
zyra resume <task|session>        resume in product TUI
zyra dev [<goal>]                 developer event interface
zyra events <task|session>        observe raw canonical events
zyra run <goal | -f file | stdin> non-interactive JSONL execution
zyra ls                           list canonical tasks and sessions
zyra scenario <action> [...]      scenario lifecycle
zyra ui [--task <id>]             open Web dashboard
zyra daemon <start|stop|status>   daemon supervision
```

兼容要求：

- `zyra run` stdout 继续保持逐行合法 JSON object。
- 非交互命令不得初始化 alternate screen 或输出 TUI 控制序列。
- 退出码 `0..5` 的既有含义保持不变，除非另有版本化迁移方案。
- 脚本和 CI 不应因默认交互 TUI 的引入而改变行为。
- 当前交互事件模式迁移到开发者命令时，应提供至少一个版本周期的提示或别名。

## 10. 实现边界建议

### 10.1 可复用基础设施

以下能力应优先复用或提炼为共享 client package：

- `apps/cli/src/api.ts`
- `apps/cli/src/control/*`
- `apps/cli/src/daemon.ts`
- `apps/cli/src/terminal/*`
- `apps/cli/src/workspace-transfer.ts`
- `packages/core/typed-api-client`
- `packages/commands`

复用不等于禁止重构。若这些模块含有开发者 UI 假设，应先抽离 transport/control 接口，再由两个 CLI 入口共同调用。

### 10.2 不作为产品基础的模块

- `apps/cli/src/session/projection.ts`
- `apps/cli/src/render/line-renderer.ts`
- 当前 `apps/cli/src/commands/interactive.ts` 的事件驱动展示循环
- 当前以普通逐行事件输出为目标的 prompt/render 组合

这些模块保留服务开发者模式；产品 TUI 不继承其状态模型。

### 10.3 建议目录形态

最终形态由实现阶段决定，但职责应接近：

```text
apps/
  cli/                         # 命令分发和发布入口
  tui/                         # 产品 TUI；也可暂时位于 apps/cli/src/tui

packages/
  cli-client/                  # 可选：共享 REST/SSE/control client
  presentation/                # raw canonical facts -> ZyraUiEvent

apps/cli/src/dev/              # 当前开发者事件界面
```

禁止为了目录整齐提前大规模移动代码。先建立清晰接口，再以最小可验证步骤迁移。

## 11. 技术栈决策门

产品 TUI 可以继续使用 TypeScript/Bun，也可以实现为独立 Rust/ratatui 前端，但必须先做短期验证，不得仅凭偏好选择。

默认优先评估 TypeScript/Bun，因为现有 API、control、daemon 和发布入口均为 TypeScript，能够减少跨语言协议和打包成本。

只有在以下能力无法达到要求时，才升级为 Rust TUI：

- Windows 终端重绘和输入稳定性；
- 长 transcript 性能；
- Unicode 宽度和 resize reflow；
- alternate screen / inline 模式；
- 可测试的组件和 snapshot 渲染；
- 单文件或稳定原生发布需求。

无论选择哪种语言，都不整体依赖 `codex-rs/tui`。如复制少量 Apache-2.0 代码，必须进行来源审查并遵守 LICENSE/NOTICE 要求。

## 12. 实施阶段

### Phase 0：契约和原型验证

- 冻结产品 TUI 与开发者 CLI 的边界。
- 建立真实任务事件样本和脱敏 fixture。
- 确认 assistant text、tool、permission、file change、completion 的 canonical 来源。
- 定义 `ZyraUiEvent/v1` 和 projector 行为。
- 用窄终端、宽终端、中文、代码块和断线恢复做最小技术原型。
- 完成 TypeScript/Bun 与 Rust/ratatui 的技术栈决策记录。

完成条件：不依赖原始事件名也能重建一个真实任务的用户对话、进度、权限和终态。

### Phase 1：Presentation Projection

- 实现 raw event/task snapshot 到 `ZyraUiEvent/v1` 的确定性投影。
- 增加 final answer fallback。
- 过滤内部噪声并建立严重性规则。
- 为 snapshot、delta、重复、gap、resume 和失败恢复增加契约测试。
- Web 与 CLI 可选择共用同一 projection package 或同一后端 projection endpoint。

完成条件：同一 canonical task 在实时消费和离线重放下得到一致的产品事件序列。

### Phase 2：TUI Shell

- 实现终端初始化、恢复、事件循环和退出保护。
- 实现 header、transcript、composer、footer/status。
- 支持 resize、滚动、follow、复制、粘贴和基本快捷键。
- 支持 Markdown、代码块和流式消息。
- 建立 snapshot/golden 测试。

完成条件：用户能够启动 `zyra`、提交普通对话任务并看到完整最终回答，全程不出现原始事件洪流。

### Phase 3：任务活动与控制

- 接入工具活动、任务进度和多代理摘要。
- 接入 cancel、continue、interrupt、redirect 和队列控制。
- 接入重连、snapshot 重建和 resume。
- 接入当前任务的 Web 跳转。

完成条件：长任务在运行、断线、恢复和完成时均保持一致、可理解的 UI 状态。

### Phase 4：权限、文件与结果

- 实现权限弹层和 canonical receipt 提交。
- 实现文件变更摘要和有界 diff。
- 实现测试结果和最终任务摘要。
- 覆盖 deny、expiry、custody loss、tool failure 和 task failure。

完成条件：涉及文件修改和权限请求的真实任务可以仅通过产品 TUI 安全完成。

### Phase 5：开发者模式迁移与产品化

- 将当前事件终端迁移到 `zyra dev/events`。
- 保持 `run`、`ls`、`scenario`、`ui`、`daemon` 兼容。
- 完成 Windows 主路径验证和可获得平台的兼容验证。
- 更新 README、Quickstart、启动文档和演示流程。
- 完成性能、脱敏、故障恢复和发布验证。

完成条件：默认入口为产品 TUI，开发者入口仍可完整观察底层事实，自动化接口无回归。

## 13. 验收场景

至少使用真实 daemon 和真实任务覆盖以下场景，不得仅以 mock 作为最终验收：

1. 简单对话：输入“收到请回复”，终端直接显示回答，不显示百余条内部事件。
2. 代码检查：显示少量可理解活动和最终分析结果。
3. 文件修改：显示变更文件、diff 摘要和验证结果。
4. 权限请求：终端展示风险与操作选项，允许或拒绝后状态正确收敛。
5. 长任务：持续运行时 UI 不无限刷屏，活动状态可更新。
6. 多代理任务：显示聚合进度，Web 可查看完整拓扑。
7. SSE 断线：显示重连状态，恢复后消息不丢失、不重复。
8. CLI 退出与恢复：退出 listener 后任务继续，`resume` 恢复同一 canonical task。
9. 内部节点失败但任务恢复：不误报整个任务失败。
10. 任务真实失败：显示明确原因、影响和可执行恢复建议。
11. 非交互执行：`zyra run` 继续输出合法 JSONL，无 TUI 控制字符。
12. 开发者模式：仍可查看完整 sequence、event type、artifact 和 revision。

## 14. 质量要求

### 14.1 正确性

- canonical task state 始终由后端拥有。
- UI 状态可以从 snapshot 和事件重建。
- 不因 UI 退出隐式篡改任务终态。
- 不把 node/lease/tool 局部失败误判为 task failure。
- 最终回答与 canonical final answer 一致。

### 14.2 终端稳定性

- 支持至少 80、120 列和 resize。
- 中文、emoji、组合字符和 ANSI 输入不得破坏布局。
- Ctrl+C、异常退出和 panic 后恢复终端模式。
- 大型输出、长单行和持续流式内容必须有界。
- inline/alternate-screen 策略需要明确并有 Windows 实测。

### 14.3 安全与隐私

- token、custody secret、capability URL、内部绝对根路径和 credential 不进入公开 transcript。
- 工具参数和输出按安全策略摘要化。
- 权限请求 fail closed。
- Web 跳转 URL 不携带不应暴露的长期秘密。

### 14.4 测试

- Presentation Projection 使用确定性 fixture 和重放测试。
- TUI 使用 snapshot/golden 测试覆盖可见变化。
- 输入、resize、滚动、流式 reflow 和权限弹层有组件测试。
- 使用真实 daemon 做端到端 smoke 和失败路径测试。
- 发布产物在 Node/Bun 或最终选定 runtime 下运行验证。

## 15. 可观测性分层

产品 TUI 的详细程度建议分为：

- 默认：用户消息、助手回复、少量活动、权限、变更和最终结果；
- 展开详情：工具摘要、子代理摘要、可操作错误和 artifact 引用；
- Developer：完整 runtime event、sequence、revision、cursor、拓扑和审计信息；
- Web：完整任务图、证据、artifact、治理和历史检索。

产品模式不得因为 `--verbose` 就无界倾倒所有内部事件。完整原始流属于 developer mode。

## 16. 风险

### 高风险

- 真实物理运行未稳定产生 assistant text delta；
- `runtime.agent.message` 混入系统、拓扑和模型消息；
- 权限模型比常规 yes/no 更复杂；
- 当前 task failure 与局部 node/lease failure 在展示语义上可能混淆；
- 直接 fork Codex TUI 会引入大量内部协议和长期同步成本。

### 中风险

- Windows 终端输入、Unicode、resize 和 scrollback 差异；
- resume 后流式内容合并与去重；
- 大型 diff、工具输出和长任务内存占用；
- 产品 CLI 与 Web 使用不同投影导致展示不一致。

### 风险控制

- 先冻结 presentation contract，再实现完整 UI；
- 用真实事件重放驱动开发；
- 原始事件与产品事件严格分层；
- 分阶段提交，每阶段必须有可运行入口和回归测试；
- 不在首个阶段同时重写后端执行架构。

## 17. 粗略工作量

以下估算以一名熟悉 TypeScript/Rust、终端 UI 和 Zyra 后端接口的工程师为基准：

| 目标 | 粗略工作量 |
|---|---:|
| 演示版：输入、简洁状态、最终回答、隐藏内部事件 | 1～2 周 |
| 可用 MVP：流式回答、权限、工具状态、取消、恢复、Markdown | 4～8 周 |
| 产品级：diff、历史、稳定滚动、Unicode、Windows 兼容、完整测试 | 2～4 个月 |
| 接近当前 Codex CLI 的完整成熟度 | 多人团队，4～8 个月以上 |

估算不包含重写 Zyra 后端。如果 assistant presentation contract 需要跨多个 Runtime 补齐，Phase 0 和 Phase 1 的工作量需要单独上调。

## 18. Definition of Done

只有同时满足以下条件，任务才算完成：

- 默认 `zyra` 是面向用户的产品 TUI；
- 当前事件终端通过显式 developer 命令保留；
- 普通用户路径不展示原始 runtime event 洪流；
- 助手最终回答始终可见，支持真实增量流或诚实的最终回答回退；
- 权限、工具、文件变更、任务失败和恢复都有产品级交互；
- CLI 与 Web 对同一 canonical task 的关键事实一致；
- `zyra run`、daemon、scenario、ui 和退出码契约无回归；
- Windows 真实端到端场景通过；
- 关键 UI 有 snapshot/golden 测试，关键协议有重放与恢复测试；
- 文档、帮助、启动说明和演示脚本更新完成；
- 未把 Codex 内部协议或另一套 Agent runtime 引入 Zyra。

## 19. 实施前必须产出的第一批交付物

1. `ZyraUiEvent/v1` schema 草案。
2. 当前真实任务的脱敏 event/task fixture。
3. raw event 到 presentation event 的映射与过滤规则。
4. assistant text 产生路径和 final answer 一致性说明。
5. TypeScript/Bun 与 Rust/ratatui 的短期技术验证报告。
6. 一张 80 列和一张 120 列的 TUI snapshot 原型。
7. 第一阶段端到端验收脚本：提交简单任务并在终端显示最终回答。

在以上交付物完成前，不开始大规模复制界面组件或重排现有 CLI 目录。
