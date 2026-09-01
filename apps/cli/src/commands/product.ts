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
import { CliPermissionSession, type PermissionRequestView } from "../control/permission.ts"
import type { UiPermissionSnapshot } from "../presentation/events.ts"
import { ProductProjection } from "../presentation/projection.ts"
import { buildBoundedWorkspaceDiff } from "../presentation/workspace-diff.ts"
import { parseProductCommand, productCommandCandidates, productCommandHelp } from "../product/commands/registry.ts"
import { openProductArtifact } from "../product/artifact/controller.ts"
import { workspaceReferenceCandidates } from "../product/files/index.ts"
import { openProductDiff } from "../product/diff/controller.ts"
import { formatExecutionMode, formatModelStatus, formatRuntimeReadiness, type ProductExecutionMode } from "../product/diagnostics/status.ts"
import { ProductDraftStore } from "../product/session/local-state.ts"
import { ProductTuiShell } from "../tui/shell.ts"
import { mutationTransportDetached, type CommandOutcome } from "../runner.ts"
import { launchUi } from "../ui.ts"

function terminalTask(task: TaskProjection): boolean {
  return task.terminal || ["completed", "failed", "blocked", "cancelled", "killed"].includes(task.status)
}

function recoveryNeedsSnapshot(error: unknown): boolean {
  if (error instanceof CliTaskError) {
    return ["gap", "cursor", "generation", "order", "binding"].some((marker) => error.code.includes(marker))
  }
  return error instanceof ZyraApiError && ["conflict", "not_found", "version", "protocol"].includes(error.category)
}

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

