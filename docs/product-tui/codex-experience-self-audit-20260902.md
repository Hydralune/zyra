# Codex 产品体验自查（2026-09-02）

## 结论

本轮自查不能给出“全部完成”的结论。

- 第一个问题——当前功能是否满足任务书：Phase I 的渲染与视觉语法已经实现；Phase J 的最后一个实现缺口——通用结构化用户问题——已经补齐 provider tool、canonical owner、typed answer、CLI 自动抢占、草稿恢复与按 canonical 时间排列的回答 history cell。request/answer/close 与审计事件在同一 SQLite 事务提交，task 终止或超时不再遗留 pending 僵尸请求。Phase K 的确定性同尺寸帧已经建立，但外部用户、人工 IME、真实 provider 问题续跑和重构后的发布门不能由组件测试代签。
- 第二个问题——任务书是否足以约束出接近 Codex 的 CLI：本轮又把结构化问题的 pending/answer/continuation identity、多问题进度、焦点恢复、重启重建和回答后 history cell 写成硬要求。结合既有信息架构、history cell、composer、活动焦点、审批、picker/pager、颜色、文案和信息预算约束，文档现在足以阻止退回“功能看板”或“只有弹窗的 Demo”。它仍要求真实用户对照，因此 snapshot 不能替代最终体验结论。
- 当前界面已经从开发者事件面板变为 Codex 式对话 Agent TUI，核心交互面不存在已知的静态 P0 功能缺口；但 Definition of Done 仍未关闭，不能在外部门禁完成前称为 Codex 等价实现。

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
| Agent 主动结构化提问 | 对齐实现 | provider tool 真实阻塞；SQLite request/revision/answer/close 原子 owner；typed API；CLI 自动抢占并保留草稿；1～3 问题显示进度并支持自由回答；回答后 history cell 按 canonical 时间插入；task 终止会关闭请求；组件 restart/reopen 通过，真实 provider + daemon restart 待归档 |

## 确定性帧

`docs/product-tui/reference-frames/zyra/` 包含 12 个场景在 80×24 与 120×40 下的 24 个渲染帧：冷启动、composer 输入、运行工具、计划、权限摘要、权限审批、结构化问题、问题回答 history cell、diff、完成、失败恢复、resume picker。来源映射见同目录 `README.md`。

这些帧证明信息层级和产品语法可以被稳定复现，不证明真实 IME、真实用户可发现性或真实 provider 经 daemon restart 的问题续跑已经通过。

## 未关闭项

1. 已实现的 `request_user_input` 需要由真实 provider 发起一次，并在真实 daemon restart 后完成同一请求回答与 tool continuation 归档。2026-09-02 的隔离实跑使用 built CLI 创建了 `task_336f783d2f79` / `run_2f25ced38810`，DeepSeek catalog、credential、route 与 dispatch 都真实建立；首个网络请求以 `provider_unavailable: Unable to connect. Is the computer able to access the url?` 失败，随后 circuit open，最终才表现为 `route_policy_rejected`。失败发生在模型响应/tool call 之前，属于当前执行环境外网限制，不能计为续跑通过，也不是本轮 user-input/TUI 路径失败。
2. Windows Terminal 中文 IME 候选窗、组合态与 exact echo 需要人工执行现有 gate。
3. 两名未参与实现者需要在没有开发者讲解的情况下完成 Codex/Zyra 同类任务。
4. 本轮重构后的可重复 release 与隔离 clean-install 已重新归档通过；真实 daemon/provider continuation 仍受当前环境外网限制，不能由发布门代签。

## 本轮验证

- `bun test ./apps/cli/test`：最终 190 passed，0 failed，920 assertions。
- `bun test ./packages/commands/test ./packages/core/typed-api-client/src`：35 passed，0 failed。
- `bun run typecheck`：全仓通过（包含 CLI、Web、typed client、commands 与 runtime TypeScript 包）。
- `bun run build:cli`：通过，生成 `apps/cli/dist/zyra.js`。
- 结构化问题最终定向 Python 回归：provider bridge、SQLite reopen、typed API 为 9 passed；runtime 与 CodeWorker 绑定为 35 passed、9 subtests passed。更早的预硬化相关集为 61 passed、5 subtests passed。
- 结构化问题 TUI 输入流：自动抢占、选项回答、草稿恢复和 answer history cell 通过。
- Windows ConPTY（最终 build）：1,000 次 resize，startup 429.037 ms，exit 279.605 ms，无 alternate screen，terminal restore 通过。
- 10,000 条可见历史 / 100,000 presentation events 性能门：replay 163.22 ms，输入 P95 0.007 ms，局部重绘 P95 23.269 ms，稳定 RSS 164.281 MiB，全部低于任务书门槛。自查发现并修复了旧渲染器先渲染全部 10,000 个 Markdown cell 再裁窗的问题；现在先排序轻量 timeline 引用，再从尾部按行预算渲染。
- 真实 built CLI：完成首次引导、`?` 帮助、`/status` 与 `/exit` 交互检查。由于代理沙箱不允许写用户 profile，首次引导持久化在该检查中显示了已脱敏的 EPERM；workspace-local state 的 ConPTY 门已通过，正常用户 PowerShell 不受这个代理沙箱限制。
- `pytest tests -q` 全量尝试：使用 workspace-local `--basetemp` 后运行 75 分钟只到 68%，过程中已经出现失败，因套件未按 slow/long-run 隔离且 `-q` 重定向直到结束才给 traceback，本次主动中止；不计为通过，也不把这些无法归因的失败归到本功能。受改动路径的定向回归结果如上。
- 真实 provider 外部回归：已启动隔离 daemon（不触碰用户的 8000 服务）和 built CLI，并确认真实 credential/catalog/route/dispatch；因当前环境不能连接 `api.deepseek.com`，未触发模型的 `request_user_input`，不计通过。隔离 CLI 与 daemon 已在审计结束后关闭。
- 产品入口与发布：commit `c0bde95b005d656e0fe5f1c8d21eb317368ca243` 的 Node CLI 双构建一致（SHA-256 `ce630b5402b22b50e7e17b3da1d6e8acdd47de677fc0fc0032ca38aa48ab6365`）；Windows zip 双构建字节一致（47,303,323 bytes，SHA-256 `d075a5a6814dbc918c386b7519ac23bfe9018f707540743892897f525ceb8425`）。隔离 clean-install `ready=true`，完成 113 个哈希锁定 Python 依赖、frozen Bun install、typecheck/build、migration、产品生命周期、重启、卸载和端口释放；receipt 文件 SHA-256 `dfc8cb12c495e790c5ee771a855f41c28bffb620846a838a7212297103657b98`。
