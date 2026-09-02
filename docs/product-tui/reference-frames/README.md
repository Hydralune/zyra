# Codex / Zyra 同尺寸体验对照

本目录不是“截图证明功能存在”，而是 Phase I～K 的产品审查输入。Zyra 帧由真实 renderer 在 80×24 与 120×40 终端尺寸下确定性生成，共 13 个场景、26 份帧；Codex 侧以本地仓库 commit `343074d4207d572809bd8cea15f4be1d09d98e0b` 中的 VT100/snapshot 为唯一参考，不混入 Claude Code 或其他 CLI 的设计。精确文件、原生尺寸与逐场景结论见 `docs/product-tui/codex-frame-review-20260902.md`。

生成 Zyra 帧：

```powershell
npx --yes bun@1.2.15 scripts/product-tui/generate_experience_frames.ts
```

## 场景映射

| 场景 | Zyra 帧 | Codex 本地证据 | 审查重点 |
|---|---|---|---|
| 冷启动 / 空闲 composer | `zyra/cold-start-*`; `zyra/composer-input-*` | `codex-rs/tui/src/history_cell/snapshots/*session_header*`; `bottom_pane/*empty.snap` | 会话头随历史滚动；输入是唯一视觉锚点 |
| 输入与命令补全 | `zyra/composer-input-*`; `zyra/command-completion-*` | `bottom_pane/chat_composer/*empty.snap`; `*slash_popup_res.snap`; `bottom_pane/*command_popup*` | popup 紧贴 composer，Esc/Tab/Enter 可发现 |
| 运行中工具 | `zyra/running-tool-*` | `chatwidget/*exec_and_status_layout*` | 单一当前活动、耗时、输出渐进披露 |
| 计划更新 | `zyra/plan-update-*` | `history_cell/plans.rs` 与 plan snapshots | 完成/当前/待办层级，不显示 graph/revision 实现术语 |
| 权限摘要与审批 | `zyra/permission-summary-*`; `zyra/permission-approval-*` | `bottom_pane/*approval_overlay_permissions_prompt.snap` | 问题—说明—选项结构；默认不显示 request identity |
| 结构化问题与回答记录 | `zyra/structured-question-*`; `zyra/structured-question-answered-*` | `bottom_pane/request_user_input` snapshots；`history_cell/request_user_input.rs` | 自动抢占、问题—选项—自由回答、草稿保留；回答后形成 history cell，不显示 request identity |
| 文件 diff | `zyra/diff-*` | `tui/src/snapshots/codex_tui__diff_render__tests__diff_gallery_80x24.snap` | 文件标题、hunk、增删色彩、滚动 footer |
| 完成 | `zyra/completed-*` | `history_cell` final answer / exec snapshots | 最终回答为最后一级内容；文件和验证可审查但不喧宾夺主 |
| 失败恢复 | `zyra/failed-recovery-*` | error history cells、status snapshots | 先说明影响和下一步，再在详情中展示技术信息 |
| 恢复选择器 | `zyra/resume-picker-*` | `tui/src/snapshots/codex_tui__resume_picker__tests__resume_picker_screen.snap` | 标题/目录/状态优先，不要求复制 task/session ID |

## 当前审查结论

- 主界面已经采用 Codex 的“会话头 + 时间线 history cell + 单一当前活动 + composer/footer”结构，不再是固定状态仪表盘。
- picker、pager、approval 使用统一的无框语法；权限会自动抢占输入并保留草稿；diff pager 对新增、删除和 hunk 使用语义颜色。
- 仍需人工关闭的发布门：Windows Terminal 中文 IME 候选窗，以及两名未参与实现者的 Codex/Zyra 同任务盲测。
- provider 主动发起的通用结构化用户问题已通过独立 `request_user_input` provider tool、SQLite canonical request/revision/answer owner、typed API、presentation 与 CLI picker 形成闭环，不借用 MCP elicitation。真实 DeepSeek provider 在问题 pending 时重启 daemon、CLI 重附着、单次回答和同一 tool continuation 已通过；task/run/request 见 frame review 与 release evidence。
- assistant durable end 现在会封存 message identity；迟到 live delta 不会在最终回答后重复拼接。task terminal 同时取消仍绑定的 picker/overlay，canonical final 保持为最后一个一级内容。

对照时必须查看同一尺寸的一对帧，并记录视觉焦点、内部术语、完成动作所需按键与详情展开次数；不能只比较字符是否相同。
