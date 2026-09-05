import type {
  ApiHealth,
  RuntimeReadiness,
  TaskProjection,
} from "../../../../packages/core/typed-api-client/src/index.ts"
import type { TaskApi } from "../api/task-api.ts"
import { RetrySupervisor } from "./retry-supervisor.ts"

export type RequestPhase = "idle" | "loading" | "ready" | "empty" | "error" | "reconnecting"

export interface RequestFailure {
  name: string
  message: string
  retryable: boolean
  occurredAt: number
  attempt: number
}

export interface TaskListState {
  phase: RequestPhase
  tasks: readonly TaskProjection[]
  total: number
  cursor?: string
  status?: string
  generation: number
  loadedAt?: number
  failure?: RequestFailure
}

export interface TaskDetailState {
  taskId?: string
  phase: RequestPhase | "not-found"
  task?: TaskProjection
  generation: number
  loadedAt?: number
  failure?: RequestFailure
  /**
   * A background refresh is in flight for a projection that is already
   * rendered.  The surface keeps showing the current turn instead of
   * collapsing back into a loading skeleton.
   */
  syncing?: boolean
  /** When the last background refresh failed while a projection stayed visible. */
  staleSince?: number
}

export interface RuntimeState {
  phase: RequestPhase
  health?: ApiHealth
  readiness?: RuntimeReadiness
  generation: number
  checkedAt?: number
  failure?: RequestFailure
  reconnectAttempt: number
  reconnectAt?: number
}

export interface WorkbenchSnapshot {
  list: TaskListState
  detail: TaskDetailState
  runtime: RuntimeState
  selectedTaskId?: string
  transportEnabled: boolean
  revision: number
}

export interface Clock {
  now(): number
  setTimeout(callback: () => void, delayMs: number): unknown
  clearTimeout(handle: unknown): void
}

const browserClock: Clock = {
  now: () => Date.now(),
  setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
}

function cloneTask(task: TaskProjection): TaskProjection {
  return {
    ...task,
    planNodes: task.planNodes.map((node) => ({
      ...node,
      dependsOn: [...node.dependsOn],
      artifactIds: [...node.artifactIds],
      metadata: { ...node.metadata },
    })),
    artifacts: task.artifacts.map((artifact) => ({
      ...artifact,
      metadata: { ...artifact.metadata },
    })),
    metadata: { ...task.metadata },
    binding: { ...task.binding },
  }
}

function cloneFailure(value: RequestFailure | undefined): RequestFailure | undefined {
  return value ? { ...value } : undefined
}

function cloneSnapshot(snapshot: WorkbenchSnapshot): WorkbenchSnapshot {
  return {
    list: {
      ...snapshot.list,
      tasks: snapshot.list.tasks.map(cloneTask),
      failure: cloneFailure(snapshot.list.failure),
    },
    detail: {
      ...snapshot.detail,
      task: snapshot.detail.task ? cloneTask(snapshot.detail.task) : undefined,
      failure: cloneFailure(snapshot.detail.failure),
    },
    runtime: {
      ...snapshot.runtime,
      health: snapshot.runtime.health
        ? {
            ...snapshot.runtime.health,
            capabilities: [...snapshot.runtime.health.capabilities],
            raw: { ...snapshot.runtime.health.raw },
          }
        : undefined,
      readiness: snapshot.runtime.readiness
        ? {
            ...snapshot.runtime.readiness,
            owners: { ...snapshot.runtime.readiness.owners },
            blockers: [...snapshot.runtime.readiness.blockers],
            raw: { ...snapshot.runtime.readiness.raw },
          }
        : undefined,
      failure: cloneFailure(snapshot.runtime.failure),
    },
    selectedTaskId: snapshot.selectedTaskId,
    transportEnabled: snapshot.transportEnabled,
    revision: snapshot.revision,
  }
}

