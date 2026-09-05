import type { Readable, Writable } from "node:stream"
import { buildCommandResultModel, type CommandQueueSnapshot, type CommandReceipt } from "@zyra/commands"
import { createSessionId, ZyraApiError, type SessionProjection, type TaskProjection } from "@zyra/typed-api-client"
import { CliApi, type IngressCapabilities, type IngressPage, type ProductExecutionConfig, type UserInputRequestProjection } from "../api.ts"
import { CliExitCode, CliTaskError, type InteractiveCommand, type ResumeCommand } from "../contracts.ts"
import {
  CliControlSession,
  formatCommandReceipt,
  parseControlIntent,
} from "../control/commands.ts"
import {
  CliPermissionSession,
  type PermissionDecisionScope,
  type PermissionModeName,
  type PermissionModeView,
  type PermissionRequestView,
  type UserSelectablePermissionMode,
} from "../control/permission.ts"
import type { UiPermissionSnapshot, UiUserInputRequest } from "../presentation/events.ts"
import { ProductProjection } from "../presentation/projection.ts"
import { buildBoundedWorkspaceDiff } from "../presentation/workspace-diff.ts"
import { parseProductCommand, productCommandCandidates, productCommandHelp } from "../product/commands/registry.ts"
import { openProductArtifact } from "../product/artifact/controller.ts"
import { workspaceReferenceCandidates } from "../product/files/index.ts"
import { openProductDiff } from "../product/diff/controller.ts"
import { formatExecutionMode, formatModelStatus, formatRuntimeReadiness, providerConfigurationFromReadiness, type ProductExecutionMode } from "../product/diagnostics/status.ts"
import { ProductOnboardingStore } from "../product/onboarding/state.ts"
import { ProductDraftStore } from "../product/session/local-state.ts"
import { copyLatestAssistantMessage, exportProductTranscript, rawTranscriptLines } from "../product/transcript/export.ts"
import { ProductTuiShell } from "../tui/shell.ts"
import { formatTerminalCapabilities } from "../tui/terminal-capabilities.ts"
import { sanitizeForOutput } from "../output.ts"
import { mutationTransportDetached, type CommandOutcome } from "../runner.ts"
import { launchUi } from "../ui.ts"
import type { LocalExecutorEnvironment } from "../terminal/lifecycle.ts"

function terminalTask(task: TaskProjection): boolean {
  return task.terminal || ["completed", "failed", "blocked", "cancelled", "killed"].includes(task.status)
}

function terminalExitCode(task: TaskProjection): CliExitCode {
  if (task.status === "completed") return CliExitCode.SUCCESS
  if (task.status === "cancelled") return CliExitCode.CANCELLED
  return CliExitCode.TASK_FAILED
}

function productTaskStatus(status: string): string {
  const labels: Readonly<Record<string, string>> = {
    idle: "尚未开始",
    pending: "准备中",
    queued: "等待执行",
    running: "运行中",
    needs_revision: "等待调整后继续",
    blocked: "已阻塞",
    completed: "已完成",
    passed: "通过",
    skipped: "已跳过",
    superseded: "已替换",
    failed: "失败",
    cancelled: "已取消",
    killed: "已终止",
  }
  return labels[status] ?? status
}

function productConnectionStatus(status: string): string {
  const labels: Readonly<Record<string, string>> = {
    idle: "未连接",
    connected: "已连接",
    reconnecting: "正在重连",
    disconnected: "连接中断",
    failed: "连接失败",
  }
  return labels[status] ?? status
}

function formatProductStatus(input: {
  view: ProductTuiShell["view"]
  fallbackSessionId?: string
  fallbackTaskId?: string
  capabilities: ProductTuiShell["terminalCapabilities"]
}): string {
  const sessionId = input.view.sessionId ?? input.fallbackSessionId
  const taskId = input.view.taskId ?? input.fallbackTaskId
  return [
    `任务 · ${productTaskStatus(input.view.taskStatus)}`,
    `连接 · ${productConnectionStatus(input.view.connection)}`,
    input.view.context ? `上下文 · 剩余 ${input.view.context.remainingPercent}%${input.view.context.compactNeeded ? " · 建议压缩" : ""}` : undefined,
    "",
    "详细信息",
    `会话编号 · ${sessionId ?? "尚未创建"}`,
    `任务编号 · ${taskId ?? "尚未创建"}`,
    formatTerminalCapabilities(input.capabilities),
  ].filter((line): line is string => line !== undefined).join("\n")
}

function recoveryNeedsSnapshot(error: unknown): boolean {
  if (error instanceof CliTaskError) {
    return ["gap", "cursor", "generation", "order", "binding"].some((marker) => error.code.includes(marker))
  }
  if (!(error instanceof ZyraApiError)) return false
  if (["conflict", "not_found", "version", "protocol", "cursor"].includes(error.category)) return true
  if (["gap", "cursor", "generation", "order", "binding"].some((marker) => error.code.includes(marker))) return true
  const body = (error as { body?: unknown }).body
  return Boolean(
    body
    && typeof body === "object"
    && !Array.isArray(body)
    && (body as Record<string, unknown>).resyncRequired === true,
  )
}

function recoveryIsTransient(error: unknown): boolean {
  return error instanceof ZyraApiError
    && (error.retryable || ["disconnect", "timeout", "server", "unavailable"].includes(error.category))
}

function shouldReportRecoveryAttempt(attempt: number): boolean {
  return attempt === 1 || (attempt & (attempt - 1)) === 0
}

const RECOVERY_ATTEMPT_BUDGET = 100
const RECOVERY_OUTAGE_BUDGET_MS = 120_000

function wait(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason)
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort)
      resolve()
    }, milliseconds)
    const abort = () => { clearTimeout(timer); reject(signal.reason) }
    signal.addEventListener("abort", abort, { once: true })
  })
}

function permissionSnapshot(view: PermissionRequestView): UiPermissionSnapshot {
  const raw = view.raw
  return Object.freeze({
    requestId: view.requestId,
    status: view.status,
    prompt: view.prompt,
    reason: view.reason,
    toolName: view.toolName,
    operation: view.operation,
    target: typeof raw.target === "string" ? raw.target : undefined,
    risk: typeof raw.risk === "string"
      ? raw.risk
      : typeof raw.risk_level === "string" ? raw.risk_level : undefined,
    expiresAt: view.expiresAt,
    scope: typeof raw.scope === "string" ? raw.scope : undefined,
    selectable: view.selectable,
  })
}

function userInputSnapshot(request: UserInputRequestProjection): UiUserInputRequest {
  return Object.freeze({
    requestId: request.requestId,
    status: request.status,
    revision: request.revision,
    questions: request.questions,
    answers: request.answers,
    createdAt: request.createdAt,
    updatedAt: request.updatedAt,
  })
}

function controlError(error: unknown): string {
  const value = error instanceof CliTaskError
    ? error.message
    : error instanceof Error ? error.message : String(error)
  return String(sanitizeForOutput(value))
}

function formatProductCommandReceipt(receipt: Parameters<typeof formatCommandReceipt>[0]): string {
  if (receipt.error) return `操作未提交 · ${receipt.error.message}`
  const action = receipt.phase === "applied"
    ? "操作已完成"
    : ["rejected", "expired", "cancelled"].includes(receipt.phase)
      ? "操作未完成"
      : "操作已提交"
  return `${action} · ${receipt.name}`
}

function commandResultLines(receipt: CommandReceipt): string[] {
  const result = buildCommandResultModel(receipt, { maximumRows: 120, maximumValueLength: 2_048 })
  const lines = [result.summary || result.displayText]
  if (result.displayText && result.displayText !== result.summary) lines.push(result.displayText)
  if (result.usage) {
    const usage = [
      `${result.usage.inputTokens} input`,
      `${result.usage.outputTokens} output`,
      result.usage.cachedTokens ? `${result.usage.cachedTokens} cached` : undefined,
      result.usage.costUsd === undefined ? undefined : `$${result.usage.costUsd.toFixed(4)}`,
    ].filter(Boolean).join(" · ")
    lines.push("", `Usage: ${usage}`)
  }
  const internalLabels = new Set(["Command", "Request", "Queue", "Task", "Run", "Session", "Idempotency", "Events"])
  for (const section of result.sections) {
    lines.push("", `## ${section.title}`)
    if (section.description) lines.push(section.description)
    for (const row of section.rows) {
      const marker = row.tone === "error" ? "!" : row.tone === "success" ? "✓" : "•"
      lines.push(`${marker} ${row.title}${row.status ? ` · ${row.status}` : ""}`)
      if (row.summary && row.summary !== row.title) lines.push(`  ${row.summary}`)
      for (const field of row.fields) {
        if (!internalLabels.has(field.label)) lines.push(`  ${field.label}: ${field.value}`)
      }
    }
  }
  return lines
}

function productQueueSummary(snapshot: CommandQueueSnapshot): string {
  if (!snapshot.items.length) return "当前没有排队消息。"
  const now = snapshot.items.filter((item) => item.priority === "now").length
  const next = snapshot.items.filter((item) => item.priority === "next").length
  const later = snapshot.items.length - now - next
  return `队列中有 ${snapshot.items.length} 条消息${now ? ` · ${now} 立即` : ""}${next ? ` · ${next} 下一步` : ""}${later ? ` · ${later} 稍后` : ""}`
}

async function openProductQueue(input: {
  shell: ProductTuiShell
  controls: CliControlSession
  signal: AbortSignal
}): Promise<string> {
  const snapshot = await input.controls.queue(input.signal, true)
  if (!snapshot.items.length || !input.shell.interactive) return productQueueSummary(snapshot)
  const selected = await input.shell.pick("排队消息", snapshot.items.map((item, index) => ({
    id: String(index),
    label: (item.text ?? item.name ?? "排队操作").replace(/\s+/gu, " ").slice(0, 120),
    detail: `${item.priority === "now" ? "立即" : item.priority === "next" ? "下一步" : "稍后"} · ${item.phase}`,
    keywords: [item.text ?? "", item.name ?? "", item.priority, item.phase],
  })), "选择一条消息可将其从队列中取消")
  if (!selected) return productQueueSummary(snapshot)
  const item = snapshot.items[Number(selected.id)]
  if (!item?.requestId) return "这条消息尚未获得可取消的服务端回执，请稍后重试。"
  const decision = await input.shell.pick(
    "取消这条排队消息吗？",
    [
      { id: "cancel", label: "取消这条消息", detail: "从队列移除，主任务继续运行" },
      { id: "keep", label: "保留", detail: "不做任何更改" },
    ],
    undefined,
    "approval",
    [(item.text ?? item.name ?? "排队操作").replace(/\s+/gu, " ").slice(0, 240)],
  )
  if (decision?.id !== "cancel") return "已保留排队消息。"
  await input.controls.cancelCommand(item.requestId, input.signal)
  return "排队消息已取消。"
}

