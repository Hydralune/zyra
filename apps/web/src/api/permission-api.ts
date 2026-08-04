import {
  OPERATION_NAMES,
  createIdempotencyKey,
  normalizeIdempotencyKey,
  type PermissionControlProjection,
} from "../../../../packages/core/typed-api-client/src/index.ts"
import type { ZyraApiClient } from "./client.ts"

export interface PermissionSessionBinding {
  taskId: string
  runId: string
  sessionId: string
}

export interface PermissionSessionClaim extends PermissionSessionBinding {
  custodyToken: string
  custodyId?: string
  custodyFingerprint?: string
  created: boolean
  verified: boolean
}

export interface PermissionQueryOptions {
  status?: string
  pendingOnly?: boolean
  limit?: number
  signal?: AbortSignal
  timeoutMs?: number
}

export interface PermissionResolveInput {
  requestId: string
  responseId: string
  effect: "allow" | "deny"
  consoleResponse: Readonly<Record<string, unknown>>
  displayResponder: string
  feedback?: string
  signal?: AbortSignal
  timeoutMs?: number
}

export class PermissionApiError extends Error {
  readonly code: string
  readonly operation: string
  readonly status: number
  readonly detail: Readonly<Record<string, unknown>>

  constructor(
    code: string,
    message: string,
    options: {
      operation: string
      status: number
      detail?: Readonly<Record<string, unknown>>
    },
  ) {
    super(message)
    this.name = "PermissionApiError"
    this.code = code
    this.operation = options.operation
    this.status = options.status
    this.detail = Object.freeze({ ...(options.detail ?? {}) })
  }
}

export class PermissionApi {
  readonly #client: ZyraApiClient
  readonly #claims = new Map<string, PermissionSessionClaim>()
  #closed = false

  constructor(client: ZyraApiClient) {
    this.#client = client
  }

  claim(binding: PermissionSessionBinding): PermissionSessionClaim | undefined {
    const normalized = normalizeBinding(binding)
    const found = this.#claims.get(bindingKey(normalized))
    return found ? freezeClaim(found) : undefined
  }

  adoptCustody(
    bindingValue: PermissionSessionBinding,
    custodyToken: string,
    metadata: {
      custodyId?: string
      custodyFingerprint?: string
      created?: boolean
    } = {},
  ): PermissionSessionClaim {
    this.#assertOpen()
    const binding = normalizeBinding(bindingValue)
    const token = sensitiveToken(custodyToken)
    const claim = freezeClaim({
      ...binding,
      custodyToken: token,
      custodyId: boundedOptional(metadata.custodyId, 512),
      custodyFingerprint: boundedOptional(metadata.custodyFingerprint, 512),
      created: metadata.created === true,
      verified: true,
    })
    this.#claims.set(bindingKey(binding), claim)
    return freezeClaim(claim)
  }

  async openSession(
    bindingValue: PermissionSessionBinding,
    options: {
      externalSessionExists?: boolean
      custodyToken?: string
      signal?: AbortSignal
      timeoutMs?: number
    } = {},
  ): Promise<PermissionSessionClaim> {
    this.#assertOpen()
    const binding = normalizeBinding(bindingValue)
    if (options.custodyToken) this.adoptCustody(binding, options.custodyToken)
    const prior = this.#claims.get(bindingKey(binding))
    if (prior) {
      await this.resumeSession(binding, {
        signal: options.signal,
        timeoutMs: options.timeoutMs,
      })
      return freezeClaim(prior)
    }
    const body = {
      session_id: binding.sessionId,
      run_id: binding.runId,
      task_id: binding.taskId,
      external_session_exists: options.externalSessionExists === true,
    }
    const response = await this.#client.endpoint<
      PermissionControlProjection,
      typeof body
    >(OPERATION_NAMES.permissionSessionOpen, {
      body,
      binding: {
        taskId: binding.taskId,
        runId: binding.runId,
      },
      idempotencyKey: createIdempotencyKey(
        OPERATION_NAMES.permissionSessionOpen,
        { taskId: binding.taskId, runId: binding.runId },
        body,
      ),
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      coordinationKey: `permission.session.open:${bindingKey(binding)}`,
      deduplicate: true,
    })
    const projection = requireOk(response.data, response.raw.status)
    const session = record(projection.raw.session, "permission session")
    const token = sensitiveToken(session.bearer_token ?? session.custody_token)
    const claim = freezeClaim({
      ...binding,
      custodyToken: token,
      custodyId: boundedOptional(session.custody_id, 512),
      custodyFingerprint: boundedOptional(session.custody_fingerprint, 512),
      created: session.created === true,
      verified: session.verified !== false && session.custody_verified !== false,
    })
    this.#claims.set(bindingKey(binding), claim)
    return freezeClaim(claim)
  }

