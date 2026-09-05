import type { MutationResult, ProductExecutionConfig } from "../api/task-api.ts"
import type { TaskLifecycleCoordinator } from "../api/lifecycle.ts"
import type { WorkbenchRouter } from "../shell/router.ts"
import type { OverlayRuntime } from "../shell/overlay-runtime.ts"
import type { WorkbenchController } from "../shell/workbench-controller.ts"
import {
  type CommandAvailabilityResult,
  type CommandCatalog,
  type CommandContext,
  type CommandDefinition,
} from "./catalog.ts"
import type { CommandHistory } from "./history.ts"
import {
  parseInput,
  validateCommandArguments,
  type ParsedInput,
} from "./parser.ts"
import type { CommandQueue, QueueOrigin, QueuedSubmission } from "./queue.ts"
import { CommandExecutionPolicy } from "./execution-policy.ts"
import type { CommandSurfaceRuntime } from "../features/commands/runtime.ts"

export type SubmissionPhase = "idle" | "validating" | "queued" | "dispatching" | "running" | "committed" | "failed" | "cancelled"

export interface SubmissionRecord {
  executionConfig?: ProductExecutionConfig
  id: string
  fingerprint: string
  value: string
  phase: SubmissionPhase
  origin: QueueOrigin
  createdAt: number
  updatedAt: number
  taskId?: string
  runId?: string
  queueId?: string
  receiptId?: string
  error?: string
}

export interface CommandCoordinatorSnapshot {
  phase: SubmissionPhase
  busy: boolean
  active?: SubmissionRecord
  recent: readonly SubmissionRecord[]
  lastError?: string
  revision: number
  enabled: boolean
}

export interface SubmitOptions {
  origin?: QueueOrigin
  taskId?: string
  runId?: string
  sessionId?: string
  taskStatus?: string
  taskActive?: boolean
  taskTerminal?: boolean
  allowQueue?: boolean
  controlMode?: "enqueue" | "steer" | "interrupt"
}

export interface SubmissionResult {
  status: "ignored" | "queued" | "committed"
  record?: SubmissionRecord
  mutation?: MutationResult
}

function hashValue(value: string): string {
  let hash = 0x811c9dc5
  for (const byte of new TextEncoder().encode(value)) {
    hash ^= byte
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, "0")
}

function submissionId(fingerprint: string): string {
  return `submission_${Date.now().toString(36)}_${fingerprint}_${Math.random()
    .toString(36)
    .slice(2, 8)}`
}

function cloneRecord(record: SubmissionRecord): SubmissionRecord {
  return { ...record }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error || "Command failed.")
}

function activeContext(
  workbench: WorkbenchController,
  options: SubmitOptions,
): CommandContext {
  const selected = workbench.selectedTask()
  const requestedTaskId = options.taskId ?? selected?.taskId
  const snapshot = workbench.getSnapshot()
  const contextualTask =
    selected?.taskId === requestedTaskId
      ? selected
      : snapshot.detail.task?.taskId === requestedTaskId
        ? snapshot.detail.task
        : snapshot.list.tasks.find((task) => task.taskId === requestedTaskId)
  return {
    taskId: requestedTaskId,
    runId: options.runId ?? contextualTask?.runId,
    sessionId: options.sessionId ?? contextualTask?.sessionId,
    taskStatus: options.taskStatus ?? contextualTask?.status,
    taskActive: options.taskActive ?? contextualTask?.active ?? false,
    taskTerminal: options.taskTerminal ?? contextualTask?.terminal ?? false,
    transportEnabled: snapshot.transportEnabled,
  }
}

function createGoal(parsed: ParsedInput): { goal: string; autoRun: boolean } {
  if (parsed.kind === "prompt") {
    return { goal: parsed.text, autoRun: true }
  }
  if (parsed.kind !== "command") throw new TypeError("Task goal is empty.")
  const flag = parsed.flags.run
  const hasNoRun = parsed.arguments.includes("--no-run") || flag === false || flag === "false"
  const hasRun = parsed.arguments.includes("--run") || flag === true || flag === "true"
  const goal = parsed.arguments
    .filter((value) => value !== "--run" && value !== "--no-run")
    .join(" ")
    .trim()
  if (!goal) throw new TypeError("Task goal must not be empty.")
  return { goal, autoRun: hasRun || !hasNoRun }
}

