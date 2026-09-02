import { ProductSessionState, type ProductActivityState, type ProductIssueState, type ProductMessageState, type ProductTimelineItem, type ProductToolState, type ProductViewState } from "../product/state/session-state.ts"
import { renderMarkdown } from "../tui/markdown.ts"
import { clipDisplay, displayWidth, graphemes, graphemeWidth, padDisplay, sanitizeTerminalText, wrapDisplay } from "../tui/text.ts"
import type { ZyraUiEvent } from "./events.ts"
import type { ProductOverlay } from "../tui/overlay/model.ts"

export type { ProductViewState } from "../product/state/session-state.ts"
export { displayWidth } from "../tui/text.ts"

export interface ProductChromeState {
  model?: string
  mode?: string
  permissions?: string
}

export interface ProductLocalHistoryItem {
  id: string
  text: string
  afterOrder: number
  sequence: number
}

export interface ProductRenderOptions {
  width: number
  workspace: string
  version?: string
  placeholder?: string
  composerText?: string
  composerCursor?: number
  notice?: string
  localHistory?: readonly ProductLocalHistoryItem[]
  running?: boolean
  showCurrentActivity?: boolean
  acceptingInput?: boolean
  height?: number
  scrollOffset?: number
  overlay?: ProductOverlay
  chrome?: ProductChromeState
  color?: boolean
}

export interface ProductRenderedFrame {
  text: string
  cursor?: { row: number; column: number }
}

type LineTone = "default" | "brand" | "header" | "secondary" | "accent" | "success" | "error" | "user" | "placeholder" | "selected"

interface RenderLine {
  text: string
  tone?: LineTone
}

interface ComposerLayout {
  lines: RenderLine[]
  cursorRow: number
  cursorColumn: number
}

const ANSI = Object.freeze({
  reset: "\u001b[0m",
  bold: "\u001b[1m",
  dim: "\u001b[2m",
  cyan: "\u001b[36m",
  green: "\u001b[32m",
  red: "\u001b[31m",
  magenta: "\u001b[35m",
})

export function reduceProductEvents(events: readonly ZyraUiEvent[]): ProductViewState {
  const state = new ProductSessionState()
  state.reconcile(events)
  return state.snapshot()
}

function line(text = "", tone: LineTone = "default"): RenderLine {
  return { text, tone }
}

function pushWrapped(
  lines: RenderLine[],
  value: string,
  prefix: string,
  width: number,
  tone: LineTone = "default",
  continuation = " ".repeat(displayWidth(prefix)),
): void {
  const available = Math.max(8, width - displayWidth(prefix))
  let first = true
  for (const source of sanitizeTerminalText(value).split("\n")) {
    const chunks = wrapDisplay(source, available)
    for (const chunk of chunks) {
      lines.push(line(`${first ? prefix : continuation}${chunk}`, tone))
      first = false
    }
  }
}

function renderSessionHeader(options: ProductRenderOptions): RenderLine[] {
  const chrome = options.chrome ?? {}
  const rows = [
    ` >_ Zyra (v${options.version ?? "0.1.0"})`,
    "",
    ` 模型: ${chrome.model ?? "自动选择"}`,
    ` 目录: ${sanitizeTerminalText(options.workspace)}`,
    chrome.permissions ? ` 权限: ${chrome.permissions}` : undefined,
  ].filter((value): value is string => value !== undefined)
  const contentWidth = Math.max(...rows.map(displayWidth), 34)
  const boxWidth = Math.min(Math.max(4, options.width), contentWidth + 2)
  const inner = boxWidth - 2
  return [
    line(`╭${"─".repeat(inner)}╮`),
    ...rows.map((row, index) => line(`│${padDisplay(row, inner)}│`, index === 0 ? "brand" : row ? "default" : "secondary")),
    line(`╰${"─".repeat(inner)}╯`),
  ]
}

