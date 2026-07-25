import type {
  McpElicitationProjection,
  McpServerProjection,
} from "./contracts.ts"
import {
  fingerprint,
  object,
  redactSecrets,
  safeDescription,
  scalar,
  stable,
} from "./value.ts"

export interface McpElicitationAnswer {
  requestId: string
  serverId: string
  ownerId: string
  requestRevision: number
  schemaDigest: string
  response: Readonly<Record<string, unknown>>
  responseDigest: string
  redactedResponse: Readonly<Record<string, unknown>>
  sensitiveFields: readonly string[]
  createdAt: string
}

export interface McpElicitationAdmission {
  allowed: boolean
  code:
    | "allow"
    | "sealed"
    | "server-disabled"
    | "server-disconnected"
    | "request-not-pending"
    | "request-expired"
    | "request-owner-mismatch"
    | "request-scope-mismatch"
    | "request-revision-stale"
    | "unknown-field"
    | "required-field-missing"
    | "value-invalid"
    | "response-too-large"
  reason: string
  answer?: McpElicitationAnswer
  field?: string
}

export function admitElicitationResponse(input: {
  server: McpServerProjection
  request: McpElicitationProjection
  response: Readonly<Record<string, unknown>>
  taskId: string
  runId: string
  sessionId: string
  sealed: boolean
  now?: Date
}): McpElicitationAdmission {
  const deny = (
    code: Exclude<McpElicitationAdmission["code"], "allow">,
    reason: string,
    field?: string,
  ): McpElicitationAdmission => Object.freeze({
    allowed: false,
    code,
    reason,
    field,
  })
  if (input.sealed) {
    return deny("sealed", "Human MCP elicitation is forbidden in sealed mode.")
  }
  if (!input.server.enabled || input.server.state === "disabled") {
    return deny(
      "server-disabled",
      input.server.disabledReason ?? "MCP server is disabled.",
    )
  }
  if (!["connected", "needs-auth"].includes(input.server.state)) {
    return deny(
      "server-disconnected",
      "MCP elicitation requires a live owner connection.",
    )
  }
  if (!["pending", "permission-pending"].includes(input.request.state)) {
    return deny(
      "request-not-pending",
      `MCP elicitation is already ${input.request.state}.`,
    )
  }
  const now = input.now ?? new Date()
  if (
    input.request.expiresAt &&
    Date.parse(input.request.expiresAt) <= now.getTime()
  ) {
    return deny("request-expired", "MCP elicitation request has expired.")
  }
  if (
    input.request.ownerId !== input.server.ownerId ||
    input.request.serverId !== input.server.id
  ) {
    return deny(
      "request-owner-mismatch",
      "MCP elicitation request owner or server changed.",
    )
  }
  if (
    input.request.taskId !== input.taskId ||
    input.request.runId !== input.runId ||
    input.request.sessionId !== input.sessionId
  ) {
    return deny(
      "request-scope-mismatch",
      "MCP elicitation request belongs to another task, run, or session.",
    )
  }
  const current = input.server.elicitations.find(
    (candidate) => candidate.id === input.request.id,
  )
  if (
    !current ||
    current.requestRevision !== input.request.requestRevision ||
    current.schemaDigest !== input.request.schemaDigest
  ) {
    return deny(
      "request-revision-stale",
      "MCP elicitation schema or revision changed.",
    )
  }
  const response = object(input.response)
  const fieldNames = new Set(input.request.fieldNames)
  for (const field of Object.keys(response)) {
    if (!fieldNames.has(field)) {
      return deny(
        "unknown-field",
        `MCP elicitation response contains unknown field ${field}.`,
        field,
      )
    }
  }
  for (const field of input.request.requiredFields) {
    if (!(field in response) || response[field] === undefined || response[field] === null) {
      return deny(
        "required-field-missing",
        `MCP elicitation response is missing required field ${field}.`,
        field,
      )
    }
  }
  const normalized: Record<string, unknown> = {}
  for (const field of input.request.fieldNames) {
    if (!(field in response)) continue
    const validation = normalizeField(response[field], field)
    if (!validation.valid) {
      return deny("value-invalid", validation.reason, field)
    }
    normalized[field] = validation.value
  }
  const serialized = stable(normalized)
  if (new TextEncoder().encode(serialized).byteLength > 64 * 1024) {
    return deny(
      "response-too-large",
      "MCP elicitation response exceeds 64 KiB.",
    )
  }
  const sensitive = new Set(input.request.sensitiveFields)
  const redacted: Record<string, unknown> = {}
  for (const [field, value] of Object.entries(normalized)) {
    redacted[field] = sensitive.has(field)
      ? value === "" || value === null
        ? "[absent]"
        : "[present]"
      : redactSecrets(value)
  }
  const answer: McpElicitationAnswer = Object.freeze({
    requestId: input.request.id,
    serverId: input.server.id,
    ownerId: input.server.ownerId,
    requestRevision: input.request.requestRevision,
    schemaDigest: input.request.schemaDigest,
    response: Object.freeze(normalized),
    responseDigest: fingerprint([
      input.request.id,
      input.request.requestRevision,
      input.request.schemaDigest,
      normalized,
    ]),
    redactedResponse: Object.freeze(redacted),
    sensitiveFields: Object.freeze([...sensitive].sort()),
    createdAt: now.toISOString(),
  })
  return Object.freeze({
    allowed: true,
    code: "allow",
    reason: "MCP elicitation response may be handed to permission runtime.",
    answer,
  })
}

