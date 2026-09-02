# Codex 产品体验自查（2026-09-02）

## 结论

本轮自查不能给出“全部完成”的结论。

- 第一个问题——当前功能是否满足任务书：Phase I 的渲染与视觉语法已经实现；Phase J 除通用结构化用户问题外已经形成用户闭环；Phase K 的确定性同尺寸帧已经建立，但外部用户和人工 IME 门不能由实现者代签。
- 第二个问题——任务书是否足以约束出接近 Codex 的 CLI：修订后的任务书已经把 Codex 的信息架构、history cell、composer、活动焦点、审批、picker/pager、颜色、文案和信息预算列为硬基线，足以防止退回“功能看板”。它仍要求真实用户对照，因此 snapshot 不能替代最终体验结论。
- 当前界面已经从开发者事件面板变为 Codex 式对话 Agent TUI，但在 provider 能主动提出可恢复的结构化问题之前，仍不能声称达到 Codex 的完整核心交互面。

## 唯一参考证据

实现只以 `G:\agent-zoo\codex` 为产品参考，主要证据包括：

- `codex-rs/tui/styles.md`
- `codex-rs/tui/src/history_cell/` 及其 session header、plan、exec、final answer snapshots
- `codex-rs/tui/src/bottom_pane/` 的 composer、command popup、approval snapshots
- `codex-rs/tui/src/resume_picker.rs` 与 resume picker snapshots
- `codex-rs/tui/src/diff_render.rs` 与 80×24 diff gallery snapshot
- `codex-rs/tui/src/history_cell/request_user_input.rs`、`chatwidget/protocol_requests.rs` 和 pending interactive replay

## 核心场景审查

| 场景 | 当前结果 | 证据/剩余项 |
|---|---|---|
| 冷启动与 composer | 对齐 | 紧凑会话头随历史滚动；composer 是视觉锚点；默认无内部 task/session ID |
| 输入、光标、completion | 对齐实现 | grapheme 光标进入帧模型；completion 紧贴 composer；人工 Windows IME 未签字 |
| 运行中工具与计划 | 对齐 | 时间线 history cell + 单一当前活动；高频更新原地覆盖 |
| 权限审批 | 对齐 | 自动抢占、草稿保留、问题—选项结构、Esc fail closed；内部 ID 隐藏 |
| diff/验证/工具详情 | 对齐 | 摘要留在时间线，完整内容进入统一 pager；diff 有语义增删/hunk 色彩 |
| 完成与失败 | 对齐 | 最终回答保持最后一级内容；错误先说明影响与恢复动作 |
| 上下文用量 | 对齐 | 只投影 canonical 真实用量；无数据时不伪造百分比 |
| 会话恢复与命名 | 对齐 | canonical `/rename`、标题优先 resume picker、goal 回退，不存在本地别名真相 |
| Agent 主动结构化提问 | 未对齐 | 缺 provider tool → canonical pending/answer/continuation → CLI 自动抢占闭环 |

## 确定性帧

`docs/product-tui/reference-frames/zyra/` 包含 10 个场景在 80×24 与 120×40 下的 20 个渲染帧：冷启动、composer 输入、运行工具、计划、权限摘要、权限审批、diff、完成、失败恢复、resume picker。来源映射见同目录 `README.md`。

这些帧证明信息层级和产品语法可以被稳定复现，不证明真实 IME、真实用户可发现性或 provider 主动问题续跑已经通过。

## 未关闭项

1. 通用 `request_user_input` 不能借用 assistant 文本或 MCP 专用 elicitation 假装实现；需要正式 provider 工具、durable identity/revision、等待与恢复语义、answer receipt、presentation 事件和 CLI picker。
2. Windows Terminal 中文 IME 候选窗、组合态与 exact echo 需要人工执行现有 gate。
3. 两名未参与实现者需要在没有开发者讲解的情况下完成 Codex/Zyra 同类任务。
4. 本轮重构后的真实 daemon/provider 与 clean-install/release 门仍需重新归档；CLI/API 与 ConPTY 回归已通过。

## 本轮验证

- `bun test ./apps/cli/test`：188 passed，0 failed。
- `bun test ./packages/commands/test ./packages/core/typed-api-client/src`：35 passed，0 failed。
- `bun run typecheck:cli`：通过。
- `bun run build:cli`：通过，生成 `apps/cli/dist/zyra.js`。
- session API 与 canonical `/rename` 集成：3 passed。
- Windows ConPTY：1,000 次 resize，startup 623.982 ms，exit 274.641 ms，无 alternate screen，terminal restore 通过。
- 真实 built CLI：完成首次引导、`?` 帮助、`/status` 与 `/exit` 交互检查。由于代理沙箱不允许写用户 profile，首次引导持久化在该检查中显示了已脱敏的 EPERM；workspace-local state 的 ConPTY 门已通过，正常用户 PowerShell 不受这个代理沙箱限制。
