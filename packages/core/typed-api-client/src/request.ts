import {
  MAX_OPERATION_NAME_BYTES,
  MAX_PATH_BYTES,
  MAX_QUERY_ITEMS,
  MAX_REQUEST_BODY_BYTES,
  boundedString,
  clampTimeout,
  isMutatingHttpMethod,
  normalizeHttpMethod,
} from "./constants.ts"
import { DeadlineBudget } from "./cancellation.ts"
import { IdempotencyRequiredError, RequestValidationError } from "./errors.ts"
import {
  type IdentityBinding,
  createIdempotencyKey,
  createRequestId,
  normalizeIdempotencyKey,
  normalizeIdentity,
  validateBinding,
} from "./identifiers.ts"

export type QueryPrimitive = string | number | boolean | null | undefined
export type QueryValue = QueryPrimitive | readonly QueryPrimitive[]
export type QueryRecord = Record<string, QueryValue>

export interface ApiRequest<TBody = unknown> {
  operation: string
  contract: string
  method: string
  path: string
  query?: QueryRecord
  body?: TBody
  headers?: HeadersInit
  requestId?: string
  correlationId?: string
  causationId?: string
  idempotencyKey?: string
  timeoutMs?: number
  signal?: AbortSignal
  binding?: IdentityBinding
  expectedStatuses?: readonly number[]
  retry?: boolean
  metadata?: Record<string, unknown>
}

export interface PreparedRequest<TBody = unknown> {
  operation: string
  contract: string
  method: string
  path: string
  query: QueryRecord
  body?: TBody
  serializedBody?: string
  headers?: HeadersInit
  requestId: string
  correlationId?: string
  causationId?: string
  idempotencyKey?: string
  timeoutMs: number
  signal?: AbortSignal
  binding: IdentityBinding
  expectedStatuses: number[]
  retry: boolean
  metadata: Record<string, unknown>
  deadline: DeadlineBudget
}

function queryPrimitive(value: QueryPrimitive, label: string): string | undefined {
  if (value === undefined) return undefined
  if (value === null) return ""
  if (typeof value === "string") return boundedString(value, 8_192, label)
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new RequestValidationError(`${label} must be finite.`)
    return String(value)
  }
  if (typeof value === "boolean") return value ? "true" : "false"
  throw new RequestValidationError(`${label} has an unsupported type.`)
}

export function normalizeQuery(query: QueryRecord | undefined): QueryRecord {
  if (!query) return {}
  const entries = Object.entries(query)
  if (entries.length > MAX_QUERY_ITEMS) {
    throw new RequestValidationError(`Query exceeds ${MAX_QUERY_ITEMS} keys.`)
  }
  const result: QueryRecord = {}
  for (const [rawKey, rawValue] of entries.sort(([left], [right]) => left.localeCompare(right))) {
    const key = boundedString(rawKey, 256, "query key")
    if (Array.isArray(rawValue)) {
      result[key] = rawValue.map((value, index) => queryPrimitive(value, `query ${key}[${index}]`))
      continue
    }
    result[key] = queryPrimitive(rawValue as QueryPrimitive, `query ${key}`)
  }
  return result
}

export function appendQuery(url: URL, query: QueryRecord): URL {
  for (const [key, rawValue] of Object.entries(query)) {
    if (Array.isArray(rawValue)) {
      for (const value of rawValue) {
        const rendered = queryPrimitive(value, `query ${key}`)
        if (rendered !== undefined) url.searchParams.append(key, rendered)
      }
      continue
    }
    const rendered = queryPrimitive(rawValue as QueryPrimitive, `query ${key}`)
    if (rendered !== undefined) url.searchParams.set(key, rendered)
  }
  return url
}

