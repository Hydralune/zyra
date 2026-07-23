import {
  DEFAULT_CLIENT_NAME,
  DEFAULT_CLIENT_VERSION,
  JSON_MEDIA_TYPE,
  JSON_UTF8_MEDIA_TYPE,
  MAX_HEADER_VALUE_BYTES,
  ZYRA_ACCEPT_HEADER,
  ZYRA_ATTEMPT_HEADER,
  ZYRA_AUTHORIZATION_HEADER,
  ZYRA_CAUSATION_ID_HEADER,
  ZYRA_CLIENT_NAME_HEADER,
  ZYRA_CLIENT_VERSION_HEADER,
  ZYRA_CONTENT_TYPE_HEADER,
  ZYRA_CONTRACT_HEADER,
  ZYRA_CORRELATION_ID_HEADER,
  ZYRA_DEADLINE_HEADER,
  ZYRA_IDEMPOTENCY_KEY_HEADER,
  ZYRA_OPERATION_HEADER,
  ZYRA_REQUEST_ID_HEADER,
  boundedString,
} from "./constants.ts"
import { normalizeIdempotencyKey } from "./identifiers.ts"
import { versionRequestHeaders, type VersionPolicy } from "./version.ts"

export type AuthTokenProvider = () => string | undefined | null | Promise<string | undefined | null>

export interface HeaderContext {
  operation: string
  contract: string
  requestId: string
  correlationId?: string
  causationId?: string
  idempotencyKey?: string
  attempt: number
  deadlineMs: number
  hasBody: boolean
}

const FORBIDDEN_REQUEST_HEADERS = new Set([
  "accept-charset",
  "accept-encoding",
  "access-control-request-headers",
  "access-control-request-method",
  "connection",
  "content-length",
  "cookie",
  "cookie2",
  "date",
  "dnt",
  "expect",
  "host",
  "keep-alive",
  "origin",
  "referer",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "via",
])

const SENSITIVE_HEADERS = new Set([
  "authorization",
  "cookie",
  "proxy-authorization",
  "set-cookie",
  "x-api-key",
  "x-zyra-service-token",
])