export class CommandCoordinator {
  readonly #catalog: CommandCatalog
  readonly #queue: CommandQueue
  readonly #history: CommandHistory
  readonly #lifecycle: TaskLifecycleCoordinator
  readonly #workbench: WorkbenchController
  readonly #router: WorkbenchRouter
  readonly #overlays: OverlayRuntime
  readonly #policy = new CommandExecutionPolicy()
  readonly #executionConfig: () => ProductExecutionConfig | undefined
  #controls?: CommandSurfaceRuntime
  readonly #listeners = new Set<() => void>()
  readonly #records = new Map<string, SubmissionRecord>()
  readonly #inflight = new Map<string, Promise<SubmissionResult>>()
  #snapshot: CommandCoordinatorSnapshot = Object.freeze({
    phase: "idle",
    busy: false,
    recent: Object.freeze([]),
    revision: 0,
    enabled: true,
  })
  #activeController?: AbortController
  readonly #acknowledgedCancels = new Map<string, MutationResult>()
  #closed = false
  #disabledReason = "Command coordinator is disabled."
  #draining = false

  constructor(options: {
    catalog: CommandCatalog
    queue: CommandQueue
    history: CommandHistory
    lifecycle: TaskLifecycleCoordinator
    workbench: WorkbenchController
    router: WorkbenchRouter
    overlays: OverlayRuntime
    controls?: CommandSurfaceRuntime
    executionConfig?: () => ProductExecutionConfig | undefined
  }) {
    this.#catalog = options.catalog
    this.#queue = options.queue
    this.#history = options.history
    this.#lifecycle = options.lifecycle
    this.#workbench = options.workbench
    this.#router = options.router
    this.#overlays = options.overlays
    this.#controls = options.controls
    this.#executionConfig = options.executionConfig ?? (() => undefined)
  }

  attachControls(controls: CommandSurfaceRuntime): void {
    this.#assertOpen()
    this.#controls = controls
  }

  getSnapshot = (): CommandCoordinatorSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  parse(value: string): ParsedInput {
    return parseInput(value, this.#catalog)
  }

  context(options: SubmitOptions = {}): CommandContext {
    const context = activeContext(this.#workbench, options)
    if (!options.taskId && !["task", "evidence"].includes(this.#router.current.kind)) {
      return { ...context, taskId: undefined, runId: undefined, sessionId: undefined,
        taskActive: false, taskTerminal: false }
    }
    return context
  }

  availability(definition: CommandDefinition, options: SubmitOptions = {}): CommandAvailabilityResult {
    return this.#catalog.availability(definition, this.context(options))
  }

  submit(value: string, options: SubmitOptions = {}): Promise<SubmissionResult> {
    this.#assertOpen()
    if (!this.#snapshot.enabled) {
      return Promise.reject(new TypeError(this.#disabledReason))
    }
    const normalized = value.trim()
    if (!normalized) return Promise.resolve({ status: "ignored" })
    const context = this.context(options)
    const parsed = this.parse(normalized)
    const globalStatus = parsed.kind === "command"
      && parsed.definition?.id === "command.runtime.status" && !context.taskId
    const lifecycleCommand = parsed.kind === "command"
      && ["task-cancel", "task-resume"].includes(parsed.definition?.execution ?? "")
    if (this.#controls?.resolve(normalized) && !globalStatus && !lifecycleCommand) {
      return this.#controls
        .submit(normalized, { mode: options.controlMode })
        .then((receipt) => ({
          status: receipt.phase === "queued" ? "queued" as const : "committed" as const,
        }))
    }
    try {
      this.#policy.assert({
        parsed,
        context,
        availability:
          parsed.kind === "command" && parsed.definition
            ? this.#catalog.availability(parsed.definition, context)
            : undefined,
        origin: options.origin ?? "keyboard",
        busy: this.#snapshot.busy,
        allowQueue: options.allowQueue !== false,
      })
    } catch (error) {
      return Promise.reject(error)
    }
    if (this.#snapshot.busy && parsed.kind === "command"
      && parsed.definition?.execution === "task-cancel" && context.taskId) {
      // Cancellation must remain available while the execution HTTP request
      // is in flight. It does not replace that request's input/queue owner.
      return this.#lifecycle.cancel({ taskId: context.taskId, runId: context.runId,
        idempotencyKey: submissionId(hashValue(`${context.taskId}:cancel`)),
        reason: parsed.arguments.join(" ").trim() || "用户停止任务" }).then((mutation) => {
        if (this.#workbench.selectedTask()?.taskId === mutation.mutation.task.taskId) {
          this.#workbench.applyMutation(mutation.mutation.task)
        }
        const active = this.#snapshot.active
        if (active && active.taskId === context.taskId && this.#activeController) {
          this.#acknowledgedCancels.set(active.id, mutation)
          this.#activeController.abort("任务已停止")
        }
        return { status: "committed", mutation }
      })
    }
    const fingerprint = hashValue(`${context.taskId ?? "new"}:${normalized}`)
    const existing = this.#inflight.get(fingerprint)
    if (existing) return existing

    if (this.#snapshot.busy && options.allowQueue !== false) {
      const queueable =
        parsed.kind === "prompt" ||
        (parsed.kind === "command" && parsed.definition?.queueable === true)
      if (queueable) {
        const duplicate = this.#queue.containsDuplicate(normalized, context.taskId)
        if (duplicate) {
          const record = this.#recordFromQueue(duplicate, fingerprint)
          return Promise.resolve({ status: "queued", record })
        }
        const queued = this.#queue.enqueue({
          value: normalized,
          origin: options.origin,
          taskId: context.taskId,
          sessionId: context.sessionId,
          priority: "next",
          executionConfig: this.#executionConfig(),
          editable: true,
          visible: true,
          remoteSafe: parsed.kind === "command" ? parsed.definition?.remoteSafe : true,
        })
        const record = this.#recordFromQueue(queued, fingerprint)
        this.#remember(record)
        this.#history.add(normalized, { taskId: context.taskId })
        this.#publish(record)
        return Promise.resolve({ status: "queued", record: cloneRecord(record) })
      }
    }

    const promise = this.#execute(normalized, fingerprint, context, options)
      .finally(() => {
        if (this.#inflight.get(fingerprint) === promise) this.#inflight.delete(fingerprint)
        if (!this.#snapshot.busy) void this.drain()
      })
    this.#inflight.set(fingerprint, promise)
    return promise
  }

  async drain(): Promise<void> {
    if (this.#draining || this.#closed || this.#snapshot.busy || !this.#snapshot.enabled) return
    this.#draining = true
    try {
      while (!this.#snapshot.busy && !this.#closed) {
        const queued = this.#queue.reserve()
        if (!queued) break
        const fingerprint = hashValue(`${queued.taskId ?? "new"}:${queued.value}`)
        try {
          const result = await this.#execute(
            queued.value,
            fingerprint,
            this.context({ taskId: queued.taskId, sessionId: queued.sessionId }),
            {
              origin: queued.origin,
              taskId: queued.taskId,
              allowQueue: false,
            },
            queued,
          )
          if (this.#closed) break
          if (result.record?.receiptId) {
            this.#queue.commit(queued.id, result.record.receiptId)
          } else {
            this.#queue.commit(queued.id)
          }
        } catch (error) {
          if (!this.#closed) this.#queue.fail(queued.id, error)
          break
        }
      }
    } finally {
      this.#draining = false
    }
  }

  cancelActive(reason = "Cancelled from command input."): boolean {
    if (!this.#activeController || this.#activeController.signal.aborted) return false
    this.#activeController.abort(reason)
    const active = this.#snapshot.active
    if (active) {
      active.phase = "cancelled"
      active.error = reason
      active.updatedAt = Date.now()
      this.#remember(active)
      this.#publish(active)
    }
    return true
  }

  disable(reason = "Command coordinator disabled."): void {
    if (!this.#snapshot.enabled) return
    this.#disabledReason = reason.trim() || "Command coordinator disabled."
    this.cancelActive(this.#disabledReason)
    this.#catalog.disable(this.#disabledReason)
    this.#policy.disable(this.#disabledReason)
    this.#replace({
      enabled: false,
      phase: "failed",
      busy: false,
      lastError: this.#disabledReason,
    })
  }

  enable(): void {
    this.#assertOpen()
    this.#catalog.enable()
    this.#policy.enable()
    this.#replace({
      enabled: true,
      phase: "idle",
      busy: false,
      lastError: undefined,
      active: undefined,
    })
  }

  records(limit = 50): SubmissionRecord[] {
    return [...this.#records.values()]
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .slice(0, Math.max(1, Math.min(500, Math.floor(limit))))
      .map(cloneRecord)
  }

  close(reason = "Command coordinator closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.cancelActive(reason)
    this.#listeners.clear()
  }

  async #execute(
    value: string,
    fingerprint: string,
    context: CommandContext,
    options: SubmitOptions,
    queued?: QueuedSubmission,
  ): Promise<SubmissionResult> {
    const now = Date.now()
    const record: SubmissionRecord = {
      id: submissionId(fingerprint),
      fingerprint,
      value,
      phase: "validating",
      origin: options.origin ?? queued?.origin ?? "keyboard",
      createdAt: now,
      updatedAt: now,
      taskId: context.taskId,
      runId: context.runId,
      queueId: queued?.id,
      executionConfig: queued ? queued.executionConfig : this.#executionConfig(),
    }
    this.#remember(record)
    this.#publish(record)
    const parsed = this.parse(value)
    this.#policy.assert({
      parsed,
      context,
      availability:
        parsed.kind === "command" && parsed.definition
          ? this.#catalog.availability(parsed.definition, context)
          : undefined,
      origin: options.origin ?? queued?.origin ?? "keyboard",
      busy: false,
      allowQueue: false,
    })
    const validation = validateCommandArguments(parsed)
    if (!validation.valid) {
      const error = new TypeError(validation.errors.join(" "))
      return this.#fail(record, error)
    }
    if (parsed.kind === "empty") return { status: "ignored" }
    if (parsed.kind === "command" && !parsed.definition) {
      return this.#fail(record, new TypeError(`Unknown command: /${parsed.trigger}`))
    }
    if (parsed.kind === "command" && parsed.definition) {
      const availability = this.#catalog.availability(parsed.definition, context)
      if (!availability.enabled) {
        return this.#fail(record, new TypeError(availability.reason ?? "Command is disabled."))
      }
    }

    record.phase = "dispatching"
    record.updatedAt = Date.now()
    this.#publish(record)
    const controller = new AbortController()
    this.#activeController = controller
    try {
      let mutation: MutationResult | undefined
      if (parsed.kind === "prompt") {
        mutation = await this.#createTask(parsed, controller.signal, record, context.sessionId)
      } else {
        mutation = await this.#dispatchCommand(parsed, context, controller.signal, record)
      }
      if (controller.signal.aborted) throw controller.signal.reason
      if (mutation) {
        record.taskId = mutation.mutation.task.taskId
        record.runId = mutation.mutation.task.runId
        record.receiptId = mutation.receipt.receiptId
        const creating = parsed.kind === "prompt" || parsed.definition?.execution === "task-create"
        if (!creating) {
          this.#workbench.applyMutation(mutation.mutation.task)
          this.#router.openTask(mutation.mutation.task.taskId, { focus: "task-detail" })
        }
      }
      record.phase = "committed"
      record.updatedAt = Date.now()
      this.#history.add(value, { taskId: context.taskId })
      this.#remember(record)
      this.#publish(record)
      return { status: "committed", record: cloneRecord(record), mutation }
    } catch (error) {
      if (controller.signal.aborted) {
        record.phase = "cancelled"
        const cancellation = this.#acknowledgedCancels.get(record.id)
        record.error = cancellation ? undefined : errorMessage(controller.signal.reason ?? error)
        record.updatedAt = Date.now()
        this.#remember(record)
        this.#publish(record)
        if (cancellation) return { status: "committed", record: cloneRecord(record), mutation: cancellation }
        throw error
      }
      return this.#fail(record, error)
    } finally {
      this.#acknowledgedCancels.delete(record.id)
      if (this.#activeController === controller) this.#activeController = undefined
    }
  }

  async #createTask(
    parsed: ParsedInput,
    signal: AbortSignal,
    record: SubmissionRecord,
    sessionId?: string,
  ): Promise<MutationResult> {
    const input = createGoal(parsed)
    const created = await this.#lifecycle.create({
      goal: input.goal,
      autoRun: false,
      sessionId,
      executionConfig: record.executionConfig,
      signal,
      idempotencyKey: `${record.id}:create`,
    })
    const task = created.mutation.task
    this.#workbench.applyMutation(task)
    this.#router.openTask(task.taskId, { focus: "task-detail" })
    if (!input.autoRun) return created
    record.taskId = task.taskId
    record.runId = task.runId
    record.phase = "running"
    this.#publish(record)
    // The creation receipt makes the task visible before its long-running
    // execution request. Live ingress can now show progress and accept stop.
    const result = await this.#lifecycle.resume({ taskId: task.taskId, runId: task.runId, signal,
      idempotencyKey: `${record.id}:run` })
    if (signal.aborted) throw signal.reason
    const route = this.#router.current
    if ((route.kind === "task" || route.kind === "evidence") && route.taskId === task.taskId) {
      this.#workbench.applyMutation(result.mutation.task)
    } else {
      void this.#workbench.refreshTasks({ preserveOnError: true })
    }
    return result
  }

  async #dispatchCommand(
    parsed: Extract<ParsedInput, { kind: "command" }>,
    context: CommandContext,
    signal: AbortSignal,
    record: SubmissionRecord,
  ): Promise<MutationResult | undefined> {
    if (this.#controls?.resolve(parsed.raw)
      && !(parsed.definition?.id === "command.runtime.status" && !context.taskId)
      && !["task-cancel", "task-resume"].includes(parsed.definition?.execution ?? "")) {
      await this.#controls.submit(parsed.raw)
      return undefined
    }
    const definition = parsed.definition!
    if (definition.execution === "task-create") {
      return this.#createTask(parsed, signal, record)
    }
    if (definition.execution === "task-cancel") {
      if (!context.taskId) throw new TypeError("Select a task before cancelling.")
      const reason = parsed.arguments.join(" ").trim() || "Cancelled from the Zyra web console."
      return this.#lifecycle.cancel({
        taskId: context.taskId,
        runId: context.runId,
        reason,
        signal,
        idempotencyKey: `${record.id}:cancel`,
      })
    }
    if (definition.execution === "task-resume") {
      if (!context.taskId) throw new TypeError("Select a task before resuming.")
      const taskId = context.taskId
      record.phase = "running"
      this.#publish(record)
      // A stopped task has no live subscription. Observe the newly admitted
      // execution while its HTTP response remains in flight.
      const timer = setInterval(() => {
        const snapshot = this.#workbench.getSnapshot()
        if (snapshot.selectedTaskId === taskId && !snapshot.detail.syncing) {
          void this.#workbench.loadTask(taskId, { background: true }).catch(() => undefined)
        }
      }, 750)
      try {
        return await this.#lifecycle.resume({ taskId, runId: context.runId, signal,
          idempotencyKey: `${record.id}:resume` })
      } finally { clearInterval(timer) }
    }
    if (definition.execution === "navigation") {
      this.#navigate(definition, parsed)
      return undefined
    }
    await this.#openOverlay(definition)
    return undefined
  }

  #navigate(
    definition: CommandDefinition,
    parsed: Extract<ParsedInput, { kind: "command" }>,
  ): void {
    if (definition.id === "command.navigation.home") {
      const status = parsed.arguments[0]
      this.#router.openTasks({ status: status && status !== "all" ? status : undefined })
      return
    }
    if (definition.id === "command.navigation.task") {
      const taskId = parsed.arguments[0]
      if (!taskId) throw new TypeError("Task identity is required.")
      this.#router.openTask(taskId)
      return
    }
    if (definition.id === "command.navigation.settings") {
      this.#router.openSettings()
      return
    }
    throw new TypeError(`Navigation command is not implemented: ${definition.id}`)
  }

  async #openOverlay(definition: CommandDefinition): Promise<void> {
    if (definition.id === "command.runtime.status") {
      const runtime = this.#workbench.getSnapshot().runtime
      const overlay = this.#overlays.open({
        kind: "transport-status",
        title: "运行时状态",
        replaceKind: true,
        payload: {
          phase: runtime.phase,
          health: runtime.health,
          readiness: runtime.readiness,
          failure: runtime.failure,
        },
      })
      await this.#workbench.refreshRuntime()
      this.#overlays.update(overlay.id, { payload: { refreshedAt: Date.now() } })
      return
    }
    if (definition.id === "command.help.commands") {
      this.#overlays.open({
        kind: "command-help",
        title: "命令与帮助",
        replaceKind: true,
        payload: { commands: this.#catalog.list() },
      })
      return
    }
    if (definition.id === "command.help.keyboard") {
      this.#overlays.open({
        kind: "keyboard-help",
        title: "快捷键",
        replaceKind: true,
        payload: {
          shortcuts: [
            ["Enter", "发送消息"],
            ["Shift+Enter", "换行"],
            ["Escape", "收起菜单或取消当前请求"],
            ["↑ / ↓", "选择补全或查看输入历史"],
            ["Ctrl+K", "开始新任务"],
          ],
        },
      })
      return
    }
    throw new TypeError(`Local overlay command is not implemented: ${definition.id}`)
  }

  #fail(record: SubmissionRecord, error: unknown): never {
    record.phase = "failed"
    record.error = errorMessage(error)
    record.updatedAt = Date.now()
    this.#remember(record)
    this.#publish(record)
    throw error instanceof Error ? error : new Error(record.error)
  }

  #recordFromQueue(entry: QueuedSubmission, fingerprint: string): SubmissionRecord {
    return {
      id: submissionId(fingerprint),
      fingerprint,
      value: entry.value,
      phase: entry.phase === "dispatching" ? "dispatching" : "queued",
      origin: entry.origin,
      createdAt: entry.createdAt,
      updatedAt: entry.updatedAt,
      taskId: entry.taskId,
      queueId: entry.id,
      receiptId: entry.receiptId,
      error: entry.error,
    }
  }

  #remember(record: SubmissionRecord): void {
    this.#records.set(record.id, cloneRecord(record))
    if (this.#records.size <= 500) return
    const remove = [...this.#records.values()]
      .sort((left, right) => left.updatedAt - right.updatedAt)
      .slice(0, this.#records.size - 500)
    for (const item of remove) this.#records.delete(item.id)
  }

  #publish(active: SubmissionRecord): void {
    if (active.phase === "queued" && this.#snapshot.busy) {
      this.#replace({})
      return
    }
    const busy = ["validating", "dispatching", "running"].includes(active.phase)
    this.#replace({
      active: cloneRecord(active),
      phase: active.phase,
      busy,
      lastError: active.error,
    })
  }

  #replace(patch: Partial<CommandCoordinatorSnapshot>): void {
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      recent: Object.freeze(this.records(20)),
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // UI observers cannot mutate command execution.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Command coordinator is closed.")
  }
}
