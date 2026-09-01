import type { Readable, Writable } from "node:stream"
import { ZyraApiError, type SessionProjection, type TaskProjection } from "@zyra/typed-api-client"
import { CliApi, type IngressCapabilities, type ProductExecutionConfig } from "../api.ts"
import { CliExitCode, CliTaskError, type InteractiveCommand, type ResumeCommand } from "../contracts.ts"
import {
  CliControlSession,
  formatCommandQueue,
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
import type { UiPermissionSnapshot } from "../presentation/events.ts"
import { ProductProjection } from "../presentation/projection.ts"
import { buildBoundedWorkspaceDiff } from "../presentation/workspace-diff.ts"
import { parseProductCommand, productCommandCandidates, productCommandHelp } from "../product/commands/registry.ts"
import { openProductArtifact } from "../product/artifact/controller.ts"
import { workspaceReferenceCandidates } from "../product/files/index.ts"
import { openProductDiff } from "../product/diff/controller.ts"
import { formatExecutionMode, formatModelStatus, formatRuntimeReadiness, type ProductExecutionMode } from "../product/diagnostics/status.ts"
import { ProductOnboardingStore } from "../product/onboarding/state.ts"
import { ProductDraftStore } from "../product/session/local-state.ts"
import { copyLatestAssistantMessage, exportProductTranscript, rawTranscriptLines } from "../product/transcript/export.ts"
import { ProductTuiShell } from "../tui/shell.ts"
import { mutationTransportDetached, type CommandOutcome } from "../runner.ts"
import { launchUi } from "../ui.ts"

function terminalTask(task: TaskProjection): boolean {
  return task.terminal || ["completed", "failed", "blocked", "cancelled", "killed"].includes(task.status)
}

function terminalExitCode(task: TaskProjection): CliExitCode {
  if (task.status === "completed") return CliExitCode.SUCCESS
  if (task.status === "cancelled") return CliExitCode.CANCELLED
  return CliExitCode.TASK_FAILED
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

function recoveryDiagnostic(error: unknown): string {
  if (error instanceof CliTaskError) return error.code
  if (error instanceof ZyraApiError) return `${error.code}/${error.category}`
  return error instanceof Error ? error.name : "unknown_error"
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

function controlError(error: unknown): string {
  if (error instanceof CliTaskError) return `${error.code}: ${error.message}`
  return error instanceof Error ? error.message : String(error)
}

function planLines(view: ProductTuiShell["view"]): string[] {
  if (!view.activities.length) return ["当前没有 canonical 计划步骤。"]
  return view.activities.map((activity, index) => {
    const marker = activity.status === "completed" ? "✓" : activity.status === "running" ? "◌" : "○"
    const detail = [activity.category, activity.outcome, activity.summary].filter(Boolean).join(" · ")
    return `${marker} ${index + 1}. ${activity.label}${detail ? ` · ${detail}` : ""}`
  })
}

function byteLabel(value: number | undefined): string {
  if (value === undefined) return "大小未知"
  if (value < 1_024) return `${value} B`
  if (value < 1_024 * 1_024) return `${(value / 1_024).toFixed(1)} KiB`
  return `${(value / (1_024 * 1_024)).toFixed(1)} MiB`
}

async function openToolBrowser(input: {
  shell: ProductTuiShell
  openArtifact: (artifactId: string) => Promise<void>
}): Promise<void> {
  const tools = input.shell.view.tools
  if (!tools.length) {
    await input.shell.page("工具调用", ["当前没有 canonical 工具调用。"])
    return
  }
  const selected = await input.shell.pick("工具调用", tools.map((tool) => ({
    id: tool.toolCallId,
    label: `${tool.status === "failed" ? "!" : tool.status === "completed" ? "✓" : "◌"} ${tool.name}`,
    detail: `${tool.summary}${tool.outputRefs?.length ? ` · ${tool.outputRefs.length} 个输出` : ""}`,
    keywords: [tool.name, tool.status, tool.summary, ...(tool.outputRefs ?? []).map((item) => item.stream)],
  })), "选择工具查看安全摘要、输出或 artifact")
  const tool = selected ? tools.find((item) => item.toolCallId === selected.id) : undefined
  if (!tool) return
  const details = [
    `${tool.name} · ${tool.status}`,
    tool.summary,
    `tool call: ${tool.toolCallId}`,
    `duration: ${tool.durationMs === undefined ? "未记录" : `${tool.durationMs}ms`}`,
    ...(tool.outputRefs ?? []).map((item) => `${item.stream} · ${byteLabel(item.sizeBytes)} · ${item.mediaType} · ${item.artifactId}`),
    ...(tool.artifactIds?.length ? [`artifacts: ${tool.artifactIds.join(", ")}`] : []),
  ]
  if (!tool.outputRefs?.length) {
    await input.shell.page(tool.name, details)
    return
  }
  const output = await input.shell.pick("工具输出", [
    { id: "__details", label: "查看工具详情", detail: "状态、耗时与 artifact 引用" },
    ...tool.outputRefs.map((item) => ({
      id: item.artifactId,
      label: item.stream,
      detail: `${byteLabel(item.sizeBytes)} · ${item.mediaType} · canonical artifact 安全预览`,
      keywords: [item.stream, item.title, item.mediaType, item.artifactId],
    })),
  ], "stdout/stderr 不进入 transcript；选择后读取服务器脱敏的 64 KiB 范围")
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
  if (!verification) return ["当前任务尚未形成 canonical 验证状态。"]
  const lines = [
    `${verification.status === "passed" ? "✓" : verification.status === "failed" ? "!" : "○"} ${verification.label}`,
    `命令级收据：${verification.commandEvidence === "recorded" ? "已记录" : "未记录；不能据此声称执行过某条命令"}`,
  ]
  for (const check of verification.checks) {
    const marker = check.status === "passed" ? "✓" : check.status === "failed" ? "!" : check.status === "skipped" ? "↷" : "○"
    const command = check.command ? ` · ${check.command}` : ""
    const exit = check.exitCode === undefined ? "" : ` · exit ${check.exitCode}`
    const summary = check.summary ? ` · ${check.summary}` : ""
    lines.push(`${marker} [${check.source}] ${check.name} · ${check.status}${command}${exit}${summary}`)
  }
  if (!verification.checks.length) lines.push("没有逐项检查收据；仅显示 canonical 最终门状态。")
  return lines
}

function agentLines(view: ProductTuiShell["view"]): string[] {
  if (!view.agents.length) return ["当前没有可见协作代理。"]
  return view.agents.map((agent, index) => `${index + 1}. ${agent.label} · ${agent.status}${agent.summary ? ` · ${agent.summary}` : ""} · ${agent.agentId}`)
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
    label: "按需询问 (default)",
    detail: "只读通常自动允许；编辑和未设规则的操作会请求确认",
    keywords: ["ask", "default", "询问", "默认"],
  },
  acceptEdits: {
    label: "自动接受编辑 (acceptEdits)",
    detail: "自动允许非高风险的工作区编辑；其他未授权操作仍询问",
    keywords: ["edit", "write", "编辑", "写入"],
  },
  dontAsk: {
    label: "不询问 (dontAsk)",
    detail: "不弹出审批；未被规则允许的操作直接拒绝",
    keywords: ["deny", "dont ask", "拒绝", "不询问"],
  },
  plan: {
    label: "计划模式 (plan)",
    detail: "允许只读信息收集，拒绝所有副作用",
    keywords: ["plan", "read only", "计划", "只读"],
  },
  auto: {
    label: "自治模式 (auto)",
    detail: "允许低风险操作和非高风险编辑；高风险或未知操作直接拒绝",
    keywords: ["auto", "autonomous", "自治", "自动"],
  },
  bypassPermissions: {
    label: "托管权限绕过 (bypassPermissions)",
    detail: "仅在后端显式授予 bypassAvailable 时可用；不可绕过 immutable deny/hook",
    keywords: ["managed", "bypass", "托管", "绕过"],
  },
})

function permissionModeStatus(mode: PermissionModeView): string {
  const choice = mode.mode === "sealed" ? undefined : PERMISSION_MODE_CHOICES[mode.mode]
  return [
    `permission mode · ${mode.mode} · revision ${mode.revision}`,
    choice?.detail ?? "sealed 自治权限策略；只能由 managed override 退出",
    `交互 · ${mode.interactive && !mode.headless ? "可询问" : "不询问"}`,
    `托管绕过 · ${mode.bypassAvailable ? "后端已授权" : "不可用"}`,
    mode.reason ? `最近变更 · ${mode.reason}${mode.changedBy ? ` · ${mode.changedBy}` : ""}` : undefined,
  ].filter(Boolean).join("\n")
}

async function pickPermissionMode(input: {
  shell: ProductTuiShell
  permissions: CliPermissionSession
  signal: AbortSignal
}): Promise<string> {
  const current = await input.permissions.mode(input.signal)
  if (current.mode === "sealed") {
    return `${permissionModeStatus(current)}\nsealed 模式不能由普通 CLI custody 退出；请结束 sealed task 或使用受管控制面。`
  }
  const names: UserSelectablePermissionMode[] = ["default", "acceptEdits", "dontAsk", "plan", "auto"]
  if (current.bypassAvailable) names.push("bypassPermissions")
  const selected = await input.shell.pick("选择当前任务权限模式", names.map((mode) => ({
    id: mode,
    label: `${mode === current.mode ? "✓ " : ""}${PERMISSION_MODE_CHOICES[mode].label}`,
    detail: PERMISSION_MODE_CHOICES[mode].detail,
    keywords: [...PERMISSION_MODE_CHOICES[mode].keywords],
  })), `当前 ${current.mode} · revision ${current.revision}；选择将提交 canonical revisioned mutation`)
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
  return `${result.reconciled ? "权限模式已从 canonical state 对账" : "权限模式已更新"}\n${permissionModeStatus(result.state)}`
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
      detail: `${permissionField(item, "risk", "risk_level") ?? "风险未标注"} · ${item.requestId}`,
      keywords: [item.requestId, permissionField(item, "target") ?? "", item.reason ?? "", permissionField(item, "scope") ?? ""],
    })), "先选择精确请求；决定会绑定 canonical request identity")
    request = selected ? selectable.find((item) => item.requestId === selected.id) : undefined
  }
  if (!request) return "未处理权限请求。"
  const decisions = [
    { id: "allow:once", label: "允许本次", detail: "仅批准当前这一个物理调用" },
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
    { id: "deny:once", label: "拒绝", detail: "拒绝当前调用并保持 fail closed" },
  ]
  const decision = await input.shell.pick("权限决定", decisions, [
    request.prompt ?? request.operation ?? request.toolName ?? "受保护操作",
    permissionField(request, "target") ? `目标：${permissionField(request, "target")}` : undefined,
    request.reason ? `原因：${request.reason}` : undefined,
    permissionField(request, "risk", "risk_level") ? `风险：${permissionField(request, "risk", "risk_level")}` : undefined,
    request.expiresAt ? `过期：${request.expiresAt}` : undefined,
    `request：${request.requestId}`,
  ].filter(Boolean).join(" · "))
  if (!decision) return `未处理权限请求 · ${request.requestId}`
  const [rawEffect, rawScope] = decision.id.split(":")
  const effect = rawEffect === "allow" ? "allow" : "deny"
  const decisionScope = (rawScope ?? "once") as PermissionDecisionScope
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
  return `权限已${effect === "allow" ? "允许" : "拒绝"} · ${scopeLabel}${installed} · ${request.requestId}`
}