export function normalizeBaseUrl(value: unknown): string {
  const raw = boundedString(value, 4_096, "API base URL")
  let parsed: URL
  try {
    parsed = new URL(raw)
  } catch (error) {
    throw new RequestValidationError("API base URL must be absolute.", {
      value: raw,
      cause: error instanceof Error ? error.message : String(error),
    })
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new RequestValidationError("API base URL must use http or https.", { protocol: parsed.protocol })
  }
  if (parsed.username || parsed.password) {
    throw new RequestValidationError("API base URL must not contain credentials.")
  }
  parsed.hash = ""
  parsed.search = ""
  parsed.pathname = parsed.pathname.replace(/\/+$/g, "")
  return parsed.toString().replace(/\/$/g, "")
}

export function normalizePath(value: unknown): string {
  const path = boundedString(value, MAX_PATH_BYTES, "request path")
  if (!path.startsWith("/")) throw new RequestValidationError("Request path must begin with '/'.", { path })
  if (path.startsWith("//")) throw new RequestValidationError("Protocol-relative request paths are forbidden.", { path })
  if (path.includes("\\") || path.includes("\u0000")) {
    throw new RequestValidationError("Request path contains forbidden characters.", { path })
  }
  const [pathname] = path.split(/[?#]/, 1)
  const segments = pathname!.split("/")
  if (segments.some((segment) => segment === "." || segment === "..")) {
    throw new RequestValidationError("Request path traversal is forbidden.", { path })
  }
  return pathname!
}

export function encodePathSegment(value: unknown, label = "path segment"): string {
  const normalized = boundedString(value, 512, label)
  if (normalized === "." || normalized === "..") throw new RequestValidationError(`${label} is reserved.`)
  return encodeURIComponent(normalized)
}

export function buildRequestUrl(baseUrl: string, path: string, query: QueryRecord = {}): URL {
  const base = normalizeBaseUrl(baseUrl)
  const normalizedPath = normalizePath(path)
  const url = new URL(`${base}${normalizedPath}`)
  appendQuery(url, normalizeQuery(query))
  return url
}

function canonicalize(value: unknown, seen: Set<object>): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new RequestValidationError("JSON numbers must be finite.")
    return value
  }
  if (typeof value === "bigint") return value.toString()
  if (value === undefined) return undefined
  if (value instanceof Date) return value.toISOString()
  if (Array.isArray(value)) return value.map((entry) => canonicalize(entry, seen) ?? null)
  if (typeof value === "object") {
    if (seen.has(value)) throw new RequestValidationError("Request body must not contain cycles.")
    seen.add(value)
    const result: Record<string, unknown> = {}
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      const entry = canonicalize((value as Record<string, unknown>)[key], seen)
      if (entry !== undefined) result[key] = entry
    }
    seen.delete(value)
    return result
  }
  throw new RequestValidationError(`Request body contains unsupported ${typeof value} value.`)
}

export function canonicalJson(value: unknown): string {
  const normalized = canonicalize(value, new Set())
  const serialized = JSON.stringify(normalized)
  if (serialized === undefined) throw new RequestValidationError("Request body cannot serialize to undefined.")
  const bytes = new TextEncoder().encode(serialized).byteLength
  if (bytes > MAX_REQUEST_BODY_BYTES) {
    throw new RequestValidationError(`Request body exceeds ${MAX_REQUEST_BODY_BYTES} bytes.`, {
      body_bytes: bytes,
    })
  }
  return serialized
}

export function requestFingerprint(request: Pick<PreparedRequest, "method" | "path" | "query" | "serializedBody">): string {
  const material = `${request.method}\n${request.path}\n${canonicalJson(request.query)}\n${request.serializedBody ?? ""}`
  const bytes = new TextEncoder().encode(material)
  let hash = 0x811c9dc5
  for (const byte of bytes) {
    hash ^= byte
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, "0")
}

function expectedStatuses(values: readonly number[] | undefined, method: string): number[] {
  const defaults =
    method === "POST" ? [200, 201, 202] : method === "DELETE" ? [200, 202, 204] : [200]
  const source = values ?? defaults
  const result = [...new Set(source.map(Number))]
  if (!result.length || result.some((value) => !Number.isInteger(value) || value < 100 || value > 599)) {
    throw new RequestValidationError("Expected statuses must contain valid HTTP status codes.")
  }
  return result.sort((left, right) => left - right)
}

