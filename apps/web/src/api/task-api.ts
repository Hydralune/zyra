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
  type StreamingResponseHandle,
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

export interface EventIngressFilterQuery {
  eventTypes?: readonly string[]
  intent?: string
  correlationId?: string
  artifactId?: string
}

export interface EventIngressPageOptions extends EventIngressFilterQuery {
  cursor?: string
  generation: number
  limit: number
  waitMs?: number
  streamMs?: number
  heartbeatMs?: number
  signal?: AbortSignal
  timeoutMs?: number
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

function eventIngressFilterQuery(
  options: EventIngressFilterQuery,
): Record<string, string | undefined> {
  const eventTypes = options.eventTypes
    ?.map((value) => String(value || "").trim())
    .filter(Boolean)
  if (eventTypes && eventTypes.length > 128) {
    throw new TypeError("Event ingress filter exceeds 128 event types")
  }
  return {
    event_types: eventTypes?.join(",") || undefined,
    intent: String(options.intent || "").trim() || undefined,
    correlation_id: String(options.correlationId || "").trim() || undefined,
    artifact_id: String(options.artifactId || "").trim() || undefined,
  }
}

function filterKey(options: EventIngressFilterQuery): string {
  return JSON.stringify(eventIngressFilterQuery(options))
}

function boundedGeneration(value: number): number {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new TypeError("Event ingress generation must be a positive safe integer")
  }
  return value
}

function boundedWait(value: number | undefined): number {
  if (value === undefined) return 750
  if (!Number.isFinite(value)) throw new TypeError("Event ingress wait must be finite")
  return Math.min(25_000, Math.max(0, Math.floor(value)))
}

function boundedStream(value: number | undefined): number {
  if (value === undefined) return 15_000
  if (!Number.isFinite(value)) throw new TypeError("Event ingress stream duration must be finite")
  return Math.min(60_000, Math.max(250, Math.floor(value)))
}

function boundedHeartbeat(value: number | undefined): number {
  if (value === undefined) return 5_000
  if (!Number.isFinite(value)) throw new TypeError("Event ingress heartbeat must be finite")
  return Math.min(60_000, Math.max(250, Math.floor(value)))
}

function requiredCursor(value: string | undefined): string {
  const cursor = String(value || "").trim()
  if (!cursor) throw new TypeError("Event ingress cursor must not be empty")
  if (new TextEncoder().encode(cursor).byteLength > 4_096) {
    throw new TypeError("Event ingress cursor exceeds 4096 bytes")
  }
  return cursor
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

  async eventIngressCapabilities(
    taskId: string,
    options: EventIngressFilterQuery & {
      signal?: AbortSignal
      timeoutMs?: number
      generation?: number
      cursor?: string
    } = {},
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskEventIngressCapabilities,
      {
        path: { task_id: normalizedTaskId },
        query: {
          ...eventIngressFilterQuery(options),
          generation:
            options.generation === undefined
              ? undefined
              : boundedGeneration(options.generation),
          cursor: options.cursor,
        },
        binding: { taskId: normalizedTaskId },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `task.event-ingress.capabilities:${normalizedTaskId}:` +
          `${options.generation ?? 1}:${options.cursor ?? "first"}:${filterKey(options)}`,
        deduplicate: true,
      },
    )
    return response.data
  }

  async eventIngressSnapshot(
    taskId: string,
    options: EventIngressPageOptions,
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskEventIngressSnapshot,
      {
        path: { task_id: normalizedTaskId },
        query: {
          ...eventIngressFilterQuery(options),
          cursor: options.cursor,
          generation: boundedGeneration(options.generation),
          limit: normalizedLimit(options.limit),
        },
        binding: { taskId: normalizedTaskId },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `task.event-ingress.snapshot:${normalizedTaskId}:` +
          `${options.generation}:${options.cursor ?? "first"}:${filterKey(options)}`,
        deduplicate: true,
      },
    )
    return response.data
  }

  async eventIngressDelta(
    taskId: string,
    options: EventIngressPageOptions,
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const waitMs = boundedWait(options.waitMs)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskEventIngressDelta,
      {
        path: { task_id: normalizedTaskId },
        query: {
          ...eventIngressFilterQuery(options),
          cursor: requiredCursor(options.cursor),
          generation: boundedGeneration(options.generation),
          limit: normalizedLimit(options.limit),
          wait_ms: waitMs,
        },
        binding: { taskId: normalizedTaskId },
        signal: options.signal,
        timeoutMs: options.timeoutMs ?? Math.max(30_000, waitMs + 10_000),
        coordinationKey:
          `task.event-ingress.delta:${normalizedTaskId}:` +
          `${options.generation}:${options.cursor}:${filterKey(options)}`,
        latestWins: true,
      },
    )
    return response.data
  }

  openEventIngressSse(
    taskId: string,
    options: EventIngressPageOptions,
  ): Promise<StreamingResponseHandle> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const waitMs = boundedWait(options.waitMs)
    const streamMs = boundedStream(options.streamMs)
    return this.#client.openEndpointStream(
      OPERATION_NAMES.taskEventIngressSse,
      {
        path: { task_id: normalizedTaskId },
        query: {
          ...eventIngressFilterQuery(options),
          cursor: requiredCursor(options.cursor),
          generation: boundedGeneration(options.generation),
          limit: normalizedLimit(options.limit),
          wait_ms: waitMs,
          stream_ms: streamMs,
          heartbeat_ms: boundedHeartbeat(options.heartbeatMs),
        },
        binding: { taskId: normalizedTaskId },
        signal: options.signal,
        timeoutMs: options.timeoutMs ?? Math.max(30_000, streamMs + 10_000),
        headers: { Accept: "text/event-stream" },
      },
    )
  }

  eventIngressWebSocketUrl(
    taskId: string,
    path: string,
    query: Record<string, string | number | boolean | undefined>,
  ): string {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const safePath = String(path || "").trim()
    if (
      !safePath.startsWith("/") ||
      safePath.startsWith("//") ||
      safePath.includes("\\") ||
      safePath.split("/").some((segment) => segment === "." || segment === "..")
    ) throw new TypeError("Event WebSocket path is unsafe")
    if (!safePath.includes(normalizedTaskId)) {
      throw new TypeError("Event WebSocket path is not bound to the requested task")
    }
    const url = new URL(safePath, this.#client.baseUrl)
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) url.searchParams.set(key, String(value))
    }
    return url.toString()
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
