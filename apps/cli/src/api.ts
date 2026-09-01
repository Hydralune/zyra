import {
  admitQueueSnapshot,
  transportBody,
  type CommandQueueSnapshot,
  type CommandTransportRequest,
} from "@zyra/commands"
import {
  OPERATION_NAMES,
  createIdempotencyKey,
  normalizeIdempotencyKey,
  normalizeIdentity,
  readServerSentEvents,
  type ControlCommandProjection,
  type EventProjection,
  type PermissionControlProjection,
  type RuntimeReadiness,
  type SessionListProjection,
  type SessionProjection,
  type TaskListProjection,
  type TaskMutationProjection,
  type TaskProjection,
  ZyraApiError,
  ZyraTypedApiClient,
} from "@zyra/typed-api-client"
import { CliTaskError, CliVerifierError } from "./contracts.ts"
import { parseProductModels, type ProductModelOption } from "./product/config/model.ts"

const INGRESS_PROTOCOL = "zyra.event-ingress/v1"
const CAPABILITIES_SCHEMA = "zyra.event-ingress-capabilities/v1"
const SNAPSHOT_SCHEMA = "zyra.event-ingress-snapshot/v1"
const DELTA_SCHEMA = "zyra.event-ingress-delta/v1"
const FRAME_SCHEMA = "zyra.event-ingress-frame/v1"

export interface IngressFrame {
  schema: typeof FRAME_SCHEMA
  kind: "event"
  source: string
  generation: number
  taskId: string
  sequence: number
  previousSequence: number
  eventId: string
  eventType: string
  cursor?: string
  presentation?: Readonly<Record<string, unknown>>
  event: Readonly<Record<string, unknown>>
  raw: Readonly<Record<string, unknown>>
}

export interface ProductExecutionConfig {
  providerId: string
  modelId: string
}

function taskCreateBody(goal: string, sealed: boolean, sessionId?: string, execution?: ProductExecutionConfig) {
  const session = sessionId?.trim() ? { session_id: sessionId.trim() } : {}
  const productExecution = execution ? {
    execution_config: {
      provider_id: execution.providerId,
      model_id: execution.modelId,
    },
  } : {}
  return sealed
    ? {
        goal,
        auto_run: false,
        sealed: true,
        sealed_autonomous: true,
        competition_mode: "sealed_autonomous",
        ...session,
        ...productExecution,
      } as const
    : { goal, auto_run: false, ...session, ...productExecution } as const
}

export function taskSubmissionIdempotencyKey(
  goal: string,
  sealed: boolean,
  submissionGeneration: string,
  sessionId?: string,
  execution?: ProductExecutionConfig,
): string {
  return createIdempotencyKey(
    OPERATION_NAMES.taskCreate,
    {},
    taskCreateBody(goal, sealed, sessionId, execution),
    submissionGeneration,
  )
}

export interface IngressPage {
  cursor: string
  generation: number
  frames: readonly IngressFrame[]
  hasMore: boolean
  caughtUp: boolean
  nextSequence: number
}

export interface IngressCapabilities {
  taskId: string
  generation: number
  subscriptionCursor: string
  subscriptionSequence: number
  sseAvailable: boolean
  raw: Readonly<Record<string, unknown>>
}

export type IngressStreamMessage =
  | { kind: "ready"; taskId: string; generation: number; sequence: number }
  | { kind: "event"; taskId: string; generation: number; sequence: number; frame: IngressFrame }
  | { kind: "heartbeat" | "close"; taskId: string; generation: number; sequence: number; cursor: string }

export interface ScenarioRun {
  schema: "zyra.scenario-run/v1"
  scenario_run_id: string
  phase: string
  terminal: boolean
  revision: number
  task_id?: string
  owner_run_id?: string
  verification_receipt?: Readonly<Record<string, unknown>>
  evidence_manifest?: Readonly<Record<string, unknown>>
  raw: Readonly<Record<string, unknown>>
}

export interface PermissionBinding {
  taskId: string
  runId: string
  sessionId: string
}

export interface PermissionSessionClaim extends PermissionBinding {
  custodyToken: string
  custodyId?: string
  custodyFingerprint?: string
  created: boolean
  verified: boolean
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new CliTaskError(`${label} must be an object.`, "contract_invalid")
  }
  return value as Record<string, unknown>
}

function stringValue(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new CliTaskError(`${label} must be a non-empty string.`, "contract_invalid")
  }
  return value
}

function integerValue(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value)) {
    throw new CliTaskError(`${label} must be a safe integer.`, "contract_invalid")
  }
  return Number(value)
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new CliTaskError(`${label} must be a boolean.`, "contract_invalid")
  }
  return value
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined
}

function permissionBinding(value: PermissionBinding): PermissionBinding {
  return Object.freeze({
    taskId: normalizeIdentity("task", value.taskId),
    runId: normalizeIdentity("run", value.runId),
    sessionId: permissionSessionIdentity(value.sessionId),
  })
}

function permissionSessionIdentity(value: unknown): string {
  const rendered = typeof value === "string" ? value.trim() : ""
  if (
    !rendered
    || rendered === "."
    || rendered === ".."
    || !/^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$/.test(rendered)
  ) {
    throw new CliTaskError("Permission session identity is invalid.", "permission_contract_invalid")
  }
  return rendered
}

function permissionIdentifier(value: unknown, label: string): string {
  const rendered = typeof value === "string" ? value.trim() : ""
  if (
    !rendered
    || rendered === "."
    || rendered === ".."
    || !/^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$/.test(rendered)
  ) {
    throw new CliTaskError(`${label} is invalid.`, "permission_contract_invalid")
  }
  return rendered
}