function errorFailure(error: unknown, attempt: number): RequestFailure {
  const name = error instanceof Error ? error.name : typeof error
  const message = error instanceof Error ? error.message : String(error)
  const normalized = `${name} ${message}`.toLowerCase()
  const retryable =
    !normalized.includes("auth") &&
    !normalized.includes("version") &&
    !normalized.includes("validation") &&
    !normalized.includes("invalid") &&
    !normalized.includes("disabled")
  return {
    name,
    message: message || "Request failed.",
    retryable,
    occurredAt: Date.now(),
    attempt,
  }
}

function notFound(error: unknown): boolean {
  if (!error) return false
  const value = `${error instanceof Error ? error.name : ""} ${
    error instanceof Error ? error.message : String(error)
  }`.toLowerCase()
  return value.includes("not found") || value.includes("404")
}

export class WorkbenchController {
  readonly #tasks: TaskApi
  readonly #clock: Clock
  readonly #listeners = new Set<() => void>()
  readonly #controllers = new Map<"list" | "detail" | "runtime", AbortController>()
  readonly #retry = new RetrySupervisor()
  readonly #details = new Map<string, TaskProjection>()
  readonly #conversationRequests = new Map<string, AbortController>()
  #snapshot: WorkbenchSnapshot
  #reconnectTimer?: unknown
  #closed = false