  async resumeSession(
    bindingValue: PermissionSessionBinding,
    options: {
      signal?: AbortSignal
      timeoutMs?: number
    } = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    this.#assertOpen()
    const binding = normalizeBinding(bindingValue)
    const claim = this.#requireClaim(binding)
    const body = {
      session_id: binding.sessionId,
      run_id: binding.runId,
      task_id: binding.taskId,
    }
    const response = await this.#client.endpoint<
      PermissionControlProjection,
      typeof body
    >(OPERATION_NAMES.permissionSessionResume, {
      path: { session_id: binding.sessionId },
      body,
      headers: custodyHeaders(claim.custodyToken),
      binding: {
        taskId: binding.taskId,
        runId: binding.runId,
      },
      idempotencyKey: createIdempotencyKey(
        OPERATION_NAMES.permissionSessionResume,
        { taskId: binding.taskId, runId: binding.runId },
        body,
      ),
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      coordinationKey: `permission.session.resume:${bindingKey(binding)}`,
      latestWins: true,
    })
    return freezeRaw(requireOk(response.data, response.raw.status).raw)
  }

  async summary(
    bindingValue: PermissionSessionBinding,
    options: PermissionQueryOptions = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    return this.#query(
      OPERATION_NAMES.permissionSummary,
      bindingValue,
      {
        limit: boundedLimit(options.limit),
      },
      options,
    )
  }

  async requests(
    bindingValue: PermissionSessionBinding,
    options: PermissionQueryOptions = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    return this.#query(
      OPERATION_NAMES.permissionRequests,
      bindingValue,
      {
        status: boundedOptional(options.status, 96),
        pending_only: options.pendingOnly,
        limit: boundedLimit(options.limit),
      },
      options,
    )
  }

  async request(
    bindingValue: PermissionSessionBinding,
    requestIdValue: string,
    options: Pick<PermissionQueryOptions, "signal" | "timeoutMs"> = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    this.#assertOpen()
    const binding = normalizeBinding(bindingValue)
    const requestId = identifier(requestIdValue, "request_id")
    const claim = this.#requireClaim(binding)
    const response = await this.#client.endpoint<PermissionControlProjection>(
      OPERATION_NAMES.permissionRequestGet,
      {
        path: { request_id: requestId },
        query: identityQuery(binding),
        headers: custodyHeaders(claim.custodyToken),
        binding: { taskId: binding.taskId, runId: binding.runId },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `permission.request.get:${bindingKey(binding)}:${requestId}`,
        latestWins: true,
      },
    )
    return freezeRaw(requireOk(response.data, response.raw.status).raw)
  }

  async resolve(
    bindingValue: PermissionSessionBinding,
    input: PermissionResolveInput,
  ): Promise<Readonly<Record<string, unknown>>> {
    this.#assertOpen()
    const binding = normalizeBinding(bindingValue)
    const claim = this.#requireClaim(binding)
    const requestId = identifier(input.requestId, "request_id")
    const responseId = identifier(input.responseId, "response_id")
    const effect = input.effect
    if (effect !== "allow" && effect !== "deny") {
      throw new TypeError("Permission response effect must be allow or deny.")
    }
    const displayResponder = boundedRequired(
      input.displayResponder,
      256,
      "display responder",
    )
    const body = {
      ...identityQuery(binding),
      effect,
      response_id: responseId,
      idempotency_key: normalizeIdempotencyKey(responseId),
      console_response: jsonRecord(input.consoleResponse),
      display_responder: displayResponder,
      feedback: boundedOptional(input.feedback, 4_096),
      require_identity_echo: true,
    }
    const response = await this.#client.endpoint<
      PermissionControlProjection,
      typeof body
    >(OPERATION_NAMES.permissionRequestResolve, {
      path: { request_id: requestId },
      body,
      headers: custodyHeaders(claim.custodyToken),
      binding: { taskId: binding.taskId, runId: binding.runId },
      idempotencyKey: body.idempotency_key,
      signal: input.signal,
      timeoutMs: input.timeoutMs,
      coordinationKey: `permission.request.resolve:${bindingKey(binding)}:${requestId}`,
      deduplicate: true,
    })
    return freezeRaw(requireOk(response.data, response.raw.status).raw)
  }

  async expire(
    bindingValue: PermissionSessionBinding,
    options: Pick<PermissionQueryOptions, "signal" | "timeoutMs"> = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    this.#assertOpen()
    const binding = normalizeBinding(bindingValue)
    const claim = this.#requireClaim(binding)
    const body = identityQuery(binding)
    const idempotencyKey = createIdempotencyKey(
      OPERATION_NAMES.permissionRequestsExpire,
      { taskId: binding.taskId, runId: binding.runId },
      body,
    )
    const response = await this.#client.endpoint<
      PermissionControlProjection,
      typeof body
    >(OPERATION_NAMES.permissionRequestsExpire, {
      body,
      headers: custodyHeaders(claim.custodyToken),
      binding: { taskId: binding.taskId, runId: binding.runId },
      idempotencyKey,
      signal: options.signal,
      timeoutMs: options.timeoutMs,
      coordinationKey: `permission.requests.expire:${bindingKey(binding)}:${idempotencyKey}`,
      deduplicate: true,
    })
    return freezeRaw(requireOk(response.data, response.raw.status).raw)
  }

  async rules(
    bindingValue: PermissionSessionBinding,
    options: Pick<PermissionQueryOptions, "signal" | "timeoutMs"> = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    return this.#query(
      OPERATION_NAMES.permissionRules,
      bindingValue,
      {},
      options,
    )
  }

  async mode(
    bindingValue: PermissionSessionBinding,
    options: Pick<PermissionQueryOptions, "signal" | "timeoutMs"> = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    return this.#query(
      OPERATION_NAMES.permissionMode,
      bindingValue,
      {},
      options,
    )
  }

  async decisions(
    bindingValue: PermissionSessionBinding,
    options: PermissionQueryOptions = {},
  ): Promise<Readonly<Record<string, unknown>>> {
    return this.#query(
      OPERATION_NAMES.permissionDecisions,
      bindingValue,
      { limit: boundedLimit(options.limit) },
      options,
    )
  }

  forget(bindingValue: PermissionSessionBinding): boolean {
    const binding = normalizeBinding(bindingValue)
    return this.#claims.delete(bindingKey(binding))
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.#claims.clear()
  }

  async #query(
    operation: string,
    bindingValue: PermissionSessionBinding,
    extraQuery: Record<string, string | number | boolean | undefined>,
    options: Pick<PermissionQueryOptions, "signal" | "timeoutMs">,
  ): Promise<Readonly<Record<string, unknown>>> {
    this.#assertOpen()
    const binding = normalizeBinding(bindingValue)
    const claim = this.#requireClaim(binding)
    const response = await this.#client.endpoint<PermissionControlProjection>(
      operation,
      {
        query: { ...identityQuery(binding), ...extraQuery },
        headers: custodyHeaders(claim.custodyToken),
        binding: { taskId: binding.taskId, runId: binding.runId },
        signal: options.signal,
        timeoutMs: options.timeoutMs,
        coordinationKey: `${operation}:${bindingKey(binding)}`,
        latestWins: true,
      },
    )
    return freezeRaw(requireOk(response.data, response.raw.status).raw)
  }

  #requireClaim(binding: PermissionSessionBinding): PermissionSessionClaim {
    const found = this.#claims.get(bindingKey(binding))
    if (!found) {
      throw new PermissionApiError(
        "permission_custody_missing",
        "Permission session custody is not available in this browser process.",
        {
          operation: "permission.custody",
          status: 401,
          detail: {
            task_id: binding.taskId,
            run_id: binding.runId,
            session_id: binding.sessionId,
          },
        },
      )
    }
    return found
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Permission API is closed.")
  }
}

