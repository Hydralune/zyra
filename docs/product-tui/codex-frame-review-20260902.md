# Codex → Zyra frame-by-frame 产品审查（2026-09-02）

## 证据边界

- 唯一参考仓库：`G:\agent-zoo\codex`
- 参考 commit：`343074d4207d572809bd8cea15f4be1d09d98e0b`
- Zyra 帧：`docs/product-tui/reference-frames/zyra/` 下 13 个场景、80×24 与 120×40 共 26 份 renderer 输出。
- Codex 帧：下表绑定 commit 内的原始 insta/VT100 snapshot。只有 `diff_gallery` 原生同时提供 80×24 和 120×40；其他场景保留 Codex fixture 的原生尺寸，不用手工补空格制造伪同尺寸截图。
- 样式依据：`codex-rs/tui/styles.md`。结构依据：`history_cell`、`chatwidget`、`bottom_pane`、`resume_picker` 与 `diff_render`。

本审查比较的是信息架构、焦点、操作路径和终态语义，不要求中英文字符逐字相同。Zyra 的中文本地化属于允许差异；task/session/request/artifact identity、schema、revision、backend worker 名进入默认帧则属于缺陷。

## 精确 Codex 证据索引

下表路径均相对于 `G:\agent-zoo\codex\codex-rs\tui\src`。

| 证据代号 | 精确 snapshot/source | 原生画布或表达式 | 可观察不变量 |
|---|---|---|---|
| C-HEADER | `history_cell/snapshots/codex_tui__history_cell__tests__session_header_indicates_yolo_mode.snap` | history cell，内容宽约 41 列 | 紧凑会话头属于 transcript；只显示产品、版本、模型、目录、权限 |
| C-COMPOSER | `bottom_pane/snapshots/codex_tui__bottom_pane__chat_composer__tests__empty.snap` | 100×10 `terminal.backend()` | `›` 是输入锚点；footer 左快捷键、右上下文 |
| C-BOTTOM | `bottom_pane/snapshots/codex_tui__bottom_pane__tests__status_and_composer_fill_height_without_bottom_padding.snap` | 30×6 `render_snapshot(&pane, area)` | activity 在 composer 上方；composer/footer 占 bottom pane 底部 |
| C-COMMAND | `bottom_pane/snapshots/codex_tui__bottom_pane__chat_composer__tests__slash_popup_res.snap` | 60×6 `terminal.backend()` | completion 与 composer 相邻，当前输入和候选同屏 |
| C-EXEC | `chatwidget/snapshots/codex_tui__chatwidget__tests__chatwidget_exec_and_status_layout_vt100_snapshot.snap` | VT100 final screen | history cell 与单一当前 activity 分离；中断提示就在状态行 |
| C-PLAN | `history_cell/snapshots/codex_tui__history_cell__tests__plan_update_with_note_and_wrapping_snapshot.snap` | source-native history cell | `Updated Plan`、变更说明、完成/待办步骤形成一个层级单元 |
| C-APPROVAL | `bottom_pane/snapshots/codex_tui__bottom_pane__approval_overlay__tests__approval_overlay_permissions_prompt.snap` | 120 列 overlay lines | 问题、原因、规则、编号选项、Enter/Esc footer |
| C-APPROVAL-HISTORY | `chatwidget/tests/snapshots/codex_tui__chatwidget__tests__approval_requests__exec_approval_history_decision_approved_short.snap` | VT100 final screen | 决策完成后变为 history，不保留可提交 modal |
| C-QUESTION | `bottom_pane/request_user_input/snapshots/codex_tui__bottom_pane__request_user_input__tests__request_user_input_options.snap` | 120 列 overlay area | 问题进度、互斥选项、说明、notes、submit/interrupt 同屏 |
| C-QUESTION-HISTORY | `history_cell/request_user_input.rs` | `RequestUserInputCell` 实现与单元测试 | answered request 形成紧凑、不可再次提交的 history cell |
| C-DIFF-80 | `snapshots/codex_tui__diff_render__tests__diff_gallery_80x24.snap` | 80×24 `terminal.backend()` | 文件摘要、文件标题、行号和增删层级 |
| C-DIFF-120 | `snapshots/codex_tui__diff_render__tests__diff_gallery_120x40.snap` | 120×40 `terminal.backend()` | 同上，并证明宽屏/Unicode 渲染 |
| C-FINAL | `chatwidget/snapshots/codex_tui__chatwidget__tests__single_line_final_answer_hides_working_status.snap` | VT100 final screen | final 出现后 working 消失；回答后直接回到 composer |
| C-STREAM-FINAL | `chatwidget/snapshots/codex_tui__chatwidget__tests__deltas_then_same_final_message_are_rendered_snapshot.snap` | combined history output | delta 与相同 final 只渲染一次 `• Here is the result.` |
| C-ERROR | `history_cell/snapshots/codex_tui__history_cell__tests__cyber_policy_error_event_narrow_snapshot.snap` | narrow history cell | 一级先说用户影响，二级给解释和恢复入口 |
| C-RESUME | `snapshots/codex_tui__resume_picker__tests__resume_picker_screen.snap` | source-native picker screen | 标题、搜索、可理解列与完整 footer；无需输入 thread ID |

