import type { UiFileChange, UiPermissionRequest, ZyraUiEvent } from "./events.ts"

interface UiMessageState {
  messageId: string
  role: "user" | "assistant"
  text: string
}

interface UiActivityState {
  activityId: string
  label: string
  status: "pending" | "running" | "completed"
  outcome?: string
}

interface UiToolState {
  toolCallId: string
  name: string
  summary: string
  status: "running" | "completed" | "failed"
}

export interface ProductViewState {
  sessionId?: string
  taskId?: string
  messages: readonly UiMessageState[]
  activities: readonly UiActivityState[]
  tools: readonly UiToolState[]
  permissions: readonly UiPermissionRequest[]
  changes: readonly UiFileChange[]
  connection: "connected" | "reconnecting" | "disconnected"
  reconnectAttempt?: number
  taskStatus: "idle" | "running" | "completed" | "failed" | "cancelled"
  taskMessage?: string
}

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
}

const COMBINING = /[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f\ufe0e\ufe0f]/u

function runeWidth(value: string): number {
  const code = value.codePointAt(0) ?? 0
  if (COMBINING.test(value) || code === 0x200d) return 0
  if (
    code >= 0x1100 && (
      code <= 0x115f
      || code === 0x2329
      || code === 0x232a
      || (code >= 0x2e80 && code <= 0xa4cf && code !== 0x303f)
      || (code >= 0xac00 && code <= 0xd7a3)
      || (code >= 0xf900 && code <= 0xfaff)
      || (code >= 0xfe10 && code <= 0xfe19)
      || (code >= 0xfe30 && code <= 0xfe6f)
      || (code >= 0xff00 && code <= 0xff60)
      || (code >= 0xffe0 && code <= 0xffe6)
      || (code >= 0x1f300 && code <= 0x1faff)
      || (code >= 0x20000 && code <= 0x3fffd)
    )
  ) return 2
  return 1
}

export function displayWidth(value: string): number {
  return Array.from(value).reduce((sum, rune) => sum + runeWidth(rune), 0)
}

function takeWidth(value: string, width: number): { head: string; tail: string } {
  let used = 0
  let head = ""
  const runes = Array.from(value)
  let index = 0
  while (index < runes.length) {
    const rune = runes[index]!
    const next = runeWidth(rune)
    if (used + next > width) break
    head += rune
    used += next
    index += 1
  }
  return { head, tail: runes.slice(index).join("") }
}

function padFit(value: string, width: number): string {
  if (displayWidth(value) <= width) return value + " ".repeat(Math.max(0, width - displayWidth(value)))
  const clipped = takeWidth(value, Math.max(1, width - 1)).head
  return `${clipped}…`
}

function clip(value: string, width: number): string {
  if (displayWidth(value) <= width) return value
  return `${takeWidth(value, Math.max(1, width - 1)).head}…`
}

function wrapLine(value: string, width: number): string[] {
  if (!value) return [""]
  const lines: string[] = []
  let remaining = value
  while (displayWidth(remaining) > width) {
    const selected = takeWidth(remaining, width)
    if (!selected.head) break
    lines.push(selected.head)
    remaining = selected.tail
  }
  lines.push(remaining)
  return lines
}

function prefixed(value: string, prefix: string, width: number): string[] {
  const continuation = " ".repeat(displayWidth(prefix))
  const available = Math.max(8, width - displayWidth(prefix))
  const output: string[] = []
  for (const source of value.split("\n")) {
    if (!source) {
      output.push("")
      continue
    }
    const chunks = wrapLine(source, available)
    for (const [index, chunk] of chunks.entries()) output.push(`${output.length === 0 && index === 0 ? prefix : continuation}${chunk}`)
  }
  return output
}

function findMessage(messages: UiMessageState[], messageId: string, role: UiMessageState["role"]): UiMessageState {
  let selected = messages.find((message) => message.messageId === messageId)
  if (!selected) {
    selected = { messageId, role, text: "" }
    messages.push(selected)
  }
  return selected
}

