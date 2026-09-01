# ADR-001：产品 TUI 内核边界与状态所有权

- 状态：Accepted for implementation
- 日期：2026-09-01
- 决策范围：Phase C～F

## 背景

Foundation 将 runtime ingress 投影为 `ZyraUiEvent/v1` 并渲染为 inline 文本，证明链路成立。但当前 shell 仍以单 task 为生命周期，renderer 每次从无界事件数组全量归约，composer、任务观察和控制循环分别拥有局部状态，无法支撑成熟的连续会话。

Codex 参考实现把 startup、app/session state、chat widget、bottom pane/composer、streaming、history cell、diff、resume picker 和 terminal lifecycle 分开维护。Zyra 不复制其私有协议，但采用相同价值的边界。

## 决策

产品 TUI 使用以下单向数据流：

```text
canonical snapshot/events
        ↓
versioned presentation projector
        ↓
bounded ProductSessionState
        ↓
ProductSessionController ← input intents / commands / approvals
        ↓
layout + components + overlays
        ↓
terminal renderer
```

约束：

1. Runtime、transport 和 API client 是 canonical facts 的来源。
2. `ProductSessionState` 只保存可重建的产品状态和有界本地 UI 状态，不成为第二套 task 真相。
3. Controller 只根据 typed state 和 mutation receipt 决策，不解析屏幕文本。
4. Renderer/组件不解析 `runtime.*`。
5. transcript、activity、tool、agent、permission、diff 均使用稳定 ID 增量归约；原始事件数增长不能导致屏幕模型无界增长。
6. overlay 是明确状态，不用 notice 字符串模拟 picker、diff viewer 或审批对话框。
7. 退出 TUI 默认 detach；cancel/interrupt 必须是显式 intent。
8. schema/cursor/generation 不安全时从 canonical snapshot 重建；不能安全重建时 fail closed。

## 模块目标

```text
apps/cli/src/product/
  state/          bounded reducers and schema migration
  controller/     session/task/control orchestration
  commands/       registry, parsing, availability
  transcript/     message/block models and export
  components/     header, activity, tool, permission, diff, footer
  overlays/       command palette, picker, details, diff
  terminal/       key decoder, viewport, layout, renderer
```

现有 `presentation/`、`control/`、`api.ts` 和 daemon 模块保留为下层依赖，并逐步迁移，避免一次性大爆炸重写。

## 后果

- 正面：状态可测、可重放、可限界；连续会话与恢复不再依赖一次性 observer；复杂组件可以独立 snapshot。
- 成本：需要逐步替换 `commands/product.ts` 中并行的观察/输入循环，并新增 PTY、故障注入和性能 harness。
- 明确不做：不复制 Codex app-server 协议，不把 Web 看板嵌入终端，不暴露 chain-of-thought。