function permissionBindingKey(value: PermissionBinding): string {
  return `${value.taskId}\u0000${value.runId}\u0000${value.sessionId}`
}

function permissionToken(value: unknown): string {
  const token = typeof value === "string"
    ? value.replace(/^Bearer\s+/i, "").trim()
    : ""
  if (!token || /[\u0000\r\n]/.test(token) || new TextEncoder().encode(token).byteLength > 8_192) {
    throw new CliTaskError(
      "Permission custody is unavailable; the runtime did not issue or verify a bearer token.",
      "permission_custody_missing",
    )
  }
  return token
}

function permissionHeaders(tokenValue: string): Headers {
  const headers = new Headers()
  headers.set("Authorization", `Bearer ${permissionToken(tokenValue)}`)
  headers.set("Cache-Control", "no-store")
  return headers
}

function permissionOk(
  projection: PermissionControlProjection,
  status: number,
): PermissionControlProjection {
  if (projection.ok && status >= 200 && status < 300) return projection
  throw new CliTaskError(
    projection.message ?? `Permission operation ${projection.operation} failed.`,
    projection.error ?? "permission_request_failed",
    { operation: projection.operation, status },
  )
}

function schema(value: Record<string, unknown>, expected: string, label: string): void {
  if (value.schema !== expected) {
    throw new CliTaskError(`${label} used an unknown schema.`, "contract_schema_unknown", {
      expected_schema: expected,
      actual_schema: typeof value.schema === "string" ? value.schema : null,
    })
  }
}

function scenarioError(value: Record<string, unknown>): never {
  const detail = value.detail && typeof value.detail === "object" && !Array.isArray(value.detail)
    ? value.detail as Record<string, unknown>
    : undefined
  const dirtyKinds = Array.isArray(detail?.dirty_kinds)
    ? detail.dirty_kinds.filter((item): item is string => typeof item === "string")
    : undefined
  if (value.error === "scenario_evidence_verification_failed") {
    throw new CliVerifierError(
      typeof value.message === "string" ? value.message : "Scenario verification failed.",
      { phase: value.phase, detail_present: value.detail !== undefined },
    )
  }
  throw new CliTaskError(
    typeof value.message === "string" ? value.message : "Scenario API request failed.",
    typeof value.error === "string" ? value.error : "scenario_request_failed",
    {
      phase: value.phase,
      retryable: value.retryable === true,
      ...(dirtyKinds?.length ? { dirty_kinds: dirtyKinds } : {}),
    },
  )
}

function parseScenarioRun(value: unknown): ScenarioRun {
  const selected = record(value, "scenario run")
  schema(selected, "zyra.scenario-run/v1", "scenario run")
  return {
    schema: "zyra.scenario-run/v1",
    scenario_run_id: stringValue(selected.scenario_run_id, "scenario run id"),
    phase: stringValue(selected.phase, "scenario phase"),
    terminal: booleanValue(selected.terminal, "scenario terminal"),
    revision: integerValue(selected.revision, "scenario revision"),
    task_id: typeof selected.task_id === "string" && selected.task_id ? selected.task_id : undefined,
    owner_run_id: typeof selected.owner_run_id === "string" && selected.owner_run_id ? selected.owner_run_id : undefined,
    verification_receipt: selected.verification_receipt && typeof selected.verification_receipt === "object"
      ? record(selected.verification_receipt, "scenario verification receipt")
      : undefined,
    evidence_manifest: selected.evidence_manifest && typeof selected.evidence_manifest === "object"
      ? record(selected.evidence_manifest, "scenario evidence manifest")
      : undefined,
    raw: { ...selected },
  }
}

export function parseIngressFrame(value: unknown, taskId: string, generation: number): IngressFrame {
  const selected = record(value, "event ingress frame")
  schema(selected, FRAME_SCHEMA, "event ingress frame")
  if (selected.kind !== "event") {
    throw new CliTaskError("Event ingress returned an unsupported frame kind.", "contract_frame_kind_unknown")
  }
  const frameTaskId = stringValue(selected.taskId, "event ingress frame task id")
  if (frameTaskId !== taskId) {
    throw new CliTaskError("Event ingress returned a cross-task frame.", "contract_cross_task_frame")
  }
  const frameGeneration = integerValue(selected.generation, "event ingress frame generation")
  if (frameGeneration !== generation) {
    throw new CliTaskError("Event ingress frame generation changed without resynchronization.", "contract_generation_mismatch")
  }
  return {
    schema: FRAME_SCHEMA,
    kind: "event",
    source: stringValue(selected.source, "event ingress frame source"),
    generation: frameGeneration,
    taskId: frameTaskId,
    sequence: integerValue(selected.sequence, "event ingress frame sequence"),
    previousSequence: integerValue(selected.previousSequence, "event ingress previous sequence"),
    eventId: stringValue(selected.eventId, "event ingress event id"),
    eventType: stringValue(selected.eventType, "event ingress event type"),
    cursor: typeof selected.cursor === "string" ? selected.cursor : undefined,
    presentation: selected.presentation === undefined ? undefined : record(selected.presentation, "product presentation"),
    event: record(selected.event, "canonical runtime event"),
    raw: { ...selected },
  }
}