function planLines(view: ProductTuiShell["view"]): string[] {
  if (view.plan) {
    const lines = [`计划 · 第 ${view.plan.revision} 版`]
    if (view.plan.changes.length) {
      lines.push("", "计划变更")
      for (const change of view.plan.changes) {
        const affected = change.affectedStepIds.length ? ` · 影响 ${change.affectedStepIds.length} 个步骤` : ""
        lines.push(`↻ ${change.summary}${affected}`)
      }
    }
    lines.push("", "步骤")
    for (const [index, step] of view.plan.steps.entries()) {
      const marker = step.status === "completed" ? "✓" : step.status === "running" ? "◌" : step.status === "failed" ? "!" : step.status === "superseded" ? "↻" : "○"
      const details = [productTaskStatus(step.status), step.assignedAgentId ? "由协作代理处理" : undefined, step.dependsOn.length ? `依赖 ${step.dependsOn.length} 个前置步骤` : undefined].filter(Boolean).join(" · ")
      lines.push(`${marker} ${index + 1}. ${step.label} · ${details}`)
      if (step.description && step.description !== step.label) lines.push(`   ${step.description}`)
    }
    return lines
  }
  if (!view.activities.length) return ["当前还没有计划步骤。"]
  return view.activities.map((activity, index) => {
    const marker = activity.status === "completed" ? "✓" : activity.status === "running" ? "◌" : "○"
    const detail = [activity.category, activity.outcome, activity.summary].filter(Boolean).join(" · ")
    return `${marker} ${index + 1}. ${activity.label}${detail ? ` · ${detail}` : ""}`
  })
}

function toolDisplayName(name: string): string {
  const normalized = name.toLowerCase()
  if (/write|edit|patch|apply/.test(normalized)) return "编辑文件"
  if (/shell|exec|command|terminal|test/.test(normalized)) return "运行命令"
  if (/search|find|read|list|glob|grep/.test(normalized)) return "检查项目"
  if (/web|http|browser|fetch/.test(normalized)) return "访问网络"
  return "使用工具"
}

function byteLabel(value: number | undefined): string {
  if (value === undefined) return "大小未知"
  if (value < 1_024) return `${value} B`
  if (value < 1_024 * 1_024) return `${(value / 1_024).toFixed(1)} KiB`
  return `${(value / (1_024 * 1_024)).toFixed(1)} MiB`
}

async function pickTaskArtifact(shell: ProductTuiShell, task: TaskProjection): Promise<string | undefined> {
  if (!task.artifacts.length) {
    shell.notice("当前任务还没有生成文件或交付物。")
    return undefined
  }
  return (await shell.pick("任务交付物", task.artifacts.map((artifact) => ({
    id: artifact.artifactId,
    label: artifact.title || artifact.path || artifact.kind || "任务产物",
    detail: artifact.mediaType || artifact.kind,
  }))))?.id
}

async function openToolBrowser(input: {
  shell: ProductTuiShell
  openArtifact: (artifactId: string) => Promise<void>
}): Promise<void> {
  const tools = input.shell.view.tools
  if (!tools.length) {
    await input.shell.page("工具调用", ["当前还没有工具调用。"])
    return
  }
  const selected = await input.shell.pick("工具调用", tools.map((tool) => ({
    id: tool.toolCallId,
    label: `${tool.status === "failed" ? "!" : tool.status === "completed" ? "✓" : "◌"} ${toolDisplayName(tool.name)}`,
    detail: `${tool.summary}${tool.outputRefs?.length ? ` · ${tool.outputRefs.length} 份输出` : ""}`,
    keywords: [tool.name, tool.status, tool.summary, ...(tool.outputRefs ?? []).map((item) => item.stream)],
  })), "选择工具查看摘要、耗时和输出")
  const tool = selected ? tools.find((item) => item.toolCallId === selected.id) : undefined
  if (!tool) return
  const details = [
    `${toolDisplayName(tool.name)} · ${productTaskStatus(tool.status)}`,
    tool.summary,
    `工具名称 · ${tool.name}`,
    `调用编号 · ${tool.toolCallId}`,
    `耗时 · ${tool.durationMs === undefined ? "未记录" : `${tool.durationMs}ms`}`,
    ...(tool.outputRefs ?? []).map((item) => `${item.stream} · ${byteLabel(item.sizeBytes)} · ${item.mediaType} · 产物 ${item.artifactId}`),
    ...(tool.artifactIds?.length ? [`产物 · ${tool.artifactIds.join(", ")}`] : []),
  ]
  if (!tool.outputRefs?.length) {
    await input.shell.page(tool.name, details)
    return
  }
  const output = await input.shell.pick("工具输出", [
    { id: "__details", label: "查看工具详情", detail: "状态、耗时与产物引用" },
    ...tool.outputRefs.map((item) => ({
      id: item.artifactId,
      label: item.stream,
      detail: `${byteLabel(item.sizeBytes)} · ${item.mediaType} · 有界安全预览`,
      keywords: [item.stream, item.title, item.mediaType, item.artifactId],
    })),
  ], "完整输出不会写入主对话；预览已脱敏且限制为 64 KiB")
  if (!output) return
  if (output.id === "__details") {
    await input.shell.page(tool.name, details)
  } else {
    try {
      await input.openArtifact(output.id)
    } catch (error) {
      input.shell.notice(`工具输出不可用 · ${controlError(error)}`)
    }
  }
}

function verificationLines(view: ProductTuiShell["view"]): string[] {
  const verification = view.verification
  if (!verification) return ["当前任务还没有验证结果。"]
  const lines = [
    `${verification.status === "passed" ? "✓" : verification.status === "failed" ? "!" : "○"} ${verification.label}`,
    `执行记录 · ${verification.commandEvidence === "recorded" ? "已记录实际命令" : "没有命令级记录；不能据此声称命令已经运行"}`,
  ]
  for (const check of verification.checks) {
    const marker = check.status === "passed" ? "✓" : check.status === "failed" ? "!" : check.status === "skipped" ? "↷" : "○"
    const command = check.command ? ` · ${check.command}` : ""
    const exit = check.exitCode === undefined ? "" : ` · exit ${check.exitCode}`
    const summary = check.summary ? ` · ${check.summary}` : ""
    lines.push(`${marker} ${check.name} · ${productTaskStatus(check.status)}${command}${exit}${summary}`)
  }
  if (!verification.checks.length) lines.push("没有逐项执行记录；这里只显示任务报告的最终验证状态。")
  return lines
}

function agentLines(view: ProductTuiShell["view"]): string[] {
  if (!view.agents.length) return ["当前没有可见协作代理。"]
  return view.agents.map((agent, index) => `${index + 1}. ${agent.label} · ${agent.status}${agent.summary ? ` · ${agent.summary}` : ""} · ${agent.agentId}`)
}

export function agentContextLines(view: ProductTuiShell["view"], agentId: string): string[] {
  const agent = view.agents.find((item) => item.agentId === agentId)
  if (!agent) return [`未找到协作代理 ${agentId}。`]
  const assigned = view.plan?.steps.filter((step) => step.assignedAgentId === agent.agentId) ?? []
  const lines = [
    `${agent.label} · ${agent.status}`,
    `identity · ${agent.agentId}`,
    ...(agent.summary ? [`summary · ${agent.summary}`] : []),
    ...(agent.impact ? [`impact · ${agent.impact}`] : []),
    ...(agent.code ? [`code · ${agent.code}`] : []),
    ...(agent.retryable !== undefined ? [`retryable · ${agent.retryable ? "yes" : "no"}`] : []),
    ...(agent.recovery ? [`recovery · ${agent.recovery}`] : []),
    "",
    `assigned plan steps · ${assigned.length}`,
    ...assigned.map((step) => `${step.status === "completed" ? "✓" : step.status === "running" ? "◌" : step.status === "failed" ? "!" : "○"} ${step.label} · ${step.status} · ${step.stepId}`),
  ]
  return lines
}

async function openAgentBrowser(input: { shell: ProductTuiShell; requestedId?: string }): Promise<void> {
  const agents = input.shell.view.agents
  if (!agents.length) {
    await input.shell.page("协作代理", agentLines(input.shell.view))
    return
  }
  const direct = input.requestedId?.trim()
  const selectedId = direct || (await input.shell.pick("协作代理", agents.map((agent) => ({
    id: agent.agentId,
    label: `${agent.status === "failed" ? "!" : ["completed", "succeeded"].includes(agent.status) ? "✓" : "◌"} ${agent.label}`,
    detail: `${agent.status}${agent.summary ? ` · ${agent.summary}` : ""}`,
    keywords: [agent.agentId, agent.label, agent.status, agent.summary ?? "", agent.code ?? ""],
  })), "输入名称/identity 过滤 · Enter 查看上下文"))?.id
  if (!selectedId) return
  await input.shell.page("代理上下文", agentContextLines(input.shell.view, selectedId))
}

export function productWorkflowGoal(name: "review" | "init", focus = ""): string {
  const selected = focus.trim()
  if (name === "review") {
    return [
      "Review the current workspace changes as an independent code reviewer.",
      "Prioritize correctness bugs, regressions, security issues, unsafe failure paths, and missing tests.",
      "Do not modify files unless the user explicitly asks after reviewing the findings.",
      "Report findings first, ordered by severity, with precise file and line references; then list residual risks and verification gaps.",
      selected ? `Review focus: ${selected}` : "Review focus: all current tracked and untracked workspace changes within the task boundary.",
    ].join("\n")
  }
  return [
    "Initialize this repository for reliable Zyra agent work.",
    "Inspect the actual repository structure, existing AGENTS.md files, build/test commands, and safety constraints before editing.",
    "Create or improve the root AGENTS.md with concise, verified instructions; preserve existing user rules and do not overwrite them blindly.",
    "Run proportionate validation and clearly report every file changed and any unverified command.",
    selected ? `Initialization focus: ${selected}` : "Initialization focus: the current workspace root.",
  ].join("\n")
}

async function showProductHelp(shell: ProductTuiShell, running: boolean): Promise<void> {
  await shell.page("快捷键与命令", [
    running ? "Enter 立即引导 · Tab 排队 · Esc 收起菜单 / 中断" : "Enter 提交 · Alt+Enter / Ctrl+J 换行 · Ctrl+E 外部编辑器",
    "Ctrl+R 恢复草稿 · PageUp/PageDown 回看 · Ctrl+C 清空/退出",
    "",
    ...productCommandHelp(running).split("\n"),
  ])
}

function permissionField(request: PermissionRequestView, ...names: string[]): string | undefined {
  for (const name of names) {
    const value = request.raw[name]
    if (typeof value === "string" && value.trim()) return value.trim()
  }
  return undefined
}