function requireOk(
  projection: PermissionControlProjection,
  status: number,
): PermissionControlProjection {
  if (projection.ok && status >= 200 && status < 300) return projection
  throw new PermissionApiError(
    projection.error ?? "permission_request_failed",
    projection.message ?? `Permission operation ${projection.operation} failed.`,
    {
      operation: projection.operation,
      status,
      detail: projection.raw,
    },
  )
}

function normalizeBinding(
  value: PermissionSessionBinding,
): PermissionSessionBinding {
  return Object.freeze({
    taskId: identifier(value.taskId, "task_id"),
    runId: identifier(value.runId, "run_id"),
    sessionId: identifier(value.sessionId, "session_id"),
  })
}

function identityQuery(binding: PermissionSessionBinding) {
  return {
    task_id: binding.taskId,
    run_id: binding.runId,
    session_id: binding.sessionId,
  }
}

function bindingKey(binding: PermissionSessionBinding): string {
  return `${binding.taskId}\u0000${binding.runId}\u0000${binding.sessionId}`
}

function custodyHeaders(token: string): Headers {
  const headers = new Headers()
  headers.set("Authorization", `Bearer ${sensitiveToken(token)}`)
  headers.set("Cache-Control", "no-store")
  return headers
}

function sensitiveToken(value: unknown): string {
  const token = boundedRequired(value, 8_192, "permission custody token")
    .replace(/^Bearer\s+/i, "")
    .trim()
  if (!token) throw new TypeError("Permission custody token must not be empty.")
  return token
}

