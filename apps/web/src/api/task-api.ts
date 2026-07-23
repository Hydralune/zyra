import {
  OPERATION_NAMES,
  createCursor,
  createIdempotencyKey,
  normalizeIdempotencyKey,
  normalizeIdentity,
  normalizeReceipt,
  type ApiHealth,
  type EventProjection,
  type IdentityBinding,
  type MutationReceipt,
  type RuntimeReadiness,
  type TaskListProjection,
  type TaskMutationProjection,
  type TaskProjection,
} from "../../../../packages/core/typed-api-client/src/index.ts"
import type { ZyraApiClient } from "./client.ts"

export interface ListTaskOptions {
  cursor?: string
  limit?: number
  status?: string
  signal?: AbortSignal
  timeoutMs?: number
}

export interface CreateTaskInput {
  goal: string
  autoRun?: boolean
  sessionId?: string
  workerPool?: Record<string, unknown>
  idempotencyKey?: string
  signal?: AbortSignal
  timeoutMs?: number
}

export interface CancelTaskInput {
  taskId: string
  runId?: string
  reason?: string
  idempotencyKey?: string
  signal?: AbortSignal
  timeoutMs?: number
}

export interface ResumeTaskInput {
  taskId: string
  runId?: string
  idempotencyKey?: string
  signal?: AbortSignal
  timeoutMs?: number
}

export interface MutationResult {
  mutation: TaskMutationProjection
  receipt: MutationReceipt
}

function normalizedLimit(value: number | undefined): number {
  if (value === undefined) return 100
  if (!Number.isFinite(value)) throw new TypeError("Task list limit must be finite")
  return Math.min(1_000, Math.max(1, Math.floor(value)))
}

function goalValue(value: string): string {
  const goal = String(value || "").trim()
  if (!goal) throw new TypeError("Task goal must not be empty")
  if (new TextEncoder().encode(goal).byteLength > 256 * 1_024) {
    throw new TypeError("Task goal exceeds 256 KiB")
  }
  return goal
}

function reasonValue(value: string | undefined): string {
  const reason = String(value || "Cancelled from the Zyra web console.").trim()
  if (!reason) throw new TypeError("Cancellation reason must not be empty")
  if (new TextEncoder().encode(reason).byteLength > 8 * 1_024) {
    throw new TypeError("Cancellation reason exceeds 8 KiB")
  }
  return reason
}

export class TaskApi {
  readonly #client: ZyraApiClient

  constructor(client: ZyraApiClient) {
    this.#client = client
  }