async function runProductControlLoop(input: {
  shell: ProductTuiShell
  controls: CliControlSession
  permissions: CliPermissionSession
  signal: AbortSignal
  refreshPermissions: () => Promise<void>
  openWeb: () => Promise<string>
  openDiff: () => Promise<boolean>
  openArtifact: (artifactId: string) => Promise<void>
  taskStatus: () => Promise<TaskProjection>
  readiness: () => ReturnType<CliApi["readiness"]>
  detach: () => void
}): Promise<void> {
  while (!input.signal.aborted) {
    const result = await input.shell.read(true)
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
        input.shell.notice(formatCommandReceipt(receipt))
        continue
      }
      const line = result.text.trim()
      if (line === "/help" || line === "?") {
        input.shell.notice(`Enter 立即重定向 · Tab 排队 · Esc 中断\n${productCommandHelp(true)}`)
        continue
      }
      if (line === "/status") {
        const view = input.shell.view
        input.shell.notice(`session ${view.sessionId ?? "未绑定"}\ntask ${view.taskId ?? "未绑定"} · ${view.taskStatus}\nconnection ${view.connection}`)
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
      if (line === "/agents") {
        await input.shell.page("协作代理", agentLines(input.shell.view))
        continue
      }
      if (line === "/permissions mode") {
        input.shell.notice(await pickPermissionMode(input))
        continue
      }
      if (line === "/permissions status") {
        input.shell.notice(permissionModeStatus(await input.permissions.mode(input.signal)))
        continue
      }
      if (line === "/permissions") {
        input.shell.notice(await resolvePermissionFromPicker(input))
        continue
      }
      if (line === "/copy") {
        const copied = await copyLatestAssistantMessage(input.shell.view)
        input.shell.notice(`已复制最近助手回答 · ${copied.byteCount} bytes`)
        continue
      }
      if (line === "/export" || line.startsWith("/export ")) {
        const exported = await exportProductTranscript({
          view: input.shell.view,
          workspace: input.shell.workspace,
          requestedPath: line.slice("/export".length).trim() || undefined,
        })
        input.shell.notice(`Transcript 已导出 · ${exported.path} · ${exported.messageCount} messages${exported.truncated ? " · 已达到安全上限" : ""}`)
        continue
      }
      if (line === "/raw") {
        await input.shell.page("Raw transcript", rawTranscriptLines(input.shell.view))
        continue
      }
      if (line === "/diff") {
        if (!await input.openDiff()) input.shell.notice("当前任务没有可审查的 canonical diff。使用 /ui 查看 artifact。")
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
        if (!artifactId) throw new CliTaskError("/artifact 需要 artifact id。", "artifact_argument_missing")
        await input.openArtifact(artifactId)
        continue
      }
      if (line === "/ui") {
        input.shell.notice(`已打开当前任务的 Web 看板：${await input.openWeb()}`)
        continue
      }
      if (line.toLowerCase() === "a" || line.toLowerCase() === "d") {
        const pending = await input.permissions.pending(input.signal)
        if (pending.length !== 1) {
          throw new CliTaskError(
            pending.length
              ? "存在多个权限请求，请使用 /approve <request-id> 或 /deny <request-id>。"
              : "当前没有可处理的权限请求。",
            "permission_selection_ambiguous",
          )
        }
        const effect = line.toLowerCase() === "a" ? "allow" : "deny"
        await input.permissions.resolve({ requestId: pending[0]!.requestId, effect, signal: input.signal })
        await input.refreshPermissions()
        input.shell.notice(`权限已${effect === "allow" ? "允许" : "拒绝"} · ${pending[0]!.requestId}`)
        continue
      }
      const intent = !line.startsWith("/")
        ? parseControlIntent(`${result.queue ? "" : "/redirect "}${line}`)
        : parseControlIntent(line)
      if (intent.kind === "queue") {
        input.shell.notice(formatCommandQueue(await input.controls.queue(input.signal, true)))
      } else if (intent.kind === "task-cancel") {
        const task = await input.controls.cancelTask(intent.reason)
        input.shell.notice(`任务取消已提交 · ${task.taskId} · ${task.status}`)
      } else if (intent.kind === "task-continue") {
        const task = await input.controls.continueTask(input.signal)
        input.shell.notice(`任务继续执行 · ${task.taskId} · ${task.status}`)
      } else if (intent.kind === "command-cancel") {
        await input.controls.cancelCommand(intent.requestId, input.signal)
        input.shell.notice(`队列命令已取消 · ${intent.requestId}`)
      } else if (intent.kind === "command-retry") {
        input.shell.notice(formatCommandReceipt(await input.controls.retry(intent.requestId, input.signal)))
      } else if (intent.kind === "permission") {
        await input.permissions.resolve({
          requestId: intent.requestId,
          effect: intent.effect,
          feedback: intent.feedback,
          signal: input.signal,
        })
        await input.refreshPermissions()
        input.shell.notice(`权限已${intent.effect === "allow" ? "允许" : "拒绝"} · ${intent.requestId}`)
      } else {
        input.shell.notice(formatCommandReceipt(await input.controls.submit(intent.text, {
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
}): Promise<{ projection: ProductProjection; cursor: string }> {
  const projection = new ProductProjection({
    task: input.task,
    generation: input.capabilities.generation,
    cursor: input.capabilities.subscriptionCursor,
  })
  let cursor = input.capabilities.subscriptionCursor
  for await (const page of input.api.snapshotIngress(input.task.taskId, input.capabilities.generation)) {
    for (const frame of page.frames) projection.apply(frame)
    cursor = page.cursor
  }
  projection.cursor(cursor)
  return { projection, cursor }
}

export async function observeProductTask(input: {
  api: CliApi
  task: TaskProjection
  shell: ProductTuiShell
  signal: AbortSignal
  resume: boolean
  openWeb?: () => Promise<string>
  permissionSession?: CliPermissionSession
}): Promise<CommandOutcome> {
  let task = input.task
  let capabilities = await input.api.ingressCapabilities(task.taskId)
  const initial = await loadProjectionSnapshot({ api: input.api, task, capabilities })
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

  let detached = false
  const detachController = new AbortController()
  const observationSignal = AbortSignal.any([input.signal, detachController.signal])
  const detach = () => {
    if (detached) return
    detached = true
    if (pendingProjectionRender) clearTimeout(pendingProjectionRender)
    pendingProjectionRender = undefined
    input.shell.notice(`已从 task ${task.taskId} 分离；未发送 cancel，使用 zyra resume ${task.taskId} 恢复观察和控制。`)
    input.shell.detachInput()
    detachController.abort(new CliTaskError("Product TUI detached from the canonical task.", "product_ui_detached"))
  }

  let controlSession: CliControlSession | undefined
  let permissionSession: CliPermissionSession | undefined = input.permissionSession
  const refreshPermissions = async (): Promise<void> => {
    if (!permissionSession?.available) return
    const pending = await permissionSession.pending(observationSignal)
    projection.permissions(pending.map(permissionSnapshot))
    renderProjection(true)
  }
  if (input.shell.interactive) {
    controlSession = new CliControlSession({ api: input.api, task })
    permissionSession ??= new CliPermissionSession({
      api: input.api,
      task,
      custodyToken: process.env.ZYRA_PERMISSION_CUSTODY_TOKEN,
    })
    if (await permissionSession.open(observationSignal)) {
      await refreshPermissions()
    } else {
      input.shell.notice(`权限控制保持关闭 · ${permissionSession.custodyError?.code ?? "permission_custody_unavailable"}`)
    }
    void runProductControlLoop({
      shell: input.shell,
      controls: controlSession,
      permissions: permissionSession,
      signal: observationSignal,
      refreshPermissions,
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
    ? input.api.runTask(task, observationSignal)
        .then(
          (value): RunOutcome => ({ ok: true, value }),
          (error: unknown): RunOutcome => ({ ok: false, error }),
        )
        .then((outcome) => {
          runOutcome = outcome
          if (outcome.ok) {
            task = outcome.value.task
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
  let settled = false
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
              input.shell.notice(`权限状态刷新失败并保持关闭 · ${controlError(error)}`)
            })
          }
          if (["runtime.task.completed", "runtime.task.failed", "runtime.task.cancelled"].includes(message.frame.eventType)) {
            task = await input.api.task(task.taskId)
            projection.refreshTask(task)
            renderProjection(true)
            if (terminalTask(task)) { settled = true; break }
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
            input.shell.notice(`权限状态刷新失败并保持关闭 · ${controlError(error)}`)
          })
          if (terminalTask(task) || (message.kind === "heartbeat" && (input.resume || runSettled))) {
            task = terminalTask(task) ? task : await input.api.task(task.taskId)
            projection.refreshTask(task)
            renderProjection(true)
            if (terminalTask(task)) { settled = true; break }
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
      if (terminalTask(task)) { settled = true; break }
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
        input.shell.notice(`连接恢复中 · 第 ${recoveryAttempts} 次 · ${recoveryDiagnostic(error)}`)
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
          input.shell.notice(`权限控制保持关闭 · ${permissionSession.custodyError?.code ?? "permission_custody_unavailable"}`)
        }
      } catch (recoveryError) {
        if (recoveryIsTransient(recoveryError) || recoveryNeedsSnapshot(recoveryError)) {
          if (shouldReportRecoveryAttempt(recoveryAttempts)) {
            input.shell.notice(`恢复探测尚未就绪 · ${recoveryDiagnostic(recoveryError)}`)
          }
          continue
        }
        throw recoveryError
      }
      projection.connected()
      await refreshPermissions().catch((permissionError) => {
        input.shell.notice(`权限状态刷新失败并保持关闭 · ${controlError(permissionError)}`)
      })
      renderProjection(true)
      if (terminalTask(task)) settled = true
    }
  }
  if (detached) {
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
  if (runSettled && runOutcome?.ok === false && !mutationTransportDetached(runOutcome.error)) throw runOutcome.error

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
  ensureTerminal?: () => Promise<void>
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
  ensureTerminal?: () => Promise<void>
  draftStore?: ProductDraftStore | null
}): Promise<CommandOutcome> {
  const cwd = input.cwd ?? process.cwd()
  const resolved = await input.api.resolveTask(input.command.identity)
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
      initial: { kind: "resume", task: resolved.task },
    })
  } finally {
    input.signal.removeEventListener("abort", abortInput)
    await shell.flushLocalState().catch(() => undefined)
    shell.close()
  }
}

type ProductSessionInput =
  | { kind: "goal"; goal: string }
  | { kind: "resume"; task: TaskProjection }

function newProductSessionId(): string {
  return `product:${crypto.randomUUID().replaceAll("-", "")}`
}

function formatRecentSessions(sessions: Awaited<ReturnType<CliApi["sessions"]>>): string {
  const lines = sessions.sessions.slice(0, 12).map((session) => {
    const status = session.statuses.join(", ") || (session.terminal ? "terminal" : "active")
    return `${session.sessionId} · ${status} · ${session.updatedAt ?? "时间未知"}`
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
    label: taskTitles.get(session.sessionId) ?? session.sessionId,
    detail: `${session.statuses.join(", ") || "unknown"} · ${session.updatedAt ?? "时间未知"}`,
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
  if (!models.length) throw new CliTaskError("canonical provider catalog 中没有当前可用模型。", "provider_model_unavailable")
  const selected = await input.shell.pick("选择后续任务模型", models.map((model, index) => ({
    id: String(index),
    label: model.displayName,
    detail: `${model.providerId}/${model.modelId} · context ${model.contextWindow || "?"}${model.defaultReasoningEffort ? ` · reasoning ${model.defaultReasoningEffort}` : model.thinkingEnabled ? " · thinking enabled" : model.reasoning ? " · reasoning" : ""}${model.supportedReasoningEfforts.length ? ` · 可选 ${model.supportedReasoningEfforts.join("/")}` : ""}`,
    keywords: [model.providerId, model.modelId, model.family],
  })), "选择会写入新 task 的 canonical execution_config；不会改变运行中 task")
  if (!selected) return undefined
  const model = models[Number(selected.id)]
  if (!model) return undefined
  let reasoningEffort: string | undefined
  if (model.supportedReasoningEfforts.length) {
    const effort = await input.shell.pick("选择推理强度", [
      {
        id: "provider-default",
        label: `Provider default${model.defaultReasoningEffort ? ` (${model.defaultReasoningEffort})` : ""}`,
        detail: "不覆盖 canonical model profile 的默认值",
        keywords: ["default", "默认"],
      },
      ...model.supportedReasoningEfforts.map((value) => ({
        id: value,
        label: value,
        detail: value === model.defaultReasoningEffort ? "当前 provider 默认值" : "写入后续 task 的 execution_config",
        keywords: [value, "reasoning", "推理"],
      })),
    ], "强度集合来自 canonical model catalog；Esc 保留 provider 默认值")
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
      label: "Standard",
      detail: "常规任务；权限与工具边界仍由 canonical task binding 决定",
      keywords: ["standard", "常规"],
    },
    {
      id: "sealed_autonomous",
      label: "Sealed autonomous",
      detail: "正式封闭自治任务；创建时写入 sealed 与 competition_mode",
      keywords: ["sealed", "autonomous", "competition", "封闭", "自治"],
    },
  ], "选择只影响后续新 task；不会改写已创建或运行中的 task")
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
  const providers = [...new Set(models.map((model) => model.providerId))]
  const runtime = readinessResult.status === "fulfilled"
    ? formatRuntimeReadiness(readinessResult.value)
    : "daemon · 已连接；runtime readiness 查询失败"
  input.shell.notice([
    "欢迎使用 Zyra 产品 CLI",
    `workspace · ${input.cwd}`,
    runtime,
    `provider catalog · ${providers.length} providers · ${models.length} available models`,
    "permission · task 创建后由 canonical session custody 管理；不可用时 fail closed",
  ].join("\n"))

  const choices = [
    ...(models.length ? [{
      id: "automatic",
      label: "使用 canonical 自动路由",
      detail: "进入 composer；可随时用 /model 为本次会话后续 task 选择模型",
      keywords: ["automatic", "route", "自动", "路由"],
    }, {
      id: "model",
      label: "为本次会话选择模型",
      detail: "从当前可用的 canonical model catalog 选择模型和推理强度",
      keywords: ["model", "provider", "模型"],
    }] : []),
    {
      id: "diagnostics",
      label: models.length ? "暂不开始，查看诊断" : "未发现可用模型，查看诊断",
      detail: "进入 composer 后运行 /doctor；不会把引导标记为完成",
      keywords: ["doctor", "diagnostic", "诊断", "配置"],
    },
  ]
  const selected = await input.shell.pick("首次使用设置", choices, models.length
    ? "Zyra 不在终端中收集 provider secret；凭据由现有安全配置提供"
    : "当前不能安全提交模型任务；先根据 /doctor 恢复 provider 配置")
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
    : "首次使用设置完成 · canonical 自动路由。输入 /model 可选择模型，/permissions status 可查看当前任务权限策略。")
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
  ensureTerminal?: () => Promise<void>
}): Promise<CommandOutcome> {
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
  let currentPermissionSession: CliPermissionSession | undefined

  while (!input.signal.aborted) {
    if (!next) {
      const entry = await input.shell.read(false)
      if (entry.kind === "exit" || entry.kind === "closed" || entry.kind === "interrupt") break
      const line = entry.text.trim()
      const command = parseProductCommand(line)
      if (command) {
        switch (command.definition.name) {
          case "exit":
          case "detach":
            next = undefined
            break
          case "help":
            input.shell.notice(`Enter 提交 · Ctrl+J 换行 · Ctrl+E 外部编辑 · Ctrl+R 恢复草稿\n${productCommandHelp(false)}`)
            continue
          case "new":
            sessionId = newProductSessionId()
            currentTaskId = undefined
            input.shell.clearTranscript()
            input.shell.notice(`已开始新会话 · ${sessionId}`)
            continue
          case "clear":
            input.shell.clearTranscript()
            input.shell.notice("本地 transcript 已清除；远端任务和历史未删除。")
            continue
          case "sessions":
            input.shell.notice(formatRecentSessions(await input.api.sessions({ limit: 12 })))
            continue
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
            input.shell.notice(`session ${view.sessionId ?? sessionId}\ntask ${view.taskId ?? currentTaskId ?? "未绑定"} · ${view.taskStatus}\nconnection ${view.connection}`)
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
            input.shell.notice(formatModelStatus(undefined, executionConfig))
            continue
          case "mode":
            if (command.args === "status") {
              input.shell.notice(formatExecutionMode(undefined, executionMode))
              continue
            }
            executionMode = await pickProductExecutionMode(input.shell) ?? executionMode
            input.shell.notice(formatExecutionMode(undefined, executionMode))
            continue
          case "agents": {
            await input.shell.page("协作代理", agentLines(input.shell.view))
            continue
          }
          case "permissions": {
            if (!currentPermissionSession?.available) {
              input.shell.notice(currentTask
                ? "当前 task 的权限 custody 不可用；请先恢复该 task，或运行 /doctor 查看诊断。"
                : "当前尚未绑定 task；权限模式会在任务创建后由 canonical permission session 拥有。")
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
          case "copy": {
            try {
              const copied = await copyLatestAssistantMessage(input.shell.view)
              input.shell.notice(`已复制最近助手回答 · ${copied.byteCount} bytes`)
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
              input.shell.notice(`Transcript 已导出 · ${exported.path} · ${exported.messageCount} messages${exported.truncated ? " · 已达到安全上限" : ""}`)
            } catch (error) {
              input.shell.notice(`导出失败 · ${controlError(error)}`)
            }
            continue
          }
          case "raw":
            await input.shell.page("Raw transcript", rawTranscriptLines(input.shell.view))
            continue
          case "diff": {
            if (!currentTask || !await openProductDiff({ api: input.api, shell: input.shell, task: currentTask, signal: input.signal })) {
              input.shell.notice("当前任务没有可审查的 canonical diff。")
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
                if (!currentTaskId) throw new CliTaskError("当前尚未绑定 task。", "task_not_bound")
                await openProductArtifact({ api: input.api, shell: input.shell, taskId: currentTaskId, artifactId, signal: input.signal })
              },
            })
            continue
          case "artifact": {
            if (!currentTaskId) {
              input.shell.notice("当前尚未绑定 task。")
              continue
            }
            if (!command.args) {
              input.shell.notice("用法：/artifact <artifact-id>")
              continue
            }
            try {
              await openProductArtifact({ api: input.api, shell: input.shell, taskId: currentTaskId, artifactId: command.args, signal: input.signal })
            } catch (error) {
              input.shell.notice(`Artifact 不可用 · ${controlError(error)}`)
            }
            continue
          }
          case "ui":
            input.shell.notice(currentTaskId
              ? `已打开 Web 看板：${(await launchUi({ baseUrl: input.baseUrl, webPort: 5173, startupTimeoutMs: input.startupTimeoutMs, open: true, taskId: currentTaskId })).url}`
              : "当前尚未绑定 task。")
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
    if (!terminalReady && input.ensureTerminal) {
      input.shell.notice("正在连接本地执行环境…")
      await input.ensureTerminal()
      terminalReady = true
      input.shell.notice(undefined)
    }
    input.shell.beginTask()
    const task = next.kind === "resume"
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
        )).task
    currentTaskId = task.taskId
    currentTask = task
    if (task.sessionId) sessionId = task.sessionId
    currentPermissionSession = input.shell.interactive
      ? new CliPermissionSession({
          api: input.api,
          task,
          custodyToken: process.env.ZYRA_PERMISSION_CUSTODY_TOKEN,
        })
      : undefined
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
    })
    if (lastOutcome.status === "detached") break
    await appendFinalDiff({ api: input.api, shell: input.shell, taskId: currentTaskId, cwd: input.cwd })
    currentTask = await input.api.task(currentTaskId).catch(() => currentTask)
    next = undefined
    if (!input.tty) {
      input.shell.finish()
      return lastOutcome
    }
    input.shell.notice("本轮已收敛。继续输入可在同一会话发起下一轮；/new 开始新会话，/exit 退出。")
  }
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