const PERMISSION_MODE_CHOICES: Readonly<Record<UserSelectablePermissionMode, {
  label: string
  detail: string
  keywords: readonly string[]
}>> = Object.freeze({
  default: {
    label: "按需询问",
    detail: "只读通常自动允许；编辑和未设规则的操作会请求确认",
    keywords: ["ask", "default", "询问", "默认"],
  },
  acceptEdits: {
    label: "自动接受编辑",
    detail: "自动允许非高风险的工作区编辑；其他未授权操作仍询问",
    keywords: ["edit", "write", "编辑", "写入"],
  },
  dontAsk: {
    label: "不询问",
    detail: "不弹出审批；未被规则允许的操作直接拒绝",
    keywords: ["deny", "dont ask", "拒绝", "不询问"],
  },
  plan: {
    label: "计划模式",
    detail: "允许只读信息收集，拒绝所有副作用",
    keywords: ["plan", "read only", "计划", "只读"],
  },
  auto: {
    label: "自治模式",
    detail: "允许低风险操作和非高风险编辑；高风险或未知操作直接拒绝",
    keywords: ["auto", "autonomous", "自治", "自动"],
  },
  bypassPermissions: {
    label: "托管权限绕过",
    detail: "仅在管理员明确授权时可用；不能绕过不可变的拒绝规则",
    keywords: ["managed", "bypass", "托管", "绕过"],
  },
})

function permissionModeStatus(mode: PermissionModeView): string {
  const choice = mode.mode === "sealed" ? undefined : PERMISSION_MODE_CHOICES[mode.mode]
  return [
    `权限模式 · ${choice?.label ?? "封闭自治"}`,
    choice?.detail ?? "由托管策略控制；普通终端不能退出该模式",
    `需要确认 · ${mode.interactive && !mode.headless ? "会在必要时询问" : "不会弹出询问"}`,
    `托管绕过 · ${mode.bypassAvailable ? "可用" : "不可用"}`,
    mode.reason ? `最近变更 · ${mode.reason}` : undefined,
    `策略版本 · ${mode.revision}`,
  ].filter(Boolean).join("\n")
}

async function pickPermissionMode(input: {
  shell: ProductTuiShell
  permissions: CliPermissionSession
  signal: AbortSignal
}): Promise<string> {
  const current = await input.permissions.mode(input.signal)
  if (current.mode === "sealed") {
    return `${permissionModeStatus(current)}\n封闭自治模式不能从普通终端退出；请结束当前任务或使用受管控制面。`
  }
  const names: UserSelectablePermissionMode[] = ["default", "acceptEdits", "dontAsk", "plan", "auto"]
  if (current.bypassAvailable) names.push("bypassPermissions")
  const selected = await input.shell.pick("选择当前任务权限模式", names.map((mode) => ({
    id: mode,
    label: `${mode === current.mode ? "✓ " : ""}${PERMISSION_MODE_CHOICES[mode].label}`,
    detail: PERMISSION_MODE_CHOICES[mode].detail,
    keywords: [...PERMISSION_MODE_CHOICES[mode].keywords],
  })), "选择后立即应用于当前任务；按 Esc 保持不变", "menu")
  if (!selected) return permissionModeStatus(current)
  const requested = selected.id as PermissionModeName
  if (!(requested in PERMISSION_MODE_CHOICES)) {
    throw new CliTaskError("选择器返回了未知权限模式。", "permission_mode_selection_invalid")
  }
  const result = await input.permissions.setMode({
    mode: requested as UserSelectablePermissionMode,
    expectedRevision: current.revision,
    signal: input.signal,
  })
  return `${result.reconciled ? "权限模式已与服务端状态核对" : "权限模式已更新"}\n${permissionModeStatus(result.state)}`
}

async function resolvePermissionRequest(input: {
  permissions: CliPermissionSession
  signal: AbortSignal
  refreshPermissions: () => Promise<void>
}, request: PermissionRequestView, effect: "allow" | "deny", decisionScope: PermissionDecisionScope): Promise<string> {
  const receipt = await input.permissions.resolve({
    requestId: request.requestId,
    effect,
    decisionScope,
    signal: input.signal,
  })
  await input.refreshPermissions()
  const scopeRule = receipt.scope_rule && typeof receipt.scope_rule === "object"
    ? receipt.scope_rule as Record<string, unknown>
    : undefined
  const scopeLabel = decisionScope === "workspace" ? "工作区持久范围" : decisionScope === "session" ? "本会话范围" : "仅本次"
  const installed = scopeRule && typeof scopeRule.installed === "boolean"
    ? ` · 规则${scopeRule.installed ? "已安装" : "已存在"}`
    : ""
  return `权限已${effect === "allow" ? "允许" : "拒绝"} · ${scopeLabel}${installed}`
}

export async function resolveSinglePermissionShortcut(input: {
  permissions: CliPermissionSession
  signal: AbortSignal
  refreshPermissions: () => Promise<void>
}, effect: "allow" | "deny"): Promise<string> {
  const pending = (await input.permissions.pending(input.signal)).filter((request) => request.selectable)
  if (pending.length !== 1) {
    throw new CliTaskError(
      pending.length ? "存在多个权限请求；请使用 /permissions 选择精确 request。" : "当前没有可处理权限请求。",
      pending.length ? "permission_shortcut_ambiguous" : "permission_selection_unavailable",
      { pending_request_count: pending.length },
    )
  }
  return resolvePermissionRequest(input, pending[0]!, effect, "once")
}

async function resolvePermissionFromPicker(input: {
  shell: ProductTuiShell
  permissions: CliPermissionSession
  signal: AbortSignal
  refreshPermissions: () => Promise<void>
}): Promise<string> {
  const pending = await input.permissions.pending(input.signal)
  const selectable = pending.filter((request) => request.selectable)
  if (!selectable.length) {
    throw new CliTaskError(
      pending.length ? "权限请求当前不可选择；custody、expiry 或绑定校验未通过。" : "当前没有待处理权限请求。",
      "permission_selection_unavailable",
    )
  }
  let request = selectable.length === 1 ? selectable[0] : undefined
  if (!request) {
    const selected = await input.shell.pick("选择权限请求", selectable.map((item) => ({
      id: item.requestId,
      label: item.prompt ?? item.operation ?? item.toolName ?? "受保护操作",
      detail: [permissionField(item, "risk", "risk_level") ?? "风险未标注", permissionField(item, "target")].filter(Boolean).join(" · "),
      keywords: [item.requestId, permissionField(item, "target") ?? "", item.reason ?? "", permissionField(item, "scope") ?? ""],
    })), "先选择精确请求；决定会绑定 canonical request identity")
    request = selected ? selectable.find((item) => item.requestId === selected.id) : undefined
  }
  if (!request) return "未处理权限请求。"
  const decisions = [
    { id: "allow:once", label: "允许本次", detail: "仅批准当前这一个操作" },
    ...(request.supportedDecisionScopes.includes("session") ? [{
      id: "allow:session",
      label: "本会话允许",
      detail: "仅匹配本会话内相同工具、操作和完全相同参数",
    }] : []),
    ...(request.supportedDecisionScopes.includes("workspace") ? [{
      id: "allow:workspace",
      label: "此工作区始终允许",
      detail: "持久保存；仅匹配此工作区内相同工具、操作和完全相同参数",
    }] : []),
    { id: "deny:once", label: "拒绝", detail: "拒绝当前操作并继续任务" },
  ]
  const permissionDescription = [
    request.prompt ?? request.operation ?? request.toolName ?? "受保护操作",
    permissionField(request, "target") ? `目标：${permissionField(request, "target")}` : undefined,
    request.reason ? `原因：${request.reason}` : undefined,
    permissionField(request, "risk", "risk_level") ? `风险：${permissionField(request, "risk", "risk_level")}` : undefined,
    request.expiresAt ? `过期：${request.expiresAt}` : undefined,
  ].filter((value): value is string => Boolean(value))
  const decision = await input.shell.pick(
    "允许 Zyra 执行这个操作吗？",
    decisions,
    undefined,
    "approval",
    permissionDescription,
  )
  if (!decision) return resolvePermissionRequest(input, request, "deny", "once")
  const [rawEffect, rawScope] = decision.id.split(":")
  const effect = rawEffect === "allow" ? "allow" : "deny"
  const decisionScope = (rawScope ?? "once") as PermissionDecisionScope
  return resolvePermissionRequest(input, request, effect, decisionScope)
}

async function resolveUserInputFromPicker(input: {
  shell: ProductTuiShell
  signal: AbortSignal
  pendingUserInputs: () => Promise<readonly UserInputRequestProjection[]>
  answerUserInput: (
    request: UserInputRequestProjection,
    answers: Readonly<Record<string, string>>,
  ) => Promise<UserInputRequestProjection>
  refreshUserInputs: () => Promise<void>
}): Promise<string> {
  const pending = (await input.pendingUserInputs()).filter((request) => request.status === "pending")
  if (!pending.length) throw new CliTaskError("当前没有等待回答的问题。", "user_input_unavailable")
  let request = pending.length === 1 ? pending[0] : undefined
  if (!request) {
    const selected = await input.shell.pick(
      "选择要回答的问题",
      pending.map((candidate) => ({
        id: candidate.requestId,
        label: candidate.questions[0]?.header ?? "Zyra 的问题",
        detail: candidate.questions[0]?.question,
      })),
      "Enter 回答 · Esc 稍后处理",
      "question",
    )
    request = selected ? pending.find((candidate) => candidate.requestId === selected.id) : undefined
  }
  if (!request) return "问题仍在等待回答；输入 /questions 可再次打开。"

  const answers: Record<string, string> = {}
  for (const [questionIndex, question] of request.questions.entries()) {
    const progress = request.questions.length > 1
      ? ` · ${questionIndex + 1}/${request.questions.length}`
      : ""
    const selected = await input.shell.pick(
      `${question.header}${progress}`,
      [
        ...question.options.map((option) => ({
          id: `option:${option.label}`,
          label: option.label,
          detail: option.description,
        })),
        { id: "other", label: "其他", detail: "输入自定义回答" },
      ],
      "↑↓ 选择 · Enter 确认 · Esc 稍后处理",
      "question",
      [question.question],
    )
    if (!selected) return "问题仍在等待回答；输入 /questions 可再次打开。"
    if (selected.id === "other") {
      const answer = await input.shell.prompt(`${question.header}${progress}`, [question.question])
      if (!answer) return "问题仍在等待回答；输入 /questions 可再次打开。"
      answers[question.id] = answer
    } else {
      answers[question.id] = selected.id.slice("option:".length)
    }
  }
  await input.answerUserInput(request, answers)
  await input.refreshUserInputs()
  return "回答已提交，Zyra 正在继续执行。"
}