export function reduceProductEvents(events: readonly ZyraUiEvent[]): ProductViewState {
  const messages: UiMessageState[] = []
  const activities = new Map<string, UiActivityState>()
  const tools = new Map<string, UiToolState>()
  const permissions = new Map<string, UiPermissionRequest>()
  const changes = new Map<string, UiFileChange>()
  let sessionId: string | undefined
  let taskId: string | undefined
  let connection: ProductViewState["connection"] = "connected"
  let reconnectAttempt: number | undefined
  let taskStatus: ProductViewState["taskStatus"] = "idle"
  let taskMessage: string | undefined

  for (const event of events) {
    switch (event.type) {
      case "session.started":
        sessionId = event.sessionId
        taskId = event.taskId
        taskStatus = event.taskId ? "running" : "idle"
        break
      case "user.message":
        findMessage(messages, event.messageId, "user").text = event.text
        break
      case "assistant.message.started":
        findMessage(messages, event.messageId, "assistant")
        break
      case "assistant.message.delta":
        findMessage(messages, event.messageId, "assistant").text += event.text
        break
      case "assistant.message.completed":
        findMessage(messages, event.messageId, "assistant").text = event.text
        break
      case "activity.started":
      case "activity.updated":
      case "activity.completed":
        activities.set(event.activityId, {
          activityId: event.activityId,
          label: event.label,
          status: event.type === "activity.completed" ? "completed" : event.type === "activity.started" ? "running" : "pending",
          outcome: event.type === "activity.completed" ? event.outcome : undefined,
        })
        break
      case "tool.started":
        tools.set(event.toolCallId, { toolCallId: event.toolCallId, name: event.name, summary: event.summary, status: "running" })
        break
      case "tool.updated": {
        const prior = tools.get(event.toolCallId)
        tools.set(event.toolCallId, { toolCallId: event.toolCallId, name: prior?.name ?? "工具", summary: event.summary, status: "running" })
        break
      }
      case "tool.completed": {
        const prior = tools.get(event.toolCallId)
        tools.set(event.toolCallId, { toolCallId: event.toolCallId, name: prior?.name ?? "工具", summary: event.summary, status: "completed" })
        break
      }
      case "tool.failed": {
        const prior = tools.get(event.toolCallId)
        tools.set(event.toolCallId, { toolCallId: event.toolCallId, name: prior?.name ?? "工具", summary: event.message, status: "failed" })
        break
      }
      case "permission.requested":
        permissions.set(event.request.requestId, event.request)
        break
      case "permission.resolved":
        permissions.delete(event.requestId)
        break
      case "workspace.changed":
        for (const change of event.changes) changes.set(`${change.kind}:${change.path}`, change)
        break
      case "task.completed":
        taskId = event.taskId
        taskStatus = "completed"
        break
      case "task.failed":
        taskId = event.taskId
        taskStatus = "failed"
        taskMessage = event.recovery ? `${event.message} ${event.recovery}` : event.message
        break
      case "task.cancelled":
        taskId = event.taskId
        taskStatus = "cancelled"
        taskMessage = event.message
        break
      case "transport.reconnecting":
        connection = "reconnecting"
        reconnectAttempt = event.attempt
        break
      case "transport.recovered":
        connection = "connected"
        reconnectAttempt = undefined
        break
      case "subagent.updated":
        break
    }
  }

  return Object.freeze({
    sessionId,
    taskId,
    messages: Object.freeze(messages.map((item) => Object.freeze({ ...item }))),
    activities: Object.freeze([...activities.values()]),
    tools: Object.freeze([...tools.values()]),
    permissions: Object.freeze([...permissions.values()]),
    changes: Object.freeze([...changes.values()]),
    connection,
    reconnectAttempt,
    taskStatus,
    taskMessage,
  })
}

function connectionLabel(state: ProductViewState): string {
  if (state.connection === "reconnecting") return `正在重连（第 ${state.reconnectAttempt ?? 1} 次）`
  if (state.connection === "disconnected") return "连接已断开"
  return "已连接"
}

