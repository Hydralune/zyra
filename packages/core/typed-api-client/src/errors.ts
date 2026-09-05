import {
  ABORT_ERROR_NAMES,
  AUTH_HTTP_STATUSES,
  DISCONNECT_ERROR_NAMES,
  ERROR_CODES,
  MAX_ERROR_BODY_BYTES,
  RETRYABLE_HTTP_STATUSES,
  RETRYABLE_NETWORK_MARKERS,
  VERSION_HTTP_STATUSES,
  boundedString,
} from "./constants.ts"
import type { IdentityBinding } from "./identifiers.ts"

export type ZyraErrorCode = (typeof ERROR_CODES)[keyof typeof ERROR_CODES] | string
export type ErrorCategory =
  | "configuration"
  | "validation"
  | "authentication"
  | "authorization"
  | "version"
  | "timeout"
  | "cancellation"
  | "disconnect"
  | "protocol"
  | "receipt"
  | "cursor"
  | "conflict"
  | "not_found"
  | "server"
  | "unknown"

export interface ErrorContext {
  operation?: string
  method?: string
  url?: string
  requestId?: string
  correlationId?: string
  attempt?: number
  status?: number
  binding?: IdentityBinding
}

export interface SerializedZyraError {
  name: string
  code: string
  message: string
  category: ErrorCategory
  retryable: boolean
  status?: number
  details: Record<string, unknown>
  context: ErrorContext
  cause?: {
    name: string
    message: string
  }
}

export interface ServerErrorEnvelope {
  error?: unknown
  code?: unknown
  message?: unknown
  detail?: unknown
  details?: unknown
  retryable?: unknown
  request_id?: unknown
  requestId?: unknown
  receipt_id?: unknown
  receiptId?: unknown
  supported_version?: unknown
  supported_versions?: unknown
  expected_version?: unknown
  actual_version?: unknown
  fallback?: unknown
}

function cleanDetails(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  const result: Record<string, unknown> = {}
  for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
    if (entry === undefined) continue
    if (/token|secret|password|authorization|cookie|api.?key/i.test(key)) {
      result[key] = "[redacted]"
      continue
    }
    result[key] = entry
  }
  return result
}

function cleanContext(value: ErrorContext | undefined): ErrorContext {
  if (!value) return {}
  const result: ErrorContext = {}
  if (value.operation) result.operation = value.operation
  if (value.method) result.method = value.method
  if (value.url) {
    try {
      const parsed = new URL(value.url, "http://zyra.invalid")
      parsed.username = ""
      parsed.password = ""
      for (const key of [...parsed.searchParams.keys()]) {
        if (/token|secret|password|authorization|cookie|api.?key/i.test(key)) {
          parsed.searchParams.set(key, "[redacted]")
        }
      }
      result.url = parsed.origin === "http://zyra.invalid" ? `${parsed.pathname}${parsed.search}` : parsed.toString()
    } catch {
      result.url = "[invalid-url]"
    }
  }
  if (value.requestId) result.requestId = value.requestId
  if (value.correlationId) result.correlationId = value.correlationId
  if (value.attempt !== undefined) result.attempt = value.attempt
  if (value.status !== undefined) result.status = value.status
  if (value.binding) result.binding = { ...value.binding }
  return result
}

function causeSummary(cause: unknown): { name: string; message: string } | undefined {
  if (!cause) return undefined
  if (cause instanceof Error) {
    return { name: cause.name || "Error", message: cause.message || String(cause) }
  }
  return { name: typeof cause, message: String(cause) }
}

export class ZyraApiError extends Error {
  readonly code: ZyraErrorCode
  readonly category: ErrorCategory
  readonly retryable: boolean
  readonly status?: number
  readonly details: Record<string, unknown>
  readonly context: ErrorContext
  override readonly cause?: unknown

  constructor(
    code: ZyraErrorCode,
    message: string,
    options: {
      category?: ErrorCategory
      retryable?: boolean
      status?: number
      details?: Record<string, unknown>
      context?: ErrorContext
      cause?: unknown
    } = {},
  ) {
    super(message)
    this.name = "ZyraApiError"
    this.code = code
    this.category = options.category ?? "unknown"
    this.retryable = options.retryable ?? false
    this.status = options.status
    this.details = cleanDetails(options.details)
    this.context = cleanContext(options.context)
    this.cause = options.cause
  }