async function runProductControlLoop(input: {
  shell: ProductTuiShell
  controls: CliControlSession
  permissions: CliPermissionSession
  permissionReady: Promise<void>
  signal: AbortSignal
  refreshPermissions: () => Promise<void>
  pendingUserInputs: () => Promise<readonly UserInputRequestProjection[]>
  answerUserInput: (
    request: UserInputRequestProjection,
    answers: Readonly<Record<string, string>>,
  ) => Promise<UserInputRequestProjection>
  refreshUserInputs: () => Promise<void>
  openWeb: () => Promise<string>
  openDiff: () => Promise<boolean>
  openArtifact: (artifactId: string) => Promise<void>
  taskStatus: () => Promise<TaskProjection>
  readiness: () => ReturnType<CliApi["readiness"]>
  detach: () => void
}): Promise<void> {
  while (!input.signal.aborted) {
    const result = await input.shell.read(true)
    if (result.kind === "shortcuts") {
      await showProductHelp(input.shell, true)
      continue
    }
    if (result.kind === "permission") {
      await input.permissionReady
      try {
        input.shell.notice(await resolvePermissionFromPicker(input))
      } catch (error) {
        input.shell.notice(`权限决定未提交 · ${controlError(error)}`)
      }
      continue
    }
    if (result.kind === "question") {
      try {
        input.shell.notice(await resolveUserInputFromPicker(input))
      } catch (error) {
        input.shell.notice(`回答未提交 · ${controlError(error)}`)
      }
      continue
    }
    if (result.kind === "closed") {
      input.detach()
      return
    }
    if (
      result.kind === "exit"
      || (result.kind === "submit" && ["/exit", "/detach"].includes(result.text.trim()))
    ) {
      input.detach()
      return
    }
    try {
      if (result.kind === "interrupt") {
        const receipt = await input.controls.submit("/change 用户从产品终端中断当前步骤", {
          mode: "interrupt",
          priority: "now",
          signal: input.signal,
        })
        input.shell.notice(formatProductCommandReceipt(receipt))
        continue
      }
      const line = result.text.trim()
      if (input.shell.view.permissions.length > 0 && (line.toLocaleLowerCase() === "a" || line.toLocaleLowerCase() === "d")) {
        input.shell.notice(await resolveSinglePermissionShortcut(input, line.toLocaleLowerCase() === "a" ? "allow" : "deny"))
        continue
      }
      if (line === "/help" || line === "?") {
        await showProductHelp(input.shell, true)
        continue
      }
      if (line === "/status") {
        const view = input.shell.view
        input.shell.notice(formatProductStatus({
          view,
          capabilities: input.shell.terminalCapabilities,
        }))
        continue
      }
      if (line === "/pwd") {
        input.shell.notice(process.cwd())
        continue
      }
      if (line === "/doctor") {
        input.shell.notice(formatRuntimeReadiness(await input.readiness()))
        continue
      }
      if (line === "/model") {
        input.shell.notice(formatModelStatus(await input.taskStatus()))
        continue
      }
      if (line === "/mode") {
        input.shell.notice(formatExecutionMode(await input.taskStatus()))
        continue
      }
      if (line === "/agents" || line.startsWith("/agents ") || line === "/subagents" || line.startsWith("/subagents ")) {
        const prefix = line.startsWith("/subagents") ? "/subagents" : "/agents"
        await openAgentBrowser({ shell: input.shell, requestedId: line.slice(prefix.length).trim() })
        continue
      }
      if (line === "/permissions mode") {
        await input.permissionReady
        input.shell.notice(await pickPermissionMode(input))
        continue
      }
      if (line === "/permissions status") {
        await input.permissionReady
        input.shell.notice(permissionModeStatus(await input.permissions.mode(input.signal)))
        continue
      }
      if (line === "/permissions") {
        await input.permissionReady
        input.shell.notice(await resolvePermissionFromPicker(input))
        continue
      }
      if (line === "/questions") {
        input.shell.rearmUserInput()
        input.shell.notice(await resolveUserInputFromPicker(input))
        continue
      }
      if (line === "/compact" || line.startsWith("/compact ")) {
        input.shell.notice(formatProductCommandReceipt(await input.controls.submit(line, {
          mode: "enqueue",
          priority: "next",
          signal: input.signal,
        })))
        continue
      }
      if (
        line === "/context" || line.startsWith("/context ")
        || line === "/tokens" || line.startsWith("/tokens ")
        || line === "/usage" || line.startsWith("/usage ")
      ) {
        const receipt = await input.controls.submit(line, {
          mode: "enqueue",
          priority: "next",
          signal: input.signal,
        })
        await input.shell.page("上下文与预算", commandResultLines(receipt))
        continue
      }
      if (line === "/btw" || line.startsWith("/btw ")) {
        if (!line.slice("/btw".length).trim()) throw new CliTaskError("/btw 需要一个问题。", "side_question_argument_missing")
        const receipt = await input.controls.submit(line, {
          mode: "enqueue",
          priority: "next",
          signal: input.signal,
        })
        await input.shell.page("旁路问题", commandResultLines(receipt))
        continue
      }
      if (
        line === "/memory" || line.startsWith("/memory ")
        || line === "/skills" || line.startsWith("/skills ")
        || line === "/mcp" || line.startsWith("/mcp ")
      ) {
        const receipt = await input.controls.submit(line, {
          mode: "enqueue",
          priority: "next",
          signal: input.signal,
        })
        const title = line.startsWith("/memory") ? "记忆" : line.startsWith("/skills") ? "技能" : "MCP"
        await input.shell.page(title, commandResultLines(receipt))
        continue
      }
      if (line === "/review" || line.startsWith("/review ")) {
        const focus = line.slice("/review".length).trim()
        input.shell.notice(formatProductCommandReceipt(await input.controls.submit(
          `/change ${productWorkflowGoal("review", focus)}`,
          { mode: "steer", priority: "now", signal: input.signal },
        )))
        continue
      }
      if (line === "/copy") {
        const copied = await copyLatestAssistantMessage(input.shell.view)
        input.shell.notice(`已复制最近一条回答 · ${byteLabel(copied.byteCount)}`)
        continue
      }
      if (line === "/export" || line.startsWith("/export ")) {
        const exported = await exportProductTranscript({
          view: input.shell.view,
          workspace: input.shell.workspace,
          requestedPath: line.slice("/export".length).trim() || undefined,
        })
        input.shell.notice(`对话已导出 · ${exported.path} · ${exported.messageCount} 条消息${exported.truncated ? " · 已达到安全上限" : ""}`)
        continue
      }
      if (line === "/raw") {
        await input.shell.page("纯文本对话", rawTranscriptLines(input.shell.view))
        continue
      }
      if (line === "/diff") {
        if (!await input.openDiff()) input.shell.notice("当前任务没有可审查的文件变更。使用 /ui 查看完整任务记录。")
        continue
      }
      if (line === "/plan") {
        await input.shell.page("计划与步骤", planLines(input.shell.view))
        continue
      }
      if (line === "/verification" || line === "/verify") {
        await input.shell.page("验证与收据", verificationLines(input.shell.view))
        continue
      }
      if (line === "/tools") {
        await openToolBrowser({ shell: input.shell, openArtifact: input.openArtifact })
        continue
      }
      if (line === "/artifact" || line.startsWith("/artifact ")) {
        const artifactId = line.slice("/artifact".length).trim()
          || await pickTaskArtifact(input.shell, await input.taskStatus())
        if (artifactId) await input.openArtifact(artifactId)
        continue
      }
      if (line === "/ui") {
        input.shell.notice(`已打开当前任务的 Web 看板：${await input.openWeb()}`)
        continue
      }
      if (line.toLowerCase() === "a" || line.toLowerCase() === "d") {
        await input.permissionReady
        const pending = await input.permissions.pending(input.signal)
        if (pending.length !== 1) {
          throw new CliTaskError(
            pending.length
              ? "存在多个权限请求，请从自动弹出的列表中选择。"
              : "当前没有可处理的权限请求。",
            "permission_selection_ambiguous",
          )
        }
        const effect = line.toLowerCase() === "a" ? "allow" : "deny"
        await input.permissions.resolve({ requestId: pending[0]!.requestId, effect, signal: input.signal })
        await input.refreshPermissions()
        input.shell.notice(`权限已${effect === "allow" ? "允许" : "拒绝"}`)
        continue
      }
      const intent = !line.startsWith("/")
        ? parseControlIntent(`${result.queue ? "" : "/redirect "}${line}`)
        : parseControlIntent(line)
      if (intent.kind === "queue") {
        input.shell.notice(await openProductQueue(input))
      } else if (intent.kind === "task-cancel") {
        const task = await input.controls.cancelTask(intent.reason)
        input.shell.notice(`任务取消已提交 · ${task.status}`)
      } else if (intent.kind === "task-continue") {
        const task = await input.controls.continueTask(input.signal)
        input.shell.notice(`任务继续执行 · ${task.status}`)
      } else if (intent.kind === "command-cancel") {
        await input.controls.cancelCommand(intent.requestId, input.signal)
        input.shell.notice(`队列命令已取消 · ${intent.requestId}`)
      } else if (intent.kind === "command-retry") {
        input.shell.notice(formatProductCommandReceipt(await input.controls.retry(intent.requestId, input.signal)))
      } else if (intent.kind === "permission") {
        await input.permissionReady
        await input.permissions.resolve({
          requestId: intent.requestId,
          effect: intent.effect,
          feedback: intent.feedback,
          signal: input.signal,
        })
        await input.refreshPermissions()
        input.shell.notice(`权限已${intent.effect === "allow" ? "允许" : "拒绝"}`)
      } else {
        input.shell.notice(formatProductCommandReceipt(await input.controls.submit(intent.text, {
          mode: intent.mode,
          priority: intent.priority,
          signal: input.signal,
        })))
      }
    } catch (error) {
      input.shell.notice(`操作未提交 · ${controlError(error)}`)
    }
  }
}

async function loadProjectionSnapshot(input: {
  api: CliApi
  task: TaskProjection
  capabilities: IngressCapabilities
  bootstrap?: ResumeBootstrap
}): Promise<{ projection: ProductProjection; cursor: string }> {
  const projection = new ProductProjection({
    task: input.task,
    generation: input.capabilities.generation,
    cursor: input.capabilities.subscriptionCursor,
  })
  let cursor = input.capabilities.subscriptionCursor
  const applyPage = (page: IngressPage): void => {
    for (const frame of page.frames) projection.apply(frame)
    cursor = page.cursor
  }
  if (input.bootstrap) {
    if (!input.bootstrap.first.done) applyPage(input.bootstrap.first.value)
    while (true) {
      const next = await input.bootstrap.iterator.next()
      if (next.done) break
      applyPage(next.value)
    }
  } else {
    for await (const page of input.api.snapshotIngress(input.task.taskId, input.capabilities.generation)) applyPage(page)
  }
  projection.cursor(cursor)
  return { projection, cursor }
}

interface ResumeBootstrap {
  capabilities: IngressCapabilities
  iterator: AsyncIterator<IngressPage>
  first: IteratorResult<IngressPage>
}

async function prefetchResumeBootstrap(api: CliApi, taskId: string): Promise<ResumeBootstrap> {
  const capabilities = await api.ingressCapabilities(taskId)
  const iterator = api.snapshotIngress(taskId, capabilities.generation)[Symbol.asyncIterator]()
  const first = await iterator.next()
  return { capabilities, iterator, first }
}