function parseIngressPage(
  value: unknown,
  taskId: string,
  expectedSchema: typeof SNAPSHOT_SCHEMA | typeof DELTA_SCHEMA,
): IngressPage {
  const selected = record(value, "event ingress page")
  schema(selected, expectedSchema, "event ingress page")
  if (selected.protocol !== INGRESS_PROTOCOL || selected.taskId !== taskId) {
    throw new CliTaskError("Event ingress protocol or task binding is invalid.", "contract_ingress_binding_invalid")
  }
  const generation = integerValue(selected.generation, "event ingress generation")
  const cursor = stringValue(selected.cursor, "event ingress cursor")
  if (!Array.isArray(selected.frames)) {
    throw new CliTaskError("Event ingress page omitted frames.", "contract_frames_missing")
  }
  const frames = selected.frames.map((entry) => parseIngressFrame(entry, taskId, generation))
  let previous = integerValue(selected.fromSequence, "event ingress from sequence")
  for (const frame of frames) {
    if (frame.previousSequence !== previous || frame.sequence <= previous) {
      throw new CliTaskError("Event ingress sequence continuity failed.", "contract_event_gap", {
        expected_previous_sequence: previous,
        actual_previous_sequence: frame.previousSequence,
        sequence: frame.sequence,
      })
    }
    previous = frame.sequence
  }
  const nextSequence = integerValue(selected.nextSequence, "event ingress next sequence")
  if (nextSequence < previous) {
    throw new CliTaskError("Event ingress cursor moved behind the last frame.", "contract_cursor_regression")
  }
  return {
    cursor,
    generation,
    frames,
    hasMore: selected.hasMore === true,
    caughtUp: expectedSchema === SNAPSHOT_SCHEMA ? selected.complete === true : selected.caughtUp === true,
    nextSequence,
  }
}

export class CliApi {
  readonly client: ZyraTypedApiClient
  readonly timeoutMs: number

  constructor(input: { baseUrl: string; token?: string; timeoutMs: number; fetch?: typeof fetch }) {
    this.timeoutMs = input.timeoutMs
    this.client = new ZyraTypedApiClient({
      baseUrl: input.baseUrl,
      token: input.token,
      fetch: input.fetch,
      timeoutMs: input.timeoutMs,
      clientName: "zyra-cli",
      clientVersion: "0.1.0",
    })
  }

  close(reason?: unknown): void {
    this.client.close(reason)
  }

  async createPendingTask(goal: string, sealed: boolean, sessionId?: string, execution?: ProductExecutionConfig): Promise<TaskMutationProjection> {
    const body = taskCreateBody(goal, sealed, sessionId, execution)
    // Idempotency owns transport retries for one explicit submission. It must
    // not collapse a later `zyra run` with the same goal into an old task.
    const idempotencyKey = taskSubmissionIdempotencyKey(goal, sealed, crypto.randomUUID(), sessionId, execution)
    const response = await this.client.endpoint<TaskMutationProjection, typeof body>(OPERATION_NAMES.taskCreate, {
      body,
      idempotencyKey,
      timeoutMs: this.timeoutMs,
      coordinationKey: `cli.task.create:${idempotencyKey}`,
      deduplicate: true,
    })
    return response.data
  }

  async writeWorkspaceFile(
    workspaceId: string,
    path: string,
    content: Uint8Array,
    signal?: AbortSignal,
  ): Promise<Readonly<Record<string, unknown>>> {
    const body = {
      path,
      mount: "task",
      encoding: "base64",
      content: Buffer.from(content).toString("base64"),
      idempotency_key: `cli-workspace-seed:${crypto.randomUUID()}`,
    }
    const response = await this.client.endpoint<Readonly<Record<string, unknown>>, typeof body>(
      OPERATION_NAMES.workspaceFileWrite,
      {
        path: { workspace_id: workspaceId },
        body,
        idempotencyKey: createIdempotencyKey(
          OPERATION_NAMES.workspaceFileWrite,
          {},
          body,
        ),
        signal,
        timeoutMs: this.timeoutMs,
        coordinationKey: `cli.workspace.write:${workspaceId}:${path}`,
      },
    )
    return response.data
  }

  async readWorkspaceFile(
    workspaceId: string,
    path: string,
    signal?: AbortSignal,
  ): Promise<Uint8Array> {
    const response = await this.client.endpoint<Readonly<Record<string, unknown>>>(
      OPERATION_NAMES.workspaceFiles,
      {
        path: { workspace_id: workspaceId },
        query: { path, mount: "task", read: true, encoding: "base64" },
        signal,
        timeoutMs: this.timeoutMs,
        coordinationKey: `cli.workspace.read:${workspaceId}:${path}`,
        deduplicate: true,
      },
    )
    const encoding = response.data.encoding
    const content = response.data.content
    if (encoding !== "base64" || typeof content !== "string") {
      throw new CliTaskError("Workspace file response is not base64 encoded.", "contract_workspace_file_invalid")
    }
    return Buffer.from(content, "base64")
  }

  async runTask(task: TaskProjection, signal?: AbortSignal): Promise<TaskMutationProjection> {
    // One invocation keeps one key across transport retries, while a later
    // explicit `zyra resume` receives a fresh generation.  Reusing a key
    // across CLI invocations would replay a previously committed 503 forever
    // after the underlying recovery defect had been repaired.
    const body = {
      requested_by: "zyra-cli",
      resume_invocation_id: `resume_${crypto.randomUUID().replaceAll("-", "")}`,
    }
    const idempotencyKey = createIdempotencyKey(OPERATION_NAMES.taskResume, task.binding, body)
    const response = await this.client.endpoint<TaskMutationProjection, typeof body>(OPERATION_NAMES.taskResume, {
      path: { task_id: task.taskId },
      body,
      binding: task.binding,
      idempotencyKey,
      signal,
      timeoutMs: this.timeoutMs,
      coordinationKey: `cli.task.run:${task.taskId}:${idempotencyKey}`,
      deduplicate: true,
    })
    return response.data
  }