  withContext(context: ErrorContext): ZyraApiError {
    Object.assign(this.context, cleanContext(context))
    return this
  }

  withDetails(details: Record<string, unknown>): ZyraApiError {
    Object.assign(this.details, cleanDetails(details))
    return this
  }

  serialize(): SerializedZyraError {
    const serialized: SerializedZyraError = {
      name: this.name,
      code: this.code,
      message: this.message,
      category: this.category,
      retryable: this.retryable,
      details: cleanDetails(this.details),
      context: cleanContext(this.context),
    }
    if (this.status !== undefined) serialized.status = this.status
    const cause = causeSummary(this.cause)
    if (cause) serialized.cause = cause
    return serialized
  }

  toJSON(): SerializedZyraError {
    return this.serialize()
  }
}

export class TransportDisabledError extends ZyraApiError {
  constructor(context: ErrorContext = {}) {
    super(ERROR_CODES.transportDisabled, "The Zyra API transport registry is disabled.", {
      category: "configuration",
      retryable: false,
      context,
    })
    this.name = "TransportDisabledError"
  }
}

export class NormalizerDisabledError extends ZyraApiError {
  constructor(contract: string, context: ErrorContext = {}) {
    super(ERROR_CODES.normalizerDisabled, `Response normalization is disabled for ${contract}.`, {
      category: "configuration",
      retryable: false,
      details: { contract },
      context,
    })
    this.name = "NormalizerDisabledError"
  }
}

export class RequestValidationError extends ZyraApiError {
  constructor(message: string, details: Record<string, unknown> = {}, context: ErrorContext = {}) {
    super(ERROR_CODES.invalidRequest, message, {
      category: "validation",
      retryable: false,
      details,
      context,
    })
    this.name = "RequestValidationError"
  }
}

export class ResponseValidationError extends ZyraApiError {
  constructor(
    message: string,
    details: Record<string, unknown> = {},
    context: ErrorContext = {},
    cause?: unknown,
  ) {
    super(ERROR_CODES.invalidResponse, message, {
      category: "protocol",
      retryable: false,
      details,
      context,
      cause,
    })
    this.name = "ResponseValidationError"
  }
}

export class MalformedJsonError extends ZyraApiError {
  constructor(message: string, context: ErrorContext = {}, cause?: unknown) {
    super(ERROR_CODES.malformedJson, message, {
      category: "protocol",
      retryable: false,
      context,
      cause,
    })
    this.name = "MalformedJsonError"
  }
}

export class RequestTimeoutError extends ZyraApiError {
  readonly timeoutMs: number

  constructor(timeoutMs: number, context: ErrorContext = {}, cause?: unknown) {
    super(ERROR_CODES.timeout, `Zyra API request exceeded its ${timeoutMs} ms deadline.`, {
      category: "timeout",
      retryable: true,
      details: { timeout_ms: timeoutMs },
      context,
      cause,
    })
    this.name = "RequestTimeoutError"
    this.timeoutMs = timeoutMs
  }
}

export class RequestCancelledError extends ZyraApiError {
  readonly reason?: unknown

  constructor(reason?: unknown, context: ErrorContext = {}, cause?: unknown) {
    const suffix = reason instanceof Error ? reason.message : typeof reason === "string" ? reason : ""
    super(ERROR_CODES.cancelled, suffix ? `Zyra API request was cancelled: ${suffix}` : "Zyra API request was cancelled.", {
      category: "cancellation",
      retryable: false,
      details: suffix ? { reason: suffix } : {},
      context,
      cause,
    })
    this.name = "RequestCancelledError"
    this.reason = reason
  }
}

export class TransportDisconnectedError extends ZyraApiError {
  constructor(message = "The Zyra API transport disconnected.", context: ErrorContext = {}, cause?: unknown) {
    super(ERROR_CODES.disconnected, message, {
      category: "disconnect",
      retryable: true,
      context,
      cause,
    })
    this.name = "TransportDisconnectedError"
  }
}