  async health(options: { signal?: AbortSignal; timeoutMs?: number } = {}): Promise<ApiHealth> {
    const response = await this.#client.endpoint<ApiHealth>(OPERATION_NAMES.health, {
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      coordinationKey: "health",
      deduplicate: true,
    })
    return response.data
  }

  async readiness(
    options: { waitMs?: number; signal?: AbortSignal; timeoutMs?: number } = {},
  ): Promise<RuntimeReadiness> {
    const waitMs =
      options.waitMs === undefined
        ? undefined
        : Math.min(30_000, Math.max(0, Math.floor(options.waitMs)))
    const response = await this.#client.endpoint<RuntimeReadiness>(OPERATION_NAMES.readiness, {
      query: { wait_ms: waitMs },
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      coordinationKey: `runtime.readiness:${waitMs ?? 0}`,
      latestWins: true,
    })
    return response.data
  }

  async list(options: ListTaskOptions = {}): Promise<TaskListProjection> {
    const limit = normalizedLimit(options.limit)
    let serverCursor: string | undefined
    let offset = 0
    if (options.cursor) {
      const cursor = this.#client.cursors.remember(options.cursor, {
        scope: "task.list",
        filter: { status: options.status ?? "", limit },
      })
      serverCursor = cursor.serverCursor
      offset = cursor.position
    }
    const response = await this.#client.endpoint<TaskListProjection>(OPERATION_NAMES.taskList, {
      query: {
        cursor: serverCursor,
        limit,
        status: options.status,
      },
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      coordinationKey: `task.list:${options.status ?? "*"}:${serverCursor ?? "first"}:${limit}`,
      deduplicate: true,
    })
    const result = response.data
    const returned = result.tasks.length
    const hasMore = Boolean(result.cursor) || offset + returned < result.total
    const cursor = hasMore
      ? createCursor({
          scope: "task.list",
          position: offset + returned,
          filter: { status: options.status ?? "", limit },
          serverCursor: result.cursor,
        })
      : undefined
    if (cursor) this.#client.cursors.remember(cursor, {
      scope: "task.list",
      filter: { status: options.status ?? "", limit },
    })
    return { ...result, cursor }
  }

  async get(taskId: string, options: { signal?: AbortSignal; timeoutMs?: number } = {}): Promise<TaskProjection> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const response = await this.#client.endpoint<TaskProjection>(OPERATION_NAMES.taskGet, {
      path: { task_id: normalizedTaskId },
      binding: { taskId: normalizedTaskId },
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      coordinationKey: `task.get:${normalizedTaskId}`,
      latestWins: true,
    })
    return response.data
  }

  async events(
    taskId: string,
    options: {
      after?: string
      cursor?: string
      limit?: number
      signal?: AbortSignal
      timeoutMs?: number
    } = {},
  ): Promise<EventProjection[]> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const response = await this.#client.endpoint<EventProjection[]>(OPERATION_NAMES.taskEvents, {
      path: { task_id: normalizedTaskId },
      query: {
        after: options.after,
        cursor: options.cursor,
        limit: normalizedLimit(options.limit),
      },
      binding: { taskId: normalizedTaskId },
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      coordinationKey: `task.events:${normalizedTaskId}:${options.after ?? options.cursor ?? "first"}`,
      latestWins: true,
    })
    return response.data
  }

  async create(input: CreateTaskInput): Promise<MutationResult> {
    const goal = goalValue(input.goal)
    const sessionId = input.sessionId
      ? normalizeIdentity("session", input.sessionId)
      : undefined
    const body = {
      goal,
      auto_run: input.autoRun ?? false,
      session_id: sessionId,
      worker_pool: input.workerPool,
    }
    const binding: IdentityBinding = { sessionId }
    const idempotencyKey = normalizeIdempotencyKey(
      input.idempotencyKey ?? createIdempotencyKey(OPERATION_NAMES.taskCreate, binding, body),
    )
    const response = await this.#client.endpoint<TaskMutationProjection, typeof body>(OPERATION_NAMES.taskCreate, {
      body,
      binding,
      idempotencyKey,
      signal: input.signal,
      timeoutMs: input.timeoutMs,
      coordinationKey: `task.create:${idempotencyKey}`,
      deduplicate: true,
    })
    const receipt = normalizeReceipt(response.raw.body, response.raw.headers, {
      requestId: response.raw.requestId,
      idempotencyKey,
      operation: OPERATION_NAMES.taskCreate,
      statusCode: response.raw.status,
      binding: response.data.task.binding,
    })
    this.#client.receipts.remember(receipt)
    return { mutation: response.data, receipt }
  }

  async cancel(input: CancelTaskInput): Promise<MutationResult> {
    const taskId = normalizeIdentity("task", input.taskId)
    const runId = input.runId ? normalizeIdentity("run", input.runId) : undefined
    const reason = reasonValue(input.reason)
    const binding: IdentityBinding = { taskId, runId }
    const body = { reason }
    const idempotencyKey = normalizeIdempotencyKey(
      input.idempotencyKey ?? createIdempotencyKey(OPERATION_NAMES.taskCancel, binding, body),
    )
    const response = await this.#client.endpoint<TaskMutationProjection, typeof body>(OPERATION_NAMES.taskCancel, {
      path: { task_id: taskId },
      body,
      binding,
      idempotencyKey,
      signal: input.signal,
      timeoutMs: input.timeoutMs,
      coordinationKey: `task.cancel:${taskId}:${idempotencyKey}`,
      deduplicate: true,
    })
    const receipt = normalizeReceipt(response.raw.body, response.raw.headers, {
      requestId: response.raw.requestId,
      idempotencyKey,
      operation: OPERATION_NAMES.taskCancel,
      statusCode: response.raw.status,
      binding: response.data.task.binding,
    })
    this.#client.receipts.remember(receipt)
    return { mutation: response.data, receipt }
  }

  async resume(input: ResumeTaskInput): Promise<MutationResult> {
    const taskId = normalizeIdentity("task", input.taskId)
    const runId = input.runId ? normalizeIdentity("run", input.runId) : undefined
    const binding: IdentityBinding = { taskId, runId }
    const body = { requested_by: "zyra-web" }
    const idempotencyKey = normalizeIdempotencyKey(
      input.idempotencyKey ?? createIdempotencyKey(OPERATION_NAMES.taskResume, binding, body),
    )
    const response = await this.#client.endpoint<TaskMutationProjection, typeof body>(OPERATION_NAMES.taskResume, {
      path: { task_id: taskId },
      body,
      binding,
      idempotencyKey,
      signal: input.signal,
      timeoutMs: input.timeoutMs,
      coordinationKey: `task.resume:${taskId}:${idempotencyKey}`,
      deduplicate: true,
    })
    const receipt = normalizeReceipt(response.raw.body, response.raw.headers, {
      requestId: response.raw.requestId,
      idempotencyKey,
      operation: OPERATION_NAMES.taskResume,
      statusCode: response.raw.status,
      binding: response.data.task.binding,
    })
    this.#client.receipts.remember(receipt)
    return { mutation: response.data, receipt }
  }
}