  async cancelTask(task: TaskProjection, reason: string): Promise<TaskMutationProjection> {
    const body = { reason }
    const idempotencyKey = createIdempotencyKey(OPERATION_NAMES.taskCancel, task.binding, body)
    const response = await this.client.endpoint<TaskMutationProjection, typeof body>(OPERATION_NAMES.taskCancel, {
      path: { task_id: task.taskId },
      body,
      binding: task.binding,
      idempotencyKey,
      timeoutMs: Math.min(this.timeoutMs, 30_000),
      coordinationKey: `cli.task.cancel:${task.taskId}:${idempotencyKey}`,
      deduplicate: true,
    })
    return response.data
  }

  async submitControlCommand(request: CommandTransportRequest): Promise<Readonly<Record<string, unknown>>> {
    const body = transportBody(request)
    const response = await this.client.endpoint<ControlCommandProjection, typeof body>(
      OPERATION_NAMES.taskControlCommand,
      {
        path: { task_id: request.taskId },
        body,
        binding: {
          taskId: request.taskId,
          runId: request.runId,
          sessionId: request.sessionId,
          requestId: request.requestId,
          controlCommandId: request.commandId,
        },
        idempotencyKey: request.idempotencyKey,
        signal: request.signal,
        timeoutMs: request.timeoutMs ?? this.timeoutMs,
        coordinationKey: `cli.control-command:${request.taskId}:${request.idempotencyKey}`,
        deduplicate: true,
      },
    )
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

  async commandQueue(input: {
    taskId: string
    sessionId?: string
    includeTerminal?: boolean
    signal?: AbortSignal
  }): Promise<CommandQueueSnapshot> {
    const taskId = normalizeIdentity("task", input.taskId)
    const response = await this.client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskCommandQueue,
      {
        path: { task_id: taskId },
        query: {
          session_id: input.sessionId,
          include_terminal: input.includeTerminal === true,
        },
        binding: { taskId, sessionId: input.sessionId },
        signal: input.signal,
        timeoutMs: Math.min(this.timeoutMs, 30_000),
        coordinationKey: `cli.command-queue:${taskId}:${input.sessionId ?? "*"}`,
        latestWins: true,
      },
    )
    return admitQueueSnapshot(response.data, {
      taskId,
      sessionId: input.sessionId,
      restored: input.includeTerminal === true,
    })
  }

  async cancelControlCommand(input: {
    taskId: string
    requestId: string
    reason: string
    idempotencyKey?: string
    signal?: AbortSignal
  }): Promise<Readonly<Record<string, unknown>>> {
    const taskId = normalizeIdentity("task", input.taskId)
    const requestId = normalizeIdentity("request", input.requestId)
    const reason = input.reason.trim()
    if (!reason) throw new TypeError("Command cancellation reason is required.")
    const candidateBody = { reason }
    const idempotencyKey = normalizeIdempotencyKey(
      input.idempotencyKey
        ?? createIdempotencyKey(
          OPERATION_NAMES.taskCommandCancel,
          { taskId, requestId },
          candidateBody,
        ),
    )
    const body = { ...candidateBody, idempotency_key: idempotencyKey }
    const response = await this.client.endpoint<Record<string, unknown>, typeof body>(
      OPERATION_NAMES.taskCommandCancel,
      {
        path: { task_id: taskId, request_id: requestId },
        body,
        binding: { taskId, requestId },
        idempotencyKey,
        signal: input.signal,
        timeoutMs: Math.min(this.timeoutMs, 30_000),
        coordinationKey: `cli.command-cancel:${taskId}:${requestId}:${idempotencyKey}`,
        deduplicate: true,
      },
    )
    return Object.freeze({ ...response.data })
  }

  async openPermissionSession(
    binding: PermissionBinding,
    input: { custodyToken?: string; externalSessionExists?: boolean; signal?: AbortSignal } = {},
  ): Promise<PermissionSessionClaim> {
    const normalized = permissionBinding(binding)
    const body = {
      session_id: normalized.sessionId,
      run_id: normalized.runId,
      task_id: normalized.taskId,
      external_session_exists: input.externalSessionExists === true,
    }
    const headers = input.custodyToken ? permissionHeaders(input.custodyToken) : undefined
    const response = await this.client.endpoint<PermissionControlProjection, typeof body>(
      OPERATION_NAMES.permissionSessionOpen,
      {
        body,
        headers,
        binding: { taskId: normalized.taskId, runId: normalized.runId },
        idempotencyKey: createIdempotencyKey(
          OPERATION_NAMES.permissionSessionOpen,
          { taskId: normalized.taskId, runId: normalized.runId },
          body,
        ),
        signal: input.signal,
        timeoutMs: Math.min(this.timeoutMs, 30_000),
        coordinationKey: `cli.permission.open:${permissionBindingKey(normalized)}`,
        deduplicate: true,
      },
    )
    const projection = permissionOk(response.data, response.raw.status)
    const session = record(projection.raw.session, "permission session")
    const custodyToken = permissionToken(
      session.bearer_token ?? session.custody_token ?? input.custodyToken,
    )
    return Object.freeze({
      ...normalized,
      custodyToken,
      custodyId: optionalString(session.custody_id),
      custodyFingerprint: optionalString(session.custody_fingerprint),
      created: session.created === true,
      verified: session.verified !== false && session.custody_verified !== false,
    })
  }