export class AuthenticationError extends ZyraApiError {
  constructor(
    message = "Zyra API authentication was rejected.",
    status = 401,
    context: ErrorContext = {},
    details: Record<string, unknown> = {},
  ) {
    super(
      status === 401 ? ERROR_CODES.authenticationRequired : ERROR_CODES.authenticationRejected,
      message,
      {
        category: status === 401 ? "authentication" : "authorization",
        retryable: false,
        status,
        context,
        details,
      },
    )
    this.name = "AuthenticationError"
  }
}

export class ApiVersionMismatchError extends ZyraApiError {
  readonly requestedVersion?: string
  readonly supportedVersions: string[]

  constructor(
    requestedVersion: string | undefined,
    supportedVersions: readonly string[],
    context: ErrorContext = {},
    status = 426,
  ) {
    const normalized = [...new Set(supportedVersions.map(String).filter(Boolean))]
    super(
      ERROR_CODES.versionMismatch,
      `Zyra API version ${requestedVersion ?? "[missing]"} is not supported; server supports ${normalized.join(", ") || "[unknown]"}.`,
      {
        category: "version",
        retryable: false,
        status,
        details: {
          requested_version: requestedVersion,
          supported_versions: normalized,
        },
        context,
      },
    )
    this.name = "ApiVersionMismatchError"
    this.requestedVersion = requestedVersion
    this.supportedVersions = normalized
  }
}

export class IdempotencyRequiredError extends ZyraApiError {
  constructor(operation: string, context: ErrorContext = {}) {
    super(ERROR_CODES.idempotencyRequired, `Mutating operation ${operation} requires an idempotency key.`, {
      category: "receipt",
      retryable: false,
      details: { operation },
      context,
    })
    this.name = "IdempotencyRequiredError"
  }
}

export class ReceiptError extends ZyraApiError {
  constructor(
    code: typeof ERROR_CODES.receiptMissing | typeof ERROR_CODES.receiptMismatch | typeof ERROR_CODES.receiptConflict,
    message: string,
    details: Record<string, unknown>,
    context: ErrorContext = {},
  ) {
    super(code, message, {
      category: "receipt",
      retryable: false,
      details,
      context,
    })
    this.name = "ReceiptError"
  }
}

export class RetryExhaustedError extends ZyraApiError {
  readonly attempts: number

  constructor(attempts: number, lastError: unknown, context: ErrorContext = {}) {
    const classified = classifyUnknownError(lastError, context)
    super(ERROR_CODES.retryExhausted, `Zyra API request exhausted ${attempts} attempts: ${classified.message}`, {
      category: classified.category,
      retryable: false,
      status: classified.status,
      details: { attempts, last_error_code: classified.code },
      context: { ...classified.context, ...context },
      cause: lastError,
    })
    this.name = "RetryExhaustedError"
    this.attempts = attempts
  }
}

export class CursorError extends ZyraApiError {
  constructor(
    code:
      | typeof ERROR_CODES.cursorInvalid
      | typeof ERROR_CODES.cursorExpired
      | typeof ERROR_CODES.cursorScopeMismatch,
    message: string,
    details: Record<string, unknown> = {},
    context: ErrorContext = {},
  ) {
    super(code, message, {
      category: "cursor",
      retryable: false,
      details,
      context,
    })
    this.name = "CursorError"
  }
}

export class HttpResponseError extends ZyraApiError {
  readonly responseHeaders: Record<string, string>
  readonly body: unknown

  constructor(
    status: number,
    message: string,
    options: {
      code?: string
      category?: ErrorCategory
      retryable?: boolean
      headers?: Headers | Record<string, string>
      body?: unknown
      details?: Record<string, unknown>
      context?: ErrorContext
    } = {},
  ) {
    super(options.code ?? (status >= 500 ? ERROR_CODES.serverError : ERROR_CODES.httpError), message, {
      category: options.category ?? statusCategory(status),
      retryable: options.retryable ?? RETRYABLE_HTTP_STATUSES.has(status),
      status,
      details: options.details,
      context: { ...options.context, status },
    })
    this.name = "HttpResponseError"
    this.responseHeaders = headerRecord(options.headers)
    this.body = options.body
  }
}

