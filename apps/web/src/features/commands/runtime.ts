import {
  auditCommandRuntime,
  buildCommandResultModel,
  CommandCoordinator,
  CommandHistoryStore,
  CommandInputEngine,
  CommandPalette,
  CommandProjectionIndex,
  CommandQueueActions,
  CommandQueueRecovery,
  CommandResultStore,
  createCommandRegistry,
  type CommandCoordinatorSnapshot,
  type CommandDeliveryMode,
  type CommandProjectionIndexAudit,
  type CommandRuntimeAudit,
  type CommandHistorySnapshot,
  type CommandInputSnapshot,
  type PaletteSnapshot,
  type QueueActionSnapshot,
  type CommandQueueItem,
  type QueueRecoverySnapshot,
  type CommandResultStoreSnapshot,
  type CommandQueueSnapshot,
  type CommandReceipt,
  type CommandTaskContext,
  type CommandTransport,
  type CommandTransportRequest,
} from "../../../../../packages/commands/src/index.ts"
import type { TaskApi } from "../../api/task-api.ts"
import type { OverlayRuntime } from "../../shell/overlay-runtime.ts"
import type { CanonicalProjectionStore } from "../../state/index.ts"
import type { WorkbenchController } from "../../shell/workbench-controller.ts"

export interface CommandSurfaceSnapshot {
  coordinator: CommandCoordinatorSnapshot
  queue?: CommandQueueSnapshot
  recovery: QueueRecoverySnapshot
  queueActions: QueueActionSnapshot
  palette: PaletteSnapshot
  history: CommandHistorySnapshot
  results: CommandResultStoreSnapshot
  input: CommandInputSnapshot
  projectionAudit: CommandProjectionIndexAudit
  audit: CommandRuntimeAudit
  lastOverlayReceipt?: CommandReceipt
  revision: number
}

class TaskApiCommandTransport implements CommandTransport {
  readonly #api: TaskApi

  constructor(api: TaskApi) {
    this.#api = api
  }

  submit(request: CommandTransportRequest): Promise<unknown> {
    return this.#api.controlCommand({
      taskId: request.taskId,
      runId: request.runId,
      text: request.text,
      arguments: request.arguments,
      requestId: request.requestId,
      commandId: request.commandId,
      actorId: request.actorId,
      sessionId: request.sessionId,
      expectedRevision: request.expectedRevision,
      sealed: request.sealed,
      idempotencyKey: request.idempotencyKey,
      signal: request.signal,
      timeoutMs: request.timeoutMs,
      priority: request.priority,
      deliveryMode: request.deliveryMode,
      retryOf: request.retryOf,
    })
  }

  queue(input: {
    taskId: string
    sessionId?: string
    includeTerminal?: boolean
    signal?: AbortSignal
  }): Promise<unknown> {
    return this.#api.commandQueue(input)
  }

  cancel(input: {
    taskId: string
    requestId: string
    reason: string
    idempotencyKey: string
    signal?: AbortSignal
  }): Promise<unknown> {
    return this.#api.cancelControlCommand(input)
  }
}

function taskContext(workbench: WorkbenchController): CommandTaskContext {
  const selected = workbench.selectedTask()
  return {
    taskId: selected?.taskId,
    runId: selected?.runId,
    sessionId: selected?.taskId ? `task:${selected.taskId}` : undefined,
    taskStatus: selected?.status,
    active: selected?.active ?? false,
    terminal: selected?.terminal ?? false,
    transportEnabled: workbench.getSnapshot().transportEnabled,
    sealed: false,
    remote: false,
  }
}

function overlayPayload(
  receipt: CommandReceipt,
  result: ReturnType<typeof buildCommandResultModel>,
): Record<string, unknown> {
  return {
    receipt,
    result,
    phase: receipt.phase,
    summary: receipt.summary,
    displayText: receipt.displayText,
    data: receipt.data,
    error: receipt.error,
    usage: receipt.usage,
    eventIds: receipt.eventIds,
    checkpointRef: receipt.checkpointRef,
    replayed: receipt.replayed,
    durable: receipt.durable,
    executed: receipt.executed,
  }
}

function overlayTitle(receipt: CommandReceipt): string {
  const title = {
    "/status": "Runtime status",
    "/graph": "Task graph",
    "/trace": "Causal trace",
    "/artifacts": "Artifacts",
    "/permissions": "Permissions",
    "/context": "Context inspector",
    "/compact": "Compact receipt",
    "/memory": "Memory inspector",
    "/model": "Provider and model route",
    "/resume": "Session resume receipt",
    "/rewind": "Session rewind receipt",
    "/export": "Session export receipt",
    "/btw": "Side question",
    "/inject": "Fault injection receipt",
    "/change": "Requirement change receipt",
    "/verify": "Verification receipt",
    "/eval": "Evaluation receipt",
    "/doctor": "Runtime doctor",
  }[receipt.name]
  return title ?? `${receipt.name} receipt`
}