## 13 个场景逐帧结论

| Zyra 场景（两种尺寸） | Codex 证据 | 焦点与下一步 | 默认内部术语 | 结构性差异与处置 | 判定 |
|---|---|---|---:|---|---|
| `cold-start-*` | C-HEADER、C-COMPOSER | 焦点为 `›` composer；`?` 发现帮助 | 0 | Zyra 中文化并保留相同 header 字段；header 随 transcript 滚动 | 关闭 |
| `composer-input-*` | C-COMPOSER、C-BOTTOM | 草稿和真实 grapheme 光标；Enter 提交 | 0 | Zyra footer 显示模型/模式，仅在 canonical 数据存在时显示上下文 | 关闭 |
| `command-completion-*` | C-COMMAND | popup 紧贴 composer；上下选择，Tab/Enter 接受 | 0 | Zyra 描述中文化；命令集合按 Zyra 已实现能力过滤，不显示空壳项 | 关闭 |
| `running-tool-*` | C-EXEC、C-BOTTOM | 唯一焦点为当前工具；Esc 中断，`/tools` 看详情 | 0 | Zyra 可补充多代理数量，但不显示代理 ID/拓扑/worker 名 | 关闭 |
| `plan-update-*` | C-PLAN | 计划说明和完成/当前/待办同属一 cell | 0 | Zyra 使用 `✔/→/□`；固定后端阶段名在 presentation 边界中文化 | 关闭 |
| `permission-summary-*` | C-APPROVAL-HISTORY | 已完成决定是历史事实；无活动选择器 | 0 | Zyra scope 中文化；request/revision/custody 不进入正文 | 关闭 |
| `permission-approval-*` | C-APPROVAL | 当前选项是唯一焦点；Enter 确认，Esc 安全拒绝 | 0 | Zyra 选项按 canonical policy 提供；task terminal 会取消 overlay | 关闭 |
| `structured-question-*` | C-QUESTION | 问题进度、选项和自由回答连续；Enter 提交 | 0 | Zyra 支持 1～3 问及 stable queue；daemon restart 后仍绑定原请求 | 关闭 |
| `structured-question-answered-*` | C-QUESTION-HISTORY | 回答成为 history；下一焦点回到当前任务/composer | 0 | Zyra 保留问题和所选答案，不显示 request/tool-call identity | 关闭 |
| `diff-*` | C-DIFF-80、C-DIFF-120 | 文件标题/hunk/增删为焦点；PageUp/PageDown，Esc 返回 | 0 | 两侧均有精确同尺寸直接证据；Zyra 使用统一 pager footer | 关闭 |
| `completed-*` | C-FINAL、C-STREAM-FINAL | 唯一 final 是最后一级内容；随后为新输入 composer | 0 | Zyra 的文件/验证摘要位于 final 前；durable end 拒绝迟到 live delta | 关闭 |
| `failed-recovery-*` | C-ERROR | 先读影响和下一步；技术码只在详情 | 0 | Zyra 区分局部失败和 task 失败，提供 `/status`/`/doctor` 等恢复动作 | 关闭 |
| `resume-picker-*` | C-RESUME | 标题/目录/状态帮助选择；Enter 恢复，Esc 返回 | 0 | Zyra 没有 Codex 的 branch 列时不伪造；canonical session ID 仅作隐藏绑定 | 关闭 |

## 本轮发现并关闭的组合缺陷

1. 短 transcript 曾让 composer 跟在内容后方而不是贴底。renderer 现按 viewport 高度在 transcript 与 bottom pane 之间分配空行；80×24、120×40 和真实 resize 都检查物理行数。
2. provider question/permission picker 曾可能在 task terminal 后继续等待输入。shell、picker 和 pager 现共享 abort signal；terminal settlement 会结束 overlay 并恢复 composer。
3. canonical final 之后曾追加本地“本轮已收敛”通知，使 final 不再是最后一级内容。该通知已移除，canonical final 复用 stream message identity 并移到时间线末尾。
4. 真实 SSE 出现 `SQL` live delta、durable `SQLite` end、迟到 `ite` live delta 的顺序，旧实现显示 `SQLiteite`。projection 现在以 assistant identity 建立 settled fence，durable end 和 snapshot restore 后拒绝迟到 live delta。
5. 中文主界面曾暴露固定英文 `Task execution / Plan / Route / Execute / Verify / Finalize`。这些 canonical 系统标签在 presentation 边界精确本地化，用户/模型自写英文不被改写。

## 尚不能由本审查关闭的门

- 两名未参与实现者在相同尺寸终端完成 Codex/Zyra 同类任务的盲测；需记录迷失点、误操作、按键数和恢复耗时。
- Windows Terminal 真实中文 IME 候选窗签字。
- 官方 Codex 当前未登录，因此认证后的完整同尺寸真实会话录制未运行；source-native snapshots 足以约束本轮实现，但不能冒充该人工证据。

上述三项未关闭前，可以说“静态 P0/P1 产品语法审查已关闭”，不能说“已达到 Codex 等价成熟度”。