function renderMessage(message: ProductMessageState, width: number): RenderLine[] {
  if (!message.text) return []
  const lines: RenderLine[] = [line()]
  const marker = message.role === "user" ? "› " : "• "
  const rendered = message.role === "assistant"
    ? renderMarkdown(message.text, Math.max(12, width - displayWidth(marker)), { streaming: message.streaming })
    : wrapDisplay(message.text, Math.max(12, width - displayWidth(marker)))
  for (const [index, value] of rendered.entries()) {
    const tone: LineTone = message.role === "user"
      ? "user"
      : /^#{1,6}\s/u.test(value)
        ? "header"
        : /^(?: {4}|>\s)/u.test(value)
          ? "secondary"
          : "default"
    lines.push(line(value ? `${index ? " ".repeat(displayWidth(marker)) : marker}${value}` : "", tone))
  }
  return lines
}

function durationLabel(durationMs: number | undefined): string {
  if (durationMs === undefined) return ""
  return durationMs < 1_000 ? ` · ${durationMs}ms` : ` · ${(durationMs / 1_000).toFixed(1)}s`
}

function toolVerb(name: string, running: boolean): string {
  const normalized = name.toLocaleLowerCase()
  if (/(apply|patch|edit|write|delete|move|rename)/u.test(normalized)) return running ? "正在编辑" : "已编辑"
  if (/(read|search|find|list|glob|grep|rg|inspect)/u.test(normalized)) return running ? "正在检查" : "已检查"
  if (/(exec|shell|bash|powershell|command|test|verify)/u.test(normalized)) return running ? "正在运行" : "已运行"
  if (/(web|http|browser|fetch)/u.test(normalized)) return running ? "正在联网" : "已联网"
  return running ? "正在使用工具" : "已调用工具"
}

function renderTool(tool: ProductToolState, width: number): RenderLine[] {
  if (tool.status === "running") return []
  const failed = tool.status === "failed"
  const lines: RenderLine[] = [line()]
  pushWrapped(lines, `${failed ? "工具未完成" : toolVerb(tool.name, false)}${durationLabel(tool.durationMs)}`, failed ? "! " : "• ", width, failed ? "error" : "default")
  if (tool.summary) pushWrapped(lines, tool.summary, "  └ ", width, "secondary", "    ")
  if (failed && tool.impact === "local") pushWrapped(lines, "本次工具调用失败，但任务仍可继续。", "    ", width, "secondary")
  if (tool.recovery) pushWrapped(lines, tool.recovery, "    ", width, "secondary")
  if (tool.outputRefs?.length || tool.artifactIds?.length) pushWrapped(lines, "详细输出可用 /tools 查看", "    ", width, "secondary")
  return lines
}

function renderPlan(state: ProductViewState, width: number): RenderLine[] {
  if (!state.plan) return []
  if (
    state.plan.revisionSource === "compatibility"
    && !state.plan.changes.length
    && state.plan.steps.every((step) => step.status === "completed" || step.status === "superseded")
  ) return []
  const lines: RenderLine[] = [line(), line("• 已更新计划")]
  const latestChange = state.plan.changes.at(-1)
  if (latestChange) pushWrapped(lines, latestChange.summary, "  └ ", width, "secondary", "    ")
  const steps = state.plan.steps.filter((step) => step.status !== "superseded")
  for (const step of steps.slice(0, 8)) {
    const marker = step.status === "completed" ? "✔ " : step.status === "running" ? "→ " : step.status === "failed" ? "✘ " : step.status === "cancelled" ? "– " : "□ "
    const tone: LineTone = step.status === "completed" ? "success" : step.status === "running" ? "accent" : step.status === "failed" ? "error" : "secondary"
    pushWrapped(lines, step.label, `    ${marker}`, width, tone, "      ")
  }
  if (steps.length > 8) pushWrapped(lines, `另有 ${steps.length - 8} 个步骤，可用 /plan 查看`, "    … ", width, "secondary", "      ")
  return lines
}

function visibleActivity(activity: ProductActivityState, hasPlan: boolean): boolean {
  if (activity.status === "running") return false
  if (hasPlan && activity.category === "plan") return false
  return activity.category === "recovery" || activity.severity === "warning" || activity.severity === "error" || activity.outcome === "failed"
}