  async resumePermissionSession(
    binding: PermissionBinding,
    custodyToken: string,
    signal?: AbortSignal,
  ): Promise<Readonly<Record<string, unknown>>> {
    const normalized = permissionBinding(binding)
    const body = {
      session_id: normalized.sessionId,
      run_id: normalized.runId,
      task_id: normalized.taskId,
    }
    const response = await this.client.endpoint<PermissionControlProjection, typeof body>(
      OPERATION_NAMES.permissionSessionResume,
      {
        path: { session_id: normalized.sessionId },
        body,
        headers: permissionHeaders(custodyToken),
        binding: { taskId: normalized.taskId, runId: normalized.runId },
        idempotencyKey: createIdempotencyKey(
          OPERATION_NAMES.permissionSessionResume,
          { taskId: normalized.taskId, runId: normalized.runId },
          body,
        ),
        signal,
        timeoutMs: Math.min(this.timeoutMs, 30_000),
        coordinationKey: `cli.permission.resume:${permissionBindingKey(normalized)}`,
        latestWins: true,
      },
    )
    return Object.freeze({ ...permissionOk(response.data, response.raw.status).raw })
  }

  async permissionRequests(
    binding: PermissionBinding,
    custodyToken: string,
    input: { status?: string; pendingOnly?: boolean; limit?: number; signal?: AbortSignal } = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    const normalized = permissionBinding(binding)
    const response = await this.client.endpoint<PermissionControlProjection>(
      OPERATION_NAMES.permissionRequests,
      {
        query: {
          task_id: normalized.taskId,
          run_id: normalized.runId,
          session_id: normalized.sessionId,
          status: input.status,
          pending_only: input.pendingOnly,
          limit: Math.max(1, Math.min(1_000, input.limit ?? 200)),
        },
        headers: permissionHeaders(custodyToken),
        binding: { taskId: normalized.taskId, runId: normalized.runId },
        signal: input.signal,
        timeoutMs: Math.min(this.timeoutMs, 30_000),
        coordinationKey: `cli.permission.requests:${permissionBindingKey(normalized)}`,
        latestWins: true,
      },
    )
    return Object.freeze({ ...permissionOk(response.data, response.raw.status).raw })
  }

  async resolvePermission(input: {
    binding: PermissionBinding
    custodyToken: string
    requestId: string
    responseId: string
    effect: "allow" | "deny"
    consoleResponse: Readonly<Record<string, unknown>>
    feedback?: string
    signal?: AbortSignal
  }): Promise<Readonly<Record<string, unknown>>> {
    const binding = permissionBinding(input.binding)
    const requestId = permissionIdentifier(input.requestId, "Permission request identity")
    const responseId = permissionIdentifier(input.responseId, "Permission response identity")
    const body = {
      task_id: binding.taskId,
      run_id: binding.runId,
      session_id: binding.sessionId,
      effect: input.effect,
      response_id: responseId,
      idempotency_key: normalizeIdempotencyKey(responseId),
      console_response: JSON.parse(JSON.stringify(input.consoleResponse)) as Record<string, unknown>,
      display_responder: "zyra-cli",
      feedback: input.feedback?.trim() || undefined,
      require_identity_echo: true,
    }
    const response = await this.client.endpoint<PermissionControlProjection, typeof body>(
      OPERATION_NAMES.permissionRequestResolve,
      {
        path: { request_id: requestId },
        body,
        headers: permissionHeaders(input.custodyToken),
        binding: { taskId: binding.taskId, runId: binding.runId },
        idempotencyKey: body.idempotency_key,
        signal: input.signal,
        timeoutMs: Math.min(this.timeoutMs, 30_000),
        coordinationKey: `cli.permission.resolve:${permissionBindingKey(binding)}:${requestId}`,
        deduplicate: true,
      },
    )
    return Object.freeze({ ...permissionOk(response.data, response.raw.status).raw })
  }

  async task(taskId: string): Promise<TaskProjection> {
    const selected = normalizeIdentity("task", taskId)
    const response = await this.client.endpoint<TaskProjection>(OPERATION_NAMES.taskGet, {
      path: { task_id: selected },
      binding: { taskId: selected },
      timeoutMs: Math.min(this.timeoutMs, 30_000),
      coordinationKey: `cli.task.get:${selected}:${Date.now()}`,
      latestWins: true,
    })
    return response.data
  }

  async tasks(input: { status?: string; limit?: number; cursor?: string } = {}): Promise<TaskListProjection> {
    const response = await this.client.endpoint<TaskListProjection>(OPERATION_NAMES.taskList, {
      query: { status: input.status, limit: input.limit ?? 100, cursor: input.cursor },
      timeoutMs: Math.min(this.timeoutMs, 30_000),
      coordinationKey: `cli.task.list:${input.status ?? "all"}:${input.cursor ?? "first"}`,
      latestWins: true,
    })
    return response.data
  }

  async sessions(input: { status?: string; limit?: number; cursor?: string } = {}): Promise<SessionListProjection> {
    const response = await this.client.endpoint<SessionListProjection>(OPERATION_NAMES.sessionList, {
      query: { status: input.status, limit: input.limit ?? 100, cursor: input.cursor },
      timeoutMs: Math.min(this.timeoutMs, 30_000),
      coordinationKey: `cli.session.list:${input.status ?? "all"}:${input.cursor ?? "first"}`,
      latestWins: true,
    })
    return response.data
  }