  constructor(tasks: TaskApi, options: { clock?: Clock } = {}) {
    this.#tasks = tasks
    this.#clock = options.clock ?? browserClock
    this.#snapshot = {
      list: {
        phase: "idle",
        tasks: Object.freeze([]),
        total: 0,
        generation: 0,
      },
      detail: {
        phase: "idle",
        generation: 0,
      },
      runtime: {
        phase: "idle",
        generation: 0,
        reconnectAttempt: 0,
      },
      transportEnabled: true,
      revision: 0,
    }
  }

  getSnapshot = (): WorkbenchSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  selectedTask(): TaskProjection | undefined {
    if (
      this.#snapshot.detail.task &&
      this.#snapshot.detail.task.taskId === this.#snapshot.selectedTaskId
    ) {
      return cloneTask(this.#snapshot.detail.task)
    }
    const selected = this.#snapshot.list.tasks.find(
      (task) => task.taskId === this.#snapshot.selectedTaskId,
    )
    return selected ? cloneTask(selected) : undefined
  }

  async bootstrap(options: { taskId?: string; status?: string; cursor?: string } = {}): Promise<void> {
    this.#assertOpen()
    const operations: Promise<unknown>[] = [
      this.refreshRuntime(),
      this.refreshTasks({ status: options.status, cursor: options.cursor }),
    ]
    if (options.taskId) operations.push(this.loadTask(options.taskId))
    await Promise.allSettled(operations)
  }

  async refreshRuntime(options: { reconnect?: boolean } = {}): Promise<RuntimeState> {
    this.#assertUsable()
    const generation = this.#snapshot.runtime.generation + 1
    const controller = this.#replaceController("runtime")
    this.#clearReconnectTimer()
    this.#replace({
      runtime: {
        ...this.#snapshot.runtime,
        phase: options.reconnect ? "reconnecting" : "loading",
        generation,
        failure: undefined,
        reconnectAt: undefined,
      },
    })
    try {
      const [health, readiness] = await Promise.all([
        this.#tasks.health({ signal: controller.signal, timeoutMs: 10_000 }),
        // Readiness probes all runtime owners and can take longer than a
        // simple health request after restart. Wait for its actual verdict.
        this.#tasks.readiness({ signal: controller.signal, timeoutMs: 30_000 }),
      ])
      if (!this.#isCurrent("runtime", controller, generation)) return this.#snapshot.runtime
      this.#replace({
        runtime: {
          phase: readiness.ready ? "ready" : "error",
          health,
          readiness,
          generation,
          checkedAt: this.#clock.now(),
          failure: readiness.ready
            ? undefined
            : {
                name: "RuntimeNotReady",
                message: readiness.blockers.join("; ") || "Runtime readiness is blocked.",
                retryable: true,
                occurredAt: this.#clock.now(),
                attempt: this.#snapshot.runtime.reconnectAttempt,
              },
          reconnectAttempt: 0,
        },
      })
      this.#retry.success("runtime", this.#clock.now())
      if (!readiness.ready) this.#scheduleReconnect()
      return this.#snapshot.runtime
    } catch (error) {
      if (!this.#isCurrent("runtime", controller, generation)) return this.#snapshot.runtime
      if (controller.signal.aborted) return this.#snapshot.runtime
      const provisional = errorFailure(error, this.#snapshot.runtime.reconnectAttempt + 1)
      const retry = this.#retry.failure("runtime", {
        retryable: provisional.retryable,
        now: this.#clock.now(),
        reason: provisional.message,
      })
      const failure = { ...provisional, attempt: retry.attempt }
      this.#replace({
        runtime: {
          ...this.#snapshot.runtime,
          phase: retry.retry ? "reconnecting" : "error",
          generation,
          failure,
          reconnectAttempt: retry.attempt,
        },
      })
      if (retry.retry) this.#scheduleReconnect(retry.delayMs)
      return this.#snapshot.runtime
    } finally {
      this.#releaseController("runtime", controller)
    }
  }

  async refreshTasks(
    options: {
      status?: string
      cursor?: string
      append?: boolean
      preserveOnError?: boolean
    } = {},
  ): Promise<TaskListState> {
    this.#assertUsable()
    const generation = this.#snapshot.list.generation + 1
    const controller = this.#replaceController("list")
    const status = options.status?.trim().toLowerCase() || undefined
    const previous = this.#snapshot.list.tasks
    this.#replace({
      list: {
        ...this.#snapshot.list,
        phase: "loading",
        generation,
        status,
        cursor: options.cursor,
        failure: undefined,
      },
    })
    try {
      const result = await this.#tasks.list({
        status,
        cursor: options.cursor,
        limit: 100,
        signal: controller.signal,
        timeoutMs: 15_000,
      })
      if (!this.#isCurrent("list", controller, generation)) return this.#snapshot.list
      const tasks = this.#mergeTasks(options.append ? previous : [], result.tasks)
      this.#replace({
        list: {
          phase: tasks.length ? "ready" : "empty",
          tasks: Object.freeze(tasks),
          total: result.total,
          cursor: result.cursor,
          status,
          generation,
          loadedAt: this.#clock.now(),
        },
      })
      this.#reconcileSelectedTask(tasks)
      void this.#hydrateConversation()
      return this.#snapshot.list
    } catch (error) {
      if (!this.#isCurrent("list", controller, generation)) return this.#snapshot.list
      if (controller.signal.aborted) return this.#snapshot.list
      const failure = errorFailure(error, 1)
      this.#replace({
        list: {
          ...this.#snapshot.list,
          phase: "error",
          tasks: options.preserveOnError === false ? Object.freeze([]) : previous,
          total: options.preserveOnError === false ? 0 : this.#snapshot.list.total,
          generation,
          failure,
        },
      })
      return this.#snapshot.list
    } finally {
      this.#releaseController("list", controller)
    }
  }

  async loadMoreTasks(): Promise<TaskListState> {
    const cursor = this.#snapshot.list.cursor
    if (!cursor || this.#snapshot.list.phase === "loading") return this.#snapshot.list
    return this.refreshTasks({
      status: this.#snapshot.list.status,
      cursor,
      append: true,
      preserveOnError: true,
    })
  }

  /**
   * `background: true` refreshes a projection that is already on screen.  The
   * detail state keeps its `ready` phase and current task so live polling never
   * replaces a running conversation with a loading skeleton, and a transient
   * refresh failure marks the projection stale instead of erasing it.
   */
  async loadTask(
    taskId: string,
    options: { background?: boolean } = {},
  ): Promise<TaskDetailState> {
    this.#assertUsable()
    const previous = this.#snapshot.detail
    const background =
      options.background === true &&
      previous.taskId === taskId &&
      previous.phase === "ready" &&
      previous.task?.taskId === taskId
    const generation = previous.generation + 1
    const controller = this.#replaceController("detail")
    this.#replace({
      selectedTaskId: taskId,
      detail: background
        ? { ...previous, generation, syncing: true }
        : {
            taskId,
            phase: "loading",
            task: previous.task?.taskId === taskId ? previous.task : undefined,
            generation,
          },
    })
    try {
      const task = await this.#tasks.get(taskId, {
        signal: controller.signal,
        timeoutMs: 15_000,
      })
      if (!this.#isCurrent("detail", controller, generation)) return this.#snapshot.detail
      this.#rememberDetail(task)
      this.#replace({
        selectedTaskId: task.taskId,
        detail: {
          taskId: task.taskId,
          phase: "ready",
          task: cloneTask(task),
          generation,
          loadedAt: this.#clock.now(),
        },
        list: {
          ...this.#snapshot.list,
          tasks: Object.freeze(this.#mergeTasks(this.#snapshot.list.tasks, [task])),
        },
      })
      void this.#hydrateConversation()
      return this.#snapshot.detail
    } catch (error) {
      if (!this.#isCurrent("detail", controller, generation)) return this.#snapshot.detail
      if (controller.signal.aborted) {
        if (background) {
          this.#replace({ detail: { ...this.#snapshot.detail, syncing: false } })
        }
        return this.#snapshot.detail
      }
      const missing = notFound(error)
      if (background && !missing) {
        this.#replace({
          detail: {
            ...this.#snapshot.detail,
            syncing: false,
            staleSince: this.#clock.now(),
            failure: errorFailure(error, 1),
          },
        })
        return this.#snapshot.detail
      }
      this.#replace({
        detail: {
          taskId,
          phase: missing ? "not-found" : "error",
          generation,
          failure: errorFailure(error, 1),
        },
      })
      return this.#snapshot.detail
    } finally {
      this.#releaseController("detail", controller)
    }
  }

  selectTask(taskId: string | undefined): void {
    this.#assertOpen()
    if (!taskId) {
      this.#replace({
        selectedTaskId: undefined,
        detail: {
          phase: "idle",
          generation: this.#snapshot.detail.generation + 1,
        },
      })
      return
    }
    const task = this.#snapshot.list.tasks.find((candidate) => candidate.taskId === taskId)
    this.#replace({
      selectedTaskId: taskId,
      detail: task
        ? {
            taskId,
            task: cloneTask(task),
            phase: "ready",
            generation: this.#snapshot.detail.generation + 1,
            loadedAt: this.#snapshot.list.loadedAt,
          }
        : {
            taskId,
            phase: "idle",
            generation: this.#snapshot.detail.generation + 1,
          },
    })
  }

  applyMutation(task: TaskProjection): void {
    this.#assertUsable()
    const cloned = cloneTask(task)
    this.#rememberDetail(task)
    this.#replace({
      selectedTaskId: task.taskId,
      detail: {
        taskId: task.taskId,
        task: cloned,
        phase: "ready",
        generation: this.#snapshot.detail.generation + 1,
        loadedAt: this.#clock.now(),
      },
      list: {
        ...this.#snapshot.list,
        phase: "ready",
        tasks: Object.freeze(this.#mergeTasks(this.#snapshot.list.tasks, [task])),
        total: Math.max(this.#snapshot.list.total, this.#snapshot.list.tasks.length + (
          this.#snapshot.list.tasks.some((entry) => entry.taskId === task.taskId) ? 0 : 1
        )),
        loadedAt: this.#clock.now(),
      },
    })
  }

  removeLocalProjection(taskId: string): void {
    this.#assertOpen()
    const tasks = this.#snapshot.list.tasks.filter((task) => task.taskId !== taskId)
    const selected = this.#snapshot.selectedTaskId === taskId
    this.#replace({
      list: {
        ...this.#snapshot.list,
        tasks: Object.freeze(tasks),
        total: Math.max(0, this.#snapshot.list.total - 1),
        phase: tasks.length ? "ready" : "empty",
      },
      ...(selected
        ? {
            selectedTaskId: undefined,
            detail: {
              phase: "idle" as const,
              generation: this.#snapshot.detail.generation + 1,
            },
          }
        : {}),
    })
  }

  cancelRequest(kind: "list" | "detail" | "runtime", reason = "Request superseded."): boolean {
    const controller = this.#controllers.get(kind)
    if (!controller || controller.signal.aborted) return false
    controller.abort(reason)
    return true
  }

  disable(reason = "Workbench data source disabled."): void {
    if (!this.#snapshot.transportEnabled) return
    for (const controller of this.#controllers.values()) controller.abort(reason)
    for (const controller of this.#conversationRequests.values()) controller.abort(reason)
    this.#clearReconnectTimer()
    this.#retry.reset()
    const failure: RequestFailure = {
      name: "WorkbenchDisabled",
      message: reason,
      retryable: false,
      occurredAt: this.#clock.now(),
      attempt: 0,
    }
    this.#replace({
      transportEnabled: false,
      runtime: {
        ...this.#snapshot.runtime,
        phase: "error",
        failure,
      },
      list: {
        ...this.#snapshot.list,
        phase: "error",
        failure,
      },
      detail: this.#snapshot.selectedTaskId
        ? {
            ...this.#snapshot.detail,
            phase: "error",
            failure,
          }
        : this.#snapshot.detail,
    })
  }

  close(reason = "Workbench controller closed."): void {
    if (this.#closed) return
    this.#closed = true
    for (const controller of this.#controllers.values()) controller.abort(reason)
    this.#controllers.clear()
    this.#clearReconnectTimer()
    this.#listeners.clear()
  }

  auditSnapshot(): WorkbenchSnapshot {
    return cloneSnapshot(this.#snapshot)
  }

  #replace(patch: Partial<WorkbenchSnapshot>): void {
    const next: WorkbenchSnapshot = {
      ...this.#snapshot,
      ...patch,
      revision: this.#snapshot.revision + 1,
    }
    this.#snapshot = next
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // UI observers cannot change request lifecycle.
      }
    }
  }

  #mergeTasks(
    current: readonly TaskProjection[],
    incoming: readonly TaskProjection[],
  ): TaskProjection[] {
    const values = new Map<string, TaskProjection>()
    for (const task of current) values.set(task.taskId, cloneTask(task))
    for (const task of incoming) {
      const detail = this.#details.get(task.taskId)
      const candidate = detail && Date.parse(detail.updatedAt) >= Date.parse(task.updatedAt) ? detail : task
      const previous = values.get(task.taskId)
      if (!previous || Date.parse(candidate.updatedAt) >= Date.parse(previous.updatedAt)) {
        values.set(task.taskId, cloneTask(candidate))
      }
    }
    return [...values.values()].sort((left, right) => {
      const updated = Date.parse(right.updatedAt) - Date.parse(left.updatedAt)
      return updated || left.taskId.localeCompare(right.taskId)
    })
  }

  #rememberDetail(task: TaskProjection): void {
    this.#details.delete(task.taskId)
    this.#details.set(task.taskId, cloneTask(task))
    if (this.#details.size > 250) this.#details.delete(this.#details.keys().next().value!)
  }

  async #hydrateConversation(): Promise<void> {
    const selected = this.#snapshot.detail.task
    if (!selected?.sessionId) return
    // History rows are summaries too. Restore prior answers from their real
    // task endpoints, with one request at a time and no cross-session reads.
    for (const row of this.#snapshot.list.tasks) {
      if (this.#closed || this.#snapshot.detail.task?.sessionId !== selected.sessionId) return
      if (row.sessionId !== selected.sessionId || row.taskId === selected.taskId) continue
      const cached = this.#details.get(row.taskId)
      if ((cached && Date.parse(cached.updatedAt) >= Date.parse(row.updatedAt))
        || this.#conversationRequests.has(row.taskId)) continue
      const controller = new AbortController()
      this.#conversationRequests.set(row.taskId, controller)
      try {
        const task = await this.#tasks.get(row.taskId, { signal: controller.signal, timeoutMs: 15_000 })
        if (this.#closed || task.sessionId !== selected.sessionId) continue
        this.#rememberDetail(task)
        this.#replace({ list: {
          ...this.#snapshot.list,
          tasks: Object.freeze(this.#mergeTasks(this.#snapshot.list.tasks, [task])),
        } })
      } catch {
        // Keep the known history row; a later list refresh can retry.
      } finally {
        this.#conversationRequests.delete(row.taskId)
      }
    }
  }

  #reconcileSelectedTask(tasks: readonly TaskProjection[]): void {
    const selected = this.#snapshot.selectedTaskId
    if (!selected) return
    // A list refresh must never supersede a detail request that is still in
    // flight: bumping the detail generation here would silently discard the
    // authoritative detail projection when both run concurrently.
    if (this.#controllers.has("detail")) return
    const task = tasks.find((candidate) => candidate.taskId === selected)
    if (!task) return
    if (this.#snapshot.detail.task?.taskId === selected) {
      // List rows omit answers, plan nodes and artifacts. They must never
      // replace an already loaded detail, even at the same update timestamp.
      if (Date.parse(task.updatedAt) > Date.parse(this.#snapshot.detail.task.updatedAt)) {
        void this.loadTask(selected, { background: true })
      }
      return
    }
    this.#replace({
      detail: {
        taskId: task.taskId,
        task: cloneTask(task),
        phase: "ready",
        generation: this.#snapshot.detail.generation,
        loadedAt: this.#snapshot.list.loadedAt,
      },
    })
  }

  #replaceController(kind: "list" | "detail" | "runtime"): AbortController {
    this.#controllers.get(kind)?.abort(`${kind} request superseded.`)
    const controller = new AbortController()
    this.#controllers.set(kind, controller)
    return controller
  }

  #releaseController(
    kind: "list" | "detail" | "runtime",
    controller: AbortController,
  ): void {
    if (this.#controllers.get(kind) === controller) this.#controllers.delete(kind)
  }

  #isCurrent(
    kind: "list" | "detail" | "runtime",
    controller: AbortController,
    generation: number,
  ): boolean {
    if (this.#closed || this.#controllers.get(kind) !== controller) return false
    return this.#snapshot[kind].generation === generation
  }

  #scheduleReconnect(delayOverride?: number): void {
    this.#clearReconnectTimer()
    if (this.#closed || !this.#snapshot.transportEnabled) return
    const delay = delayOverride ?? Math.max(250, this.#retry.remainingDelay("runtime", this.#clock.now()))
    this.#replace({
      runtime: {
        ...this.#snapshot.runtime,
        reconnectAt: this.#clock.now() + delay,
      },
    })
    this.#reconnectTimer = this.#clock.setTimeout(() => {
      this.#reconnectTimer = undefined
      void this.refreshRuntime({ reconnect: true })
    }, delay)
  }

  #clearReconnectTimer(): void {
    if (this.#reconnectTimer === undefined) return
    this.#clock.clearTimeout(this.#reconnectTimer)
    this.#reconnectTimer = undefined
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Workbench controller is closed.")
  }

  #assertUsable(): void {
    this.#assertOpen()
    if (!this.#snapshot.transportEnabled) {
      throw new TypeError("Workbench data source is disabled.")
    }
  }
}