function renderActivity(activity: ProductActivityState, width: number, hasPlan: boolean): RenderLine[] {
  if (!visibleActivity(activity, hasPlan)) return []
  const failed = activity.outcome === "failed" || activity.severity === "error"
  const lines: RenderLine[] = [line()]
  pushWrapped(lines, activity.label, failed ? "! " : "• ", width, failed ? "error" : "default")
  if (activity.summary) pushWrapped(lines, activity.summary, "  └ ", width, "secondary", "    ")
  if (activity.impact === "local") pushWrapped(lines, "局部问题已隔离，任务整体状态不受影响。", "    ", width, "secondary")
  return lines
}

function renderIssue(issue: ProductIssueState, width: number): RenderLine[] {
  const lines: RenderLine[] = [line()]
  pushWrapped(lines, issue.message, issue.severity === "error" ? "! " : "• ", width, issue.severity === "error" ? "error" : "default")
  if (issue.recovery) pushWrapped(lines, issue.recovery, "  └ ", width, "secondary", "    ")
  else if (issue.retryable) pushWrapped(lines, "可以重试或继续当前任务。", "  └ ", width, "secondary", "    ")
  return lines
}

function renderUserInput(request: ProductViewState["userInputHistory"][number], width: number): RenderLine[] {
  const answered = request.questions.filter((question) => (request.answers?.[question.id]?.answers.length ?? 0) > 0).length
  const interrupted = request.status !== "answered"
  const lines: RenderLine[] = [
    line(),
    line(`• 问题 · ${answered}/${request.questions.length} 已回答${interrupted ? " · 已中断" : ""}`, interrupted ? "secondary" : "default"),
  ]
  for (const question of request.questions) {
    pushWrapped(lines, question.question, "  • ", width, "default", "    ")
    const answers = request.answers?.[question.id]?.answers ?? []
    if (!answers.length) {
      pushWrapped(lines, "未回答", "    └ ", width, "secondary", "      ")
      continue
    }
    for (const answer of answers) pushWrapped(lines, answer, "    回答：", width, "accent", "          ")
  }
  return lines
}

function changeLabel(kind: ProductViewState["changes"][number]["kind"]): string {
  if (kind === "created") return "A"
  if (kind === "deleted") return "D"
  if (kind === "renamed") return "R"
  return "M"
}

function renderWorkspace(state: ProductViewState, width: number): RenderLine[] {
  if (!state.changes.length) return []
  const lines: RenderLine[] = [line(), line(`• 已编辑 ${state.changes.length} 个文件`, "success")]
  for (const change of state.changes.slice(0, 8)) {
    const renamed = change.previousPath ? `${change.previousPath} → ${change.path}` : change.path
    pushWrapped(lines, `${changeLabel(change.kind)} ${renamed}`, "  └ ", width, change.kind === "deleted" ? "error" : "secondary", "    ")
  }
  if (state.changes.length > 8) pushWrapped(lines, `另有 ${state.changes.length - 8} 个文件，可用 /diff 查看`, "    … ", width, "secondary", "      ")
  return lines
}

function renderVerification(state: ProductViewState, width: number): RenderLine[] {
  const verification = state.verification
  if (!verification || verification.status === "not_run") return []
  const passed = verification.status === "passed"
  const lines: RenderLine[] = [line(), line(`${passed ? "•" : "!"} ${verification.label}`, passed ? "success" : "error")]
  const commands = verification.checks.filter((check) => check.command)
  for (const check of commands.slice(0, 4)) {
    const result = check.exitCode === undefined ? check.status : `${check.status} · exit ${check.exitCode}`
    pushWrapped(lines, `${check.command} (${result})`, "  └ ", width, check.status === "passed" ? "secondary" : "error", "    ")
  }
  if (!commands.length) {
    for (const detail of verification.details.slice(0, 3)) pushWrapped(lines, detail, "  └ ", width, passed ? "secondary" : "error", "    ")
  }
  if (verification.commandEvidence === "not_recorded") pushWrapped(lines, "未记录命令级验证收据", "  └ ", width, "secondary", "    ")
  return lines
}