function toolLines(view: ProductTuiShell["view"]): string[] {
  if (!view.tools.length) return ["当前没有 canonical 工具调用。"]
  return view.tools.map((tool, index) => {
    const marker = tool.status === "completed" ? "✓" : tool.status === "failed" ? "!" : "◌"
    const duration = tool.durationMs === undefined ? "耗时未知" : `${tool.durationMs}ms`
    const artifacts = tool.artifactIds?.length ? ` · artifacts: ${tool.artifactIds.join(", ")}` : ""
    return `${marker} ${index + 1}. ${tool.name} · ${tool.status} · ${duration} · ${tool.summary}${artifacts}`
  })
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
  const decision = await input.shell.pick("权限决定", [
    { id: "allow", label: "允许本次", detail: "仅提交后端正式支持的本次决定" },
    { id: "deny", label: "拒绝", detail: "保持 fail closed" },
  ], [
    request.prompt ?? request.operation ?? request.toolName ?? "受保护操作",
    permissionField(request, "target") ? `目标：${permissionField(request, "target")}` : undefined,
    request.reason ? `原因：${request.reason}` : undefined,
    permissionField(request, "risk", "risk_level") ? `风险：${permissionField(request, "risk", "risk_level")}` : undefined,
    request.expiresAt ? `过期：${request.expiresAt}` : undefined,
    `request：${request.requestId}`,
  ].filter(Boolean).join(" · "))
  if (!decision) return `未处理权限请求 · ${request.requestId}`
  const effect = decision.id === "allow" ? "allow" : "deny"
  await input.permissions.resolve({ requestId: request.requestId, effect, signal: input.signal })
  await input.refreshPermissions()
  return `权限已${effect === "allow" ? "允许" : "拒绝"} · ${request.requestId}`
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
      if (line === "/permissions") {
        input.shell.notice(await resolvePermissionFromPicker(input))
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
        await input.shell.page("工具调用", toolLines(input.shell.view))
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
      exitCode: task.status === "completed" ? CliExitCode.SUCCESS : CliExitCode.TASK_FAILED,
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

  let permissionSession: CliPermissionSession | undefined
  const refreshPermissions = async (): Promise<void> => {
    if (!permissionSession?.available) return
    const pending = await permissionSession.pending(observationSignal)
    projection.permissions(pending.map(permissionSnapshot))
    renderProjection(true)
  }
  if (input.shell.interactive) {
    const controls = new CliControlSession({ api: input.api, task })
    permissionSession = new CliPermissionSession({
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
      controls,
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
  let windows = 0
  let settled = false
  while (!settled && !observationSignal.aborted) {
    try {
      let closed = false
      for await (const message of input.api.streamIngress(task.taskId, cursor, capabilities.generation, observationSignal)) {
        if (message.kind === "event") {
          projection.apply(message.frame)
          if (message.frame.cursor) cursor = message.frame.cursor
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
        } else if (message.kind === "heartbeat" || message.kind === "close") {
          if (message.sequence < projection.lastSequence) {
            throw new CliTaskError("Product SSE cursor regressed behind projected state.", "contract_cursor_regression")
          }
          cursor = message.cursor
          projection.cursor(cursor)
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
      projection.reconnecting(recoveryAttempts)
      renderProjection(true)
      if (recoveryAttempts > 6) {
        throw new CliTaskError("Product event recovery exhausted its retry budget.", "event_stream_recovery_exhausted", {
          task_id: task.taskId,
          revision: projection.revision,
        })
      }
      await wait(Math.min(2_000, 100 * (2 ** (recoveryAttempts - 1))), observationSignal)
      let replace = recoveryNeedsSnapshot(error)
      if (!replace) {
        try {
          const probed = await input.api.ingressCapabilities(task.taskId, cursor, capabilities.generation)
          replace = probed.generation !== capabilities.generation
          capabilities = probed
        } catch (probeError) {
          if (!recoveryNeedsSnapshot(probeError)) throw probeError
          replace = true
        }
      }
      if (replace) {
        capabilities = await input.api.ingressCapabilities(task.taskId)
        task = await input.api.task(task.taskId)
        const replacement = await loadProjectionSnapshot({ api: input.api, task, capabilities })
        projection = replacement.projection
        cursor = replacement.cursor
      }
      projection.connected()
      await refreshPermissions().catch((permissionError) => {
        input.shell.notice(`权限状态刷新失败并保持关闭 · ${controlError(permissionError)}`)
      })
      renderProjection(true)
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
    exitCode: task.status === "completed" ? CliExitCode.SUCCESS : CliExitCode.TASK_FAILED,
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
      initial: input.command.goal ? { kind: "goal", goal: input.command.goal } : undefined,
    })
  } finally {
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
  return typeof providerId === "string" && typeof modelId === "string" && providerId && modelId
    ? { providerId, modelId }
    : undefined
}

type ProductModelSelection = ProductExecutionConfig & {
  defaultReasoningEffort?: string
  thinkingEnabled?: boolean
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
    detail: `${model.providerId}/${model.modelId} · context ${model.contextWindow || "?"}${model.defaultReasoningEffort ? ` · reasoning ${model.defaultReasoningEffort}` : model.thinkingEnabled ? " · thinking enabled" : model.reasoning ? " · reasoning" : ""}`,
    keywords: [model.providerId, model.modelId, model.family],
  })), "选择会写入新 task 的 canonical execution_config；不会改变运行中 task")
  if (!selected) return undefined
  const model = models[Number(selected.id)]
  return model ? {
    providerId: model.providerId,
    modelId: model.modelId,
    ...(model.defaultReasoningEffort ? { defaultReasoningEffort: model.defaultReasoningEffort } : {}),
    ...(model.thinkingEnabled ? { thinkingEnabled: true } : {}),
  } : undefined
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

async function runProductSession(input: {
  api: CliApi
  shell: ProductTuiShell
  signal: AbortSignal
  cwd: string
  tty: boolean
  baseUrl: string
  startupTimeoutMs: number
  initial?: ProductSessionInput
  ensureTerminal?: () => Promise<void>
}): Promise<CommandOutcome> {
  let next = input.initial
  let sessionId = next?.kind === "resume"
    ? next.task.sessionId ?? `task:${next.task.taskId}`
    : newProductSessionId()
  let currentTaskId = next?.kind === "resume" ? next.task.taskId : undefined
  let currentTask = next?.kind === "resume" ? next.task : undefined
  let executionConfig: ProductModelSelection | undefined = executionConfigFromTask(currentTask)
  let executionMode: ProductExecutionMode = "standard"
  let lastOutcome: CommandOutcome | undefined
  let terminalReady = false

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
            const permissions = input.shell.view.permissions
            input.shell.notice(permissions.length ? permissions.map((request) => `${request.requestId} · ${request.action}`).join("\n") : "当前没有待处理权限请求。")
            continue
          }
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
            await input.shell.page("工具调用", toolLines(input.shell.view))
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
          executionConfig ? { providerId: executionConfig.providerId, modelId: executionConfig.modelId } : undefined,
        )).task
    currentTaskId = task.taskId
    currentTask = task
    if (task.sessionId) sessionId = task.sessionId
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
  return {
    exitCode: CliExitCode.SUCCESS,
    status: lastOutcome?.status === "detached" ? "detached" : "exited",
    taskId: lastOutcome?.taskId,
    runId: lastOutcome?.runId,
    result: { schema: "zyra.cli-product-session-result.v1", last_status: lastOutcome?.status, session_id: sessionId },
  }
}