  async readiness(signal?: AbortSignal): Promise<RuntimeReadiness> {
    const response = await this.client.endpoint<RuntimeReadiness>(OPERATION_NAMES.readiness, {
      signal,
      timeoutMs: Math.min(this.timeoutMs, 30_000),
      coordinationKey: `cli.runtime.readiness:${Date.now()}`,
      latestWins: true,
    })
    return response.data
  }

  async providerModels(signal?: AbortSignal): Promise<readonly ProductModelOption[]> {
    const response = await this.client.endpoint<Record<string, unknown>>(OPERATION_NAMES.providerModels, {
      query: { available_only: true },
      signal,
      timeoutMs: Math.min(this.timeoutMs, 30_000),
      coordinationKey: `cli.provider.models:${Date.now()}`,
      latestWins: true,
    })
    return parseProductModels(response.data)
  }

  async diffReviewManifest(taskId: string, artifactId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const task = normalizeIdentity("task", taskId)
    const artifact = normalizeIdentity("artifact", artifactId)
    const response = await this.client.endpoint<Record<string, unknown>>(OPERATION_NAMES.taskDiffReviewManifest, {
      path: { task_id: task, artifact_id: artifact },
      binding: { taskId: task, artifactId: artifact },
      signal,
      timeoutMs: Math.min(this.timeoutMs, 30_000),
      coordinationKey: `cli.diff.manifest:${task}:${artifact}`,
      latestWins: true,
    })
    return response.data
  }

  async diffReviewPage(input: {
    taskId: string
    artifactId: string
    fileId: string
    revision: string
    page: number
    maximumBytes: number
    maximumLines: number
    signal?: AbortSignal
  }): Promise<Record<string, unknown>> {
    const task = normalizeIdentity("task", input.taskId)
    const artifact = normalizeIdentity("artifact", input.artifactId)
    const fileId = input.fileId.trim()
    if (!fileId || fileId.length > 512 || /[\u0000\r\n]/u.test(fileId)) throw new TypeError("Diff file identity is invalid.")
    const response = await this.client.endpoint<Record<string, unknown>>(OPERATION_NAMES.taskDiffReviewPage, {
      path: { task_id: task, artifact_id: artifact, file_id: fileId },
      query: {
        revision: input.revision,
        page: Math.max(0, Math.floor(input.page)),
        maximum_bytes: Math.max(1_024, Math.min(8 * 1_024 * 1_024, Math.floor(input.maximumBytes))),
        maximum_lines: Math.max(1, Math.min(100_000, Math.floor(input.maximumLines))),
      },
      binding: { taskId: task, artifactId: artifact },
      signal: input.signal,
      timeoutMs: Math.min(this.timeoutMs, 30_000),
      coordinationKey: `cli.diff.page:${task}:${artifact}:${fileId}:${input.revision}:${input.page}`,
      deduplicate: true,
    })
    return response.data
  }

  async session(sessionId: string): Promise<SessionProjection> {
    const selected = normalizeIdentity("session", sessionId)
    const response = await this.client.endpoint<SessionProjection>(OPERATION_NAMES.sessionGet, {
      path: { session_id: selected },
      timeoutMs: Math.min(this.timeoutMs, 30_000),
      coordinationKey: `cli.session.get:${selected}:${Date.now()}`,
      latestWins: true,
    })
    return response.data
  }

  async resolveTask(identity: string): Promise<{ task: TaskProjection; session?: SessionProjection }> {
    try {
      return { task: await this.task(identity) }
    } catch (error) {
      if (!(error instanceof TypeError) && (!(error instanceof ZyraApiError) || error.category !== "not_found")) throw error
    }
    const session = await this.session(identity)
    if (session.resolution !== "resolved" || !session.resumeTaskId) {
      throw new CliTaskError("Session resolver is ambiguous and cannot select a task.", "session_resolution_ambiguous", {
        session_id: session.sessionId,
        task_count: session.taskCount,
      })
    }
    return { task: await this.task(session.resumeTaskId), session }
  }

  async events(taskId: string): Promise<readonly EventProjection[]> {
    const selected = normalizeIdentity("task", taskId)
    const response = await this.client.endpoint<EventProjection[]>(OPERATION_NAMES.taskEvents, {
      path: { task_id: selected },
      query: { limit: 10_000 },
      binding: { taskId: selected },
      timeoutMs: Math.min(this.timeoutMs, 30_000),
      coordinationKey: `cli.task.events:${selected}:${Date.now()}`,
      latestWins: true,
    })
    return response.data
  }