function renderTimelineItem(
  item: ProductTimelineItem,
  state: ProductViewState,
  maps: {
    messages: ReadonlyMap<string, ProductMessageState>
    activities: ReadonlyMap<string, ProductActivityState>
    tools: ReadonlyMap<string, ProductToolState>
    issues: ReadonlyMap<string, ProductIssueState>
    userInputs: ReadonlyMap<string, ProductViewState["userInputHistory"][number]>
  },
  width: number,
): RenderLine[] {
  if (item.kind === "message") return maps.messages.has(item.id) ? renderMessage(maps.messages.get(item.id)!, width) : []
  if (item.kind === "activity") return maps.activities.has(item.id) ? renderActivity(maps.activities.get(item.id)!, width, Boolean(state.plan)) : []
  if (item.kind === "tool") return maps.tools.has(item.id) ? renderTool(maps.tools.get(item.id)!, width) : []
  if (item.kind === "issue") return maps.issues.has(item.id) ? renderIssue(maps.issues.get(item.id)!, width) : []
  if (item.kind === "user_input") return maps.userInputs.has(item.id) ? renderUserInput(maps.userInputs.get(item.id)!, width) : []
  if (item.kind === "plan") return renderPlan(state, width)
  if (item.kind === "workspace") return renderWorkspace(state, width)
  if (item.kind === "verification") return renderVerification(state, width)
  return []
}

function renderTimelineWindow(
  state: ProductViewState,
  width: number,
  lineBudget: number,
  localHistory: readonly ProductLocalHistoryItem[] = [],
): { lines: RenderLine[]; omitted: number } {
  const maps = {
    messages: new Map(state.messages.map((item) => [item.messageId, item])),
    activities: new Map(state.activities.map((item) => [item.activityId, item])),
    tools: new Map(state.tools.map((item) => [item.toolCallId, item])),
    issues: new Map(state.issues.map((item) => [item.issueId, item])),
    userInputs: new Map(state.userInputHistory.map((item) => [item.requestId, item])),
  }
  const timeline = state.timeline?.length
    ? state.timeline
    : state.messages.map((message, order): ProductTimelineItem => ({ kind: "message", id: message.messageId, order }))
  const entries: Array<
    | { kind: "timeline"; afterOrder: number; sequence: number; item: ProductTimelineItem }
    | { kind: "notice"; afterOrder: number; sequence: number; text: string }
  > = [
    ...timeline.map((item) => ({
      kind: "timeline" as const,
      afterOrder: item.order,
      sequence: 0,
      item,
    })),
    ...localHistory.map((item) => ({
      kind: "notice" as const,
      afterOrder: item.afterOrder,
      sequence: item.sequence,
      text: item.text,
    })),
  ].sort((left, right) =>
    left.afterOrder - right.afterOrder || left.sequence - right.sequence,
  )
  const segments: RenderLine[][] = []
  let renderedLines = 0
  let firstIncluded = entries.length
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index]!
    const segment = entry.kind === "timeline"
      ? renderTimelineItem(entry.item, state, maps, width)
      : renderNotice(entry.text, width)
    if (!segment.length) continue
    segments.unshift(segment)
    renderedLines += segment.length
    firstIncluded = index
    if (renderedLines >= Math.max(24, lineBudget)) break
  }
  return { lines: segments.flat(), omitted: firstIncluded }
}