export function statusCategory(status: number): ErrorCategory {
  if (status === 401) return "authentication"
  if (status === 403) return "authorization"
  if (VERSION_HTTP_STATUSES.has(status)) return "version"
  if (status === 404 || status === 410) return "not_found"
  if (status === 408) return "timeout"
  if (status === 409 || status === 412) return "conflict"
  if (status >= 400 && status < 500) return "validation"
  if (status >= 500) return "server"
  return "unknown"
}

export function headerRecord(headers: Headers | Record<string, string> | undefined): Record<string, string> {
  if (!headers) return {}
  if (headers instanceof Headers) {
    const result: Record<string, string> = {}
    headers.forEach((value, key) => {
      result[key.toLowerCase()] = value
    })
    return result
  }
  const result: Record<string, string> = {}
  for (const [key, value] of Object.entries(headers)) result[key.toLowerCase()] = String(value)
  return result
}

export function errorMessage(value: unknown, fallback = "Unknown Zyra API error"): string {
  if (value instanceof Error && value.message) return value.message
  if (typeof value === "string" && value.trim()) return value.trim()
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>
    for (const key of ["message", "detail", "error", "code"]) {
      if (typeof record[key] === "string" && record[key].trim()) return record[key].trim()
    }
  }
  return fallback
}

export function errorCode(value: unknown, fallback: string = ERROR_CODES.httpError): string {
  if (!value || typeof value !== "object") return fallback
  const record = value as Record<string, unknown>
  for (const key of ["code", "error", "error_code"]) {
    if (typeof record[key] === "string" && /^[a-zA-Z][a-zA-Z0-9_.-]{1,127}$/.test(record[key])) {
      return record[key]
    }
  }
  return fallback
}

export function isAbortLike(error: unknown): boolean {
  if (!error) return false
  if (error instanceof DOMException && error.name === "AbortError") return true
  if (error instanceof Error && ABORT_ERROR_NAMES.has(error.name)) return true
  const message = errorMessage(error, "").toLowerCase()
  return message === "aborted" || message.includes("operation was aborted") || message.includes("request aborted")
}

export function isDisconnectLike(error: unknown): boolean {
  if (!error || isAbortLike(error)) return false
  if (error instanceof Error && DISCONNECT_ERROR_NAMES.has(error.name)) {
    const message = error.message.toLowerCase()
    if (RETRYABLE_NETWORK_MARKERS.some((marker) => message.includes(marker))) return true
  }
  const message = errorMessage(error, "").toLowerCase()
  return RETRYABLE_NETWORK_MARKERS.some((marker) => message.includes(marker))
}

export function isRetryableError(error: unknown): boolean {
  if (error instanceof ZyraApiError) return error.retryable
  return isDisconnectLike(error)
}

export function classifyUnknownError(error: unknown, context: ErrorContext = {}): ZyraApiError {
  if (error instanceof ZyraApiError) {
    return Object.keys(context).length ? error.withContext(context) : error
  }
  if (isAbortLike(error)) {
    return new RequestCancelledError(undefined, context, error)
  }
  if (isDisconnectLike(error)) {
    return new TransportDisconnectedError(errorMessage(error, "The Zyra API connection failed."), context, error)
  }
  return new ZyraApiError(ERROR_CODES.httpError, errorMessage(error), {
    category: "unknown",
    retryable: false,
    context,
    cause: error,
  })
}

function normalizedSupportedVersions(body: ServerErrorEnvelope, headers: Headers): string[] {
  const values: string[] = []
  const header = headers.get("x-zyra-api-version")
  if (header) values.push(...header.split(","))
  const candidates = [body.supported_version, body.expected_version]
  for (const candidate of candidates) if (typeof candidate === "string") values.push(candidate)
  if (Array.isArray(body.supported_versions)) {
    for (const candidate of body.supported_versions) if (typeof candidate === "string") values.push(candidate)
  }
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))]
}

