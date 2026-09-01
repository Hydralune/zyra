import { ProductSessionState, type ProductViewState } from "../product/state/session-state.ts"
import { renderMarkdown } from "../tui/markdown.ts"
import { clipDisplay, displayWidth, padDisplay, sanitizeTerminalText, wrapDisplay } from "../tui/text.ts"
import type { ZyraUiEvent } from "./events.ts"
import type { ProductOverlay } from "../tui/overlay/model.ts"

export type { ProductViewState } from "../product/state/session-state.ts"
export { displayWidth } from "../tui/text.ts"

export interface ProductRenderOptions {
  width: number
  workspace: string
  version?: string
  placeholder?: string
  composerText?: string
  notice?: string
  running?: boolean
  height?: number
  scrollOffset?: number
  overlay?: ProductOverlay
}

export function reduceProductEvents(events: readonly ZyraUiEvent[]): ProductViewState {
  const state = new ProductSessionState()
  state.reconcile(events)
  return state.snapshot()
}

function prefixed(value: string, prefix: string, width: number): string[] {
  const continuation = " ".repeat(displayWidth(prefix))
  const available = Math.max(8, width - displayWidth(prefix))
  const output: string[] = []
  for (const source of sanitizeTerminalText(value).split("\n")) {
    const chunks = wrapDisplay(source, available)
    for (const [index, chunk] of chunks.entries()) output.push(`${output.length === 0 && index === 0 ? prefix : continuation}${chunk}`)
  }
  return output
}

function connectionLabel(state: ProductViewState): string {
  if (state.connection === "reconnecting") return `正在重连（第 ${state.reconnectAttempt ?? 1} 次）`
  if (state.connection === "disconnected") return "连接已断开"
  return "已连接"
}

function renderMessage(lines: string[], message: ProductViewState["messages"][number], width: number): void {
  if (!message.text) return
  lines.push("")
  const marker = message.role === "user" ? "› " : message.streaming ? "◌ " : "• "
  const bodyWidth = Math.max(12, width - displayWidth(marker))
  const rendered = message.role === "assistant"
    ? renderMarkdown(message.text, bodyWidth)
    : wrapDisplay(message.text, bodyWidth)
  for (const [index, line] of rendered.entries()) {
    lines.push(line ? `${index ? " ".repeat(displayWidth(marker)) : marker}${line}` : "")
  }
}

function renderActivity(lines: string[], state: ProductViewState, width: number): void {
  const active = state.activities.filter((item) => item.status === "running")
  const pending = state.activities.filter((item) => item.status === "pending")
  const completed = state.activities.filter((item) => item.status === "completed")
  if (!active.length && !pending.length && !completed.length) return
  lines.push("")
  lines.push(...prefixed(`计划 · ${completed.length} 完成 · ${active.length} 进行中 · ${pending.length} 待执行`, "  ", width))
  for (const item of active.slice(0, 3)) lines.push(...prefixed(`${item.label}（Esc 中断）`, "◌ ", width))
  if (!active.length && completed.length) lines.push(...prefixed(`已完成 ${completed.length} 个步骤`, "✓ ", width))
}

function renderTools(lines: string[], state: ProductViewState, width: number): void {
  const visible = state.tools.slice(-8)
  if (!visible.length) return
  lines.push("")
  for (const tool of visible) {
    const marker = tool.status === "failed" ? "! " : tool.status === "completed" ? "✓ " : "◌ "
    const duration = tool.durationMs === undefined ? "" : ` · ${tool.durationMs < 1_000 ? `${tool.durationMs}ms` : `${(tool.durationMs / 1_000).toFixed(1)}s`}`
    const artifacts = tool.artifactIds?.length ? ` · ${tool.artifactIds.length} artifact` : ""
    lines.push(...prefixed(`${tool.name} · ${tool.summary}${duration}${artifacts}`, marker, width))
  }
  if (state.tools.length > visible.length) lines.push(...prefixed(`${state.tools.length - visible.length} 个较早工具调用已折叠`, "… ", width))
}