export function elicitationCommandPayload(
  answer: McpElicitationAnswer,
): {
  raw: string
  display: string
  digest: string
} {
  const rawPayload = {
    request_id: answer.requestId,
    server_id: answer.serverId,
    owner_id: answer.ownerId,
    request_revision: answer.requestRevision,
    schema_digest: answer.schemaDigest,
    response: answer.response,
    response_digest: answer.responseDigest,
  }
  const displayPayload = {
    request_id: answer.requestId,
    server_id: answer.serverId,
    owner_id: answer.ownerId,
    request_revision: answer.requestRevision,
    schema_digest: answer.schemaDigest,
    response: answer.redactedResponse,
    response_digest: answer.responseDigest,
  }
  return Object.freeze({
    raw: JSON.stringify(rawPayload),
    display: JSON.stringify(displayPayload),
    digest: answer.responseDigest,
  })
}

export class McpElicitationDrafts {
  readonly #drafts = new Map<string, Readonly<Record<string, unknown>>>()
  #enabled = true
  #disabledReason = "MCP elicitation drafts are disabled."

  update(
    request: McpElicitationProjection,
    field: string,
    value: unknown,
  ): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    if (!request.fieldNames.includes(field)) {
      throw new TypeError("MCP elicitation field is not declared by canonical schema.")
    }
    const normalized = normalizeField(value, field)
    if (!normalized.valid) throw new TypeError(normalized.reason)
    const existing = this.#drafts.get(request.id) ?? Object.freeze({})
    const next = Object.freeze({
      ...existing,
      [field]: normalized.value,
    })
    this.#drafts.set(request.id, next)
    return redactDraft(request, next)
  }

  remove(
    request: McpElicitationProjection,
    field: string,
  ): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    const current = { ...(this.#drafts.get(request.id) ?? {}) }
    delete current[field]
    const next = Object.freeze(current)
    this.#drafts.set(request.id, next)
    return redactDraft(request, next)
  }

  response(requestId: string): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    return this.#drafts.get(requestId) ?? Object.freeze({})
  }

  display(request: McpElicitationProjection): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    return redactDraft(request, this.#drafts.get(request.id) ?? {})
  }

  clear(requestId?: string): void {
    this.#assertEnabled()
    if (requestId) this.#drafts.delete(requestId)
    else this.#drafts.clear()
  }

  disable(reason = "MCP elicitation drafts are disabled."): void {
    this.#drafts.clear()
    this.#enabled = false
    this.#disabledReason = safeDescription(reason, "MCP elicitation drafts are disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function normalizeField(
  value: unknown,
  field: string,
):
  | { valid: true; value: unknown }
  | { valid: false; reason: string } {
  if (value === null) return { valid: true, value: null }
  if (typeof value === "string") {
    if (new TextEncoder().encode(value).byteLength > 32 * 1024) {
      return {
        valid: false,
        reason: `MCP elicitation field ${field} exceeds 32 KiB.`,
      }
    }
    if (/[\u0000]/.test(value)) {
      return {
        valid: false,
        reason: `MCP elicitation field ${field} contains a null byte.`,
      }
    }
    return { valid: true, value }
  }
  if (typeof value === "number") {
    return Number.isFinite(value)
      ? { valid: true, value }
      : {
          valid: false,
          reason: `MCP elicitation field ${field} is not finite.`,
        }
  }
  if (typeof value === "boolean") return { valid: true, value }
  if (Array.isArray(value)) {
    if (value.length > 1000) {
      return {
        valid: false,
        reason: `MCP elicitation field ${field} has too many values.`,
      }
    }
    const normalized: unknown[] = []
    for (let index = 0; index < value.length; index += 1) {
      const child = normalizeField(value[index], `${field}[${index}]`)
      if (!child.valid) return child
      normalized.push(child.value)
    }
    return { valid: true, value: Object.freeze(normalized) }
  }
  if (value && typeof value === "object") {
    const source = object(value)
    if (Object.keys(source).length > 256) {
      return {
        valid: false,
        reason: `MCP elicitation field ${field} has too many properties.`,
      }
    }
    const normalized: Record<string, unknown> = {}
    for (const [key, item] of Object.entries(source)) {
      if (!key || key.length > 256 || /[\u0000-\u001f]/.test(key)) {
        return {
          valid: false,
          reason: `MCP elicitation field ${field} contains an invalid property.`,
        }
      }
      const child = normalizeField(item, `${field}.${key}`)
      if (!child.valid) return child
      normalized[key] = child.value
    }
    return { valid: true, value: Object.freeze(normalized) }
  }
  return {
    valid: false,
    reason: `MCP elicitation field ${field} has an unsupported value.`,
  }
}

function redactDraft(
  request: McpElicitationProjection,
  draft: Readonly<Record<string, unknown>>,
): Readonly<Record<string, unknown>> {
  const sensitive = new Set(request.sensitiveFields)
  const output: Record<string, unknown> = {}
  for (const [field, value] of Object.entries(draft)) {
    output[field] = sensitive.has(field)
      ? scalar(value) || value
        ? "[present]"
        : "[absent]"
      : redactSecrets(value)
  }
  return Object.freeze(output)
}