export async function observeProductTask(input: {
  api: CliApi
  task: TaskProjection
  shell: ProductTuiShell
  signal: AbortSignal
  resume: boolean
  openWeb?: () => Promise<string>
  permissionSession?: CliPermissionSession
  bootstrap?: ResumeBootstrap
  executorEnvironment?: LocalExecutorEnvironment
}): Promise<CommandOutcome> {
  let task = input.task
  let capabilities = input.bootstrap?.capabilities ?? await input.api.ingressCapabilities(task.taskId)
  const bootstrap = input.bootstrap?.capabilities.taskId === task.taskId ? input.bootstrap : undefined
  const initial = await loadProjectionSnapshot({ api: input.api, task, capabilities, bootstrap })
  let cursor = initial.cursor
  let projection = initial.projection
  let lastProjectionRenderAt = 0
  let pendingProjectionRender: ReturnType<typeof setTimeout> | undefined
  const flushProjection = (): void => {
    pendingProjectionRender = undefined
    input.shell.update(projection.snapshot().events)
    lastProjectionRenderAt = performance.now()
  }
  const renderProjection = (force = false): void => {
    const now = performance.now()
    const remaining = 33 - (now - lastProjectionRenderAt)
    if (force || remaining <= 0) {
      if (pendingProjectionRender) clearTimeout(pendingProjectionRender)
      flushProjection()
      return
    }
    pendingProjectionRender ??= setTimeout(flushProjection, remaining)
  }
  projection.connected()
  renderProjection(true)

  if (!capabilities.sseAvailable && !terminalTask(task)) {
    projection.disconnected()
    renderProjection(true)
    throw new CliTaskError("实时事件流不可用；请恢复 SSE 后重试。", "event_stream_unavailable", {
      recovery: `恢复 SSE 后运行 zyra resume ${task.taskId}`,
      task_id: task.taskId,
      revision: projection.revision,
    })
  }

  const resumableTerminal = input.resume && ["failed", "blocked"].includes(task.status)
  const resumedFromRevision = resumableTerminal ? `${task.status}:${task.updatedAt}` : undefined
  let resumedExecutionAdvanced = !resumableTerminal
  const observationHasSettled = (candidate: TaskProjection): boolean => {
    const revision = `${candidate.status}:${candidate.updatedAt}`
    if (terminalTask(candidate) && resumedFromRevision !== undefined && revision === resumedFromRevision) {
      return false
    }
    if (!terminalTask(candidate)) {
      resumedExecutionAdvanced = true
      return false
    }
    if (resumedFromRevision !== undefined && revision !== resumedFromRevision) resumedExecutionAdvanced = true
    return resumedExecutionAdvanced
  }
  if (terminalTask(task) && !resumableTerminal) {
    projection.complete()
    renderProjection(true)
    return {
      exitCode: terminalExitCode(task),
      status: task.status,
      taskId: task.taskId,
      runId: task.runId,
      result: { schema: "zyra.cli-product-result.v1", revision: projection.revision, resumed: input.resume },
    }
  }

  let settled = false
  let detached = false
  const detachController = new AbortController()
  const observationSignal = AbortSignal.any([input.signal, detachController.signal])
  const detach = () => {
    if (detached || settled) return
    detached = true
    if (pendingProjectionRender) clearTimeout(pendingProjectionRender)
    pendingProjectionRender = undefined
    input.shell.notice("已从当前任务分离；远端任务继续运行。下次使用 zyra resume 或 /resume 选择并恢复。")
    input.shell.detachInput()
    detachController.abort(new CliTaskError("Product TUI detached from the canonical task.", "product_ui_detached"))
  }

  let controlSession: CliControlSession | undefined
  let permissionSession: CliPermissionSession | undefined = input.permissionSession
  let controlLoop: Promise<void> | undefined
  const refreshPermissions = async (): Promise<void> => {
    if (!permissionSession?.available) return
    const pending = await permissionSession.pending(observationSignal)
    projection.permissions(pending.map(permissionSnapshot))
    renderProjection(true)
  }
  const pendingUserInputs = async (): Promise<readonly UserInputRequestProjection[]> => (
    input.api.userInputRequests(task.taskId, { includeTerminal: false, signal: observationSignal })
  )
  const refreshUserInputs = async (): Promise<void> => {
    const requests = await input.api.userInputRequests(task.taskId, {
      includeTerminal: true,
      signal: observationSignal,
    })
    projection.userInputs(requests.map(userInputSnapshot))
    renderProjection(true)
  }
  const answerUserInput = async (
    request: UserInputRequestProjection,
    answers: Readonly<Record<string, string>>,
  ): Promise<UserInputRequestProjection> => input.api.answerUserInput({
    taskId: request.taskId,
    runId: request.runId,
    requestId: request.requestId,
    expectedRevision: request.revision,
    answers,
    signal: observationSignal,
  })
  if (input.shell.interactive) {
    controlSession = new CliControlSession({ api: input.api, task })
    permissionSession ??= new CliPermissionSession({
      api: input.api,
      task,
      custodyToken: process.env.ZYRA_PERMISSION_CUSTODY_TOKEN,
    })
    const permissionReady = permissionSession.open(observationSignal).then(async (opened) => {
      if (opened) await refreshPermissions()
      else input.shell.notice("权限控制当前不可用；为安全起见，新权限请求会被拒绝。使用 /doctor 查看详情。")
    }).catch((error) => {
      input.shell.notice("权限控制当前不可用；为安全起见，新权限请求会被拒绝。使用 /doctor 查看详情。")
    })
    void refreshUserInputs().catch((error) => {
      input.shell.notice(`用户问题暂时无法同步 · ${controlError(error)}`)
    })
    controlLoop = runProductControlLoop({
      shell: input.shell,
      controls: controlSession,
      permissions: permissionSession,
      permissionReady,
      signal: observationSignal,
      refreshPermissions,
      pendingUserInputs,
      answerUserInput,
      refreshUserInputs,
      openWeb: input.openWeb ?? (async () => {
        throw new CliTaskError("当前入口无法启动 Web 看板。", "product_web_launcher_unavailable")
      }),
      openDiff: async () => openProductDiff({ api: input.api, shell: input.shell, task, signal: observationSignal }),
      openArtifact: async (artifactId) => openProductArtifact({ api: input.api, shell: input.shell, taskId: task.taskId, artifactId, signal: observationSignal }),
      taskStatus: async () => input.api.task(task.taskId),
      readiness: async () => input.api.readiness(observationSignal),
      detach,
    }).catch((error) => {
      if (!detached && !input.signal.aborted) input.shell.notice(`控制输入已停止 · ${controlError(error)}`)
    })
  }

  type RunOutcome =
    | { ok: true; value: Awaited<ReturnType<CliApi["runTask"]>> }
    | { ok: false; error: unknown }
  let runOutcome: RunOutcome | undefined
  let runSettled = false
  const shouldRun = input.resume || ["pending", "paused", "interrupted"].includes(task.status)
  const runResult = shouldRun
    ? input.api.runTask(task, observationSignal, input.executorEnvironment)
        .then(
          (value): RunOutcome => ({ ok: true, value }),
          (error: unknown): RunOutcome => ({ ok: false, error }),
        )
        .then((outcome) => {
          runOutcome = outcome
          if (outcome.ok) {
            task = outcome.value.task
            observationHasSettled(task)
            projection.refreshTask(task)
            renderProjection(true)
          }
          return outcome
        })
        .finally(() => { runSettled = true })
    : undefined

  let recoveryAttempts = 0
  let recoveryStartedAt: number | undefined
  let windows = 0
  while (!settled && !observationSignal.aborted) {
    try {
      let closed = false
      for await (const message of input.api.streamIngress(task.taskId, cursor, capabilities.generation, observationSignal)) {
        if (message.kind === "event") {
          projection.apply(message.frame)
          if (message.frame.cursor) cursor = message.frame.cursor
          // A verified canonical frame proves that the replacement stream is
          // alive.  From this point, a later disconnect starts a new bounded
          // retry streak instead of accumulating failures across hours of
          // otherwise healthy reconnect windows.
          recoveryAttempts = 0
          recoveryStartedAt = undefined
          renderProjection()
          if (message.frame.eventType.startsWith("runtime.permission.")) {
            await refreshPermissions().catch((error) => {
              input.shell.notice("权限状态暂时无法刷新，当前保持关闭。使用 /doctor 查看详情。")
            })
          }
          if (message.frame.eventType.startsWith("runtime.tool.")) {
            await refreshUserInputs().catch(() => undefined)
          }
          if (["runtime.task.completed", "runtime.task.failed", "runtime.task.cancelled"].includes(message.frame.eventType)) {
            task = await input.api.task(task.taskId)
            projection.refreshTask(task)
            renderProjection(true)
            if (observationHasSettled(task)) { settled = true; break }
          }
        } else if (message.kind === "live") {
          if (projection.applyLive(message.frame)) {
            recoveryAttempts = 0
            renderProjection()
          }
        } else if (message.kind === "heartbeat" || message.kind === "close") {
          if (message.sequence < projection.lastSequence) {
            throw new CliTaskError("Product SSE cursor regressed behind projected state.", "contract_cursor_regression")
          }
          cursor = message.cursor
          projection.cursor(cursor)
          recoveryAttempts = 0
          recoveryStartedAt = undefined
          if (message.kind === "close") closed = true
          await refreshPermissions().catch((error) => {
            input.shell.notice("权限状态暂时无法刷新，当前保持关闭。使用 /doctor 查看详情。")
          })
          await refreshUserInputs().catch(() => undefined)
          if (observationHasSettled(task) || (message.kind === "heartbeat" && (input.resume || runSettled))) {
            task = observationHasSettled(task) ? task : await input.api.task(task.taskId)
            projection.refreshTask(task)
            renderProjection(true)
            if (observationHasSettled(task)) { settled = true; break }
          }
        }
      }
      if (settled) break
      if (!closed) {
        throw new CliTaskError("Product SSE disconnected without a canonical close cursor.", "event_stream_disconnected")
      }
      task = await input.api.task(task.taskId)
      projection.refreshTask(task)
      renderProjection(true)
      if (observationHasSettled(task)) { settled = true; break }
      if (runSettled && runOutcome?.ok === false && !mutationTransportDetached(runOutcome.error)) throw runOutcome.error
      recoveryAttempts = 0
      windows += 1
      if (windows > 10_000) throw new CliTaskError("Product stream exceeded its bounded reconnect window.", "event_stream_budget")
    } catch (error) {
      if (detached) break
      if (input.signal.aborted) throw input.signal.reason
      if (observationSignal.aborted) throw observationSignal.reason
      recoveryAttempts += 1
      recoveryStartedAt ??= performance.now()
      projection.reconnecting(recoveryAttempts)
      if (shouldReportRecoveryAttempt(recoveryAttempts)) {
        input.shell.status(`连接中断，正在进行第 ${recoveryAttempts} 次恢复…`)
      }
      renderProjection(true)
      if (
        recoveryAttempts > RECOVERY_ATTEMPT_BUDGET
        || performance.now() - recoveryStartedAt > RECOVERY_OUTAGE_BUDGET_MS
      ) {
        throw new CliTaskError("Product event recovery exhausted its retry budget.", "event_stream_recovery_exhausted", {
          task_id: task.taskId,
          revision: projection.revision,
          attempts: recoveryAttempts,
          outage_budget_ms: RECOVERY_OUTAGE_BUDGET_MS,
        })
      }
      await wait(Math.min(2_000, 100 * (2 ** (recoveryAttempts - 1))), observationSignal)
      let replace = recoveryNeedsSnapshot(error)
      let replacementCapabilitiesReady = false
      try {
        if (!replace) {
          const previousGeneration = capabilities.generation
          const probed = await input.api.ingressCapabilities(task.taskId, cursor, capabilities.generation)
          replace = probed.generation !== previousGeneration
          capabilities = probed
          replacementCapabilitiesReady = replace
        }
        if (replace && !replacementCapabilitiesReady) {
          const nextGeneration = capabilities.generation >= 2_147_483_647
            ? 1
            : capabilities.generation + 1
          capabilities = await input.api.ingressCapabilities(task.taskId, undefined, nextGeneration)
        }
        task = await input.api.task(task.taskId)
        controlSession?.refreshTask(task)
        if (replace) {
          const replacement = await loadProjectionSnapshot({ api: input.api, task, capabilities })
          projection = replacement.projection
          cursor = replacement.cursor
        } else {
          projection.refreshTask(task)
        }
        if (permissionSession && !await permissionSession.open(observationSignal)) {
          input.shell.notice("权限控制当前不可用；为安全起见，新权限请求会被拒绝。使用 /doctor 查看详情。")
        }
      } catch (recoveryError) {
        if (recoveryIsTransient(recoveryError) || recoveryNeedsSnapshot(recoveryError)) {
          if (shouldReportRecoveryAttempt(recoveryAttempts)) {
            input.shell.status("连接仍在恢复，正在等待本地执行环境就绪…")
          }
          continue
        }
        throw recoveryError
      }
      projection.connected()
      input.shell.status(undefined)
      await refreshPermissions().catch((permissionError) => {
        input.shell.notice("权限状态暂时无法刷新，当前保持关闭。使用 /doctor 查看详情。")
      })
      await refreshUserInputs().catch(() => undefined)
      renderProjection(true)
      if (observationHasSettled(task)) settled = true
    }
  }
  if (detached) {
    await controlLoop
    return {
      exitCode: CliExitCode.SUCCESS,
      status: "detached",
      taskId: task.taskId,
      runId: task.runId,
      result: {
        schema: "zyra.cli-product-result.v1",
        revision: projection.revision,
        resumed: input.resume,
        remote_task_cancelled: false,
      },
    }
  }
  if (input.signal.aborted) throw input.signal.reason
  // A rejected run mutation can race the canonical terminal event.  Once the
  // task owner has committed a terminal result, that result is authoritative;
  // surfacing the transport rejection would replace a useful failed/blocked
  // product state with a generic CLI contract error during detach or resume.
  if (
    runSettled
    && runOutcome?.ok === false
    && !mutationTransportDetached(runOutcome.error)
    && !terminalTask(task)
  ) throw runOutcome.error

  task = await input.api.task(task.taskId)
  if (!terminalTask(task) && runResult) {
    const outcome = await runResult
    if (!outcome.ok && !mutationTransportDetached(outcome.error)) throw outcome.error
    task = await input.api.task(task.taskId)
  }
  if (!terminalTask(task)) {
    throw new CliTaskError("Product task mutation settled without canonical terminal state.", "task_terminal_state_missing")
  }
  projection.refreshTask(task)
  projection.complete()
  renderProjection(true)
  input.shell.detachInput()
  // The canonical event stream can settle before the long-lived task.run
  // response.  Release that transport explicitly so its HTTP socket cannot
  // keep an otherwise completed CLI process alive.
  detachController.abort(new CliTaskError(
    "Product observation settled at the canonical terminal state.",
    "product_observation_settled",
  ))
  // Aborting only signals the fetch transport.  Wait for the mutation promise
  // to unwind so the typed client can dispose its request deadline timer and
  // release every listener before the product session returns to the prompt.
  // The promise normalizes rejection into RunOutcome, so this cannot replace
  // the authoritative canonical terminal state with a transport error.
  if (runResult) await runResult
  await controlLoop
  return {
    exitCode: terminalExitCode(task),
    status: task.status,
    taskId: task.taskId,
    runId: task.runId,
    result: { schema: "zyra.cli-product-result.v1", revision: projection.revision, resumed: input.resume },
  }
}

