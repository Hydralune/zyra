import { readdir } from "node:fs/promises"
import type { Readable, Writable } from "node:stream"
import { ZyraApiError, type TaskProjection } from "@zyra/typed-api-client"
import { CliApi, type IngressCapabilities, type IngressFrame } from "../api.ts"
import { CliExitCode, CliTaskError, type InteractiveCommand, type ResumeCommand } from "../contracts.ts"
import {
  ACTIVE_CONTROL_COMMANDS,
  CliControlSession,
  formatCommandQueue,
  formatCommandReceipt,
  parseControlIntent,
} from "../control/commands.ts"
import { CliPermissionSession, type PermissionRequestView } from "../control/permission.ts"
import type { UiPermissionSnapshot } from "../presentation/events.ts"
import { ProductProjection } from "../presentation/projection.ts"
import { buildBoundedWorkspaceDiff } from "../presentation/workspace-diff.ts"
import { ProductTuiShell } from "../tui/shell.ts"
import { mutationTransportDetached, type CommandOutcome } from "../runner.ts"
import { launchUi } from "../ui.ts"

const PRODUCT_COMMANDS = Object.freeze([
  "/help", "/exit", "/ui", ...ACTIVE_CONTROL_COMMANDS,
])

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

async function runProductControlLoop(input: {
  shell: ProductTuiShell
  controls: CliControlSession
  permissions: CliPermissionSession
  signal: AbortSignal
  refreshPermissions: () => Promise<void>
  openWeb: () => Promise<string>
}): Promise<void> {
  while (!input.signal.aborted) {
    const result = await input.shell.read(true)
    if (result.kind === "exit" || (result.kind === "submit" && result.text.trim() === "/exit")) {
      input.shell.notice("输入已分离；远端任务会继续运行，可稍后使用 zyra resume 恢复。")
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
        input.shell.notice(`Enter 立即重定向 · Tab 排队 · Esc 中断 · ${PRODUCT_COMMANDS.join("  ")}`)
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

async function snapshot(input: {
  api: CliApi
  task: TaskProjection
  capabilities: IngressCapabilities
}): Promise<{ frames: IngressFrame[]; cursor: string }> {
  const frames: IngressFrame[] = []
  let cursor = input.capabilities.subscriptionCursor
  for await (const page of input.api.snapshotIngress(input.task.taskId, input.capabilities.generation)) {
    frames.push(...page.frames)
    cursor = page.cursor
  }
  return { frames, cursor }
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
  const initial = await snapshot({ api: input.api, task, capabilities })
  let cursor = initial.cursor
  let projection = new ProductProjection({
    task,
    generation: capabilities.generation,
    frames: initial.frames,
    cursor,
  })
  projection.connected()
  input.shell.update(projection.snapshot().events)

  if (!capabilities.sseAvailable && !terminalTask(task)) {
    projection.disconnected()
    input.shell.update(projection.snapshot().events)
    throw new CliTaskError("实时事件流不可用；请恢复 SSE 后重试。", "event_stream_unavailable", {
      recovery: `恢复 SSE 后运行 zyra resume ${task.taskId}`,
      task_id: task.taskId,
      revision: projection.revision,
    })
  }

  const resumableTerminal = input.resume && ["failed", "blocked"].includes(task.status)
  if (terminalTask(task) && !resumableTerminal) {
    projection.complete()
    input.shell.update(projection.snapshot().events)
    return {
      exitCode: task.status === "completed" ? CliExitCode.SUCCESS : CliExitCode.TASK_FAILED,
      status: task.status,
      taskId: task.taskId,
      runId: task.runId,
      result: { schema: "zyra.cli-product-result.v1", revision: projection.revision, resumed: input.resume },
    }
  }

  let permissionSession: CliPermissionSession | undefined
  const refreshPermissions = async (): Promise<void> => {
    if (!permissionSession?.available) return
    const pending = await permissionSession.pending(input.signal)
    projection.permissions(pending.map(permissionSnapshot))
    input.shell.update(projection.snapshot().events)
  }
  if (input.shell.interactive) {
    const controls = new CliControlSession({ api: input.api, task })
    permissionSession = new CliPermissionSession({
      api: input.api,
      task,
      custodyToken: process.env.ZYRA_PERMISSION_CUSTODY_TOKEN,
    })
    if (await permissionSession.open(input.signal)) {
      await refreshPermissions()
    } else {
      input.shell.notice(`权限控制保持关闭 · ${permissionSession.custodyError?.code ?? "permission_custody_unavailable"}`)
    }
    void runProductControlLoop({
      shell: input.shell,
      controls,
      permissions: permissionSession,
      signal: input.signal,
      refreshPermissions,
      openWeb: input.openWeb ?? (async () => {
        throw new CliTaskError("当前入口无法启动 Web 看板。", "product_web_launcher_unavailable")
      }),
    }).catch((error) => input.shell.notice(`控制输入已停止 · ${controlError(error)}`))
  }

  type RunOutcome =
    | { ok: true; value: Awaited<ReturnType<CliApi["runTask"]>> }
    | { ok: false; error: unknown }
  let runOutcome: RunOutcome | undefined
  let runSettled = false
  const shouldRun = input.resume || ["pending", "paused", "interrupted"].includes(task.status)
  const runResult = shouldRun
    ? input.api.runTask(task, input.signal)
        .then(
          (value): RunOutcome => ({ ok: true, value }),
          (error: unknown): RunOutcome => ({ ok: false, error }),
        )
        .then((outcome) => {
          runOutcome = outcome
          if (outcome.ok) {
            task = outcome.value.task
            projection.refreshTask(task)
            input.shell.update(projection.snapshot().events)
          }
          return outcome
        })
        .finally(() => { runSettled = true })
    : undefined

  let recoveryAttempts = 0
  let windows = 0
  let settled = false
  while (!settled && !input.signal.aborted) {
    try {
      let closed = false
      for await (const message of input.api.streamIngress(task.taskId, cursor, capabilities.generation, input.signal)) {
        if (message.kind === "event") {
          projection.apply(message.frame)
          if (message.frame.cursor) cursor = message.frame.cursor
          input.shell.update(projection.snapshot().events)
          if (message.frame.eventType.startsWith("runtime.permission.")) {
            await refreshPermissions().catch((error) => {
              input.shell.notice(`权限状态刷新失败并保持关闭 · ${controlError(error)}`)
            })
          }
          if (["runtime.task.completed", "runtime.task.failed", "runtime.task.cancelled"].includes(message.frame.eventType)) {
            task = await input.api.task(task.taskId)
            projection.refreshTask(task)
            input.shell.update(projection.snapshot().events)
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
            input.shell.update(projection.snapshot().events)
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
      input.shell.update(projection.snapshot().events)
      if (terminalTask(task)) { settled = true; break }
      if (runSettled && runOutcome?.ok === false && !mutationTransportDetached(runOutcome.error)) throw runOutcome.error
      recoveryAttempts = 0
      windows += 1
      if (windows > 10_000) throw new CliTaskError("Product stream exceeded its bounded reconnect window.", "event_stream_budget")
    } catch (error) {
      if (input.signal.aborted) throw input.signal.reason
      recoveryAttempts += 1
      projection.reconnecting(recoveryAttempts)
      input.shell.update(projection.snapshot().events)
      if (recoveryAttempts > 6) {
        throw new CliTaskError("Product event recovery exhausted its retry budget.", "event_stream_recovery_exhausted", {
          task_id: task.taskId,
          revision: projection.revision,
        })
      }
      await wait(Math.min(2_000, 100 * (2 ** (recoveryAttempts - 1))), input.signal)
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
        const replacement = await snapshot({ api: input.api, task, capabilities })
        projection = new ProductProjection({
          task,
          generation: capabilities.generation,
          frames: replacement.frames,
          cursor: replacement.cursor,
        })
        cursor = replacement.cursor
      }
      projection.connected()
      await refreshPermissions().catch((permissionError) => {
        input.shell.notice(`权限状态刷新失败并保持关闭 · ${controlError(permissionError)}`)
      })
      input.shell.update(projection.snapshot().events)
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
  input.shell.update(projection.snapshot().events)
  input.shell.detachInput()
  return {
    exitCode: task.status === "completed" ? CliExitCode.SUCCESS : CliExitCode.TASK_FAILED,
    status: task.status,
    taskId: task.taskId,
    runId: task.runId,
    result: { schema: "zyra.cli-product-result.v1", revision: projection.revision, resumed: input.resume },
  }
}

async function candidates(cwd: string): Promise<readonly string[]> {
  const references = await readdir(cwd, { withFileTypes: true })
    .then((entries) => entries.slice(0, 500).map((entry) => `@${entry.name}${entry.isDirectory() ? "/" : ""}`))
    .catch(() => [] as string[])
  return [...PRODUCT_COMMANDS, ...references]
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
    // Diff is an optional bounded convenience view. The canonical Web route
    // remains available through /ui when local materialization is absent.
  }
}

export async function executeProductInteractive(input: {
  command: InteractiveCommand
  api: CliApi
  stdin: Readable
  stdout: Writable
  signal: AbortSignal
  cwd?: string
}): Promise<CommandOutcome> {
  const cwd = input.cwd ?? process.cwd()
  const tty = Boolean((input.stdin as Readable & { isTTY?: boolean }).isTTY)
  if (!input.command.goal && !tty) {
    throw new CliTaskError("zyra without a goal requires an interactive terminal.", "interactive_terminal_required")
  }
  const shell = new ProductTuiShell({ stdin: input.stdin, output: input.stdout, workspace: cwd, candidates: await candidates(cwd) })
  shell.start()
  try {
    let goal = input.command.goal
    if (!goal) {
      const entry = await shell.read(false)
      if (entry.kind !== "submit") {
        return { exitCode: CliExitCode.SUCCESS, status: "exited" }
      }
      goal = entry.text
    }
    const created = await input.api.createPendingTask(goal, false)
    const outcome = await observeProductTask({
      api: input.api,
      task: created.task,
      shell,
      signal: input.signal,
      resume: false,
      openWeb: async () => (await launchUi({
        baseUrl: input.command.baseUrl,
        webPort: 5173,
        startupTimeoutMs: input.command.startupTimeoutMs,
        open: true,
        taskId: created.task.taskId,
      })).url,
    })
    await appendFinalDiff({ api: input.api, shell, taskId: outcome.taskId, cwd })
    shell.finish()
    return outcome
  } finally {
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
}): Promise<CommandOutcome> {
  const cwd = input.cwd ?? process.cwd()
  const resolved = await input.api.resolveTask(input.command.identity)
  const shell = new ProductTuiShell({ stdin: input.stdin, output: input.stdout, workspace: cwd, candidates: await candidates(cwd) })
  shell.start()
  try {
    const outcome = await observeProductTask({
      api: input.api,
      task: resolved.task,
      shell,
      signal: input.signal,
      resume: true,
      openWeb: async () => (await launchUi({
        baseUrl: input.command.baseUrl,
        webPort: 5173,
        startupTimeoutMs: input.command.startupTimeoutMs,
        open: true,
        taskId: resolved.task.taskId,
      })).url,
    })
    await appendFinalDiff({ api: input.api, shell, taskId: outcome.taskId, cwd })
    shell.finish()
    return outcome
  } finally {
    shell.close()
  }
}
