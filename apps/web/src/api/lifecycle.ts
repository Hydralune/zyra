import {
  RequestCancelledError,
  createIdempotencyKey,
  normalizeIdentity,
  type MutationReceipt,
  type TaskProjection,
} from "../../../../packages/core/typed-api-client/src/index.ts"
import type {
  CancelTaskInput,
  CreateTaskInput,
  MutationResult,
  ResumeTaskInput,
  TaskApi,
} from "./task-api.ts"

export type LifecycleAction = "create" | "cancel" | "resume"
export type LifecyclePhase = "prepared" | "in_flight" | "committed" | "failed" | "cancelled"

export interface LifecycleRecord {
  operationId: string
  action: LifecycleAction
  phase: LifecyclePhase
  idempotencyKey: string
  taskId?: string
  runId?: string
  requestStartedAt?: number
  settledAt?: number
  receipt?: MutationReceipt
  error?: {
    name: string
    message: string
  }
}

function operationId(action: LifecycleAction, idempotencyKey: string): string {
  let hash = 0x811c9dc5
  for (const byte of new TextEncoder().encode(`${action}:${idempotencyKey}`)) {
    hash ^= byte
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return `lifecycle_${action}_${hash.toString(16).padStart(8, "0")}`
}

function cloneRecord(record: LifecycleRecord): LifecycleRecord {
  return {
    ...record,
    receipt: record.receipt ? { ...record.receipt, binding: { ...record.receipt.binding } } : undefined,
    error: record.error ? { ...record.error } : undefined,
  }
}

export class TaskLifecycleCoordinator {
  readonly #api: TaskApi
  readonly #records = new Map<string, LifecycleRecord>()
  readonly #inflight = new Map<string, Promise<MutationResult>>()
  readonly #controllers = new Map<string, AbortController>()
  readonly #listeners = new Set<(record: LifecycleRecord) => void>()
  #closed = false

  constructor(api: TaskApi) {
    this.#api = api
  }

  async create(input: CreateTaskInput): Promise<MutationResult> {
    const body = {
      goal: input.goal,
      auto_run: input.autoRun ?? false,
      session_id: input.sessionId,
      worker_pool: input.workerPool,
      execution_config: input.executionConfig,
    }
    const idempotencyKey =
      input.idempotencyKey ??
      createIdempotencyKey("task.create", { sessionId: input.sessionId }, body)
    return this.#run("create", idempotencyKey, input.signal, (signal) =>
      this.#api.create({ ...input, idempotencyKey, signal }),
    )
  }

  async cancel(input: CancelTaskInput): Promise<MutationResult> {
    const taskId = normalizeIdentity("task", input.taskId)
    const body = { reason: input.reason ?? "Cancelled from the Zyra web console." }
    const idempotencyKey =
      input.idempotencyKey ??
      createIdempotencyKey("task.cancel", { taskId, runId: input.runId }, body)
    return this.#run("cancel", idempotencyKey, input.signal, (signal) =>
      this.#api.cancel({ ...input, taskId, idempotencyKey, signal }),
      { taskId, runId: input.runId },
    )
  }

  async resume(input: ResumeTaskInput): Promise<MutationResult> {
    const taskId = normalizeIdentity("task", input.taskId)
    const body = { requested_by: "zyra-web" }
    const idempotencyKey =
      input.idempotencyKey ??
      createIdempotencyKey("task.resume", { taskId, runId: input.runId }, body)
    return this.#run("resume", idempotencyKey, input.signal, (signal) =>
      this.#api.resume({ ...input, taskId, idempotencyKey, signal }),
      { taskId, runId: input.runId },
    )
  }

  cancelOperation(operationIdValue: string, reason?: unknown): boolean {
    const controller = this.#controllers.get(operationIdValue)
    if (!controller || controller.signal.aborted) return false
    controller.abort(reason ?? "Lifecycle operation cancelled.")
    return true
  }

  listen(listener: (record: LifecycleRecord) => void): () => void {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  record(operationIdValue: string): LifecycleRecord | undefined {
    const record = this.#records.get(operationIdValue)
    return record ? cloneRecord(record) : undefined
  }

  history(): LifecycleRecord[] {
    return [...this.#records.values()]
      .map(cloneRecord)
      .sort((left, right) => (left.requestStartedAt ?? 0) - (right.requestStartedAt ?? 0))
  }

  inFlight(): LifecycleRecord[] {
    return [...this.#records.values()]
      .filter((record) => record.phase === "in_flight")
      .map(cloneRecord)
  }

  close(reason?: unknown): void {
    if (this.#closed) return
    this.#closed = true
    for (const controller of this.#controllers.values()) {
      controller.abort(reason ?? "Lifecycle coordinator closed.")
    }
  }

  async #run(
    action: LifecycleAction,
    idempotencyKey: string,
    callerSignal: AbortSignal | undefined,
    execute: (signal: AbortSignal) => Promise<MutationResult>,
    binding: { taskId?: string; runId?: string } = {},
  ): Promise<MutationResult> {
    if (this.#closed) throw new RequestCancelledError("Lifecycle coordinator is closed.")
    const id = operationId(action, idempotencyKey)
    const existing = this.#inflight.get(id)
    if (existing) return existing
    const controller = new AbortController()
    this.#controllers.set(id, controller)
    const cancel = () => controller.abort(callerSignal?.reason)
    if (callerSignal?.aborted) cancel()
    else callerSignal?.addEventListener("abort", cancel, { once: true })
    const record: LifecycleRecord = {
      operationId: id,
      action,
      phase: "prepared",
      idempotencyKey,
      taskId: binding.taskId,
      runId: binding.runId,
    }
    this.#remember(record)
    record.phase = "in_flight"
    record.requestStartedAt = Date.now()
    this.#remember(record)
    const promise = execute(controller.signal)
      .then((result) => {
        const task = result.mutation.task
        this.#assertCausality(action, binding, task, result.receipt)
        record.phase = "committed"
        record.taskId = task.taskId
        record.runId = task.runId
        record.receipt = result.receipt
        record.settledAt = Date.now()
        this.#remember(record)
        return result
      })
      .catch((error) => {
        record.phase =
          controller.signal.aborted || error instanceof RequestCancelledError
            ? "cancelled"
            : "failed"
        record.error = {
          name: error instanceof Error ? error.name : typeof error,
          message: error instanceof Error ? error.message : String(error),
        }
        record.settledAt = Date.now()
        this.#remember(record)
        throw error
      })
      .finally(() => {
        this.#controllers.delete(id)
        if (callerSignal) callerSignal.removeEventListener("abort", cancel)
        if (this.#inflight.get(id) === promise) this.#inflight.delete(id)
      })
    this.#inflight.set(id, promise)
    return promise
  }

  #assertCausality(
    action: LifecycleAction,
    expected: { taskId?: string; runId?: string },
    task: TaskProjection,
    receipt: MutationReceipt,
  ): void {
    if (expected.taskId && expected.taskId !== task.taskId) {
      throw new TypeError(`${action} returned task ${task.taskId}, expected ${expected.taskId}`)
    }
    if (expected.runId && expected.runId !== task.runId) {
      throw new TypeError(`${action} returned run ${task.runId}, expected ${expected.runId}`)
    }
    if (receipt.binding.taskId !== task.taskId || receipt.binding.runId !== task.runId) {
      throw new TypeError(`${action} receipt is not bound to the returned task/run`)
    }
  }

  #remember(record: LifecycleRecord): void {
    this.#records.set(record.operationId, cloneRecord(record))
    for (const listener of this.#listeners) {
      try {
        listener(cloneRecord(record))
      } catch {
        // UI observers cannot change transport semantics.
      }
    }
  }
}