function beginWorkspaceIndex(shell: ProductTuiShell, cwd: string): void {
  void workspaceReferenceCandidates(cwd)
    .then((references) => shell.addCandidates(references))
    .catch(() => undefined)
}

async function appendFinalDiff(input: {
  api: CliApi
  shell: ProductTuiShell
  taskId?: string
  cwd: string
}): Promise<void> {
  if (!input.taskId) return
  try {
    const task = await input.api.task(input.taskId)
    const diff = await buildBoundedWorkspaceDiff(task, input.cwd)
    if (diff) input.shell.append([diff])
  } catch {
    input.shell.notice("本地 Diff 便利视图不可用或历史 artifact 已丢失；任务状态未受影响，可使用 /diff 或 /ui 检查 canonical 记录。")
  }
}

export async function executeProductInteractive(input: {
  command: InteractiveCommand
  api: CliApi
  stdin: Readable
  stdout: Writable
  signal: AbortSignal
  cwd?: string
  ensureTerminal?: () => Promise<LocalExecutorEnvironment>
  draftStore?: ProductDraftStore | null
  onboardingStore?: ProductOnboardingStore | null
}): Promise<CommandOutcome> {
  const cwd = input.cwd ?? process.cwd()
  const tty = Boolean((input.stdin as Readable & { isTTY?: boolean }).isTTY)
  if (!input.command.goal && !tty) {
    throw new CliTaskError("zyra without a goal requires an interactive terminal.", "interactive_terminal_required")
  }
  const draftStore = input.draftStore === null
    ? undefined
    : input.draftStore ?? (input.stdin === process.stdin ? ProductDraftStore.open({ workspace: cwd }) : undefined)
  const shell = new ProductTuiShell({ stdin: input.stdin, output: input.stdout, workspace: cwd, candidates: productCommandCandidates(), draftStore })
  shell.start()
  const abortInput = () => shell.detachInput()
  if (input.signal.aborted) abortInput()
  else input.signal.addEventListener("abort", abortInput, { once: true })
  beginWorkspaceIndex(shell, cwd)
  try {
    const onboardingStore = input.onboardingStore === null
      ? undefined
      : input.onboardingStore
        ?? (tty && !input.command.goal && input.stdin === process.stdin && process.env.ZYRA_SKIP_ONBOARDING !== "1"
          ? new ProductOnboardingStore()
          : undefined)
    const initialExecutionConfig = onboardingStore
      ? await runProductOnboarding({ api: input.api, shell, signal: input.signal, cwd, store: onboardingStore })
      : undefined
    return await runProductSession({
      api: input.api,
      shell,
      signal: input.signal,
      cwd,
      tty,
      baseUrl: input.command.baseUrl,
      startupTimeoutMs: input.command.startupTimeoutMs,
      ensureTerminal: input.ensureTerminal,
      initialExecutionConfig,
      initial: input.command.goal ? { kind: "goal", goal: input.command.goal } : undefined,
    })
  } finally {
    input.signal.removeEventListener("abort", abortInput)
    await shell.flushLocalState().catch(() => undefined)
    shell.close()
  }
}

export async function executeProductResume(input: {
  command: ResumeCommand
  api: CliApi
  stdin: Readable
  stdout: Writable
  signal: AbortSignal
  cwd?: string
  ensureTerminal?: () => Promise<LocalExecutorEnvironment>
  draftStore?: ProductDraftStore | null
}): Promise<CommandOutcome> {
  const cwd = input.cwd ?? process.cwd()
  const bootstrapPromise = prefetchResumeBootstrap(input.api, input.command.identity).catch(() => undefined)
  const [resolved, directBootstrap] = await Promise.all([
    input.api.resolveTask(input.command.identity),
    bootstrapPromise,
  ])
  const bootstrap = directBootstrap?.capabilities.taskId === resolved.task.taskId ? directBootstrap : undefined
  const tty = Boolean((input.stdin as Readable & { isTTY?: boolean }).isTTY)
  const draftStore = input.draftStore === null
    ? undefined
    : input.draftStore ?? (input.stdin === process.stdin ? ProductDraftStore.open({ workspace: cwd }) : undefined)
  const shell = new ProductTuiShell({ stdin: input.stdin, output: input.stdout, workspace: cwd, candidates: productCommandCandidates(), draftStore })
  shell.start()
  const abortInput = () => shell.detachInput()
  if (input.signal.aborted) abortInput()
  else input.signal.addEventListener("abort", abortInput, { once: true })
  beginWorkspaceIndex(shell, cwd)
  try {
    return await runProductSession({
      api: input.api,
      shell,
      signal: input.signal,
      cwd,
      tty,
      baseUrl: input.command.baseUrl,
      startupTimeoutMs: input.command.startupTimeoutMs,
      ensureTerminal: input.ensureTerminal,
      initial: { kind: "resume", task: resolved.task, bootstrap },
    })
  } finally {
    input.signal.removeEventListener("abort", abortInput)
    await shell.flushLocalState().catch(() => undefined)
    shell.close()
  }
}

type ProductSessionInput =
  | { kind: "goal"; goal: string }
  | { kind: "resume"; task: TaskProjection; bootstrap?: ResumeBootstrap }

function newProductSessionId(): string {
  return createSessionId()
}

function formatRecentSessions(sessions: Awaited<ReturnType<CliApi["sessions"]>>): string {
  const lines = sessions.sessions.slice(0, 12).map((session) => {
    const status = session.statuses.map(productTaskStatus).join("、") || (session.terminal ? "已结束" : "进行中")
    return `${session.title ?? session.sessionId} · ${status} · ${session.updatedAt ?? "时间未知"}`
  })
  if (!lines.length) lines.push("没有可恢复的历史会话。")
  if (sessions.degraded?.length) lines.push(`⚠ ${sessions.degraded.length} 条旧版或损坏会话记录已安全隔离；未用于恢复选择。`)
  return lines.join("\n")
}

async function pickRecentSession(api: CliApi, shell: ProductTuiShell): Promise<SessionProjection | undefined> {
  const response = await api.sessions({ limit: 24 })
  const eligible = response.sessions.filter((session) => session.resolution === "resolved" && session.resumeTaskId)
  const taskTitles = new Map<string, string>()
  await Promise.all(eligible.slice(0, 12).map(async (session) => {
    const taskId = session.resumeTaskId!
    const task = await api.task(taskId).catch(() => undefined)
    if (task?.userGoal) taskTitles.set(session.sessionId, task.userGoal.replace(/\s+/gu, " ").slice(0, 72))
  }))
  const selected = await shell.pick("恢复会话", eligible.map((session) => ({
    id: session.sessionId,
    label: session.title ?? taskTitles.get(session.sessionId) ?? session.sessionId,
    detail: `${session.statuses.map(productTaskStatus).join("、") || "状态未知"} · ${session.updatedAt ?? "时间未知"}`,
    keywords: [session.sessionId, session.resumeTaskId ?? "", ...session.statuses],
  })), `输入筛选 · ↑↓ 选择 · Enter 恢复 · Esc 返回${response.degraded?.length ? ` · ${response.degraded.length} 条损坏记录已隔离` : ""}`)
  return selected ? eligible.find((session) => session.sessionId === selected.id) : undefined
}