function renderCurrentActivity(state: ProductViewState, options: ProductRenderOptions): RenderLine[] {
  const width = options.width
  if (options.showCurrentActivity === false) return []
  if (state.connection === "reconnecting") return [line(), line(`• 正在重连（第 ${state.reconnectAttempt ?? 1} 次）`, "accent")]
  if (["completed", "failed", "blocked", "needs_revision", "killed", "cancelled"].includes(state.taskStatus)) return []
  const activeTool = state.tools.filter((tool) => tool.status === "running").at(-1)
  const activeStep = state.plan?.revisionSource === "canonical_graph"
    ? state.plan.steps.find((step) => step.status === "running")
    : undefined
  const activeActivity = state.activities.filter((activity) => activity.status === "running").at(-1)
  const activeAgents = state.agents.filter((agent) => !["completed", "failed", "cancelled"].includes(agent.status))
  const running = options.running || state.taskStatus === "running" || Boolean(activeTool || activeStep || activeActivity || activeAgents.length)
  if (!running) return []
  const lines: RenderLine[] = [line()]
  const focus = activeTool
    ? `${toolVerb(activeTool.name, true)}${activeTool.summary ? `：${activeTool.summary}` : ""}`
    : activeStep
      ? `正在处理：${activeStep.label}`
      : activeActivity
        ? activeActivity.label
      : activeAgents.length
        ? `正在协调 ${activeAgents.length} 个代理`
        : "正在工作"
  const elapsed = activeTool?.durationMs === undefined
    ? undefined
    : activeTool.durationMs < 1_000 ? `${activeTool.durationMs}ms` : `${Math.floor(activeTool.durationMs / 1_000)}s`
  pushWrapped(lines, `${focus}（${elapsed ? `${elapsed} · ` : ""}esc 中断）`, "• ", width, "accent")
  if (activeTool?.outputRefs?.length) pushWrapped(lines, "输出正在记录，可用 /tools 查看", "  └ ", width, "secondary", "    ")
  if (state.agents.length && !focus.includes("代理")) {
    const agentSummary = activeAgents.length === state.agents.length
      ? `${state.agents.length} 个协作代理正在处理子任务`
      : `${state.agents.length} 个协作代理 · ${activeAgents.length} 正在处理`
    pushWrapped(lines, agentSummary, "  └ ", width, "secondary", "    ")
  }
  return lines
}

function renderTaskOutcome(state: ProductViewState, width: number): RenderLine[] {
  if (!state.taskMessage) return []
  const failed = ["failed", "blocked", "needs_revision", "killed"].includes(state.taskStatus)
  const lines: RenderLine[] = [line()]
  pushWrapped(lines, state.taskMessage, failed ? "! " : "• ", width, failed ? "error" : "default")
  return lines
}

function renderPermissionPrompt(state: ProductViewState, width: number): RenderLine[] {
  if (!state.permissions.length) return []
  const request = state.permissions[0]!
  const lines: RenderLine[] = [line(), line(state.permissions.length === 1 ? "  需要你的许可" : `  有 ${state.permissions.length} 个操作等待许可`, "header")]
  pushWrapped(lines, request.action, "  ", width)
  if (request.reason) pushWrapped(lines, `原因：${request.reason}`, "  ", width, "secondary")
  if (request.target) pushWrapped(lines, `目标：${request.target}`, "  ", width, "secondary")
  const policy = [request.risk ? `风险：${request.risk}` : undefined, request.scope ? `范围：${request.scope}` : undefined, request.expiresAt ? `到期：${request.expiresAt}` : undefined].filter(Boolean).join(" · ")
  if (policy) pushWrapped(lines, policy, "  ", width, "secondary")
  lines.push(line("› 按 Enter 查看允许范围，或按 Esc 拒绝", "selected"))
  return lines
}

function renderNotice(value: string | undefined, width: number): RenderLine[] {
  if (!value) return []
  const normalized = sanitizeTerminalText(value)
  const failed = /^(?:!|操作未提交|错误|无法|不可用|拒绝|未知命令|权限[^\n]*关闭|Artifact 不可用|[^\n]*(?:失败|未保存))/u.test(normalized)
  const lines: RenderLine[] = [line()]
  pushWrapped(lines, normalized, failed ? "! " : "• ", width, failed ? "error" : "default")
  return lines
}

