import type {
  ConnectionSnapshot,
  IngressBatch,
  IngressEvent,
} from "../events/ingress/index.ts"
import type {
  EventSubscriptionOptions,
  TaskEventTransport,
} from "../api/event-transport.ts"
import {
  ProjectionError,
  type ApplyBatchOptions,
  type CanonicalProjectionState,
  type ProjectionChangeSet,
  type ProjectionIngressBinding,
  type ProjectionPersistence,
  type ProjectionSelector,
  type ProjectionStoreAudit,
  type ProjectionStoreOptions,
  type ProjectionTransactionResult,
} from "./contracts.ts"
import {
  createBrowserProjectionPersistence,
  ProjectionPersistenceCoordinator,
} from "./persistence.ts"
import { ProjectionReducer } from "./reducer.ts"
import {
  ProjectionSelectorRegistry,
  type SelectorListener,
} from "./selectors.ts"
import {
  createEmptyProjectionState,
  normalizeProjectionLimits,
  replaceDiagnostics,
} from "./state.ts"
import { auditProjectionIntegrity } from "./integrity.ts"

type StateListener = (
  state: CanonicalProjectionState,
  previous: CanonicalProjectionState,
  changes: ProjectionChangeSet,
) => void

interface BindingRecord {
  taskId: string
  transport: TaskEventTransport
  unsubscribe: () => void
  options: EventSubscriptionOptions
  connection?: ConnectionSnapshot
  closed: boolean
}

export class CanonicalProjectionStore {
  readonly id: string
  readonly #now: () => number
  readonly #reducer: ProjectionReducer
  readonly #selectors = new ProjectionSelectorRegistry()
  readonly #listeners = new Set<StateListener>()
  readonly #bindings = new Map<string, BindingRecord>()
  readonly #persistence?: ProjectionPersistenceCoordinator
  readonly #persistenceDebounceMs: number
  readonly #autoPersist: boolean
  readonly #restoreOnStart: boolean
  #state: CanonicalProjectionState
  #closed = false
  #disabled = false
  #restored = false
  #ready: Promise<CanonicalProjectionState>
  #persistTimer?: ReturnType<typeof setTimeout>
  #persistRequestedRevision = -1
  #applyQueue = Promise.resolve()

  constructor(options: ProjectionStoreOptions = {}) {
    this.id = normalizeStoreId(options.id ?? "workbench")
    this.#now = options.now ?? Date.now
    this.#state = createEmptyProjectionState(this.#now())
    const limits = normalizeProjectionLimits(options.limits)
    this.#reducer = new ProjectionReducer(limits, this.#now)
    this.#persistenceDebounceMs = limits.persistenceDebounceMs
    this.#autoPersist = options.autoPersist ?? true
    this.#restoreOnStart = options.restore ?? true
    const persistence =
      options.persistence ??
      (typeof window !== "undefined"
        ? createBrowserProjectionPersistence()
        : undefined)
    if (persistence) {
      this.#persistence = new ProjectionPersistenceCoordinator(
        this.id,
        persistence,
        this.#now,
      )
    }
    if (options.disabled) this.disable("Store constructed disabled.")
    this.#ready = this.#restoreOnStart
      ? this.restore().catch((error) => {
          this.#recordFailure("RESTORE_FAILED", error)
          return this.#state
        })
      : Promise.resolve(this.#state)
  }

  get ready(): Promise<CanonicalProjectionState> {
    return this.#ready
  }

  get state(): CanonicalProjectionState {
    return this.#state
  }