function executionConfigFromTask(task?: TaskProjection): ProductExecutionConfig | undefined {
  const raw = task?.metadata.product_execution_config
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined
  const providerId = (raw as Record<string, unknown>).provider_id
  const modelId = (raw as Record<string, unknown>).model_id
  const reasoningEffort = (raw as Record<string, unknown>).reasoning_effort
  return typeof providerId === "string" && typeof modelId === "string" && providerId && modelId
    ? { providerId, modelId, ...(typeof reasoningEffort === "string" && reasoningEffort ? { reasoningEffort } : {}) }
    : undefined
}

type ProductModelSelection = ProductExecutionConfig & {
  defaultReasoningEffort?: string
  thinkingEnabled?: boolean
  supportedReasoningEfforts?: readonly string[]
}

async function pickProductModel(input: {
  api: CliApi
  shell: ProductTuiShell
  signal: AbortSignal
}): Promise<ProductModelSelection | undefined> {
  const models = await input.api.providerModels(input.signal)
  if (!models.length) throw new CliTaskError("当前没有可用模型；请运行 /doctor 检查配置。", "provider_model_unavailable")
  const selected = await input.shell.pick("选择后续任务模型", models.map((model, index) => ({
    id: String(index),
    label: model.displayName,
    detail: `${model.providerId}/${model.modelId} · 上下文 ${model.contextWindow || "未知"}${model.defaultReasoningEffort ? ` · 默认推理 ${model.defaultReasoningEffort}` : model.thinkingEnabled || model.reasoning ? " · 支持推理" : ""}${model.supportedReasoningEfforts.length ? ` · 可选 ${model.supportedReasoningEfforts.join("/")}` : ""}`,
    keywords: [model.providerId, model.modelId, model.family],
  })), "选择只影响之后创建的任务，不会改变正在运行的任务")
  if (!selected) return undefined
  const model = models[Number(selected.id)]
  if (!model) return undefined
  let reasoningEffort: string | undefined
  if (model.supportedReasoningEfforts.length) {
    const effort = await input.shell.pick("选择推理强度", [
      {
        id: "provider-default",
        label: `模型默认值${model.defaultReasoningEffort ? `（${model.defaultReasoningEffort}）` : ""}`,
        detail: "使用模型提供方的默认推理强度",
        keywords: ["default", "默认"],
      },
      ...model.supportedReasoningEfforts.map((value) => ({
        id: value,
        label: value,
        detail: value === model.defaultReasoningEffort ? "当前默认值" : "应用于之后创建的任务",
        keywords: [value, "reasoning", "推理"],
      })),
    ], "按 Esc 保留模型默认值", "menu")
    if (effort && effort.id !== "provider-default") reasoningEffort = effort.id
  }
  return {
    providerId: model.providerId,
    modelId: model.modelId,
    ...(reasoningEffort ? { reasoningEffort } : {}),
    ...(model.defaultReasoningEffort ? { defaultReasoningEffort: model.defaultReasoningEffort } : {}),
    ...(model.thinkingEnabled ? { thinkingEnabled: true } : {}),
    supportedReasoningEfforts: model.supportedReasoningEfforts,
  }
}

async function pickProductExecutionMode(shell: ProductTuiShell): Promise<ProductExecutionMode | undefined> {
  const selected = await shell.pick("选择后续任务执行模式", [
    {
      id: "standard",
      label: "标准",
      detail: "常规任务；敏感操作仍会根据权限策略询问",
      keywords: ["standard", "常规"],
    },
    {
      id: "sealed_autonomous",
      label: "封闭自治",
      detail: "用于正式自治任务；不挂载 CLI 当前目录，运行后不可降低权限边界",
      keywords: ["sealed", "autonomous", "competition", "封闭", "自治"],
    },
  ], "选择只影响之后创建的任务，不会改写已创建或运行中的任务", "menu")
  return selected?.id === "standard" || selected?.id === "sealed_autonomous" ? selected.id : undefined
}

async function runProductOnboarding(input: {
  api: CliApi
  shell: ProductTuiShell
  signal: AbortSignal
  cwd: string
  store: ProductOnboardingStore
}): Promise<ProductModelSelection | undefined> {
  const stored = await input.store.load()
  if (stored.status === "complete") return undefined
  if (stored.status === "invalid") {
    input.shell.notice(`首次使用状态未被信任 · ${stored.reason}\n运行 zyra doctor 检查环境；修复或备份状态后可重新进入引导。`)
    return undefined
  }

  const [readinessResult, modelsResult] = await Promise.allSettled([
    input.api.readiness(input.signal),
    input.api.providerModels(input.signal),
  ])
  const models = modelsResult.status === "fulfilled" ? modelsResult.value : []
  const ready = readinessResult.status === "fulfilled" && readinessResult.value.ready
  input.shell.notice([
    "欢迎使用 Zyra",
    ready ? "本地执行环境已就绪。" : "本地执行环境尚未完全就绪；进入终端后可运行 /doctor。",
    models.length ? `已发现 ${models.length} 个可用模型。` : "尚未发现可用模型；请先运行 /doctor。",
  ].join("\n"))

  const choices = [
    ...(models.length ? [{
      id: "automatic",
      label: "自动选择模型",
      detail: "由 Zyra 根据任务选择；之后可随时用 /model 更改",
      keywords: ["automatic", "route", "自动", "路由"],
    }, {
      id: "model",
      label: "选择模型和推理强度",
      detail: "从当前可用模型中选择",
      keywords: ["model", "provider", "模型"],
    }] : []),
    {
      id: "diagnostics",
      label: models.length ? "暂不设置" : "检查模型配置",
      detail: "进入终端后运行 /doctor 检查环境",
      keywords: ["doctor", "diagnostic", "诊断", "配置"],
    },
  ]
  const selected = await input.shell.pick(
    "首次使用设置",
    choices,
    models.length
      ? "模型凭据由当前环境安全管理；Zyra 不会要求你在终端中粘贴密钥"
      : "当前还不能提交模型任务；请根据 /doctor 的提示恢复配置",
    "menu",
  )
  if (!selected || selected.id === "diagnostics") {
    input.shell.notice("首次使用引导尚未完成。输入 /doctor 查看恢复动作，/help 查看命令。")
    return undefined
  }
  const execution = selected.id === "model"
    ? await pickProductModel(input)
    : undefined
  if (selected.id === "model" && !execution) {
    input.shell.notice("未选择模型；首次使用引导将在下次启动时继续。")
    return undefined
  }
  try {
    await input.store.complete(execution ? "explicit-model" : "automatic")
  } catch (error) {
    input.shell.notice(`首次使用选择已应用于本次会话，但完成状态未保存 · ${controlError(error)}`)
  }
  input.shell.notice(execution
    ? `首次使用设置完成。\n${formatModelStatus(undefined, execution)}`
    : "首次使用设置完成。输入 /model 可选择模型；任务运行时可用 /permissions status 查看权限策略。")
  return execution
}