function renderOverlay(overlay: ProductOverlay, width: number): RenderLine[] {
  const lines: RenderLine[] = [line()]
  if (overlay.kind !== "completion") lines.push(line(`  ${sanitizeTerminalText(overlay.title)}`, "header"))
  if (overlay.description?.length) {
    lines.push(line())
    for (const description of overlay.description) pushWrapped(lines, description, "  ", width, "secondary")
    lines.push(line())
  }
  if (overlay.query !== undefined && overlay.kind !== "completion") lines.push(line(
    overlay.kind === "question"
      ? `› ${overlay.query || "输入你的回答"}`
      : `  搜索：${overlay.query || "输入以筛选"}`,
    overlay.kind === "question" ? "user" : "secondary",
  ))
  if (!overlay.rows.length && overlay.kind !== "question") lines.push(line("  没有匹配项", "secondary"))
  for (const [index, row] of overlay.rows.entries()) {
    const selected = index === overlay.selected
    const marker = overlay.kind === "pager" ? "  " : selected ? "› " : "  "
    const detail = row.detail ? `  ${row.detail}` : ""
    const label = overlay.kind === "picker" || overlay.kind === "menu" || overlay.kind === "approval" || overlay.kind === "question" ? `${index + 1}. ${row.label}` : row.label
    const tone = selected
      ? "selected"
      : overlay.kind === "pager"
        ? row.tone ?? "default"
        : "secondary"
    pushWrapped(lines, `${label}${detail}`, marker, width, tone, "  ")
  }
  return lines
}

function composerLayout(value: string, cursor: number, width: number, placeholder: string): ComposerLayout {
  const available = Math.max(1, width - 2)
  const safe = sanitizeTerminalText(value)
  if (!safe) return {
    lines: [line(`› ${placeholder}`, "placeholder")],
    cursorRow: 0,
    cursorColumn: 2,
  }
  const selectedCursor = Math.max(0, Math.min(safe.length, cursor))
  const content: string[] = [""]
  let row = 0
  let column = 0
  let consumed = 0
  let cursorRow = 0
  let cursorColumn = 0
  const recordCursor = () => {
    cursorRow = row
    cursorColumn = column
  }
  if (selectedCursor === 0) recordCursor()
  for (const item of graphemes(safe)) {
    if (item === "\n") {
      row += 1
      column = 0
      content.push("")
      consumed += item.length
      if (consumed <= selectedCursor) recordCursor()
      continue
    }
    const itemWidth = graphemeWidth(item)
    if (column > 0 && column + itemWidth > available) {
      row += 1
      column = 0
      content.push("")
    }
    content[row] += item
    column += itemWidth
    consumed += item.length
    if (consumed <= selectedCursor) recordCursor()
  }
  return {
    lines: content.map((item, index) => line(`${index ? "  " : "› "}${item}`, index === 0 ? "user" : "default")),
    cursorRow,
    cursorColumn: 2 + cursorColumn,
  }
}

function twoColumnFooter(left: string, right: string, width: number): string {
  const safeLeft = sanitizeTerminalText(left)
  const safeRight = sanitizeTerminalText(right)
  if (!safeRight) return clipDisplay(`  ${safeLeft}`, width)
  if (displayWidth(safeLeft) + displayWidth(safeRight) + 2 > width) return clipDisplay(`  ${safeLeft}`, width)
  return `  ${safeLeft}${" ".repeat(Math.max(1, width - 2 - displayWidth(safeLeft) - displayWidth(safeRight)))}${safeRight}`
}

function styleLeading(text: string, code: string): string {
  const marker = text.match(/^(\s*(?:[›•!✔✘+\-]|→|□|…))/u)?.[1]
  if (!marker) return `${code}${text}${ANSI.reset}`
  return `${code}${marker}${ANSI.reset}${text.slice(marker.length)}`
}

function styleLine(value: RenderLine, enabled: boolean): string {
  if (!enabled || !value.text) return value.text
  const tone = value.tone ?? "default"
  if (tone === "brand") return value.text.replace("Zyra", `${ANSI.bold}${ANSI.magenta}Zyra${ANSI.reset}`)
  if (tone === "header") return `${ANSI.bold}${value.text}${ANSI.reset}`
  if (tone === "secondary") return `${ANSI.dim}${value.text}${ANSI.reset}`
  if (tone === "accent" || tone === "selected") return `${ANSI.cyan}${value.text}${ANSI.reset}`
  if (tone === "success") return styleLeading(value.text, ANSI.green)
  if (tone === "error") return `${ANSI.red}${value.text}${ANSI.reset}`
  if (tone === "user") return styleLeading(value.text, ANSI.cyan)
  if (tone === "placeholder") {
    const prefix = value.text.slice(0, 1)
    return `${ANSI.cyan}${prefix}${ANSI.reset}${ANSI.dim}${value.text.slice(1)}${ANSI.reset}`
  }
  return value.text
}