  readonly getSnapshot = (): CanonicalProjectionState => this.#state

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#assertOpen()
    const wrapped: StateListener = () => listener()
    this.#listeners.add(wrapped)
    return () => this.#listeners.delete(wrapped)
  }

  listen(listener: StateListener): () => void {
    this.#assertOpen()
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  select<T>(selector: ProjectionSelector<T>): T {
    this.#assertAvailable()
    return this.#selectors.select(this.#state, selector)
  }

  subscribeSelector<T>(
    selector: ProjectionSelector<T>,
    listener: SelectorListener<T>,
  ): () => void {
    this.#assertAvailable()
    const handle = this.#selectors.subscribe(this.#state, selector, listener)
    return handle.close
  }

  externalSelector<T>(selector: ProjectionSelector<T>): {
    getSnapshot(): T
    subscribe(listener: () => void): () => void
  } {
    this.#assertAvailable()
    let value = this.select(selector)
    const listeners = new Set<() => void>()
    let closeSelector: (() => void) | undefined
    return {
      getSnapshot: () => value,
      subscribe: (listener) => {
        listeners.add(listener)
        if (!closeSelector) {
          closeSelector = this.subscribeSelector(selector, (next) => {
            value = next
            for (const item of [...listeners]) item()
          })
        }
        return () => {
          listeners.delete(listener)
          if (listeners.size > 0) return
          closeSelector?.()
          closeSelector = undefined
        }
      },
    }
  }

  apply(
    batch: IngressBatch,
    options: ApplyBatchOptions = {},
  ): ProjectionTransactionResult {
    this.#assertAvailable()
    const previous = this.#state
    let result: ProjectionTransactionResult
    try {
      result = this.#reducer.apply(previous, batch, {
        expectedRevision: options.expectedRevision ?? previous.revision,
        source: options.source ?? "ingress",
        connection: options.connection,
      })
    } catch (error) {
      this.#recordFailure("REDUCTION_FAILED", error, batch.taskId)
      throw error
    }
    if (result.state === previous) return result
    this.#commit(previous, result)
    return result
  }

  enqueue(
    batch: IngressBatch,
    options: ApplyBatchOptions = {},
  ): Promise<ProjectionTransactionResult> {
    this.#assertAvailable()
    let resolve!: (value: ProjectionTransactionResult) => void
    let reject!: (error: unknown) => void
    const result = new Promise<ProjectionTransactionResult>((accept, fail) => {
      resolve = accept
      reject = fail
    })
    this.#applyQueue = this.#applyQueue
      .then(async () => {
        await this.#ready
        return this.apply(batch, options)
      })
      .then(resolve, reject)
      .then(
        () => undefined,
        () => undefined,
      )
    return result
  }

  applyOptimistic(
    event: IngressEvent,
    generation?: number,
  ): ProjectionTransactionResult {
    this.#assertAvailable()
    const previous = this.#state
    const result = this.#reducer.applyOptimistic(previous, event, generation)
    this.#commit(previous, result)
    return result
  }

  bind(
    transport: TaskEventTransport,
    taskId: string,
    options: EventSubscriptionOptions = {},
  ): ProjectionIngressBinding {
    this.#assertAvailable()
    const normalizedTaskId = taskId.trim()
    if (!normalizedTaskId) {
      throw new ProjectionError(
        "INVALID_TASK_ID",
        "Projection ingress binding requires a task id.",
      )
    }
    const existing = this.#bindings.get(normalizedTaskId)
    if (existing && !existing.closed) {
      if (existing.transport !== transport) {
        throw new ProjectionError(
          "BINDING_CONFLICT",
          `Task ${normalizedTaskId} is already bound to another event transport.`,
          { taskId: normalizedTaskId },
        )
      }
      return this.#bindingHandle(existing)
    }
    const cursor = options.cursor ?? this.#state.cursors[normalizedTaskId]?.cursor
    const record: BindingRecord = {
      taskId: normalizedTaskId,
      transport,
      options: {
        ...options,
        cursor,
      },
      unsubscribe: () => undefined,
      closed: false,
    }
    const unsubscribe = transport.subscribe(
      normalizedTaskId,
      {
        batch: (batch) => {
          void this.enqueue(batch, {
            source: "ingress",
            connection: record.connection,
          }).catch((error) => {
            this.#recordFailure(
              "INGRESS_APPLY_FAILED",
              error,
              normalizedTaskId,
            )
          })
        },
        status: (snapshot) => {
          record.connection = snapshot
          options.status?.(snapshot)
        },
        diagnostic: options.diagnostic,
      },
      {
        ...options,
        cursor,
      },
    )
    record.unsubscribe = unsubscribe
    this.#bindings.set(normalizedTaskId, record)
    return this.#bindingHandle(record)
  }

  unbind(taskId: string): boolean {
    const record = this.#bindings.get(taskId)
    if (!record || record.closed) return false
    record.closed = true
    record.unsubscribe()
    this.#bindings.delete(taskId)
    return true
  }

  pinTask(taskId: string): void {
    this.#assertAvailable()
    const runtime = this.#state.runtimes[taskId]
    if (!runtime || runtime.pinned) return
    this.#replaceRuntime(taskId, { ...runtime, pinned: true })
  }

  unpinTask(taskId: string): void {
    this.#assertAvailable()
    const runtime = this.#state.runtimes[taskId]
    if (!runtime || !runtime.pinned) return
    this.#replaceRuntime(taskId, { ...runtime, pinned: false })
  }

  async restore(): Promise<CanonicalProjectionState> {
    this.#assertOpen()
    if (!this.#persistence) {
      this.#restored = true
      return this.#state
    }
    if (this.#disabled || this.#persistence.disabled) {
      throw new ProjectionError(
        "RESTORE_DISABLED",
        "Projection restore is disabled.",
      )
    }
    const restored = await this.#persistence.restore()
    if (!restored) {
      this.#restored = true
      return this.#state
    }
    const candidate = restored.state
    if (
      this.#state.revision > 0 &&
      candidate.revision < this.#state.revision
    ) {
      throw new ProjectionError(
        "STALE_SNAPSHOT",
        `Restored projection revision ${candidate.revision} is older than live revision ${this.#state.revision}.`,
        {
          restoredRevision: candidate.revision,
          liveRevision: this.#state.revision,
        },
      )
    }
    for (const [taskId, liveCursor] of Object.entries(this.#state.cursors)) {
      const restoredCursor = candidate.cursors[taskId]
      if (
        restoredCursor &&
        restoredCursor.generation === liveCursor.generation &&
        restoredCursor.committedSequence < liveCursor.committedSequence
      ) {
        throw new ProjectionError(
          "STALE_SNAPSHOT_CURSOR",
          `Restored cursor for ${taskId} regresses from ${liveCursor.committedSequence} to ${restoredCursor.committedSequence}.`,
          {
            taskId,
            restoredSequence: restoredCursor.committedSequence,
            liveSequence: liveCursor.committedSequence,
          },
        )
      }
    }
    const previous = this.#state
    const migrated = restored.migratedFrom !== undefined
    this.#state = migrated
      ? replaceDiagnostics(candidate, {
          ...candidate.diagnostics,
          migratedSnapshots: candidate.diagnostics.migratedSnapshots + 1,
        })
      : candidate
    this.#restored = true
    const changes = allChanges(this.#state)
    this.#notify(previous, changes)
    return this.#state
  }

  async persist(): Promise<void> {
    this.#assertOpen()
    if (!this.#persistence) return
    if (this.#disabled || this.#persistence.disabled) {
      throw new ProjectionError(
        "PERSISTENCE_DISABLED",
        "Projection persistence is disabled.",
      )
    }
    await this.#persistence.persist(this.#state)
    this.#state = replaceDiagnostics(this.#state, {
      ...this.#state.diagnostics,
      persistedSnapshots: this.#state.diagnostics.persistedSnapshots + 1,
    })
  }

  async flush(): Promise<void> {
    await this.#applyQueue
    if (this.#persistTimer) {
      clearTimeout(this.#persistTimer)
      this.#persistTimer = undefined
      await this.persist()
    }
    await this.#persistence?.flush()
  }

  disable(reason = "Canonical projection store disabled."): void {
    if (this.#disabled) return
    this.#disabled = true
    this.#reducer.disable()
    this.#selectors.disable()
    this.#persistence?.disable()
    for (const taskId of [...this.#bindings.keys()]) this.unbind(taskId)
    this.#recordFailure("PROJECTION_DISABLED", reason)
  }

  enable(): void {
    if (!this.#disabled || this.#closed) return
    this.#disabled = false
    this.#reducer.enable()
    this.#selectors.enable()
    this.#persistence?.enable()
  }

  async clearPersisted(): Promise<void> {
    this.#assertOpen()
    await this.#persistence?.clear()
  }

  audit(): ProjectionStoreAudit {
    return {
      storeId: this.id,
      revision: this.#state.revision,
      closed: this.#closed,
      disabled: this.#disabled,
      restored: this.#restored,
      bindings: Object.freeze([...this.#bindings.keys()].sort()),
      persistedRevision: this.#persistence?.persistedRevision ?? -1,
      scheduledPersistence: Boolean(this.#persistTimer),
      state: {
        tasks: Object.keys(this.#state.tasks).length,
        nodes: Object.keys(this.#state.nodes).length,
        workers: Object.keys(this.#state.workers).length,
        tools: Object.keys(this.#state.tools).length,
        artifacts: Object.keys(this.#state.artifacts).length,
        memories: Object.keys(this.#state.memories).length,
        schedulers: Object.keys(this.#state.schedulers).length,
        recoveries: Object.keys(this.#state.recoveries).length,
        commands: Object.keys(this.#state.commands).length,
        permissions: Object.keys(this.#state.permissions).length,
        overlays: Object.keys(this.#state.overlays).length,
        sessions: Object.keys(this.#state.sessions).length,
        events: Object.keys(this.#state.causality.byEvent).length,
        mutations: Object.keys(this.#state.mutations).length,
        tombstones: Object.keys(this.#state.tombstones).length,
        orphans: Object.keys(this.#state.orphans).length,
        optimistic: Object.keys(this.#state.optimistic).length,
        partials: Object.keys(this.#state.partials).length,
      },
    }
  }

  integrity() {
    return auditProjectionIntegrity(this.#state)
  }

  async close(reason = "Canonical projection store closed."): Promise<void> {
    if (this.#closed) return
    for (const taskId of [...this.#bindings.keys()]) this.unbind(taskId)
    try {
      if (!this.#disabled && this.#autoPersist) await this.flush()
    } catch (error) {
      this.#recordFailure("CLOSE_PERSIST_FAILED", error)
    }
    this.#closed = true
    if (this.#persistTimer) {
      clearTimeout(this.#persistTimer)
      this.#persistTimer = undefined
    }
    this.#selectors.clear()
    this.#listeners.clear()
    await this.#persistence?.close()
    void reason
  }

  #bindingHandle(record: BindingRecord): ProjectionIngressBinding {
    return {
      taskId: record.taskId,
      close: () => this.unbind(record.taskId),
      connection: () =>
        record.connection ?? record.transport.snapshot(record.taskId),
    }
  }

  #commit(
    previous: CanonicalProjectionState,
    result: ProjectionTransactionResult,
  ): void {
    this.#state = result.state
    this.#notify(previous, result.changes)
    if (this.#autoPersist) this.#schedulePersist()
  }

  #notify(
    previous: CanonicalProjectionState,
    changes: ProjectionChangeSet,
  ): void {
    const notified = this.#selectors.notify(this.#state, changes)
    if (notified > 0) {
      this.#state = replaceDiagnostics(this.#state, {
        ...this.#state.diagnostics,
        selectorNotifications:
          this.#state.diagnostics.selectorNotifications + notified,
      })
    }
    for (const listener of [...this.#listeners]) {
      try {
        listener(this.#state, previous, changes)
      } catch {
        // A browser observer cannot change transaction commit semantics.
      }
    }
  }

  #schedulePersist(): void {
    if (!this.#persistence || this.#disabled || this.#closed) return
    this.#persistRequestedRevision = Math.max(
      this.#persistRequestedRevision,
      this.#state.revision,
    )
    if (this.#persistTimer) return
    this.#persistTimer = setTimeout(() => {
      this.#persistTimer = undefined
      const requested = this.#persistRequestedRevision
      void this.persist()
        .then(() => {
          if (
            this.#state.revision > requested &&
            this.#autoPersist &&
            !this.#closed
          ) {
            this.#schedulePersist()
          }
        })
        .catch((error) => this.#recordFailure("PERSIST_FAILED", error))
    }, this.#persistenceDebounceMs)
  }

  #replaceRuntime(
    taskId: string,
    runtime: CanonicalProjectionState["runtimes"][string],
  ): void {
    if (!runtime) return
    const previous = this.#state
    this.#state = Object.freeze({
      ...previous,
      revision: previous.revision + 1,
      committedAtMs: this.#now(),
      runtimes: Object.freeze({
        ...previous.runtimes,
        [taskId]: Object.freeze(runtime),
      }),
    })
    const changes: ProjectionChangeSet = Object.freeze({
      revision: this.#state.revision,
      taskIds: new Set([taskId]),
      domains: new Set<ProjectionChangeSet["domains"] extends ReadonlySet<infer T> ? T : never>(),
      entityKeys: new Set<string>(),
      eventIds: new Set<string>(),
      causalKeys: new Set<string>(),
      cursorTaskIds: new Set<string>(),
      diagnostic: false,
    })
    this.#notify(previous, changes)
    if (this.#autoPersist) this.#schedulePersist()
  }

  #recordFailure(
    code: string,
    error: unknown,
    taskId?: string,
  ): void {
    const message = error instanceof Error ? error.message : String(error)
    this.#state = replaceDiagnostics(this.#state, {
      ...this.#state.diagnostics,
      persistenceFailures:
        code.includes("PERSIST") || code.includes("RESTORE")
          ? this.#state.diagnostics.persistenceFailures + 1
          : this.#state.diagnostics.persistenceFailures,
      rejectedTransactions:
        code.includes("REDUCTION") || code.includes("APPLY")
          ? this.#state.diagnostics.rejectedTransactions + 1
          : this.#state.diagnostics.rejectedTransactions,
      rejectedSnapshots:
        code.includes("RESTORE")
          ? this.#state.diagnostics.rejectedSnapshots + 1
          : this.#state.diagnostics.rejectedSnapshots,
      lastError: {
        code,
        message,
        atMs: this.#now(),
        taskId,
      },
    })
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new ProjectionError(
        "PROJECTION_CLOSED",
        "Canonical projection store is closed.",
      )
    }
  }

  #assertAvailable(): void {
    this.#assertOpen()
    if (this.#disabled) {
      throw new ProjectionError(
        "PROJECTION_DISABLED",
        "Canonical projection store is disabled.",
      )
    }
  }
}

function normalizeStoreId(value: string): string {
  const normalized = value.trim()
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(normalized)) {
    throw new ProjectionError(
      "INVALID_STORE_ID",
      `Invalid projection store id: ${value}`,
    )
  }
  return normalized
}

function allChanges(state: CanonicalProjectionState): ProjectionChangeSet {
  return Object.freeze({
    revision: state.revision,
    taskIds: new Set(Object.keys(state.tasks)),
    domains: new Set<ProjectionChangeSet["domains"] extends ReadonlySet<infer T> ? T : never>([
      "task",
      "node",
      "worker",
      "tool",
      "artifact",
      "memory",
      "scheduler",
      "recovery",
      "command",
      "permission",
      "overlay",
      "session",
      "event",
    ]),
    entityKeys: new Set([
      ...Object.keys(state.tasks).map((id) => `task:${id}`),
      ...Object.keys(state.nodes).map((id) => `node:${id}`),
      ...Object.keys(state.workers).map((id) => `worker:${id}`),
      ...Object.keys(state.tools).map((id) => `tool:${id}`),
      ...Object.keys(state.artifacts).map((id) => `artifact:${id}`),
      ...Object.keys(state.memories).map((id) => `memory:${id}`),
      ...Object.keys(state.schedulers).map((id) => `scheduler:${id}`),
      ...Object.keys(state.recoveries).map((id) => `recovery:${id}`),
      ...Object.keys(state.commands).map((id) => `command:${id}`),
      ...Object.keys(state.permissions).map((id) => `permission:${id}`),
      ...Object.keys(state.overlays).map((id) => `overlay:${id}`),
      ...Object.keys(state.sessions).map((id) => `session:${id}`),
    ]),
    eventIds: new Set(Object.keys(state.causality.byEvent)),
    causalKeys: new Set(["*"]),
    cursorTaskIds: new Set(Object.keys(state.cursors)),
    diagnostic: true,
  })
}

export function createCanonicalProjectionStore(
  options: ProjectionStoreOptions = {},
): CanonicalProjectionStore {
  return new CanonicalProjectionStore(options)
}