async function runProductSession(input: {
  api: CliApi
  shell: ProductTuiShell
  signal: AbortSignal
  cwd: string
  tty: boolean
  baseUrl: string
  startupTimeoutMs: number
  initial?: ProductSessionInput
  initialExecutionConfig?: ProductModelSelection
  ensureTerminal?: () => Promise<LocalExecutorEnvironment>
}): Promise<CommandOutcome> {
  const trace = (stage: string): void => {
    if (process.env.ZYRA_CLI_TRACE_SHUTDOWN === "1") process.stderr.write(`[zyra session] ${stage}\n`)
  }
  let next = input.initial
  let sessionId = next?.kind === "resume"
    ? next.task.sessionId ?? `task:${next.task.taskId}`
    : newProductSessionId()
  let currentTaskId = next?.kind === "resume" ? next.task.taskId : undefined
  let currentTask = next?.kind === "resume" ? next.task : undefined
  let executionConfig: ProductModelSelection | undefined = executionConfigFromTask(currentTask) ?? input.initialExecutionConfig
  let executionMode: ProductExecutionMode = "standard"
  let lastOutcome: CommandOutcome | undefined
  let terminalReady = false
  let executorEnvironment: LocalExecutorEnvironment | undefined
  let currentPermissionSession: CliPermissionSession | undefined
  const refreshChrome = () => input.shell.setChrome({
    model: executionConfig?.modelId ?? "自动选择",
    mode: executionMode === "sealed_autonomous" ? "自治" : "标准",
  })
  refreshChrome()

  while (!input.signal.aborted) {
    if (!next) {
      const entry = await input.shell.read(false)
      if (entry.kind === "exit" || entry.kind === "closed" || entry.kind === "interrupt") break
      if (entry.kind === "shortcuts") {
        await showProductHelp(input.shell, false)
        continue
      }
      if (entry.kind === "permission" || entry.kind === "question") continue
      const line = entry.text.trim()
      const command = parseProductCommand(line)
      if (command) {
        switch (command.definition.name) {
          case "exit":
          case "detach":
            next = undefined
            break
          case "help":
            await showProductHelp(input.shell, false)
            continue
          case "new":
            sessionId = newProductSessionId()
            currentTaskId = undefined
            input.shell.clearTranscript()
            input.shell.notice("已开始新会话。")
            continue
          case "clear":
            input.shell.clearTranscript()
            input.shell.notice("本地对话显示已清除；远端任务和历史没有删除。")
            continue
          case "sessions":
            input.shell.notice(formatRecentSessions(await input.api.sessions({ limit: 12 })))
            continue
          case "rename": {
            if (!currentTask) {
              input.shell.notice("当前还没有可命名的会话。")
              continue
            }
            if (!command.args) {
              input.shell.notice("用法：/rename <标题>")
              continue
            }
            const controls = new CliControlSession({ api: input.api, task: currentTask })
            const receipt = await controls.submit(`/rename ${JSON.stringify(command.args)}`, {
              mode: "enqueue",
              priority: "next",
              signal: input.signal,
            })
            currentTask = await input.api.task(currentTask.taskId).catch(() => currentTask)
            input.shell.notice(receipt.phase === "applied"
              ? `会话已命名为“${command.args}”。`
              : formatProductCommandReceipt(receipt))
            continue
          }
          case "resume": {
            if (!command.args) {
              const session = await pickRecentSession(input.api, input.shell)
              if (!session?.resumeTaskId) {
                input.shell.notice("未选择可恢复会话。")
                continue
              }
              command.args = session.resumeTaskId
            }
            const resolved = await input.api.resolveTask(command.args)
            sessionId = resolved.task.sessionId ?? resolved.session?.sessionId ?? `task:${resolved.task.taskId}`
            next = { kind: "resume", task: resolved.task }
            break
          }
          case "status": {
            const view = input.shell.view
            input.shell.notice(formatProductStatus({
              view,
              fallbackSessionId: sessionId,
              fallbackTaskId: currentTaskId,
              capabilities: input.shell.terminalCapabilities,
            }))
            continue
          }
          case "pwd":
            input.shell.notice(input.cwd)
            continue
          case "doctor":
            input.shell.notice(formatRuntimeReadiness(await input.api.readiness(input.signal)))
            continue
          case "model":
            if (command.args === "status") {
              input.shell.notice(formatModelStatus(currentTask, executionConfig))
              continue
            }
            executionConfig = await pickProductModel({ api: input.api, shell: input.shell, signal: input.signal }) ?? executionConfig
            refreshChrome()
            input.shell.notice(formatModelStatus(undefined, executionConfig))
            continue
          case "mode":
            if (command.args === "status") {
              input.shell.notice(formatExecutionMode(undefined, executionMode))
              continue
            }
            executionMode = await pickProductExecutionMode(input.shell) ?? executionMode
            refreshChrome()
            input.shell.notice(formatExecutionMode(undefined, executionMode))
            continue
          case "agents": {
            await openAgentBrowser({ shell: input.shell, requestedId: command.args })
            continue
          }
          case "permissions": {
            if (!currentPermissionSession?.available) {
              input.shell.notice(currentTask
                ? "当前任务的权限控制不可用；请先恢复该任务，或运行 /doctor 查看诊断。"
                : "当前还没有任务；创建任务后才能查看或修改它的权限模式。")
              continue
            }
            if (command.args === "mode") {
              input.shell.notice(await pickPermissionMode({
                shell: input.shell,
                permissions: currentPermissionSession,
                signal: input.signal,
              }))
              continue
            }
            if (command.args === "status") {
              input.shell.notice(permissionModeStatus(await currentPermissionSession.mode(input.signal)))
              continue
            }
            if (command.args) {
              input.shell.notice("用法：/permissions [mode|status]")
              continue
            }
            const [mode, permissions] = await Promise.all([
              currentPermissionSession.mode(input.signal),
              currentPermissionSession.pending(input.signal),
            ])
            input.shell.notice([
              permissionModeStatus(mode),
              permissions.length
                ? permissions.map((request) => `${request.requestId} · ${request.operation || request.toolName || "受保护操作"}`).join("\n")
                : "当前没有待处理权限请求。",
            ].join("\n"))
            continue
          }
          case "review":
            next = { kind: "goal", goal: productWorkflowGoal("review", command.args) }
            break
          case "init":
            next = { kind: "goal", goal: productWorkflowGoal("init", command.args) }
            break
          case "compact":
            input.shell.notice("/compact 只能在任务运行期间使用。")
            continue
          case "copy": {
            try {
              const copied = await copyLatestAssistantMessage(input.shell.view)
              input.shell.notice(`已复制最近一条回答 · ${byteLabel(copied.byteCount)}`)
            } catch (error) {
              input.shell.notice(`复制失败 · ${controlError(error)}`)
            }
            continue
          }
          case "export": {
            try {
              const exported = await exportProductTranscript({
                view: input.shell.view,
                workspace: input.cwd,
                requestedPath: command.args || undefined,
              })
              input.shell.notice(`对话已导出 · ${exported.path} · ${exported.messageCount} 条消息${exported.truncated ? " · 已达到安全上限" : ""}`)
            } catch (error) {
              input.shell.notice(`导出失败 · ${controlError(error)}`)
            }
            continue
          }
          case "raw":
            await input.shell.page("纯文本对话", rawTranscriptLines(input.shell.view))
            continue
          case "diff": {
            if (!currentTask || !await openProductDiff({ api: input.api, shell: input.shell, task: currentTask, signal: input.signal })) {
              input.shell.notice("当前任务没有可审查的文件变更。")
            }
            continue
          }
          case "plan":
            await input.shell.page("计划与步骤", planLines(input.shell.view))
            continue
          case "verification":
            await input.shell.page("验证与收据", verificationLines(input.shell.view))
            continue
          case "tools":
            await openToolBrowser({
              shell: input.shell,
              openArtifact: async (artifactId) => {
                if (!currentTaskId) throw new CliTaskError("当前还没有任务。", "task_not_bound")
                await openProductArtifact({ api: input.api, shell: input.shell, taskId: currentTaskId, artifactId, signal: input.signal })
              },
            })
            continue
          case "artifact": {
            if (!currentTaskId) {
              input.shell.notice("当前还没有任务。")
              continue
            }
            if (!command.args) {
              const artifactId = await pickTaskArtifact(input.shell, await input.api.task(currentTaskId))
              if (!artifactId) continue
              command.args = artifactId
            }
            try {
              await openProductArtifact({ api: input.api, shell: input.shell, taskId: currentTaskId, artifactId: command.args, signal: input.signal })
            } catch (error) {
              input.shell.notice(`任务产物不可用 · ${controlError(error)}`)
            }
            continue
          }
          case "ui":
            input.shell.notice(currentTaskId
              ? `已打开 Web 看板：${(await launchUi({ baseUrl: input.baseUrl, webPort: 5173, startupTimeoutMs: input.startupTimeoutMs, open: true, taskId: currentTaskId })).url}`
              : "当前还没有任务。")
            continue
          default:
            input.shell.notice(`${command.raw} 只能在任务运行期间使用。`)
            continue
        }
        if (!next) break
      } else if (line.startsWith("/")) {
        input.shell.notice(`未知命令：${line.split(/\s/u)[0]}。输入 /help 查看可用命令。`)
        continue
      } else if (line) {
        next = { kind: "goal", goal: entry.text }
      } else {
        continue
      }
    }

    if (!next) break
    // A non-terminal resume reacquires execution ownership through task.run,
    // so it needs a fresh local terminal node after the previous CLI detached.
    // Terminal history remains observation-only and does not pay this cost.
    const resumesExecution = next.kind === "resume"
      && (!terminalTask(next.task) || ["failed", "blocked"].includes(next.task.status))
    if ((next.kind === "goal" || resumesExecution) && typeof input.api.readiness === "function") {
      try {
        const readiness = await input.api.readiness(input.signal)
        const provider = providerConfigurationFromReadiness(readiness)
        if (!provider?.configured) {
          throw new CliTaskError(
            provider
              ? "当前 daemon 未加载可用模型凭据。请确认 Zyra 项目中的 .env.deepseek.local 已配置，停止旧 daemon 后重新启动 CLI。"
              : "当前 daemon 未提供模型配置状态，可能仍是旧进程。请停止旧 daemon，并由当前版本 Zyra CLI 重新启动。",
            provider ? "provider_not_configured" : "provider_configuration_unknown",
          )
        }
      } catch (error) {
        if (!input.tty) throw error
        if (next.kind === "goal") input.shell.restoreDraft(next.goal)
        input.shell.notice(`任务未启动；输入已保留 · ${controlError(error)}`)
        next = undefined
        continue
      }
    }
    if (
      executionMode === "standard"
      && !terminalReady
      && input.ensureTerminal
      && (next.kind === "goal" || resumesExecution)
    ) {
      input.shell.status("正在连接本地执行环境…")
      try {
        executorEnvironment = await input.ensureTerminal()
      } catch (error) {
        input.shell.status(undefined)
        if (!input.tty) throw error
        if (next.kind === "goal") input.shell.restoreDraft(next.goal)
        input.shell.notice(`本地执行环境连接失败；输入已保留 · ${controlError(error)}`)
        next = undefined
        continue
      }
      terminalReady = true
      input.shell.status(undefined)
    }
    input.shell.beginTask()
    const submittedId = next.kind === "goal" ? input.shell.submitted(next.goal) : undefined
    let task: TaskProjection
    try {
      task = next.kind === "resume"
        ? next.task
        : (await input.api.createPendingTask(
            next.goal,
            executionMode === "sealed_autonomous",
            sessionId,
            executionConfig ? {
              providerId: executionConfig.providerId,
              modelId: executionConfig.modelId,
              ...(executionConfig.reasoningEffort ? { reasoningEffort: executionConfig.reasoningEffort } : {}),
            } : undefined,
            executionMode === "standard" ? executorEnvironment : undefined,
          )).task
    } catch (error) {
      if (!input.tty) throw error
      if (next.kind === "goal") input.shell.restoreDraft(next.goal, submittedId)
      input.shell.notice(`任务未启动；输入已恢复到编辑框 · ${controlError(error)}`)
      next = undefined
      continue
    }
    currentTaskId = task.taskId
    currentTask = task
    executionConfig = executionConfigFromTask(task) ?? executionConfig
    refreshChrome()
    if (task.sessionId) sessionId = task.sessionId
    const continuingPermissionCustody = currentPermissionSession?.custodyToken
      ?? process.env.ZYRA_PERMISSION_CUSTODY_TOKEN
    currentPermissionSession = input.shell.interactive
      ? new CliPermissionSession({
          api: input.api,
          task,
          custodyToken: continuingPermissionCustody,
        })
      : undefined
    try {
      lastOutcome = await observeProductTask({
        api: input.api,
        task,
        shell: input.shell,
        signal: input.signal,
        resume: next.kind === "resume",
        openWeb: async () => (await launchUi({
          baseUrl: input.baseUrl,
          webPort: 5173,
          startupTimeoutMs: input.startupTimeoutMs,
          open: true,
          taskId: currentTaskId,
        })).url,
        permissionSession: currentPermissionSession,
        bootstrap: next.kind === "resume" ? next.bootstrap : undefined,
        executorEnvironment: executionMode === "standard" ? executorEnvironment : undefined,
      })
    } catch (error) {
      if (!input.tty) throw error
      input.shell.status(undefined)
      input.shell.notice(`任务执行连接中断；会话仍可继续 · ${controlError(error)}`)
      currentTask = await input.api.task(currentTaskId).catch(() => currentTask)
      next = undefined
      continue
    }
    trace(`observation complete · ${lastOutcome.status}`)
    if (lastOutcome.status === "detached") break
    currentTask = await input.api.task(currentTaskId).catch(() => currentTask)
    trace("canonical refresh complete")
    trace("final diff start")
    await appendFinalDiff({ api: input.api, shell: input.shell, taskId: currentTaskId, cwd: input.cwd })
    trace("final diff complete")
    next = undefined
    if (!input.tty) {
      input.shell.finish()
      return lastOutcome
    }
    trace("idle composer next")
  }
  trace("session loop complete")
  input.shell.finish()
  const detached = lastOutcome?.status === "detached"
  const interrupted = input.signal.aborted && !detached
  return {
    exitCode: interrupted ? CliExitCode.CANCELLED : CliExitCode.SUCCESS,
    status: detached ? "detached" : interrupted ? "cancelled" : "exited",
    taskId: lastOutcome?.taskId,
    runId: lastOutcome?.runId,
    result: { schema: "zyra.cli-product-session-result.v1", last_status: lastOutcome?.status, session_id: sessionId },
  }
}
