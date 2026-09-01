# Codex → Zyra 产品 CLI / TUI 覆盖矩阵

状态：Phase B 基线（实施中）
参考源码：`G:\agent-zoo\codex`
目标仓库：`G:\agent-zoo\zyra`
审计日期：2026-09-01

## 口径

- Codex 是唯一产品参考；证据必须来自本地源码、测试、snapshot 或真实入口。
- `已实现` 表示已有稳定产品路径和相称测试；`部分` 表示仅覆盖基本路径；`缺失` 表示没有用户闭环；`N/A` 只用于明确不属于 Zyra 产品边界的能力。
- `完成证据` 在对应实现和验证提交落地前保持 `—`，不得用计划替代完成。
- P0/P1 必须实现或逐项批准例外。P2 不是批量排除项，仍记录理由。

## 覆盖矩阵

| ID | 能力/场景 | Codex 证据 | Codex 可观察行为 | Zyra 当前状态 | 等级 | 后端依赖 | Zyra 实现位置 | 必需测试 | 完成证据 |
|---|---|---|---|---|---|---|---|---|---|
| BOOT-01 | 无参数启动与交互就绪 | `tui/src/startup_orchestration.rs`; `startup_preflight.rs`; `tui_startup_tests.rs` | 预检后进入可输入 composer，错误进入产品提示 | 部分：composer 延迟注册执行终端，100 轮启动 P95 969ms；首次配置引导仍缺 | P0 | daemon health、provider readiness | `apps/cli/src/main.ts`; `daemon.ts`; `commands/product.ts` | 单元、真实 PTY、冷启动 E2E | `f1623d89`；ConPTY 100/100 |
| BOOT-02 | 带初始 goal 启动 | `tui/src/cli.rs`; `startup_draft.rs`; `startup_draft_tests.rs` | 初始文本进入统一会话提交流程 | 已实现（基本路径） | P0 | task create/run | `args.ts`; `commands/product.ts` | 参数、PTY、真实 daemon | Foundation commits；待成熟门复验 |
| BOOT-03 | 启动故障和可执行诊断 | `startup_error.rs`; `startup_preflight_tests.rs` | 配置、认证、cwd、终端问题给出恢复动作 | 部分 | P0 | 结构化 health/version/provider 状态 | `daemon.ts`; 待建 `diagnostics/` | 故障注入、snapshot、clean-room | — |
| BOOT-04 | 帮助、版本、退出 | `tui/src/cli.rs`; `slash_command.rs`; `app.rs` | 帮助可发现，退出与取消分离 | 已实现；异常强杀仍待独立 100 轮 | P0 | 无 | `args.ts`; `main.ts`; `commands/product.ts` | 参数、PTY、退出码 | `25344dc1`, `80f03d11`, `f1623d89` |
| TERM-01 | inline/alternate-screen 生命周期 | `tui/src/tui.rs`; `custom_terminal.rs`; `terminal_probe.rs`; `startup_replay_tests.rs` | 探测能力、恢复终端、保留 scrollback | 部分：inline/scrollback 和正常生命周期已证实；能力探测与 crash 尚缺 | P0 | 无 | `tui/live-renderer.ts`; `terminal/lifecycle.ts` | 真实 PTY、异常退出、snapshot | `f1623d89`；100 轮无 alternate screen |
| TERM-02 | resize 与 transcript reflow | `transcript_reflow.rs`; `resize_reflow_cap.rs`; `app/tests.rs` 中 resize/reflow tests | resize 后重排且不重复/丢失 | 部分：有界全屏重排，真实 ConPTY 1,000 次通过；高频 transcript 并发仍待 | P0 | 无 | `tui/live-renderer.ts`; `presentation/renderer.ts` | 60–200 列、1000 次 resize | `f1623d89`; 1,000 resize pass |
| TERM-03 | Unicode/中文/emoji/组合字符宽度 | `width.rs`; `wrapping.rs`; `line_truncation_tests.rs`; `effort_status_line_tests.rs` | grapheme 安全截断与换行 | 已实现 grapheme/ZWJ/组合字符宽度与编辑；IME 实机另列 COMP-02 | P0 | 无 | `tui/text.ts`; `input/draft.ts` | property、golden、真实 PTY | `25344dc1`, `1f637896` |
| TERM-04 | terminal restore（raw/cursor/paste） | `tui.rs`; `custom_terminal.rs`; startup replay tests | 正常/异常退出恢复模式 | 部分：100 轮正常退出 raw/paste 恢复；异常强杀门未完成 | P0 | 无 | `tui/composer.ts`; `terminal/lifecycle.ts`; ConPTY harness | 信号、crash、EOF、100 次循环 | `f1623d89`; 100/100 normal exit |
| COMP-01 | 多行编辑和光标移动 | `bottom_pane/chat_composer.rs`; `textarea.rs` | 多行、行内移动、提交语义稳定 | 已实现基础编辑、grapheme 光标、word/home/end、分块 CSI | P0 | 无 | `tui/composer.ts`; `input/draft.ts` | 按键状态机、PTY | `25344dc1`, `1f637896` |
| COMP-02 | IME、paste burst、大段粘贴 | `paste_burst.rs`; `chat_composer.rs`; bottom-pane AGENTS invariants | bracketed paste、burst 合并、占位与确认 | 部分：bracketed paste、分块 UTF-8、256KiB 边界/丢弃恢复；IME 实机未完成 | P0 | 无 | `tui/composer.ts`; `input/draft.ts` | PTY paste、分块 UTF-8、IME | `1f637896` |
| COMP-03 | history、草稿、撤销/重做 | `chat_composer_history.rs`; `input_restore.rs`; app replay tests | 历史检索、恢复队列/草稿 | 部分：进程内 history/stash/undo/redo；crash 后草稿持久恢复未完成 | P0 | session snapshot 可选 | `input/draft.ts`; 待建 `session/local-state.ts` | 单元、crash/resume | `25344dc1` |
| COMP-04 | 外部编辑器 | `external_editor.rs`; `external_editor_tests.rs` | 暂退 raw mode，编辑后安全恢复 | 已实现（基本路径） | P1 | 无 | `input/editor.ts`; `tui/composer.ts` | 编辑器失败、PTY restore | — |
| COMP-05 | 异步重绘期间输入不丢失 | `bottom_pane/mod.rs`; `chatwidget/input_flow.rs`; rendering tests | 输出更新与 composer state 分离 | 部分：共享全量 redraw，缺高频压力证据 | P0 | 无 | `tui/shell.ts`; `tui/live-renderer.ts` | 高频事件+输入 PTY | — |
| CMD-01 | slash command popup、过滤、参数提示 | `slash_command.rs`; `bottom_pane/slash_commands.rs`; `command_popup.rs`; `chat_composer/slash_input.rs` | 输入 `/` 即发现、过滤、选择，支持 inline args | 已实现 registry、状态过滤、键盘 popup 与描述；复杂参数 schema 提示仍薄 | P0 | 命令能力清单 | `product/commands/registry.ts`; `tui/overlay/completion.ts` | 单元、snapshot、PTY | `0e416db0` |
| CMD-02 | `/status` / `/pwd` / `/help` | `chatwidget/slash_dispatch.rs`; `status/`; `working_directory.rs` | 展示当前会话、目录和配置 | 已实现，另有 `/doctor` | P0 | task/session/config status | `commands/product.ts`; `product/diagnostics/` | 单元、snapshot、真实 daemon | `0e416db0` |
| CMD-03 | `/new` / `/clear` / `/resume` | `session_flow.rs`; `session_resume.rs`; `resume_picker.rs`; slash dispatch | 新会话、清 UI、选择历史恢复 | 已实现连续进程内工作流与 picker；真实 daemon 多轮复验仍待 | P0 | session list/detail、task create | `commands/product.ts` | E2E、PTY、恢复 | `25344dc1`, `0e416db0` |
| CMD-04 | `/diff` / `/mention` / 文件补全 | `get_git_diff.rs`; `diff_model.rs`; `file_search.rs`; `file_search_popup.rs` | 可搜索引用文件并打开 diff | 已实现 canonical diff viewer 和深层 `@` 索引；引用编码/规模压测仍需 | P0 | diff artifacts/workspace catalog | `product/files/`; `product/diff/` | path security、snapshot、PTY | `0e416db0`, `d85a22d6` |
| CMD-05 | `/model` / 推理强度 | `model_catalog.rs`; `model_popups.rs`; `reasoning_shortcuts.rs`; picker snapshots | 查看并修改当前模型/强度，状态同步 | 部分：canonical model 选择真实绑定；只显示 provider 默认强度，catalog 未提供 supported 集合故不开放覆盖 | P0 | provider/model catalog + supported reasoning efforts | `product/config/model.ts`; `commands/product.ts` | contract、冲突、E2E | `0e416db0`, `540a1ef1` |
| CMD-06 | `/permissions` / 模式 | `permissions_menu.rs`; `permission_popups.rs`; app permission tests | 查看/选择权限策略并同步会话 | 部分：多请求 picker 与 standard/sealed task 绑定完成；会话/持久权限规则 API 缺失 | P0 | permission mode/rules APIs | `control/permission.ts`; mode picker | custody、冲突、PTY | `0e416db0`, `540a1ef1` |
| CMD-07 | `/copy` / `/export` / raw scrollback | `clipboard_copy.rs`; `transcript_export.rs`; `pager_overlay.rs`; `/raw` dispatch | 导出和复制对话，提供 copy-friendly 模式 | 已实现：最近助手回答复制、workspace 内 Markdown 导出、8 MiB raw pager；复制 4 MiB/导出 256 MiB 安全上限均显式失败或标记，不覆盖文件 | P1 | bounded transcript state | `product/transcript/export.ts`; command controller; system clipboard adapter | 单元、全 CLI 回归；真实 Windows clipboard smoke 待发布门 | 当前提交 |
| CMD-08 | `/review` / `/init` / `/compact` | `review.rs`; `slash_dispatch.rs`; `session_flow.rs` | 发起产品级专用工作流 | 缺失 | P1 | Zyra task templates/capabilities | 命令 registry/controller | contract、真实任务 | — |
| CMD-09 | Codex 账户、Apps、Plugins、Pets | `slash_command.rs`; plugin/app/pets modules | Codex 专属账户和装饰能力 | 不适用或延期：Zyra 无对应产品边界 | P2/N/A | 未来产品决策 | 无 | 逐项产品评审 | 例外尚未批准 |
| FILE-01 | 递归文件/目录引用 | `file_search.rs`; `mention_codec.rs`; connector mentions | `@` 搜索、选择、编码引用 | 部分：64 层/20k 上限、Unicode 与后台索引完成；超大仓性能与正式 mention codec 待补 | P0 | workspace root/canonical file catalog | `product/files/index.ts`; completion popup | 规模、Unicode、路径逃逸 | `d85a22d6` |
| FILE-02 | 安全路径显示和边界 | `additional_dirs.rs`; `working_directory.rs`; core workspace roots tests | workspace/额外根受策略约束 | 已实现本地索引 symlink/junction 拒绝、secret-name 排除和 diff path policy；额外根未支持 | P0 | workspace roots | `product/files/index.ts`; `presentation/workspace-diff.ts` | symlink/path traversal | `d85a22d6` |
| CHAT-01 | transcript 一级消息模型 | `thread_transcript.rs`; `chatwidget/transcript.rs`; `history_cell/` | 用户、助手、工具/计划单元有稳定身份 | 已实现有界 `ProductSessionState` 与稳定 identity；正式 assistant delta 来源仍是后端缺口 | P0 | stable message/item IDs | `product/state/session-state.ts` | reducer、replay、100k event | `25344dc1`, `1f637896` |
| CHAT-02 | Markdown 完整渲染 | `markdown.rs`; `markdown_render.rs`; `markdown_render_tests.rs` | 标题、列表、表格、引用、代码、链接 | 已实现可读标题/列表/任务项/表格/引用/代码/链接；复制和复杂 CommonMark 仍待 | P0 | 无 | `tui/markdown.ts` | golden 60/80/120/160 | `61aa506c` |
| CHAT-03 | 流式 Markdown 与最终一致 | `markdown_stream.rs`; `streaming/controller.rs`; `code_fence.rs`; render tests | 未闭合块安全 holdback，最终 canonical render 一致 | 部分：inline/link holdback 与 fence 增量完成；正式 text delta 和 property chunk 测试待补 | P0 | 正式 text delta IDs | `tui/markdown.ts`; presentation v2 | chunk/property/replay | `61aa506c` |
| CHAT-04 | 超长内容有界与滚动 | `transcript_reflow.rs`; `pager_overlay.rs`; scroll state | 历史与 overlay 可滚动，reflow 有 cap | 部分：消息/实体/frame/viewport/overlay 有界，100k 通过；10k RSS 与 8h 尚待 | P0 | artifact references | bounded state + viewport | 10k items、100k events、RSS | `1f637896` |
| PLAN-01 | 计划与步骤状态 | `history_cell/plans.rs`; `chatwidget/plan_implementation.rs`; plan tests | 计划版本、步骤状态和变更清晰 | 部分：canonical planNodes 状态、主视图聚合和 `/plan` pager 完成；计划版本/变更契约待补 | P0 | canonical plan/step facts | `projector.ts`; state; `/plan` | contract、replay、snapshot | `3e023ed8`, `1b78438f` |
| TOOL-01 | 工具生命周期、耗时、结果 | `tool_lifecycle.rs`; `history_cell/exec.rs`; core tool lifecycle tests | started/update/completed/failed，摘要与耗时 | 已实现 versioned lifecycle、耗时、artifact refs、折叠主视图和 `/tools` pager；真实 provider 覆盖待扩 | P0 | stable tool call IDs/timestamps/artifact | presentation v2; state; `/tools` | reducer、snapshot、长 stdout | `3e023ed8`, `1b78438f` |
| TOOL-02 | stdout/stderr 有界与展开 | `history_cell/exec.rs`; `unified_exec_footer.rs`; truncation core tests | 有界预览、状态和详情 | 缺失 | P0 | output artifacts/ranges | tool output model/overlay | 10MB output、ANSI 攻击 | — |
| AGENT-01 | 多代理聚合与切换 | `multi_agents.rs`; `chatwidget/`; app agent picker tests | 主/子代理状态、选择和审批可见 | 部分：稳定 worker/attempt 聚合、主视图和 `/agents` pager；选择代理上下文与 100-agent E2E 待补 | P0 | stable agent identity + aggregation | presentation v2; renderer; pager | 100 agents、失败/等待、E2E | `3e023ed8`, `1b78438f` |
| PERM-01 | 高可见单权限请求 | `approval_overlay.rs`; `history_cell/approvals.rs`; approval snapshots | 动作、命令/patch、风险、选择清楚 | 部分 | P0 | canonical permission request | `control/permission.ts`; renderer | snapshot、真实 custody | — |
| PERM-02 | 多请求选择和正确绑定 | app pending approvals; inactive-thread approval tests | 多线程/多请求不串单 | 已实现 canonical pending picker、精确 identity 绑定和决定 picker | P0 | list/get/resolve + binding | permissions overlay/controller | 并发、错误选择、PTY | `0e416db0` |
| PERM-03 | expiry/custody/conflict fail closed | permission tests; core approval contracts | 过期、冲突、丢失上下文不允许 | 已有底层基础，产品恢复不足 | P0 | receipt/custody/revision | `control/permission.ts`; `api.ts` | 故障注入、resume | — |
| PERM-04 | 一次/会话/持久范围 | permissions menu/profile tests | 用户选择生效范围 | 缺失 | P1 | permission rules/mode mutation | permissions controller | scope E2E、安全审计 | — |
| DIFF-01 | 按文件 diff 模型/浏览 | `diff_model.rs`; `diff_render.rs`; `history_cell/patches.rs` | 文件状态、hunk、滚动定位 | 已实现 artifact/file picker、paged hunk viewer、20k 行预算和 Web 降级；跨文件快捷跳转仍可增强 | P0 | task diff-review endpoints 已存在 | `product/diff/`; pager | 多文件、大 diff、snapshot | `0e416db0` |
| DIFF-02 | 新增/删除/重命名/二进制 | diff model/render tests | 类型语义稳定 | 部分：change summary | P0 | canonical diff metadata | presentation v2 + diff model | fixture、真实 workspace | — |
| VERIFY-01 | 验证命令、结果、跳过/未运行 | history cells/tool/turn lifecycle | 失败不伪装成功，结果可审查 | 部分：summary/detail string | P0 | canonical verifier evidence | presentation v2; verification component | 通过/失败/skip/not-run | — |
| SESS-01 | 连续多轮会话 | `chatwidget/session_flow.rs`; `input_submission.rs`; app turn tests | 一个进程连续提交 turn | 已实现同进程多 task、同 session 延续、new/clear；真实 daemon 长程多轮待 Phase G | P0 | append/new task semantics | `commands/product.ts` product loop | 真实 daemon 多轮 | `25344dc1` |
| SESS-02 | 最近会话 picker | `resume_picker.rs`; preview tests; `named_session_lookup.rs` | 按时间/cwd/标题选择 | 已实现 canonical session list、task title lookup 与键盘 picker | P0 | session list/detail 已有 | picker overlay + session controller | snapshot、真实 API | `0e416db0` |
| SESS-03 | detach/cancel/exit 区分 | `interrupts.rs`; `session_flow.rs`; composer submission snapshots | Ctrl+C/退出/中断语义分开 | 已实现 detach/exit 不 cancel、cancel/interrupt 独立；真实长任务 resume 对账待 Phase G | P0 | task/turn controls | session controller + command registry | PTY、退出码、resume | `80f03d11`, `f1623d89` |
| SESS-04 | 历史 replay 与损坏降级 | `chatwidget/replay.rs`; app replay tests; session resume | 顺序回放、草稿/队列恢复、错误明确 | 部分：projection replay、单条损坏历史隔离、丢失 Diff 明示；本地草稿 crash 恢复和迁移仍缺 | P0 | versioned snapshot/history | typed client + product session UI | old schema、corrupt fixture | `51c78b2f` |
| CTRL-01 | queue/redirect/interrupt/cancel/continue | `input_queue.rs`; `interrupts.rs`; turn submission tests | 运行中 steer/queue，冲突可解释 | 部分：底层命令存在，发现性弱 | P0 | revisioned control API 已有 | `control/commands.ts`; session controller | mutation/revision/E2E | — |
| CTRL-02 | mutation idempotency/conflict | core turn input tests; app mismatch/race tests | 重试不重复，实际 turn/revision 可恢复 | 部分 | P0 | idempotency/revision | `control/commands.ts`; typed client | lost-ack、409、race | — |
| NET-01 | SSE 重连、duplicate/gap | app server session/replay; streaming controller | 重连不重复，gap 重建 | 已实现：cursor/generation gap 走 snapshot；连续失败预算在收到已验证 canonical 帧后重置；100 次异常断线恢复不重复最终内容 | P0 | ingress cursor/generation/snapshot | `presentation/projection.ts`; `commands/product.ts` | 100 次异常断线故障注入、gap replacement、duplicate contract | 当前提交；真实 daemon restart 另见 NET-02 |
| NET-02 | daemon restart/generation change | startup/replay/session state tests | snapshot 重建并继续控制 | 部分：重建 projection，未证实持续 composer | P0 | generation + durable task state | product observer/session controller | real restart E2E | — |
| NET-03 | worker/tool 局部失败与整体状态 | `tool_lifecycle.rs`; turn lifecycle; history replay failures | 局部失败不等同 turn failure | 部分：映射规则脆弱 | P0 | severity/cause contract | presentation v2 projector | fixtures、真实 fault | — |
| WEB-01 | 打开当前 task Web 路由 | Codex `/app` 是类似跨界入口；Zyra 独有 Web 协同 | 当前上下文跳转且不泄密 | 已实现基本路径 | P0 | stable public route | `ui.ts`; `/ui` | URL/redaction/E2E | Foundation commits；待一致性复验 |
| WEB-02 | CLI/Web canonical 事实一致 | Codex app-server/TUI 同源 thread facts | 状态、回答、approval、diff 同源 | 部分，未形成自动对账 | P0 | canonical APIs | CLI/Web contract tests | cross-view E2E | — |
| AUTO-01 | `run` 纯 JSONL/退出码 | Codex exec/noninteractive CLI；Zyra 既有公开契约 | 人类与机器输出分离 | 已实现 | P0 | task lifecycle | `runner.ts`; `output.ts` | JSONL、ANSI、退出码 | 既有 tests；待全回归 |
| AUTO-02 | developer raw events | Codex debug/rollout；Zyra 产品独有兼容要求 | 完整内部事实仍可观察 | 已实现 | P0 | raw event spine | `commands/interactive.ts`; `events` | 契约回归 | Foundation commits |
| REL-01 | 可重复 build/install | Codex npm launcher、platform package、version/update modules | 干净环境安装并诊断版本 | 部分：本仓构建产物可用，无发行闭包 | P0 | version/compat endpoint | package scripts、待建 release scripts | clean-room Windows | — |
| REL-02 | 升级、state/schema migration | `updates.rs`; `version.rs`; session replay compatibility | 版本变化不静默破坏历史 | 缺失 | P1 | versioned state/protocol | migrations + diagnostics | old state matrix | — |
| REL-03 | 脱敏诊断包 | `debug_config.rs`; startup errors; feedback/log collection | 输出可分享且不泄密 | 部分：JSONL scrubber，不是诊断闭环 | P0 | health/config summaries | 待建 `diagnostics/` | secret corpus、clean-room | — |
| PERF-01 | 10k transcript/100k events/8h soak | Codex bounded history/reflow/streaming tests（产品行为基线） | 长时仍可输入、滚动、恢复 | 部分：10k transcript/100k events/RSS/延迟门已通过；100 次异常断线、100 exits、1k resize 已通过；8h component soak 仍在运行，尚未计为通过 | P0 | artifact/cursor | bounded state + ConPTY/perf harness | 文档第 15 节全部指标 | `1f637896`, `f1623d89`, `2ff5c482`；当前提交 |

## 当前结论

- 现有 Foundation 在 transport、projection、基础权限 custody、控制 mutation、JSONL 和 daemon supervision 上可复用。
- 最大结构性缺口是：没有长期存活的 session controller，没有增量且有界的产品状态，没有命令/overlay 体系，没有 Markdown/diff/tool/agent 组件，也没有发布级 PTY 与性能 harness。
- 后端并非从零：typed API 已包含 session list/detail、task command queue、permission control、diff review、terminal、workspace 和 artifact 相关端点。Phase C/D 应优先复用这些正式契约，而不是从屏幕文本猜状态。
- Codex 专属账户、插件、Apps、Pets 等项目暂列 P2/N/A；必须在 Phase H 逐项批准，不影响 P0 工作流先行。

## 真实 Codex PTY 状态

本地源码入口 `G:\agent-zoo\codex\codex-cli\bin\codex.js` 已执行探测，但当前 checkout 缺少可选平台包 `@openai/codex-win32-x64`，因此尚不能生成真实 Codex PTY 记录。该项保持未通过；在平台二进制可用前，审计只采用源码、测试和 snapshot 证据，不伪称实机已验证。