function identifier(value: unknown, label: string): string {
  const rendered = boundedRequired(value, 512, label)
  if (
    rendered === "."
    || rendered === ".."
    || !/^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$/.test(rendered)
  ) {
    throw new TypeError(`${label} is invalid.`)
  }
  return rendered
}

function boundedRequired(
  value: unknown,
  maximumBytes: number,
  label: string,
): string {
  const rendered = typeof value === "string" ? value.trim() : ""
  if (
    !rendered
    || /[\u0000\r\n]/.test(rendered)
    || new TextEncoder().encode(rendered).byteLength > maximumBytes
  ) {
    throw new TypeError(`${label} must be a bounded non-empty string.`)
  }
  return rendered
}

function boundedOptional(
  value: unknown,
  maximumBytes: number,
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return boundedRequired(value, maximumBytes, "permission field")
}

function boundedLimit(value: number | undefined): number {
  if (value === undefined) return 200
  if (!Number.isSafeInteger(value)) {
    throw new TypeError("Permission query limit must be a safe integer.")
  }
  return Math.min(1_000, Math.max(1, value))
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object.`)
  }
  return value as Record<string, unknown>
}

function jsonRecord(
  value: Readonly<Record<string, unknown>>,
): Record<string, unknown> {
  return JSON.parse(JSON.stringify(value)) as Record<string, unknown>
}

function freezeRaw(
  value: Readonly<Record<string, unknown>>,
): Readonly<Record<string, unknown>> {
  return Object.freeze({ ...value })
}

function freezeClaim(value: PermissionSessionClaim): PermissionSessionClaim {
  return Object.freeze({ ...value })
}