function renderAgents(lines: string[], state: ProductViewState, width: number): void {
  if (!state.agents.length) return
  lines.push("")
  const active = state.agents.filter((agent) => !["completed", "failed", "cancelled"].includes(agent.status))
  const failed = state.agents.filter((agent) => agent.status === "failed")
  lines.push(...prefixed(`协作代理 · ${active.length} 活跃 · ${failed.length} 失败 · ${state.agents.length} 总计`, "◎ ", width))
  for (const agent of [...active, ...failed].slice(0, 5)) lines.push(...prefixed(`${agent.label} · ${agent.status}${agent.summary ? ` · ${agent.summary}` : ""}`, "  ", width))
}

function renderIssues(lines: string[], state: ProductViewState, width: number): void {
  for (const issue of state.issues.slice(-8)) {
    lines.push("")
    const marker = issue.severity === "error" ? "! " : issue.severity === "warning" ? "▲ " : "• "
    lines.push(...prefixed(`${issue.message}${issue.code ? `（${issue.code}）` : ""}`, marker, width))
    if (issue.recovery) lines.push(...prefixed(issue.recovery, "  ", width))
    else if (issue.retryable) lines.push(...prefixed("该问题可重试；使用 /continue 或恢复 task。", "  ", width))
  }
}

function renderPermissions(lines: string[], state: ProductViewState, width: number): void {
  for (const permission of state.permissions) {
    lines.push("")
    lines.push(...prefixed(`需要权限：${permission.action}${permission.target ? ` · ${permission.target}` : ""}`, "! ", width))
    if (permission.reason) lines.push(...prefixed(permission.reason, "  ", width))
    if (permission.risk) lines.push(...prefixed(`风险：${permission.risk}`, "  ", width))
    if (permission.scope) lines.push(...prefixed(`作用域：${permission.scope}`, "  ", width))
    if (permission.expiresAt) lines.push(...prefixed(`有效期至：${permission.expiresAt}`, "  ", width))
    lines.push(...prefixed(`[A] 允许本次   [D] 拒绝 · ${permission.requestId}`, "  ", width))
  }
}

function renderWorkspace(lines: string[], state: ProductViewState, width: number): void {
  if (state.changes.length) {
    lines.push("")
    lines.push(...prefixed(`${state.changes.length} 个文件发生变更`, "✓ ", width))
    for (const change of state.changes.slice(0, 12)) lines.push(...prefixed(`${change.kind.padEnd(8)} ${change.path}`, "  ", width))
    if (state.changes.length > 12) lines.push(...prefixed(`${state.changes.length - 12} 个文件已折叠；/diff 查看`, "… ", width))
  }
  if (state.diff?.lines.length) {
    lines.push(...prefixed("Diff 预览：", "  ", width))
    for (const raw of state.diff.lines.slice(0, 160)) {
      const line = sanitizeTerminalText(raw)
      const marker = line.startsWith("+") ? "+ " : line.startsWith("-") ? "- " : "  "
      lines.push(...prefixed(line, marker, width))
    }
    if (state.diff.truncated || state.diff.lines.length > 160) lines.push(...prefixed("diff 已折叠；使用 /diff 或 /ui 查看完整审查。", "… ", width))
  }
}

function renderVerification(lines: string[], state: ProductViewState, width: number): void {
  if (!state.verification) return
  lines.push("")
  const marker = state.verification.status === "passed" ? "✓ " : state.verification.status === "failed" ? "! " : "• "
  lines.push(...prefixed(state.verification.label, marker, width))
  for (const detail of state.verification.details) lines.push(...prefixed(detail, "  ", width))
}

