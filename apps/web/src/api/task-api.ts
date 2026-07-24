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
  type ControlCommandProjection,
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

export interface ControlCommandInput {
  taskId: string
  runId: string
  text: string
  arguments?: Readonly<Record<string, unknown>>
  requestId: string
  commandId: string
  actorId?: string
  sessionId?: string
  expectedRevision?: number
  sealed?: boolean
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

export interface ArtifactCatalogApiOptions {
  cursor?: string
  limit?: number
  nodeIds?: readonly string[]
  workerIds?: readonly string[]
  mediaTypes?: readonly string[]
  contentFamilies?: readonly string[]
  revisions?: readonly string[]
  createdAfter?: string
  createdBefore?: string
  includeDeleted?: boolean
  signal?: AbortSignal
  timeoutMs?: number
}

export interface ArtifactReadApiOptions {
  revision?: string
  offset?: number
  length?: number
  purpose?: "preview" | "search" | "media" | "download" | "metadata"
  signal?: AbortSignal
  timeoutMs?: number
}

export interface DiffReviewReadOptions {
  revision?: string
  signal?: AbortSignal
  timeoutMs?: number
}

export interface DiffReviewPageOptions extends DiffReviewReadOptions {
  page: number
  maximumBytes: number
  maximumLines: number
}

export interface DiffReviewFileContentOptions extends DiffReviewReadOptions {
  version: "base" | "current"
}

export interface DiffReviewCommentInput {
  revision: string
  diffId: string
  action: "select" | "comment" | "comment_update" | "comment_resolve"
  selection: Readonly<Record<string, unknown>>
  body: string
  commentId?: string
  expectedReviewRevision: number
  actorId: string
  causationId: string
  sealed?: boolean
  permissionPermitId?: string
  signal?: AbortSignal
  timeoutMs?: number
}

export interface DiffReviewMutationOptions {
  signal?: AbortSignal
  timeoutMs?: number
  idempotencyKey: string
  causationId: string
}

export interface TerminalListOptions {
  includeClosed?: boolean
  signal?: AbortSignal
  timeoutMs?: number
}

export interface TerminalCreateInput {
  taskId: string
  runId: string
  sessionId: string
  workerId: string
  commandId: string
  toolCallId: string
  spanId: string
  command: string
  title?: string
  cwd?: string
  shell?: string
  rows?: number
  cols?: number
  actorId?: string
  sealed?: boolean
  permissionPermitId?: string
  environment?: Readonly<Record<string, string>>
  idempotencyKey?: string
  signal?: AbortSignal
  timeoutMs?: number
}

export interface TerminalTicketInput {
  taskId: string
  runId: string
  terminalId: string
  sessionId: string
  cursor: number
  origin: string
  signal?: AbortSignal
  timeoutMs?: number
}

export interface TerminalKillInput {
  taskId: string
  runId: string
  terminalId: string
  sessionId: string
  workerId: string
  toolCallId: string
  spanId: string
  actorId: string
  reason: string
  sealed?: boolean
  permissionPermitId?: string
  idempotencyKey?: string
  signal?: AbortSignal
  timeoutMs?: number
}

export interface BrowserObservabilityOptions {
  view?:
    | "summary"
    | "history"
    | "trace"
    | "health"
    | "downloads"
    | "screenshots"
    | "artifacts"
    | "replay"
    | "integration"
    | "trajectory"
    | "commits"
  browserSessionId?: string
  workerRequestId?: string
  limit?: number
  afterSequence?: number
  signal?: AbortSignal
  timeoutMs?: number
}

export interface BrowserControlInput {
  taskId: string
  runId: string
  browserSessionId: string
  workerRequestId?: string
  actionId?: string
  action: "navigate" | "stop" | "retry" | "inspect"
  commandId: string
  requestId: string
  actorId: string
  reason: string
  url?: string
  retryAction?: Readonly<Record<string, unknown>>
  expectedGeneration?: number
  expectedTaskRevision?: number
  sealed?: boolean
  idempotencyKey: string
  signal?: AbortSignal
  timeoutMs?: number
}

function normalizedLimit(value: number | undefined): number {
  if (value === undefined) return 100
  if (!Number.isFinite(value)) throw new TypeError("Task list limit must be finite")
  return Math.min(1_000, Math.max(1, Math.floor(value)))
}

function browserObservabilityLimit(value: number | undefined): number {
  if (value === undefined) return 100
  if (!Number.isSafeInteger(value) || value < 0 || value > 5_000) {
    throw new TypeError(
      "Browser observability limit must be an integer from 0 through 5000.",
    )
  }
  return value
}

function browserObservabilitySequence(value: number | undefined): number {
  if (value === undefined) return 0
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(
      "Browser observability sequence must be a non-negative safe integer.",
    )
  }
  return value
}