export function renderProductSnapshot(events: readonly ZyraUiEvent[], options: ProductRenderOptions): string {
  const width = Math.max(40, Math.floor(options.width))
  const state = reduceProductEvents(events)
  const lines: string[] = []
  const inner = width - 2
  const header = ` >_ Zyra (v${options.version ?? "0.1.0"})`
  const workspace = ` ${options.workspace} · ${connectionLabel(state)} · 产品模式`
  lines.push(`╭${"─".repeat(inner)}╮`)
  lines.push(`│${padFit(header, inner)}│`)
  lines.push(`│${padFit(workspace, inner)}│`)
  lines.push(`╰${"─".repeat(inner)}╯`)

  for (const message of state.messages) {
    if (!message.text) continue
    lines.push("")
    lines.push(...prefixed(message.text, message.role === "user" ? "› " : "• ", width))
  }

  const runningActivity = state.activities.find((activity) => activity.status === "running")
  const completedActivities = state.activities.filter((activity) => activity.status === "completed").length
  if (runningActivity) {
    lines.push("")
    lines.push(...prefixed(`${runningActivity.label}（Esc 中断）`, "• ", width))
  } else if (completedActivities) {
    lines.push("")
    lines.push(...prefixed(`已完成 ${completedActivities} 个步骤`, "✓ ", width))
  }

  const visibleTools = state.tools.slice(-3)
  for (const tool of visibleTools) lines.push(...prefixed(tool.summary, tool.status === "failed" ? "! " : "  ", width))

  for (const permission of state.permissions) {
    lines.push("")
    lines.push(...prefixed(`需要权限：${permission.action}${permission.target ? ` · ${permission.target}` : ""}`, "! ", width))
    if (permission.reason) lines.push(...prefixed(permission.reason, "  ", width))
    if (permission.risk) lines.push(...prefixed(`风险：${permission.risk}`, "  ", width))
    if (permission.scope) lines.push(...prefixed(`作用域：${permission.scope}`, "  ", width))
    if (permission.expiresAt) lines.push(...prefixed(`有效期至：${permission.expiresAt}`, "  ", width))
    lines.push(...prefixed(`[A] 允许本次   [D] 拒绝 · ${permission.requestId}`, "  ", width))
  }

  if (state.changes.length) {
    lines.push("")
    lines.push(...prefixed(`${state.changes.length} 个文件发生变更`, "✓ ", width))
    for (const change of state.changes.slice(0, 5)) lines.push(...prefixed(`${change.kind.padEnd(8)} ${change.path}`, "  ", width))
  }

  if (state.taskMessage) {
    lines.push("")
    lines.push(...prefixed(state.taskMessage, state.taskStatus === "failed" ? "! " : "• ", width))
  }
  if (state.connection === "reconnecting") {
    lines.push("")
    lines.push(...prefixed(connectionLabel(state), "◌ ", width))
  }

  if (options.notice) {
    lines.push("")
    lines.push(...prefixed(options.notice, "! ", width))
  }
  lines.push("")
  lines.push(...prefixed(options.composerText || options.placeholder || "向 Zyra 描述任务", "› ", width))
  const leadingStatus = options.running
    ? "Tab 排队 · Esc 中断"
    : state.taskStatus === "completed"
      ? "任务已完成"
      : state.taskStatus === "failed"
        ? "任务失败"
        : "? 查看快捷键"
  const status = `${leadingStatus} · ${connectionLabel(state)}${state.taskId ? ` · ${state.taskId}` : ""}`
  lines.push(clip(`  ${status}`, width))
  let visible = lines.map((line) => clip(line, width))
  const height = options.height === undefined ? undefined : Math.max(8, Math.floor(options.height))
  if (height !== undefined && visible.length > height) {
    const header = visible.slice(0, 4)
    const footer = visible.slice(-4)
    const body = visible.slice(4, -4)
    const bodyBudget = Math.max(0, height - header.length - footer.length - 1)
    const offset = Math.max(0, Math.min(body.length, Math.floor(options.scrollOffset ?? 0)))
    const end = Math.max(0, body.length - offset)
    const start = Math.max(0, end - bodyBudget)
    const hidden = start + (body.length - end)
    visible = [
      ...header,
      clip(`… ${hidden} 行已隐藏 · PageUp/PageDown 滚动`, width),
      ...body.slice(start, end),
      ...footer,
    ]
  }
  return `${visible.join("\n")}\n`
}
