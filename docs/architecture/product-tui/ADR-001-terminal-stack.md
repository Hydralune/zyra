# ADR-001：产品 TUI 首选 TypeScript/Bun

- 状态：接受，用于 Phase 0～MVP；保留重新评估门
- 日期：2026-09-01
- 唯一参考实现：`G:\agent-zoo\codex\codex-rs\tui`

## 决策

Zyra 产品 TUI 的 Phase 0、Presentation Projection 和 MVP shell 首选 TypeScript/Bun。暂不建立独立 Rust/ratatui binary。

这不是对最终技术栈的永久承诺。若 Windows 输入/重绘、长 transcript、Unicode、resize reflow、snapshot testability 或发布稳定性达不到下述门槛，再用同一 `ZyraUiEvent/v1` 契约替换前端实现，而不是改写后端协议。

## 参考基线

本决策只参考本地 Codex CLI：

- `codex-rs/tui/src/app.rs`：应用事件循环和 lifecycle；
- `codex-rs/tui/src/chatwidget.rs`：transcript、working status 与 task interaction；
- `codex-rs/tui/src/bottom_pane/chat_composer.rs`：composer 与快捷键；
- `codex-rs/tui/src/app_server_session.rs`：前端与 canonical session 的边界；
- `codex-rs/tui/**/snapshots/*.snap`：窄宽终端、Unicode、composer、approval 与状态的 golden strategy。

采用的是信息架构和工程验证方式，不复制 Codex 内部 crates、App Server protocol 或 Agent runtime。

## 短期验证结果

### TypeScript/Bun

Phase 0 原型已验证：

- 一个 discriminated union 可以在现有 CLI 工程内表达版本化产品事件；
- 纯 projector 能从真实 task snapshot + ingress fixture 重建对话与终态；
- 57 条 agent message、node failure、audit/route/dispatch 噪声被过滤；
- canonical final answer fallback 不伪造 streaming；
- 80、120 列 golden snapshots 已建立；
- 40 列测试覆盖中文、emoji、Markdown code fence 和重连提示；
- reducer/renderer 不依赖 raw event names；
- 当前方案没有新增第三方依赖，沿用已有 Bun test、TypeScript typecheck 和 Node 发布构建。

TypeScript 路径可以直接复用 `CliApi`、typed client、control、permission custody、daemon、terminal node 和 workspace transfer，无需新增跨进程桥接。

### Rust/ratatui

本地 Codex 证明 Rust/ratatui 能实现成熟、高性能 TUI；但把它用于 Zyra 当前阶段需要同时新增：

- 独立 workspace/binary 和 Windows 发布产物；
- REST/SSE/cursor/snapshot/control/permission 的第二套 client；
- TypeScript 与 Rust 之间的 schema generation 或手工同步；
- daemon discovery、terminal node registration 和 workspace transfer 的跨语言复用边界；
- 两套构建、测试、签名和升级路径。

这些成本不会自动解决当前最大风险：后端尚未稳定提供 assistant 正文、permission product fields、safe tool summary 和 diff 来源。先换语言会延迟契约闭环。

## 选择理由

当前风险位于 presentation data contract，而非绘制吞吐。TypeScript 路径用最少新边界验证了真实任务投影，并能保持现有发布入口兼容。Rust 的优势在 transcript 性能、terminal primitives 和成熟 widget ecosystem；这些优势应在有测量证据时启用。

## 重新评估门

满足任一项即创建 Rust/ratatui spike，并使用相同 fixture/golden/acceptance 对比：

- Windows Terminal 中连续 resize 或异常退出无法可靠恢复终端；
- 10,000 条 transcript、持续 delta 或大型 diff 无法在目标硬件保持可接受输入延迟；
- Unicode display width、emoji/combining reflow 不能通过固定 golden matrix；
- inline 与 alternate-screen 双模式需要的 terminal control 无法在 Node/Bun 稳定实现；
- Node/Bun 发布产物不满足最终单文件、启动时间或供应链要求；
- UI component snapshot 无法保持确定性。

重新评估前必须有可复现 benchmark 或失败 fixture，不能仅以语言偏好改变决定。

## 结果

- `apps/cli/src/presentation` 作为 MVP 实验落点，不提前建立新 workspace package。
- `ZyraUiEvent/v1` 保持与语言无关，未来 Rust 前端可消费同一协议。
- 现有 developer CLI 不迁移、不重排。
- 默认 `zyra` 入口尚未切换；Phase 2 shell 达到真实任务验收后再切换。