  async ingressCapabilities(taskId: string, cursor?: string, expectedGeneration?: number): Promise<IngressCapabilities> {
    const selected = normalizeIdentity("task", taskId)
    const capabilitiesResponse = await this.client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskEventIngressCapabilities,
      {
        path: { task_id: selected },
        query: { generation: expectedGeneration, cursor },
        binding: { taskId: selected },
        timeoutMs: Math.min(this.timeoutMs, 30_000),
        coordinationKey: `cli.ingress.capabilities:${selected}`,
        deduplicate: true,
      },
    )
    const capabilities = record(capabilitiesResponse.data, "event ingress capabilities")
    schema(capabilities, CAPABILITIES_SCHEMA, "event ingress capabilities")
    const generation = integerValue(capabilities.generation, "event ingress generation")
    const subscriptionCursor = stringValue(capabilities.subscriptionCursor, "event ingress subscription cursor")
    const subscriptionSequence = integerValue(capabilities.subscriptionSequence, "event ingress subscription sequence")
    const transports = Array.isArray(capabilities.transports) ? capabilities.transports : []
    const sseAvailable = transports.some((entry) => {
      const transport = entry && typeof entry === "object" && !Array.isArray(entry)
        ? entry as Record<string, unknown>
        : {}
      return transport.kind === "sse" && transport.available === true
    })
    if (
      capabilities.protocol !== INGRESS_PROTOCOL
      || capabilities.taskId !== selected
      || capabilities.canonicalOwner !== "typescript.RuntimeEventSpine"
      || capabilities.canonicalWriteAllowed !== false
      || capabilities.snapshotRequired !== true
      || capabilities.subscribeBeforeSnapshot !== true
    ) {
      throw new CliTaskError("Event ingress capabilities violate the canonical read-only contract.", "contract_ingress_capabilities_invalid")
    }
    return { taskId: selected, generation, subscriptionCursor, subscriptionSequence, sseAvailable, raw: capabilities }
  }

  async *snapshotIngress(taskId: string, generation: number): AsyncGenerator<IngressPage> {
    const selected = normalizeIdentity("task", taskId)
    let cursor: string | undefined
    for (let pages = 0; pages < 10_000; pages += 1) {
      const response = await this.client.endpoint<Record<string, unknown>>(
        OPERATION_NAMES.taskEventIngressSnapshot,
        {
          path: { task_id: selected },
          query: { cursor, generation, limit: 500 },
          binding: { taskId: selected },
          timeoutMs: Math.min(this.timeoutMs, 30_000),
          coordinationKey: `cli.ingress.snapshot:${selected}:${cursor ?? "first"}`,
          deduplicate: true,
        },
      )
      const current = parseIngressPage(response.data, selected, SNAPSHOT_SCHEMA)
      yield current
      cursor = current.cursor
      if (!current.hasMore) {
        if (!current.caughtUp) {
          throw new CliTaskError("Event ingress snapshot did not reach a delta cursor.", "contract_snapshot_incomplete")
        }
        return
      }
    }
    throw new CliTaskError("Event ingress snapshot exceeded the bounded page budget.", "contract_snapshot_budget")
  }

  async openIngress(taskId: string): Promise<IngressPage> {
    const capabilities = await this.ingressCapabilities(taskId)
    let combined: IngressPage | undefined
    for await (const page of this.snapshotIngress(capabilities.taskId, capabilities.generation)) {
      combined = combined ? { ...page, frames: [...combined.frames, ...page.frames] } : page
    }
    if (!combined) throw new CliTaskError("Event ingress snapshot returned no page.", "contract_snapshot_incomplete")
    return combined
  }

  async *streamIngress(
    taskId: string,
    cursor: string,
    generation: number,
    signal?: AbortSignal,
  ): AsyncGenerator<IngressStreamMessage> {
    const selected = normalizeIdentity("task", taskId)
    if (!cursor) throw new CliTaskError("Event stream requires a server cursor.", "contract_cursor_missing")
    const handle = await this.client.openEndpointStream(OPERATION_NAMES.taskEventIngressSse, {
      path: { task_id: selected },
      query: { cursor, generation, limit: 500, wait_ms: 750, stream_ms: 5_000, heartbeat_ms: 1_000 },
      binding: { taskId: selected },
      signal,
      timeoutMs: Math.min(this.timeoutMs, 15_000),
      headers: { Accept: "text/event-stream" },
    })
    try {
      for await (const event of readServerSentEvents(handle.response)) {
        let value: Record<string, unknown>
        try {
          value = record(JSON.parse(event.data), `SSE ${event.event} frame`)
        } catch (error) {
          if (error instanceof CliTaskError) throw error
          throw new CliTaskError(`SSE ${event.event} frame contains malformed JSON.`, "contract_stream_json_invalid")
        }
        schema(value, FRAME_SCHEMA, `SSE ${event.event} frame`)
        const kind = stringValue(value.kind, "SSE frame kind")
        const frameTaskId = stringValue(value.taskId, "SSE task id")
        const frameGeneration = integerValue(value.generation, "SSE generation")
        const sequence = integerValue(value.sequence, "SSE sequence")
        if (frameTaskId !== selected || frameGeneration !== generation) {
          throw new CliTaskError("SSE frame binding changed without resynchronization.", "contract_stream_binding_invalid")
        }
        if (event.event !== kind) {
          throw new CliTaskError("SSE named event and payload kind disagree.", "contract_stream_kind_invalid")
        }
        if (kind === "event") {
          yield { kind, taskId: selected, generation, sequence, frame: parseIngressFrame(value, selected, generation) }
        } else if (kind === "ready") {
          yield { kind, taskId: selected, generation, sequence }
        } else if (kind === "heartbeat" || kind === "close") {
          yield { kind, taskId: selected, generation, sequence, cursor: stringValue(value.cursor, "SSE cursor") }
        } else {
          throw new CliTaskError("SSE returned an unknown frame kind.", "contract_stream_kind_unknown")
        }
      }
    } finally {
      handle.close("CLI event stream window complete")
    }
  }

  async nextIngress(
    taskId: string,
    cursor: string,
    generation: number,
    waitMs = 750,
    signal?: AbortSignal,
  ): Promise<IngressPage> {
    const selected = normalizeIdentity("task", taskId)
    const response = await this.client.endpoint<Record<string, unknown>>(
      OPERATION_NAMES.taskEventIngressDelta,
      {
        path: { task_id: selected },
        query: { cursor, generation, limit: 500, wait_ms: waitMs },
        binding: { taskId: selected },
        signal,
        timeoutMs: Math.min(this.timeoutMs, Math.max(10_000, waitMs + 5_000)),
        coordinationKey: `cli.ingress.delta:${selected}:${cursor}`,
        latestWins: true,
      },
    )
    return parseIngressPage(response.data, selected, DELTA_SCHEMA)
  }

  async scenarioRegistry(): Promise<Record<string, unknown>> {
    const response = await this.client.endpoint<Record<string, unknown>>(OPERATION_NAMES.scenarioRegistry)
    const result = record(response.data, "scenario registry")
    schema(result, "zyra.scenario-registry/v1", "scenario registry")
    return result
  }

  async scenarioList(input: { includeArchived: boolean; limit: number; offset: number }): Promise<Record<string, unknown>> {
    const response = await this.client.endpoint<Record<string, unknown>>(OPERATION_NAMES.scenarioRunList, {
      query: {
        include_archived: input.includeArchived ? "true" : "false",
        limit: input.limit,
        offset: input.offset,
      },
      coordinationKey: `cli.scenario.list:${input.includeArchived}:${input.limit}:${input.offset}`,
      latestWins: true,
    })
    const result = record(response.data, "scenario list")
    schema(result, "zyra.scenario-run-page/v1", "scenario list")
    return result
  }

  async scenarioCreate(input: {
    scenarioId?: string
    definitionVersion?: string
    profileId?: string
    policyId?: string
    policyDigest?: string
    mode: "sealed" | "interactive"
    value: string
    seed: number
    labels: Readonly<Record<string, string>>
    preflight?: readonly Readonly<Record<string, unknown>>[]
  }): Promise<ScenarioRun> {
    const scenarioId = input.scenarioId || "foundation.short-owner-chain"
    const body = {
      scenario_id: scenarioId,
      definition_version: input.definitionVersion,
      profile_id: input.profileId || (scenarioId.startsWith("live.") ? "live.heterogeneous-sealed" : "foundation.local-sealed"),
      policy_id: input.policyId || "sealed-autonomous-foundation",
      policy_digest: input.policyDigest,
      mode: input.mode,
      input: input.value,
      seed: input.seed,
      labels: { ...input.labels },
      preflight: input.preflight,
    }
    const response = await this.client.endpoint<Record<string, unknown>, typeof body>(OPERATION_NAMES.scenarioRunCreate, {
      body,
      idempotencyKey: createIdempotencyKey("scenario-create", {}, body),
      timeoutMs: this.timeoutMs,
      coordinationKey: `cli.scenario.create:${scenarioId}:${input.mode}:${input.seed}`,
    })
    const result = record(response.data, "scenario create response")
    if (result.schema === "zyra.scenario-error/v1") scenarioError(result)
    schema(result, "zyra.scenario-create-response/v1", "scenario create response")
    return parseScenarioRun(result.run)
  }

  async scenarioMutate(
    operation: typeof OPERATION_NAMES.scenarioRunStart | typeof OPERATION_NAMES.scenarioRunCancel,
    runId: string,
    body: Readonly<Record<string, unknown>>,
  ): Promise<ScenarioRun> {
    const response = await this.client.endpoint<Record<string, unknown>>(operation, {
      path: { scenario_run_id: runId },
      body,
      idempotencyKey: createIdempotencyKey(operation.replaceAll(".", "-"), {}, { scenario_run_id: runId, ...body }),
      timeoutMs: this.timeoutMs,
      coordinationKey: `cli.${operation}:${runId}`,
    })
    const result = record(response.data, "scenario mutation response")
    if (result.schema === "zyra.scenario-error/v1") scenarioError(result)
    const expected = operation === OPERATION_NAMES.scenarioRunStart
      ? "zyra.scenario-start-response/v1"
      : "zyra.scenario-cancel-response/v1"
    schema(result, expected, "scenario mutation response")
    return parseScenarioRun(result.run)
  }

  async scenarioVerify(runId: string): Promise<Record<string, unknown>> {
    const body = {}
    const response = await this.client.endpoint<Record<string, unknown>>(OPERATION_NAMES.scenarioRunVerify, {
      path: { scenario_run_id: runId },
      body,
      idempotencyKey: createIdempotencyKey("scenario-verify", {}, { scenario_run_id: runId }),
      timeoutMs: this.timeoutMs,
      coordinationKey: `cli.scenario.verify:${runId}`,
    })
    const result = record(response.data, "scenario verify response")
    if (result.schema === "zyra.scenario-error/v1") scenarioError(result)
    schema(result, "zyra.scenario-verify-response/v1", "scenario verify response")
    const receipt = record(result.verification_receipt, "scenario verification receipt")
    schema(receipt, "zyra.scenario-evidence-verification/v1", "scenario verification receipt")
    return result
  }

  async scenarioEvidence(runId: string): Promise<Record<string, unknown>> {
    const response = await this.client.endpoint<Record<string, unknown>>(OPERATION_NAMES.scenarioRunEvidence, {
      path: { scenario_run_id: runId },
      timeoutMs: this.timeoutMs,
      coordinationKey: `cli.scenario.evidence:${runId}`,
      latestWins: true,
    })
    const result = record(response.data, "scenario evidence response")
    if (result.schema === "zyra.scenario-error/v1") scenarioError(result)
    schema(result, "zyra.scenario-evidence-response/v1", "scenario evidence response")
    if (!result.evidence_manifest || !result.verification_receipt) {
      throw new CliVerifierError("Scenario evidence response is incomplete.")
    }
    return result
  }
}