export class CommandSurfaceRuntime {
  readonly registry = createCommandRegistry()
  readonly coordinator: CommandCoordinator
  readonly palette: CommandPalette
  readonly history = new CommandHistoryStore()
  readonly results = new CommandResultStore()
  readonly input: CommandInputEngine
  readonly projectionIndex = new CommandProjectionIndex()
  readonly recovery: CommandQueueRecovery
  readonly queueActions: CommandQueueActions
  readonly #workbench: WorkbenchController
  readonly #overlays: OverlayRuntime
  readonly #projections: CanonicalProjectionStore
  readonly #listeners = new Set<() => void>()
  #snapshot: CommandSurfaceSnapshot
  #unsubscribeCoordinator: () => void
  #unsubscribeQueue?: () => void
  #unsubscribeProjections: () => void
  #unsubscribeRecovery: () => void
  #unsubscribeQueueActions: () => void
  #unsubscribePalette: () => void
  #unsubscribeHistory: () => void
  #unsubscribeResults: () => void
  #unsubscribeOverlays: () => void
  #unsubscribeInput: () => void
  #closed = false

  constructor(options: {
    api: TaskApi
    workbench: WorkbenchController
    overlays: OverlayRuntime
    projections: CanonicalProjectionStore
  }) {
    this.#workbench = options.workbench
    this.#overlays = options.overlays
    this.#projections = options.projections
    this.coordinator = new CommandCoordinator({
      registry: this.registry,
      transport: new TaskApiCommandTransport(options.api),
      context: () => taskContext(this.#workbench),
    })
    this.palette = new CommandPalette({
      registry: this.registry,
      context: () => taskContext(this.#workbench),
    })
    this.input = new CommandInputEngine({
      registry: this.registry,
      palette: this.palette,
      history: this.history,
      context: () => taskContext(this.#workbench),
    })
    this.#rebuildProjectionIndex()
    this.recovery = new CommandQueueRecovery({
      adapter: {
        restore: (input) => this.coordinator.restoreQueue(input),
        reconcile: (input) => this.coordinator.reconcileQueue(input),
        projections: (taskId) => this.projectionIndex.queueForTask(taskId),
      },
    })
    this.queueActions = new CommandQueueActions({
      cancel: (item, reason) => this.#cancelQueueItemDirect(item, reason),
      retry: (item, mode) => this.#retryQueueItemDirect(item, mode),
      refresh: async (taskId) => {
        if (
          taskId &&
          taskId !== this.recovery.getSnapshot().taskId
        ) {
          await this.recovery.bind(taskId)
          return
        }
        await this.recovery.refresh("manual", { force: true })
      },
    })
    const initialProjectionAudit = this.projectionIndex.audit()
    const initialRecovery = this.recovery.getSnapshot()
    const initialQueueActions = this.queueActions.getSnapshot()
    const initialHistory = this.history.getSnapshot()
    const initialResults = this.results.getSnapshot()
    const initialInput = this.input.getSnapshot()
    this.#snapshot = {
      coordinator: this.coordinator.getSnapshot(),
      recovery: initialRecovery,
      queueActions: initialQueueActions,
      palette: this.palette.getSnapshot(),
      history: initialHistory,
      results: initialResults,
      input: initialInput,
      projectionAudit: initialProjectionAudit,
      audit: auditCommandRuntime({
        registry: this.registry,
        coordinator: this.coordinator.getSnapshot(),
        recovery: initialRecovery,
        actions: initialQueueActions,
        projection: initialProjectionAudit,
        history: initialHistory,
      }),
      revision: 0,
    }
    this.#unsubscribeCoordinator = this.coordinator.subscribe(() => {
      const coordinator = this.coordinator.getSnapshot()
      const receipt = coordinator.lastReceipt
      if (
        receipt &&
        receipt.commandId !== this.#snapshot.lastOverlayReceipt?.commandId
      ) {
        this.#openReceipt(receipt)
        this.recovery.receipt(receipt)
      }
      this.#bindQueue()
      this.#replace({
        coordinator,
        queue: this.coordinator.queue?.getSnapshot(),
        lastOverlayReceipt:
          receipt ?? this.#snapshot.lastOverlayReceipt,
      })
    })
    this.#unsubscribeProjections = this.#projections.subscribe(() => {
      this.#reconcileCanonicalProjection()
    })
    this.#unsubscribeRecovery = this.recovery.subscribe(() => {
      const recovery = this.recovery.getSnapshot()
      this.#bindQueue()
      this.#replace({
        recovery,
        queue: recovery.restored ?? this.coordinator.queue?.getSnapshot(),
      })
    })
    this.#unsubscribeQueueActions = this.queueActions.subscribe(() => {
      this.#replace({
        queueActions: this.queueActions.getSnapshot(),
      })
    })
    this.#unsubscribePalette = this.palette.subscribe(() => {
      this.#replace({ palette: this.palette.getSnapshot() })
    })
    this.#unsubscribeHistory = this.history.subscribe(() => {
      this.#replace({ history: this.history.getSnapshot() })
    })
    this.#unsubscribeResults = this.results.subscribe(() => {
      this.#replace({ results: this.results.getSnapshot() })
    })
    this.#unsubscribeOverlays = this.#overlays.subscribe(() => {
      if (!this.#overlays.containsKind("command-result")) {
        this.results.close(undefined, "Command result overlay dismissed.")
      }
    })
    this.#unsubscribeInput = this.input.subscribe(() => {
      this.#replace({ input: this.input.getSnapshot() })
    })
  }

  getSnapshot = (): CommandSurfaceSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  resolve(value: string): boolean {
    const trigger = value.trim().split(/\s+/, 1)[0]
    return Boolean(this.registry.resolve(trigger))
  }

  async submit(
    value: string,
    options: {
      mode?: CommandDeliveryMode
      sealed?: boolean
    } = {},
  ): Promise<CommandReceipt> {
    const context = taskContext(this.#workbench)
    const mode = options.mode ?? "enqueue"
    const capture = this.input.beginSubmit(value, mode)
    let receipt: CommandReceipt
    try {
      receipt = await this.coordinator.submit(value, {
        mode,
        context: {
          ...context,
          sealed: options.sealed ?? context.sealed,
        },
      })
      this.input.settle(capture.id, receipt)
    } catch (error) {
      this.input.fail(capture.id, error)
      throw error
    }
    if (receipt.phase === "queued") {
      await this.recovery.refresh("receipt", { force: true })
    }
    return receipt
  }

  async restoreQueue(
    options: {
      includeTerminal?: boolean
      signal?: AbortSignal
    } = {},
  ): Promise<CommandQueueSnapshot | undefined> {
    const context = taskContext(this.#workbench)
    if (!context.taskId) return undefined
    if (options.signal?.aborted) return undefined
    if (this.recovery.getSnapshot().taskId !== context.taskId) {
      return this.bindTask(context.taskId)
    }
    const snapshot = await this.recovery.refresh("manual", {
      force: true,
      includeTerminal: options.includeTerminal,
    })
    if (!snapshot) return undefined
    this.#bindQueue()
    this.#replace({
      queue: snapshot,
    })
    return snapshot
  }

  async bindTask(taskId?: string): Promise<CommandQueueSnapshot | undefined> {
    const previousTaskId = this.recovery.getSnapshot().taskId
    if (previousTaskId && previousTaskId !== taskId) {
      this.results.closeTask(previousTaskId, "Task route changed.")
    }
    await this.#projections.ready
    const snapshot = await this.recovery.bind(taskId)
    this.#bindQueue()
    this.#replace({
      recovery: this.recovery.getSnapshot(),
      queue: snapshot,
    })
    return snapshot
  }

  async cancel(
    operationId: string,
    reason?: string,
  ): Promise<CommandReceipt> {
    const receipt = await this.coordinator.cancel(operationId, reason)
    await this.restoreQueue({
      includeTerminal: true,
    })
    return receipt
  }

  async cancelQueueItem(
    item: CommandQueueItem,
    reason?: string,
  ): Promise<CommandReceipt> {
    return this.queueActions.cancel(item, reason)
  }

  async #cancelQueueItemDirect(
    item: CommandQueueItem,
    reason?: string,
  ): Promise<CommandReceipt> {
    const record = this.coordinator
      .records(500)
      .find((value) =>
        value.identity.requestId === item.requestId ||
        value.identity.commandId === item.commandId)
    const receipt = record
      ? await this.coordinator.cancel(record.operationId, reason)
      : await this.coordinator.cancelQueueItem(item, reason)
    return receipt
  }

  cancelActive(reason = "Cancelled from the command input."): boolean {
    const active = this.coordinator.getSnapshot().active
    if (
      !active ||
      !["validated", "running", "queued"].includes(active.phase)
    ) {
      return false
    }
    void this.cancel(active.operationId, reason)
    return true
  }

  async retry(
    operationId: string,
    mode?: CommandDeliveryMode,
  ): Promise<CommandReceipt> {
    const receipt = await this.coordinator.retry(operationId, { mode })
    await this.restoreQueue({
      includeTerminal: true,
    })
    return receipt
  }

  async retryQueueItem(
    item: CommandQueueItem,
    mode?: CommandDeliveryMode,
  ): Promise<CommandReceipt> {
    return this.queueActions.retry(item, mode)
  }

  async #retryQueueItemDirect(
    item: CommandQueueItem,
    mode?: CommandDeliveryMode,
  ): Promise<CommandReceipt> {
    const record = this.coordinator
      .records(500)
      .find((value) =>
        value.identity.requestId === item.requestId ||
        value.identity.commandId === item.commandId)
    const receipt = record?.receipt
      ? await this.coordinator.retry(record.operationId, { mode })
      : await this.coordinator.retryQueueItem(item, { mode })
    return receipt
  }

  disable(reason?: string): void {
    this.coordinator.disable(reason)
    this.recovery.disable(reason)
    this.queueActions.disable(reason)
    this.results.disable(reason)
    this.input.disable(reason)
  }

  enable(): void {
    this.coordinator.enable()
    this.recovery.enable()
    this.queueActions.enable()
    this.results.enable()
    this.input.enable()
  }

  close(reason = "Command surface closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#unsubscribeCoordinator()
    this.#unsubscribeProjections()
    this.#unsubscribeRecovery()
    this.#unsubscribeQueueActions()
    this.#unsubscribePalette()
    this.#unsubscribeHistory()
    this.#unsubscribeResults()
    this.#unsubscribeOverlays()
    this.#unsubscribeInput()
    this.#unsubscribeQueue?.()
    this.#unsubscribeQueue = undefined
    this.recovery.close(reason)
    this.queueActions.close()
    this.projectionIndex.close()
    this.palette.destroy()
    void this.history.close()
    this.results.closeStore(reason)
    this.input.close(reason)
    this.coordinator.close(reason)
    this.#listeners.clear()
  }

  #openReceipt(receipt: CommandReceipt): void {
    const result = this.results.open(receipt, {
      trace: this.projectionIndex.traceReceipt(receipt),
    }).model
    const audit = auditCommandRuntime({
      registry: this.registry,
      coordinator: this.coordinator.getSnapshot(),
      queue: this.coordinator.queue?.getSnapshot(),
      recovery: this.recovery.getSnapshot(),
      actions: this.queueActions.getSnapshot(),
      projection: this.projectionIndex.audit(),
      history: this.history.getSnapshot(),
      receipt,
      result,
    })
    this.#overlays.open({
      kind: "command-result",
      title: result.title || overlayTitle(receipt),
      replaceKind: true,
      payload: {
        ...overlayPayload(receipt, result),
        clientAudit: audit,
      },
    })
  }

  #bindQueue(): void {
    const queue = this.coordinator.queue
    if (!queue || this.#unsubscribeQueue) return
    this.#unsubscribeQueue = queue.subscribe(() => {
      this.#replace({
        queue: queue.getSnapshot(),
      })
    })
  }

  #reconcileCanonicalProjection(): void {
    this.#rebuildProjectionIndex()
    const taskId = this.recovery.getSnapshot().taskId
    if (!taskId || !this.coordinator.queue) {
      this.#replace({ projectionAudit: this.projectionIndex.audit() })
      return
    }
    const queue = this.recovery.projectionChanged()
    this.#replace({
      queue,
      recovery: this.recovery.getSnapshot(),
      projectionAudit: this.projectionIndex.audit(),
    })
  }

  #rebuildProjectionIndex(): void {
    const state = this.#projections.state
    this.projectionIndex.replace(
      Object.values(state.commands),
      Object.values(state.causality.byEvent),
    )
  }

  #replace(patch: Partial<CommandSurfaceSnapshot>): void {
    const selected = {
      ...this.#snapshot,
      ...patch,
      revision: this.#snapshot.revision + 1,
    }
    this.#snapshot = Object.freeze({
      ...selected,
      audit: auditCommandRuntime({
        registry: this.registry,
        coordinator: selected.coordinator,
        queue: selected.queue,
        recovery: selected.recovery,
        actions: selected.queueActions,
        projection: selected.projectionAudit,
        history: selected.history,
        receipt: selected.lastOverlayReceipt,
      }),
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // A React subscriber cannot alter command settlement.
      }
    }
  }
}