function normalizeHeaderName(value: string): string {
  const name = String(value || "").trim().toLowerCase()
  if (!/^[!#$%&'*+\-.^_`|~0-9a-z]+$/.test(name)) throw new TypeError(`Invalid header name: ${value}`)
  return name
}

function normalizeHeaderValue(value: unknown, name: string): string {
  const rendered = String(value)
  if (/[\r\n\u0000]/.test(rendered)) throw new TypeError(`Invalid header value for ${name}`)
  if (new TextEncoder().encode(rendered).byteLength > MAX_HEADER_VALUE_BYTES) {
    throw new TypeError(`Header ${name} exceeds ${MAX_HEADER_VALUE_BYTES} bytes`)
  }
  return rendered
}

export function copyHeaders(input?: HeadersInit): Headers {
  const result = new Headers()
  if (!input) return result
  const source = new Headers(input)
  source.forEach((value, name) => result.append(name, value))
  return result
}

export function mergeHeaders(...sources: Array<HeadersInit | undefined>): Headers {
  const result = new Headers()
  for (const source of sources) {
    if (!source) continue
    const headers = new Headers(source)
    headers.forEach((value, name) => result.set(name, value))
  }
  return result
}

export function sanitizeCustomHeaders(input?: HeadersInit): Headers {
  const result = new Headers()
  if (!input) return result
  const headers = new Headers(input)
  headers.forEach((value, rawName) => {
    const name = normalizeHeaderName(rawName)
    if (FORBIDDEN_REQUEST_HEADERS.has(name)) throw new TypeError(`Custom header is forbidden: ${name}`)
    result.set(name, normalizeHeaderValue(value, name))
  })
  return result
}

export function bearerToken(value: unknown): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  const token = boundedString(value, 8_192, "authentication token")
  if (/^Bearer\s+/i.test(token)) {
    const unwrapped = token.replace(/^Bearer\s+/i, "").trim()
    if (!unwrapped) throw new TypeError("Bearer token must not be empty")
    return `Bearer ${unwrapped}`
  }
  return `Bearer ${token}`
}

export async function resolveAuthHeader(provider?: AuthTokenProvider): Promise<string | undefined> {
  if (!provider) return undefined
  return bearerToken(await provider())
}

export function redactHeaders(input: HeadersInit, replacement = "[redacted]"): Record<string, string> {
  const headers = new Headers(input)
  const result: Record<string, string> = {}
  headers.forEach((value, name) => {
    result[name.toLowerCase()] = SENSITIVE_HEADERS.has(name.toLowerCase()) ? replacement : value
  })
  return result
}

export function headerValue(headers: Headers, name: string): string | undefined {
  const value = headers.get(name)
  return value === null || value === "" ? undefined : value
}

export function integerHeader(headers: Headers, name: string): number | undefined {
  const value = headerValue(headers, name)
  if (value === undefined) return undefined
  const parsed = Number.parseInt(value, 10)
  return Number.isFinite(parsed) ? parsed : undefined
}

export function booleanHeader(headers: Headers, name: string): boolean | undefined {
  const value = headerValue(headers, name)?.trim().toLowerCase()
  if (value === undefined) return undefined
  if (value === "1" || value === "true" || value === "yes") return true
  if (value === "0" || value === "false" || value === "no") return false
  return undefined
}

export class HeaderPolicy {
  readonly #version: VersionPolicy
  readonly #clientName: string
  readonly #clientVersion: string
  readonly #auth?: AuthTokenProvider
  readonly #defaults: Headers

  constructor(options: {
    version: VersionPolicy
    clientName?: string
    clientVersion?: string
    auth?: AuthTokenProvider
    defaults?: HeadersInit
  }) {
    this.#version = { ...options.version }
    this.#clientName = boundedString(options.clientName ?? DEFAULT_CLIENT_NAME, 128, "client name")
    this.#clientVersion = boundedString(options.clientVersion ?? DEFAULT_CLIENT_VERSION, 64, "client version")
    this.#auth = options.auth
    this.#defaults = sanitizeCustomHeaders(options.defaults)
  }

  async build(context: HeaderContext, custom?: HeadersInit): Promise<Headers> {
    const headers = mergeHeaders(this.#defaults, sanitizeCustomHeaders(custom))
    headers.set(ZYRA_ACCEPT_HEADER, JSON_MEDIA_TYPE)
    if (context.hasBody) headers.set(ZYRA_CONTENT_TYPE_HEADER, JSON_UTF8_MEDIA_TYPE)
    headers.set(ZYRA_CLIENT_NAME_HEADER, this.#clientName)
    headers.set(ZYRA_CLIENT_VERSION_HEADER, this.#clientVersion)
    headers.set(ZYRA_REQUEST_ID_HEADER, normalizeHeaderValue(context.requestId, ZYRA_REQUEST_ID_HEADER))
    headers.set(ZYRA_OPERATION_HEADER, boundedString(context.operation, 128, "operation"))
    headers.set(ZYRA_CONTRACT_HEADER, boundedString(context.contract, 128, "contract"))
    headers.set(ZYRA_ATTEMPT_HEADER, String(Math.max(1, Math.floor(context.attempt))))
    headers.set(ZYRA_DEADLINE_HEADER, String(Math.max(0, Math.floor(context.deadlineMs))))
    if (context.correlationId) {
      headers.set(
        ZYRA_CORRELATION_ID_HEADER,
        normalizeHeaderValue(context.correlationId, ZYRA_CORRELATION_ID_HEADER),
      )
    }
    if (context.causationId) {
      headers.set(ZYRA_CAUSATION_ID_HEADER, normalizeHeaderValue(context.causationId, ZYRA_CAUSATION_ID_HEADER))
    }
    if (context.idempotencyKey) {
      headers.set(ZYRA_IDEMPOTENCY_KEY_HEADER, normalizeIdempotencyKey(context.idempotencyKey))
    }
    for (const [name, value] of Object.entries(versionRequestHeaders(this.#version))) headers.set(name, value)
    const authorization = await resolveAuthHeader(this.#auth)
    if (authorization) headers.set(ZYRA_AUTHORIZATION_HEADER, authorization)
    return headers
  }

  defaults(): Record<string, string> {
    return redactHeaders(this.#defaults)
  }

  hasAuthProvider(): boolean {
    return Boolean(this.#auth)
  }
}

export function assertResponseCorrelation(
  headers: Headers,
  expectedRequestId: string,
  options: { required?: boolean } = {},
): string {
  const actual = headerValue(headers, ZYRA_REQUEST_ID_HEADER)
  if (!actual && options.required !== false) {
    throw new TypeError(`Response omitted ${ZYRA_REQUEST_ID_HEADER}`)
  }
  if (actual && actual !== expectedRequestId) {
    throw new TypeError(`Response request id mismatch: expected ${expectedRequestId}, received ${actual}`)
  }
  return actual ?? expectedRequestId
}