export function mapHttpError(
  status: number,
  body: unknown,
  headers: Headers,
  context: ErrorContext = {},
): ZyraApiError {
  const envelope = body && typeof body === "object" && !Array.isArray(body) ? (body as ServerErrorEnvelope) : {}
  const fallback = `Zyra API returned HTTP ${status}.`
  const candidate = errorMessage(body, fallback)
  // Proxies often return an HTML error page instead of the API envelope.
  // Keep the original body for diagnostics, but never use the page as a message.
  const message = /text\/html|application\/xhtml\+xml/i.test(headers.get("content-type") ?? "")
    || /<!doctype\s+html|<\/?(?:html|head|body|title|h1)(?:\s|>)/i.test(candidate)
    ? fallback : candidate
  const code = errorCode(body, status >= 500 ? ERROR_CODES.serverError : ERROR_CODES.httpError)
  const details = cleanDetails(
    envelope.details && typeof envelope.details === "object"
      ? envelope.details
      : envelope.detail && typeof envelope.detail === "object"
        ? envelope.detail
        : {},
  )
  if (AUTH_HTTP_STATUSES.has(status)) {
    return new AuthenticationError(message, status, context, { ...details, server_code: code })
  }
  if (VERSION_HTTP_STATUSES.has(status) && (code.includes("version") || normalizedSupportedVersions(envelope, headers).length)) {
    const requested =
      typeof envelope.actual_version === "string"
        ? envelope.actual_version
        : headers.get("x-zyra-requested-version") ?? undefined
    return new ApiVersionMismatchError(requested, normalizedSupportedVersions(envelope, headers), context, status)
  }
  if (status === 408 || code.includes("timeout")) {
    return new RequestTimeoutError(Number(details.timeout_ms ?? 0) || 0, { ...context, status })
  }
  if (status === 409 && code.includes("receipt")) {
    return new ReceiptError(ERROR_CODES.receiptConflict, message, { ...details, server_code: code }, context)
  }
  if (status === 409 && code.includes("idempot")) {
    return new ReceiptError(ERROR_CODES.receiptConflict, message, { ...details, server_code: code }, context)
  }
  return new HttpResponseError(status, message, {
    code,
    category: statusCategory(status),
    retryable:
      typeof envelope.retryable === "boolean" ? envelope.retryable : RETRYABLE_HTTP_STATUSES.has(status),
    headers,
    body,
    details: {
      ...details,
      fallback: envelope.fallback,
      request_id: envelope.request_id ?? envelope.requestId,
      receipt_id: envelope.receipt_id ?? envelope.receiptId,
    },
    context,
  })
}

export async function readErrorBody(response: Response): Promise<unknown> {
  const declared = Number(response.headers.get("content-length") ?? "0")
  if (Number.isFinite(declared) && declared > MAX_ERROR_BODY_BYTES) {
    return {
      error: ERROR_CODES.responseTooLarge,
      message: `Error response body exceeds ${MAX_ERROR_BODY_BYTES} bytes.`,
      content_length: declared,
    }
  }
  const text = await response.text()
  if (!text) return {}
  const encoded = new TextEncoder().encode(text)
  if (encoded.byteLength > MAX_ERROR_BODY_BYTES) {
    return {
      error: ERROR_CODES.responseTooLarge,
      message: `Error response body exceeds ${MAX_ERROR_BODY_BYTES} bytes.`,
      preview: text.slice(0, 1_024),
    }
  }
  try {
    return JSON.parse(text)
  } catch {
    return { message: text.slice(0, 4_096), malformed_json: true }
  }
}

export async function errorFromResponse(response: Response, context: ErrorContext = {}): Promise<ZyraApiError> {
  const body = await readErrorBody(response)
  return mapHttpError(response.status, body, response.headers, {
    ...context,
    status: response.status,
  })
}

export function serializeError(error: unknown, context: ErrorContext = {}): SerializedZyraError {
  return classifyUnknownError(error, context).serialize()
}

export function assertNever(value: never, label = "value"): never {
  throw new ZyraApiError(ERROR_CODES.internalInvariant, `Unexpected ${label}: ${String(value)}`, {
    category: "protocol",
    retryable: false,
  })
}

export function safeErrorLabel(value: unknown): string {
  try {
    return boundedString(errorMessage(value), 512, "error message")
  } catch {
    return "Unknown Zyra API error"
  }
}