function browserControlRevision(
  value: number | undefined,
  label: string,
): number | undefined {
  if (value === undefined) return undefined
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${label} must be a non-negative safe integer.`)
  }
  return value
}

function browserControlReason(value: string): string {
  const normalized = String(value || "").trim()
  if (!normalized) throw new TypeError("Browser control reason is required.")
  if (new TextEncoder().encode(normalized).byteLength > 8 * 1024) {
    throw new TypeError("Browser control reason exceeds 8 KiB.")
  }
  return normalized
}

function browserControlUrl(value: string | undefined): string | undefined {
  if (value === undefined) return undefined
  const parsed = new URL(String(value).trim())
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new TypeError("Browser control URL must use http or https.")
  }
  parsed.username = ""
  parsed.password = ""
  return parsed.toString()
}

function goalValue(value: string): string {
  const goal = String(value || "").trim()
  if (!goal) throw new TypeError("Task goal must not be empty")
  if (new TextEncoder().encode(goal).byteLength > 256 * 1_024) {
    throw new TypeError("Task goal exceeds 256 KiB")
  }
  return goal
}

function diffReviewIdentity(value: unknown, label: string): string {
  const normalized = String(value ?? "").trim()
  if (
    normalized.length < 1
    || normalized.length > 255
    || !/^[A-Za-z0-9][A-Za-z0-9._:@/+~-]*$/.test(normalized)
  ) {
    throw new TypeError(`${label} is not a valid diff-review identity`)
  }
  return normalized
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

function artifactFilter(
  values: readonly string[] | undefined,
  label: string,
): string | undefined {
  if (!values?.length) return undefined
  if (values.length > 128) {
    throw new TypeError(`Artifact ${label} filter exceeds 128 values`)
  }
  const normalized: string[] = []
  for (const value of values) {
    const selected = String(value || "").trim()
    if (!selected) continue
    if (
      selected.includes(",")
      || /[\r\n\u0000]/.test(selected)
      || new TextEncoder().encode(selected).byteLength > 512
    ) {
      throw new TypeError(`Artifact ${label} filter contains an invalid value`)
    }
    if (!normalized.includes(selected)) normalized.push(selected)
  }
  return normalized.length ? normalized.join(",") : undefined
}

function boundedArtifactCursor(value: string | undefined): string | undefined {
  const selected = String(value || "").trim()
  if (!selected) return undefined
  if (
    new TextEncoder().encode(selected).byteLength > 4_096
    || !/^[A-Za-z0-9_-]+$/.test(selected)
  ) {
    throw new TypeError("Artifact catalog cursor is invalid")
  }
  return selected
}

function optionalArtifactRevision(value: string | undefined): string | undefined {
  const selected = String(value || "").trim().toLowerCase()
  if (!selected) return undefined
  if (!/^sha256:[0-9a-f]{64}$/.test(selected)) {
    throw new TypeError("Artifact revision must be a SHA-256 revision")
  }
  return selected
}

function optionalArtifactTimestamp(
  value: string | undefined,
  label: string,
): string | undefined {
  const selected = String(value || "").trim()
  if (!selected) return undefined
  if (!Number.isFinite(Date.parse(selected))) {
    throw new TypeError(`Artifact ${label} timestamp is invalid`)
  }
  return selected
}

function boundedArtifactOffset(value: number | undefined): number {
  if (value === undefined) return 0
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError("Artifact byte offset must be a non-negative safe integer")
  }
  return value
}

function boundedArtifactLength(value: number | undefined): number {
  if (value === undefined) return 256 * 1_024
  if (!Number.isSafeInteger(value) || value < 1 || value > 1024 * 1024) {
    throw new TypeError("Artifact byte range length must be between 1 and 1048576")
  }
  return value
}

function artifactReadPurpose(
  value: ArtifactReadApiOptions["purpose"],
): NonNullable<ArtifactReadApiOptions["purpose"]> {
  const selected = value ?? "preview"
  if (
    selected !== "preview"
    && selected !== "search"
    && selected !== "media"
    && selected !== "download"
    && selected !== "metadata"
  ) {
    throw new TypeError("Artifact read purpose is invalid")
  }
  return selected
}

function artifactCatalogKey(options: ArtifactCatalogApiOptions): string {
  return JSON.stringify({
    limit: normalizedLimit(options.limit),
    nodeIds: [...(options.nodeIds ?? [])].sort(),
    workerIds: [...(options.workerIds ?? [])].sort(),
    mediaTypes: [...(options.mediaTypes ?? [])].sort(),
    contentFamilies: [...(options.contentFamilies ?? [])].sort(),
    revisions: [...(options.revisions ?? [])].sort(),
    createdAfter: options.createdAfter ?? "",
    createdBefore: options.createdBefore ?? "",
    includeDeleted: options.includeDeleted === true,
  })
}

function boundedDiffReviewInteger(
  value: number,
  name: string,
  minimum: number,
  maximum: number,
): number {
  if (
    !Number.isSafeInteger(value)
    || value < minimum
    || value > maximum
  ) {
    throw new TypeError(
      `${name} must be an integer from ${minimum} through ${maximum}`,
    )
  }
  return value
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

  async artifactCatalog(
    taskId: string,
    options: ArtifactCatalogApiOptions = {},
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const limit = normalizedLimit(options.limit)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskArtifactCatalog,
      {
        path: { task_id: normalizedTaskId },
        query: {
          cursor: boundedArtifactCursor(options.cursor),
          limit,
          node_id: artifactFilter(options.nodeIds, "node"),
          worker_id: artifactFilter(options.workerIds, "worker"),
          media_type: artifactFilter(options.mediaTypes, "media type"),
          content_family: artifactFilter(
            options.contentFamilies,
            "content family",
          ),
          revision: artifactFilter(options.revisions, "revision"),
          created_after: optionalArtifactTimestamp(
            options.createdAfter,
            "created-after",
          ),
          created_before: optionalArtifactTimestamp(
            options.createdBefore,
            "created-before",
          ),
          include_deleted: options.includeDeleted ? "true" : undefined,
        },
        binding: { taskId: normalizedTaskId },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `task.artifacts.catalog:${normalizedTaskId}:`
          + `${options.cursor ?? "first"}:${artifactCatalogKey(options)}`,
        deduplicate: true,
      },
    )
    return response.data
  }

  async artifactMetadata(
    taskId: string,
    artifactId: string,
    options: ArtifactReadApiOptions = {},
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const normalizedArtifactId = normalizeIdentity("artifact", artifactId)
    const revision = optionalArtifactRevision(options.revision)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskArtifactMetadata,
      {
        path: {
          task_id: normalizedTaskId,
          artifact_id: normalizedArtifactId,
        },
        query: {
          revision,
          purpose: "metadata",
        },
        binding: {
          taskId: normalizedTaskId,
          artifactId: normalizedArtifactId,
        },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `task.artifacts.metadata:${normalizedTaskId}:`
          + `${normalizedArtifactId}:${revision ?? "canonical"}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async artifactContent(
    taskId: string,
    artifactId: string,
    options: ArtifactReadApiOptions = {},
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const normalizedArtifactId = normalizeIdentity("artifact", artifactId)
    const revision = optionalArtifactRevision(options.revision)
    const offset = boundedArtifactOffset(options.offset)
    const length = boundedArtifactLength(options.length)
    const purpose = artifactReadPurpose(options.purpose)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskArtifactContent,
      {
        path: {
          task_id: normalizedTaskId,
          artifact_id: normalizedArtifactId,
        },
        query: {
          revision,
          offset,
          length,
          purpose,
        },
        binding: {
          taskId: normalizedTaskId,
          artifactId: normalizedArtifactId,
        },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `task.artifacts.content:${normalizedTaskId}:${normalizedArtifactId}:`
          + `${revision ?? "canonical"}:${offset}:${length}:${purpose}`,
        deduplicate: true,
      },
    )
    return response.data
  }

  async artifactReceipts(
    taskId: string,
    artifactId: string,
    options: { signal?: AbortSignal; timeoutMs?: number } = {},
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const normalizedArtifactId = normalizeIdentity("artifact", artifactId)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskArtifactReceipts,
      {
        path: {
          task_id: normalizedTaskId,
          artifact_id: normalizedArtifactId,
        },
        binding: {
          taskId: normalizedTaskId,
          artifactId: normalizedArtifactId,
        },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `task.artifacts.receipts:${normalizedTaskId}:${normalizedArtifactId}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async browserObservability(
    taskId: string,
    options: BrowserObservabilityOptions = {},
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const browserSessionId = options.browserSessionId
      ? normalizeIdentity("session", options.browserSessionId)
      : undefined
    const workerRequestId = options.workerRequestId
      ? normalizeIdentity("request", options.workerRequestId)
      : undefined
    const view = options.view ?? "summary"
    const limit = browserObservabilityLimit(options.limit)
    const afterSequence = browserObservabilitySequence(options.afterSequence)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskBrowserObservability,
      {
        path: { task_id: normalizedTaskId },
        query: {
          view,
          browser_session_id: browserSessionId,
          worker_request_id: workerRequestId,
          limit,
          after_sequence: afterSequence,
        },
        binding: {
          taskId: normalizedTaskId,
          sessionId: browserSessionId,
          requestId: workerRequestId,
        },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `task.browser-observability:${normalizedTaskId}:${view}:`
          + `${browserSessionId ?? "*"}:${workerRequestId ?? "*"}:`
          + `${afterSequence}:${limit}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async browserControl(
    input: BrowserControlInput,
  ): Promise<Record<string, unknown>> {
    const taskId = normalizeIdentity("task", input.taskId)
    const runId = normalizeIdentity("run", input.runId)
    const browserSessionId = normalizeIdentity(
      "session",
      input.browserSessionId,
    )
    const workerRequestId = input.workerRequestId
      ? normalizeIdentity("request", input.workerRequestId)
      : undefined
    const actionId = input.actionId
      ? diffReviewIdentity(input.actionId, "browser action")
      : undefined
    const commandId = diffReviewIdentity(input.commandId, "browser command")
    const requestId = normalizeIdentity("request", input.requestId)
    const actorId = diffReviewIdentity(input.actorId, "browser actor")
    const expectedGeneration = browserControlRevision(
      input.expectedGeneration,
      "Browser expected generation",
    )
    const expectedTaskRevision = browserControlRevision(
      input.expectedTaskRevision,
      "Browser expected task revision",
    )
    const url =
      input.action === "navigate"
        ? browserControlUrl(input.url)
        : undefined
    if (input.action === "navigate" && !url) {
      throw new TypeError("Navigate requires an http or https URL.")
    }
    const retryAction =
      input.action === "retry"
        ? input.retryAction
        : undefined
    if (
      input.action === "retry"
      && (
        !retryAction
        || typeof retryAction.action !== "string"
        || !retryAction.arguments
        || typeof retryAction.arguments !== "object"
        || Array.isArray(retryAction.arguments)
      )
    ) {
      throw new TypeError("Retry requires one bounded prior browser action.")
    }
    const binding: IdentityBinding = {
      taskId,
      runId,
      sessionId: browserSessionId,
      requestId,
      controlCommandId: commandId,
    }
    const body = {
      run_id: runId,
      session_id: browserSessionId,
      idempotency_key: normalizeIdempotencyKey(input.idempotencyKey),
      sealed: input.sealed === true,
      competition_mode:
        input.sealed === true ? "sealed_autonomous" : "interactive",
      browser_viewer_control: {
        schema: "zyra.browser-viewer.control.v1",
        action: input.action,
        command_id: commandId,
        request_id: requestId,
        actor_id: actorId,
        reason: browserControlReason(input.reason),
        browser_session_id: browserSessionId,
        worker_request_id: workerRequestId,
        action_id: actionId,
        url,
        retry_action: retryAction,
        expected_generation: expectedGeneration,
        expected_task_revision: expectedTaskRevision,
        sealed: input.sealed === true,
      },
    }
    const response = await this.#client.endpoint<
      Record<string, unknown>,
      typeof body
    >(OPERATION_NAMES.taskBrowserControl, {
      path: { task_id: taskId },
      body,
      binding,
      idempotencyKey: body.idempotency_key,
      signal: input.signal,
      timeoutMs: input.timeoutMs,
      coordinationKey:
        `task.browser-control:${taskId}:${body.idempotency_key}`,
      deduplicate: true,
    })
    return Object.freeze({
      ...response.data,
      status_code: response.raw.status,
      receipt_replayed:
        response.raw.headers.get("X-Zyra-Receipt-Replayed") === "true",
    })
  }

  async diffReviewManifest(
    taskId: string,
    artifactId: string,
    options: DiffReviewReadOptions = {},
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const normalizedArtifactId = normalizeIdentity("artifact", artifactId)
    const revision = optionalArtifactRevision(options.revision)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskDiffReviewManifest,
      {
        path: {
          task_id: normalizedTaskId,
          artifact_id: normalizedArtifactId,
        },
        query: { revision },
        binding: {
          taskId: normalizedTaskId,
          artifactId: normalizedArtifactId,
        },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `task.diff-review.manifest:${normalizedTaskId}:`
          + `${normalizedArtifactId}:${revision ?? "canonical"}`,
        latestWins: true,
      },
    )
    return response.data
  }

  async diffReviewPage(
    taskId: string,
    artifactId: string,
    fileId: string,
    options: DiffReviewPageOptions,
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const normalizedArtifactId = normalizeIdentity("artifact", artifactId)
    const normalizedFileId = diffReviewIdentity(fileId, "Diff file")
    const revision = optionalArtifactRevision(options.revision)
    const page = boundedDiffReviewInteger(
      options.page,
      "diff page",
      0,
      100_000,
    )
    const maximumBytes = boundedDiffReviewInteger(
      options.maximumBytes,
      "diff page bytes",
      1_024,
      8 * 1_024 * 1_024,
    )
    const maximumLines = boundedDiffReviewInteger(
      options.maximumLines,
      "diff page lines",
      1,
      100_000,
    )
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskDiffReviewPage,
      {
        path: {
          task_id: normalizedTaskId,
          artifact_id: normalizedArtifactId,
          file_id: normalizedFileId,
        },
        query: {
          revision,
          page,
          maximum_bytes: maximumBytes,
          maximum_lines: maximumLines,
        },
        binding: {
          taskId: normalizedTaskId,
          artifactId: normalizedArtifactId,
        },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `task.diff-review.page:${normalizedTaskId}:${normalizedArtifactId}:`
          + `${normalizedFileId}:${revision ?? "canonical"}:${page}:`
          + `${maximumBytes}:${maximumLines}`,
        deduplicate: true,
      },
    )
    return response.data
  }

  async diffReviewFileContent(
    taskId: string,
    artifactId: string,
    fileId: string,
    options: DiffReviewFileContentOptions,
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const normalizedArtifactId = normalizeIdentity("artifact", artifactId)
    const normalizedFileId = diffReviewIdentity(fileId, "Diff file")
    const revision = optionalArtifactRevision(options.revision)
    if (options.version !== "base" && options.version !== "current") {
      throw new TypeError("Diff file content version is invalid")
    }
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskDiffReviewFileContent,
      {
        path: {
          task_id: normalizedTaskId,
          artifact_id: normalizedArtifactId,
          file_id: normalizedFileId,
        },
        query: {
          revision,
          version: options.version,
        },
        binding: {
          taskId: normalizedTaskId,
          artifactId: normalizedArtifactId,
        },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey:
          `task.diff-review.file-content:${normalizedTaskId}:`
          + `${normalizedArtifactId}:${normalizedFileId}:`
          + `${revision ?? "canonical"}:${options.version}`,
        latestWins: options.version === "current",
        deduplicate: options.version === "base",
      },
    )
    return response.data
  }

  async diffReviewComment(
    taskId: string,
    artifactId: string,
    input: DiffReviewCommentInput,
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const normalizedArtifactId = normalizeIdentity("artifact", artifactId)
    const causationId = diffReviewIdentity(input.causationId, "Causation")
    const idempotencyKey = normalizeIdempotencyKey(
      createIdempotencyKey(
        "task.diff-review.comment",
        {
          taskId: normalizedTaskId,
          artifactId: normalizedArtifactId,
        },
        `${input.diffId}:${input.action}:${input.expectedReviewRevision}:${causationId}`,
      ),
    )
    const response = await this.#client.endpoint<
      Record<string, unknown>,
      Record<string, unknown>
    >(OPERATION_NAMES.taskDiffReviewComment, {
      path: {
        task_id: normalizedTaskId,
        artifact_id: normalizedArtifactId,
      },
      body: {
        schema: "zyra.diff-review-comment-request.v1",
        artifact_revision: optionalArtifactRevision(input.revision),
        diff_id: diffReviewIdentity(input.diffId, "Diff"),
        action: input.action,
        selection: input.selection,
        body: input.body,
        comment_id: input.commentId,
        expected_review_revision: boundedDiffReviewInteger(
          input.expectedReviewRevision,
          "review revision",
          0,
          Number.MAX_SAFE_INTEGER,
        ),
        actor_id: diffReviewIdentity(input.actorId, "Actor"),
        causation_id: causationId,
        session_id: `console:${normalizedTaskId}`,
        session_revision: 0,
        worker_request_id: `console-diff-review:${normalizedTaskId}`,
        tool_call_id: causationId,
        sealed: Boolean(input.sealed),
        permission_permit_id: input.permissionPermitId,
      },
      binding: {
        taskId: normalizedTaskId,
        artifactId: normalizedArtifactId,
      },
      idempotencyKey,
      causationId,
      signal: input.signal,
      timeoutMs: input.timeoutMs,
      coordinationKey:
        `task.diff-review.comment:${normalizedTaskId}:`
        + `${normalizedArtifactId}:${idempotencyKey}`,
      deduplicate: true,
    })
    return response.data
  }

  async diffReviewApply(
    taskId: string,
    artifactId: string,
    body: unknown,
    options: DiffReviewMutationOptions,
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const normalizedArtifactId = normalizeIdentity("artifact", artifactId)
    const idempotencyKey = normalizeIdempotencyKey(options.idempotencyKey)
    const causationId = diffReviewIdentity(options.causationId, "Causation")
    const response = await this.#client.endpoint<
      Record<string, unknown>,
      unknown
    >(OPERATION_NAMES.taskDiffReviewApply, {
      path: {
        task_id: normalizedTaskId,
        artifact_id: normalizedArtifactId,
      },
      body,
      binding: {
        taskId: normalizedTaskId,
        artifactId: normalizedArtifactId,
      },
      idempotencyKey,
      causationId,
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      coordinationKey:
        `task.diff-review.apply:${normalizedTaskId}:`
        + `${normalizedArtifactId}:${idempotencyKey}`,
      deduplicate: true,
    })
    return response.data
  }

  async diffReviewRollback(
    taskId: string,
    transactionId: string,
    body: unknown,
    options: DiffReviewMutationOptions,
  ): Promise<Record<string, unknown>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const normalizedTransactionId = diffReviewIdentity(
      transactionId,
      "Workspace transaction",
    )
    const idempotencyKey = normalizeIdempotencyKey(options.idempotencyKey)
    const causationId = diffReviewIdentity(options.causationId, "Causation")
    const response = await this.#client.endpoint<
      Record<string, unknown>,
      unknown
    >(OPERATION_NAMES.taskDiffReviewRollback, {
      path: {
        task_id: normalizedTaskId,
        transaction_id: normalizedTransactionId,
      },
      body,
      binding: { taskId: normalizedTaskId },
      idempotencyKey,
      causationId,
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      coordinationKey:
        `task.diff-review.rollback:${normalizedTaskId}:`
        + `${normalizedTransactionId}:${idempotencyKey}`,
      deduplicate: true,
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

  async terminalList(
    taskId: string,
    options: TerminalListOptions = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskTerminalList,
      {
        path: { task_id: normalizedTaskId },
        query: { include_closed: options.includeClosed === true },
        binding: { taskId: normalizedTaskId },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `task.terminals.list:${normalizedTaskId}:${Boolean(options.includeClosed)}`,
        latestWins: true,
      },
    )
    return Object.freeze({ ...response.data })
  }

  async terminalGet(
    taskId: string,
    terminalId: string,
    options: { signal?: AbortSignal; timeoutMs?: number } = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const selectedTerminal = diffReviewIdentity(terminalId, "terminal")
    const response = await this.#client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskTerminalGet,
      {
        path: { task_id: normalizedTaskId, terminal_id: selectedTerminal },
        binding: { taskId: normalizedTaskId },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `task.terminals.get:${normalizedTaskId}:${selectedTerminal}`,
        latestWins: true,
      },
    )
    return Object.freeze({ ...response.data })
  }

  async terminalCreate(
    input: TerminalCreateInput,
  ): Promise<Readonly<Record<string, unknown>>> {
    const taskId = normalizeIdentity("task", input.taskId)
    const runId = normalizeIdentity("run", input.runId)
    const sessionId = normalizeIdentity("session", input.sessionId)
    const command = String(input.command || "")
    const commandBytes = new TextEncoder().encode(command).byteLength
    if (!command.trim() || commandBytes > 256 * 1_024 || command.includes("\u0000")) {
      throw new TypeError("Terminal command must contain 1..262144 non-NUL UTF-8 bytes.")
    }
    const dimension = (value: number | undefined, fallback: number, minimum: number, maximum: number) => {
      const selected = value ?? fallback
      if (!Number.isSafeInteger(selected) || selected < minimum || selected > maximum) {
        throw new TypeError(`Terminal dimension must be in ${minimum}..${maximum}.`)
      }
      return selected
    }
    const body = {
      run_id: runId,
      session_id: sessionId,
      worker_id: diffReviewIdentity(input.workerId, "terminal worker"),
      command_id: diffReviewIdentity(input.commandId, "terminal command identity"),
      tool_call_id: diffReviewIdentity(input.toolCallId, "terminal tool call"),
      span_id: normalizeIdentity("span", input.spanId),
      command,
      title: String(input.title || "Terminal").slice(0, 256),
      cwd: input.cwd ? String(input.cwd) : undefined,
      shell: input.shell ? String(input.shell) : undefined,
      rows: dimension(input.rows, 24, 2, 500),
      cols: dimension(input.cols, 80, 2, 1_000),
      actor_id: diffReviewIdentity(input.actorId || "zyra-web-terminal", "terminal actor"),
      sealed: input.sealed === true,
      competition_mode: input.sealed ? "sealed_autonomous" : "interactive",
      permission_permit_id: input.permissionPermitId
        ? diffReviewIdentity(input.permissionPermitId, "terminal permit")
        : undefined,
      environment: { ...(input.environment ?? {}) },
    }
    const binding: IdentityBinding = { taskId, runId, sessionId, spanId: input.spanId }
    const idempotencyKey = normalizeIdempotencyKey(
      input.idempotencyKey
        ?? createIdempotencyKey(OPERATION_NAMES.taskTerminalCreate, binding, body),
    )
    const response = await this.#client.endpoint<Record<string, unknown>, typeof body>(
      OPERATION_NAMES.taskTerminalCreate,
      {
        path: { task_id: taskId },
        body,
        binding,
        idempotencyKey,
        signal: input.signal,
        timeoutMs: input.timeoutMs,
        coordinationKey: `task.terminals.create:${taskId}:${idempotencyKey}`,
        deduplicate: true,
      },
    )
    return Object.freeze({
      ...response.data,
      receipt_replayed: response.raw.headers.get("X-Zyra-Receipt-Replayed") === "true",
    })
  }

  async terminalTicket(
    input: TerminalTicketInput,
  ): Promise<Readonly<Record<string, unknown>>> {
    const taskId = normalizeIdentity("task", input.taskId)
    const runId = normalizeIdentity("run", input.runId)
    const sessionId = normalizeIdentity("session", input.sessionId)
    const terminalId = diffReviewIdentity(input.terminalId, "terminal")
    if (!Number.isSafeInteger(input.cursor) || input.cursor < 0) {
      throw new TypeError("Terminal ticket cursor must be a non-negative safe integer.")
    }
    const origin = new URL(input.origin).origin
    const body = {
      run_id: runId,
      session_id: sessionId,
      cursor: input.cursor,
      origin,
      protocol: "zyra.terminal.v1",
    }
    const response = await this.#client.endpoint<Record<string, unknown>, typeof body>(
      OPERATION_NAMES.taskTerminalTicket,
      {
        path: { task_id: taskId, terminal_id: terminalId },
        body,
        binding: { taskId, runId, sessionId },
        signal: input.signal,
        timeoutMs: input.timeoutMs,
        coordinationKey: `task.terminals.ticket:${taskId}:${terminalId}:${input.cursor}`,
        latestWins: true,
      },
    )
    return Object.freeze({ ...response.data })
  }

  async terminalKill(
    input: TerminalKillInput,
  ): Promise<Readonly<Record<string, unknown>>> {
    const taskId = normalizeIdentity("task", input.taskId)
    const runId = normalizeIdentity("run", input.runId)
    const sessionId = normalizeIdentity("session", input.sessionId)
    const terminalId = diffReviewIdentity(input.terminalId, "terminal")
    const reason = String(input.reason || "Killed from Zyra terminal viewer.").trim()
    if (!reason || new TextEncoder().encode(reason).byteLength > 2_048) {
      throw new TypeError("Terminal kill reason must contain 1..2048 UTF-8 bytes.")
    }
    const body = {
      run_id: runId,
      session_id: sessionId,
      worker_id: diffReviewIdentity(input.workerId, "terminal worker"),
      tool_call_id: diffReviewIdentity(input.toolCallId, "terminal tool call"),
      span_id: normalizeIdentity("span", input.spanId),
      actor_id: diffReviewIdentity(input.actorId, "terminal actor"),
      reason,
      sealed: input.sealed === true,
      competition_mode: input.sealed ? "sealed_autonomous" : "interactive",
      permission_permit_id: input.permissionPermitId
        ? diffReviewIdentity(input.permissionPermitId, "terminal permit")
        : undefined,
    }
    const binding: IdentityBinding = { taskId, runId, sessionId, spanId: input.spanId }
    const idempotencyKey = normalizeIdempotencyKey(
      input.idempotencyKey
        ?? createIdempotencyKey(OPERATION_NAMES.taskTerminalKill, binding, body),
    )
    const response = await this.#client.endpoint<Record<string, unknown>, typeof body>(
      OPERATION_NAMES.taskTerminalKill,
      {
        path: { task_id: taskId, terminal_id: terminalId },
        body,
        binding,
        idempotencyKey,
        signal: input.signal,
        timeoutMs: input.timeoutMs,
        coordinationKey: `task.terminals.kill:${taskId}:${terminalId}:${idempotencyKey}`,
        deduplicate: true,
      },
    )
    return Object.freeze({
      ...response.data,
      receipt_replayed: response.raw.headers.get("X-Zyra-Receipt-Replayed") === "true",
    })
  }

  terminalWebSocketUrl(
    taskId: string,
    terminalId: string,
    path: string,
    query: { ticket: string; cursor: number; protocol: string },
  ): string {
    const normalizedTaskId = normalizeIdentity("task", taskId)
    const selectedTerminal = diffReviewIdentity(terminalId, "terminal")
    const safePath = String(path || "").trim()
    if (
      !safePath.startsWith("/") || safePath.startsWith("//") || safePath.includes("\\")
      || safePath.split("/").some((segment) => segment === "." || segment === "..")
      || !safePath.includes(encodeURIComponent(normalizedTaskId))
      || !safePath.includes(encodeURIComponent(selectedTerminal))
    ) throw new TypeError("Terminal WebSocket path is unsafe or cross-bound.")
    const url = new URL(safePath, this.#client.baseUrl)
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
    url.searchParams.set("ticket", query.ticket)
    url.searchParams.set("cursor", String(query.cursor))
    url.searchParams.set("protocol", query.protocol)
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

  async controlCommand(
    input: ControlCommandInput,
  ): Promise<Readonly<Record<string, unknown>>> {
    const taskId = normalizeIdentity("task", input.taskId)
    const runId = normalizeIdentity("run", input.runId)
    const requestId = normalizeIdentity("request", input.requestId)
    const commandId = normalizeIdentity("control_command", input.commandId)
    const text = String(input.text || "").trim()
    if (!text.startsWith("/")) {
      throw new TypeError("Control command text must begin with a slash.")
    }
    if (new TextEncoder().encode(text).byteLength > 256 * 1_024) {
      throw new TypeError("Control command text exceeds 256 KiB.")
    }
    const expectedRevision =
      input.expectedRevision === undefined
        ? undefined
        : Number(input.expectedRevision)
    if (
      expectedRevision !== undefined &&
      (!Number.isSafeInteger(expectedRevision) || expectedRevision < 0)
    ) {
      throw new TypeError("Expected control revision must be a non-negative safe integer.")
    }
    const binding: IdentityBinding = {
      taskId,
      runId,
      requestId,
      controlCommandId: commandId,
      sessionId: input.sessionId
        ? normalizeIdentity("session", input.sessionId)
        : undefined,
    }
    const commandBody = {
      text,
      arguments: { ...(input.arguments ?? {}) },
      request_id: requestId,
      command_id: commandId,
      actor_id: String(input.actorId || "zyra-web-topology").trim(),
      session_id: binding.sessionId,
      expected_session_revision: expectedRevision,
      sealed: input.sealed === true,
      competition_mode: input.sealed ? "sealed_autonomous" : "interactive",
    }
    const idempotencyKey = normalizeIdempotencyKey(
      input.idempotencyKey ??
        createIdempotencyKey(
          OPERATION_NAMES.taskControlCommand,
          binding,
          commandBody,
        ),
    )
    const body = {
      ...commandBody,
      idempotency_key: idempotencyKey,
    }
    const response = await this.#client.endpoint<
      ControlCommandProjection,
      typeof body
    >(OPERATION_NAMES.taskControlCommand, {
      path: { task_id: taskId },
      body,
      binding,
      idempotencyKey,
      signal: input.signal,
      timeoutMs: input.timeoutMs,
      coordinationKey: `task.control-command:${taskId}:${idempotencyKey}`,
      deduplicate: true,
    })
    return Object.freeze({
      ...response.data.raw,
      task: response.data.task,
      control_request: response.data.controlRequest,
      command: response.data.command,
      command_result: response.data.commandResult,
      event: response.data.event,
      intervention_counted: response.data.interventionCounted,
      receipt_replayed: response.raw.headers.get("X-Zyra-Receipt-Replayed") === "true",
    })
  }
}