export function prepareRequest<TBody>(
  request: ApiRequest<TBody>,
  options: { defaultTimeoutMs?: number; now?: () => number } = {},
): PreparedRequest<TBody> {
  const operation = boundedString(request.operation, MAX_OPERATION_NAME_BYTES, "operation")
  const contract = boundedString(request.contract, 128, "contract")
  const method = normalizeHttpMethod(request.method)
  const path = normalizePath(request.path)
  const query = normalizeQuery(request.query)
  const timeoutMs = clampTimeout(request.timeoutMs, options.defaultTimeoutMs)
  const binding = validateBinding(request.binding ?? {})
  const requestId = request.requestId
    ? normalizeIdentity("request", request.requestId)
    : createRequestId()
  const correlationId = request.correlationId
    ? normalizeIdentity("request", request.correlationId)
    : undefined
  const causationId = request.causationId
    ? normalizeIdentity("event", request.causationId)
    : undefined
  const serializedBody = request.body === undefined ? undefined : canonicalJson(request.body)
  let idempotencyKey = request.idempotencyKey
    ? normalizeIdempotencyKey(request.idempotencyKey)
    : undefined
  if (isMutatingHttpMethod(method) && request.retry !== false && !idempotencyKey) {
    idempotencyKey = createIdempotencyKey(operation, binding, request.body ?? null)
  }
  if (isMutatingHttpMethod(method) && request.retry !== false && !idempotencyKey) {
    throw new IdempotencyRequiredError(operation, { method, operation, requestId })
  }
  return {
    operation,
    contract,
    method,
    path,
    query,
    body: request.body,
    serializedBody,
    headers: request.headers,
    requestId,
    correlationId,
    causationId,
    idempotencyKey,
    timeoutMs,
    signal: request.signal,
    binding,
    expectedStatuses: expectedStatuses(request.expectedStatuses, method),
    retry: request.retry ?? true,
    metadata: { ...(request.metadata ?? {}) },
    deadline: new DeadlineBudget(timeoutMs, { now: options.now }),
  }
}

export function clonePreparedRequest<TBody>(request: PreparedRequest<TBody>): PreparedRequest<TBody> {
  return {
    ...request,
    query: { ...request.query },
    binding: { ...request.binding },
    expectedStatuses: [...request.expectedStatuses],
    metadata: { ...request.metadata },
    deadline: request.deadline,
  }
}

export class RequestFactory {
  readonly #defaultTimeoutMs: number
  readonly #baseMetadata: Record<string, unknown>
  readonly #now: () => number

  constructor(options: {
    defaultTimeoutMs?: number
    metadata?: Record<string, unknown>
    now?: () => number
  } = {}) {
    this.#defaultTimeoutMs = clampTimeout(options.defaultTimeoutMs)
    this.#baseMetadata = { ...(options.metadata ?? {}) }
    this.#now = options.now ?? Date.now
  }

  create<TBody>(request: ApiRequest<TBody>): PreparedRequest<TBody> {
    return prepareRequest(
      {
        ...request,
        metadata: { ...this.#baseMetadata, ...(request.metadata ?? {}) },
      },
      { defaultTimeoutMs: this.#defaultTimeoutMs, now: this.#now },
    )
  }

  get(
    operation: string,
    contract: string,
    path: string,
    options: Omit<ApiRequest<never>, "operation" | "contract" | "method" | "path" | "body"> = {},
  ): PreparedRequest<never> {
    return this.create({ ...options, operation, contract, method: "GET", path })
  }

  post<TBody>(
    operation: string,
    contract: string,
    path: string,
    body: TBody,
    options: Omit<ApiRequest<TBody>, "operation" | "contract" | "method" | "path" | "body"> = {},
  ): PreparedRequest<TBody> {
    return this.create({ ...options, operation, contract, method: "POST", path, body })
  }
}