export function renderProductFrame(state: ProductViewState, options: ProductRenderOptions): ProductRenderedFrame {
  const width = Math.max(40, Math.floor(options.width))
  const height = options.height === undefined ? undefined : Math.max(8, Math.floor(options.height))
  const scrollOffset = Math.max(0, Math.floor(options.scrollOffset ?? 0))
  const lineBudget = (height ?? 60) + scrollOffset + 64
  const timeline = renderTimelineWindow(state, width, lineBudget, options.localHistory)
  const body: RenderLine[] = [
    ...renderSessionHeader({ ...options, width }),
    ...(timeline.omitted ? [line(), line(`… ${timeline.omitted} 个较早记录已虚拟化；PageUp 继续回看`, "secondary")] : []),
    ...timeline.lines,
    ...renderCurrentActivity(state, { ...options, width }),
    ...renderTaskOutcome(state, width),
    ...(options.overlay ? [] : renderPermissionPrompt(state, width)),
    ...renderNotice(options.notice, width),
    ...(options.overlay ? renderOverlay(options.overlay, width) : []),
  ]
  const acceptingInput = options.acceptingInput ?? true
  const composer = composerLayout(
    acceptingInput ? options.composerText ?? "" : "",
    acceptingInput ? options.composerCursor ?? (options.composerText?.length ?? 0) : 0,
    width,
    acceptingInput ? options.placeholder ?? "让 Zyra 处理任何任务" : "输入暂不可用",
  )
  const chrome = options.chrome ?? {}
  const context = state.context ? `${state.context.remainingPercent}% 上下文` : undefined
  const right = [context, chrome.model, chrome.mode].filter(Boolean).join(" · ")
  const left = options.overlay
    ? options.overlay.footer ?? (options.overlay.kind === "pager"
        ? "↑↓ 滚动 · esc 返回"
        : options.overlay.kind === "approval"
          ? "↑↓ 选择 · enter 确认 · esc 拒绝"
          : "↑↓ 选择 · enter 确认 · esc 返回")
    : options.running
      ? "tab 排队消息 · esc 中断"
      : "? 查看快捷键"
  const modalOverlay = options.overlay && options.overlay.kind !== "completion"
  const footer: RenderLine[] = modalOverlay
    ? [line(), line(twoColumnFooter(left, right, width), "secondary")]
    : [line(), ...composer.lines, line(twoColumnFooter(left, right, width), "secondary")]

  let visibleBody = body
  if (height !== undefined) {
    const bodyBudget = Math.max(0, height - footer.length)
    if (body.length > bodyBudget) {
      const usable = Math.max(0, bodyBudget - 1)
      const end = Math.max(0, body.length - Math.min(scrollOffset, body.length))
      const start = Math.max(0, end - usable)
      const hidden = start + (body.length - end)
      const virtualization = timeline.omitted ? ` · ${timeline.omitted} 个较早记录已虚拟化` : ""
      visibleBody = [line(`… ${hidden} 行已隐藏${virtualization} · PageUp/PageDown 滚动`, "secondary"), ...body.slice(start, end)]
    }
  }
  const visible = [...visibleBody, ...footer].map((item) => ({ ...item, text: clipDisplay(item.text, width) }))
  const composerStart = visible.length - footer.length + 1
  const cursor = acceptingInput && !modalOverlay
    ? {
        row: composerStart + Math.min(composer.cursorRow, composer.lines.length - 1),
        column: Math.min(width - 1, composer.cursorColumn),
      }
    : undefined
  return {
    text: `${visible.map((item) => styleLine(item, options.color === true)).join("\n")}\n`,
    cursor,
  }
}

export function renderProductState(state: ProductViewState, options: ProductRenderOptions): string {
  return renderProductFrame(state, options).text
}

export function renderProductSnapshot(events: readonly ZyraUiEvent[], options: ProductRenderOptions): string {
  return renderProductState(reduceProductEvents(events), options)
}