function renderOverlay(lines: string[], overlay: ProductOverlay, width: number): void {
  const heading = `╭─ ${sanitizeTerminalText(overlay.title)} `
  lines.push(`${heading}${"─".repeat(Math.max(1, width - displayWidth(heading) - 1))}╮`)
  if (overlay.query !== undefined) lines.push(clipDisplay(`│ 搜索：${overlay.query || "输入以筛选"}`, width))
  if (!overlay.rows.length) lines.push(clipDisplay("│   没有匹配项", width))
  for (const [index, row] of overlay.rows.entries()) {
    const marker = index === overlay.selected ? "›" : " "
    const detail = row.detail ? `  ${row.detail}` : ""
    lines.push(clipDisplay(`│ ${marker} ${row.label}${detail}`, width))
  }
  if (overlay.footer) lines.push(clipDisplay(`│ ${overlay.footer}`, width))
  lines.push(clipDisplay(`╰${"─".repeat(Math.max(1, width - 2))}╯`, width))
}

export function renderProductState(state: ProductViewState, options: ProductRenderOptions): string {
  const width = Math.max(40, Math.floor(options.width))
  const lines: string[] = []
  const inner = width - 2
  const header = ` >_ Zyra (v${options.version ?? "0.1.0"})`
  const workspace = ` ${sanitizeTerminalText(options.workspace)} · ${connectionLabel(state)} · 产品模式`
  lines.push(`╭${"─".repeat(inner)}╮`)
  lines.push(`│${padDisplay(header, inner)}│`)
  lines.push(`│${padDisplay(workspace, inner)}│`)
  lines.push(`╰${"─".repeat(inner)}╯`)

  for (const message of state.messages) renderMessage(lines, message, width)
  renderActivity(lines, state, width)
  renderTools(lines, state, width)
  renderAgents(lines, state, width)
  renderIssues(lines, state, width)
  renderPermissions(lines, state, width)
  renderWorkspace(lines, state, width)
  renderVerification(lines, state, width)

  if (state.taskMessage) {
    lines.push("")
    lines.push(...prefixed(state.taskMessage, state.taskStatus === "failed" ? "! " : "• ", width))
  }
  if (state.connection === "reconnecting") {
    lines.push("")
    lines.push(...prefixed(connectionLabel(state), "◌ ", width))
  }
  const totalEvicted = Object.values(state.evicted).reduce((sum, value) => sum + value, 0)
  if (totalEvicted) lines.push(...prefixed(`${totalEvicted} 个较早条目已从内存视图折叠`, "… ", width))
  if (options.notice) {
    lines.push("")
    lines.push(...prefixed(options.notice, "! ", width))
  }
  if (options.overlay) {
    lines.push("")
    renderOverlay(lines, options.overlay, width)
  }
  lines.push("")
  lines.push(...prefixed(options.composerText || options.placeholder || "向 Zyra 描述任务", "› ", width))
  const leadingStatus = options.running
    ? "Tab 排队 · Esc 中断"
    : state.taskStatus === "completed"
      ? "任务已完成 · 可继续输入新任务"
      : state.taskStatus === "failed"
        ? "任务失败 · /resume 或输入新任务"
        : "/help 查看命令"
  const status = `${leadingStatus} · ${connectionLabel(state)}${state.taskId ? ` · ${state.taskId}` : ""}`
  lines.push(clipDisplay(`  ${status}`, width))

  let visible = lines.map((line) => clipDisplay(line, width))
  const height = options.height === undefined ? undefined : Math.max(8, Math.floor(options.height))
  if (height !== undefined && visible.length > height) {
    const fixedHeader = visible.slice(0, 4)
    const footer = visible.slice(-4)
    const body = visible.slice(4, -4)
    const bodyBudget = Math.max(0, height - fixedHeader.length - footer.length - 1)
    const offset = Math.max(0, Math.min(body.length, Math.floor(options.scrollOffset ?? 0)))
    const end = Math.max(0, body.length - offset)
    const start = Math.max(0, end - bodyBudget)
    const hidden = start + (body.length - end)
    visible = [
      ...fixedHeader,
      clipDisplay(`… ${hidden} 行已隐藏 · PageUp/PageDown 滚动`, width),
      ...body.slice(start, end),
      ...footer,
    ]
  }
  return `${visible.join("\n")}\n`
}

export function renderProductSnapshot(events: readonly ZyraUiEvent[], options: ProductRenderOptions): string {
  return renderProductState(reduceProductEvents(events), options)
}
