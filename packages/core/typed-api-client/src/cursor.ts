import {
  DEFAULT_CURSOR_TTL_MS,
  MAX_CURSOR_BYTES,
  MAX_QUERY_ITEMS,
  boundedString,
  clampInteger,
} from "./constants.ts"
import { CursorError } from "./errors.ts"
import type { IdentityBinding } from "./identifiers.ts"
import { bindingKey, bindingToSnakeCase, validateBinding } from "./identifiers.ts"

export interface CursorPayload {
  version: 1
  scope: string
  position: number
  issuedAt: number
  expiresAt: number
  binding: IdentityBinding
  filter?: Record<string, string>
  serverCursor?: string
}

export interface CursorPage {
  cursor?: string
  position: number
  hasMore: boolean
  limit: number
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = ""
  for (const byte of bytes) binary += String.fromCharCode(byte)
  if (typeof btoa === "function") return btoa(binary)
  const buffer = (globalThis as unknown as { Buffer?: { from(value: Uint8Array): { toString(format: string): string } } }).Buffer
  if (buffer) return buffer.from(bytes).toString("base64")
  throw new TypeError("No base64 encoder is available")
}

function base64ToBytes(value: string): Uint8Array {
  if (typeof atob === "function") {
    const binary = atob(value)
    const result = new Uint8Array(binary.length)
    for (let index = 0; index < binary.length; index += 1) result[index] = binary.charCodeAt(index)
    return result
  }
  const buffer = (
    globalThis as unknown as {
      Buffer?: { from(value: string, format: string): Uint8Array }
    }
  ).Buffer
  if (buffer) return new Uint8Array(buffer.from(value, "base64"))
  throw new TypeError("No base64 decoder is available")
}

function base64UrlEncode(value: string): string {
  return bytesToBase64(new TextEncoder().encode(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "")
}

function base64UrlDecode(value: string): string {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/")
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=")
  return new TextDecoder().decode(base64ToBytes(padded))
}

function canonicalFilter(value: Record<string, unknown> | undefined): Record<string, string> | undefined {
  if (!value) return undefined
  const entries = Object.entries(value)
  if (entries.length > MAX_QUERY_ITEMS) throw new CursorError("cursor_invalid", "Cursor filter is too large.")
  const result: Record<string, string> = {}
  for (const [rawKey, rawValue] of entries.sort(([left], [right]) => left.localeCompare(right))) {
    const key = boundedString(rawKey, 128, "cursor filter key")
    const value = boundedString(String(rawValue), 1_024, `cursor filter ${key}`)
    result[key] = value
  }
  return Object.keys(result).length ? result : undefined
}

function canonicalPayload(payload: CursorPayload): CursorPayload {
  const scope = boundedString(payload.scope, 256, "cursor scope")
  const position = clampInteger(payload.position, 0, Number.MAX_SAFE_INTEGER, "cursor position")
  const issuedAt = clampInteger(payload.issuedAt, 0, Number.MAX_SAFE_INTEGER, "cursor issued time")
  const expiresAt = clampInteger(payload.expiresAt, 0, Number.MAX_SAFE_INTEGER, "cursor expiry time")
  if (expiresAt <= issuedAt) throw new CursorError("cursor_invalid", "Cursor expiry must follow issue time.")
  const serverCursor = payload.serverCursor
    ? boundedString(payload.serverCursor, MAX_CURSOR_BYTES, "server cursor")
    : undefined
  return {
    version: 1,
    scope,
    position,
    issuedAt,
    expiresAt,
    binding: validateBinding(payload.binding),
    filter: canonicalFilter(payload.filter),
    serverCursor,
  }
}

export function encodeCursor(payload: CursorPayload): string {
  const canonical = canonicalPayload(payload)
  const serialized = JSON.stringify({
    v: canonical.version,
    s: canonical.scope,
    p: canonical.position,
    i: canonical.issuedAt,
    e: canonical.expiresAt,
    b: bindingToSnakeCase(canonical.binding),
    f: canonical.filter,
    c: canonical.serverCursor,
  })
  const encoded = base64UrlEncode(serialized)
  if (new TextEncoder().encode(encoded).byteLength > MAX_CURSOR_BYTES) {
    throw new CursorError("cursor_invalid", `Cursor exceeds ${MAX_CURSOR_BYTES} bytes.`)
  }
  return encoded
}

export function decodeCursor(
  value: unknown,
  options: {
    scope?: string
    binding?: IdentityBinding
    filter?: Record<string, unknown>
    now?: number
    allowExpired?: boolean
  } = {},
): CursorPayload {
  const encoded = boundedString(value, MAX_CURSOR_BYTES, "cursor")
  let raw: unknown
  try {
    raw = JSON.parse(base64UrlDecode(encoded))
  } catch (error) {
    throw new CursorError("cursor_invalid", "Cursor is not valid base64url JSON.", {
      cause: error instanceof Error ? error.message : String(error),
    })
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new CursorError("cursor_invalid", "Cursor payload must be an object.")
  }
  const record = raw as Record<string, unknown>
  if (record.v !== 1) throw new CursorError("cursor_invalid", `Unsupported cursor version: ${String(record.v)}`)
  const binding =
    record.b && typeof record.b === "object" && !Array.isArray(record.b)
      ? snakeBinding(record.b as Record<string, unknown>)
      : {}
  const payload = canonicalPayload({
    version: 1,
    scope: record.s as string,
    position: record.p as number,
    issuedAt: record.i as number,
    expiresAt: record.e as number,
    binding,
    filter: record.f as Record<string, string> | undefined,
    serverCursor: record.c as string | undefined,
  })
  const now = options.now ?? Date.now()
  if (!options.allowExpired && now > payload.expiresAt) {
    throw new CursorError("cursor_expired", "Cursor has expired.", {
      expires_at: payload.expiresAt,
      now,
    })
  }
  if (options.scope && payload.scope !== options.scope) {
    throw new CursorError("cursor_scope_mismatch", "Cursor scope does not match this request.", {
      expected_scope: options.scope,
      actual_scope: payload.scope,
    })
  }
  if (options.binding && bindingKey(payload.binding) !== bindingKey(validateBinding(options.binding))) {
    throw new CursorError("cursor_scope_mismatch", "Cursor identity binding does not match this request.", {
      expected_binding: bindingToSnakeCase(options.binding),
      actual_binding: bindingToSnakeCase(payload.binding),
    })
  }
  const filter = canonicalFilter(options.filter)
  if (JSON.stringify(payload.filter ?? {}) !== JSON.stringify(filter ?? {})) {
    throw new CursorError("cursor_scope_mismatch", "Cursor filter does not match this request.", {
      expected_filter: filter,
      actual_filter: payload.filter,
    })
  }
  return payload
}

function snakeBinding(value: Record<string, unknown>): IdentityBinding {
  return validateBinding({
    sessionId: optionalString(value.session_id),
    runId: optionalString(value.run_id),
    taskId: optionalString(value.task_id),
    spanId: optionalString(value.span_id),
    checkpointId: optionalString(value.checkpoint_id),
    toolId: optionalString(value.tool_id),
    artifactId: optionalString(value.artifact_id),
    controlCommandId: optionalString(value.control_command_id),
    requestId: optionalString(value.request_id),
    receiptId: optionalString(value.receipt_id),
    eventId: optionalString(value.event_id),
  })
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined
}

export function createCursor(options: {
  scope: string
  position: number
  binding?: IdentityBinding
  filter?: Record<string, unknown>
  serverCursor?: string
  ttlMs?: number
  now?: number
}): string {
  const issuedAt = options.now ?? Date.now()
  const ttlMs = clampInteger(options.ttlMs ?? DEFAULT_CURSOR_TTL_MS, 1, 24 * 60 * 60_000, "cursor TTL")
  return encodeCursor({
    version: 1,
    scope: options.scope,
    position: options.position,
    issuedAt,
    expiresAt: issuedAt + ttlMs,
    binding: options.binding ?? {},
    filter: canonicalFilter(options.filter),
    serverCursor: options.serverCursor,
  })
}

export function cursorPage(options: {
  scope: string
  offset: number
  returned: number
  total?: number
  limit: number
  binding?: IdentityBinding
  filter?: Record<string, unknown>
  serverCursor?: string
  ttlMs?: number
}): CursorPage {
  const offset = clampInteger(options.offset, 0, Number.MAX_SAFE_INTEGER, "cursor offset")
  const returned = clampInteger(options.returned, 0, Number.MAX_SAFE_INTEGER, "returned item count")
  const limit = clampInteger(options.limit, 1, 1_000, "page limit")
  const position = offset + returned
  const hasMore =
    Boolean(options.serverCursor) ||
    (options.total !== undefined ? position < options.total : returned >= limit)
  return {
    cursor: hasMore
      ? createCursor({
          scope: options.scope,
          position,
          binding: options.binding,
          filter: options.filter,
          serverCursor: options.serverCursor,
          ttlMs: options.ttlMs,
        })
      : undefined,
    position,
    hasMore,
    limit,
  }
}

export class CursorJournal {
  readonly #entries = new Map<string, CursorPayload>()
  readonly #maximum: number

  constructor(maximum = 1_024) {
    this.#maximum = clampInteger(maximum, 1, 100_000, "cursor journal maximum")
  }

  remember(cursor: string, options: Parameters<typeof decodeCursor>[1] = {}): CursorPayload {
    const payload = decodeCursor(cursor, options)
    const key = `${payload.scope}|${bindingKey(payload.binding)}`
    const current = this.#entries.get(key)
    if (current && payload.position < current.position) {
      throw new CursorError("cursor_invalid", "Cursor position moved backwards.", {
        previous_position: current.position,
        next_position: payload.position,
      })
    }
    this.#entries.delete(key)
    this.#entries.set(key, payload)
    while (this.#entries.size > this.#maximum) {
      const oldest = this.#entries.keys().next().value
      if (oldest === undefined) break
      this.#entries.delete(oldest)
    }
    return payload
  }

  get(scope: string, binding: IdentityBinding = {}): CursorPayload | undefined {
    const value = this.#entries.get(`${scope}|${bindingKey(validateBinding(binding))}`)
    return value ? { ...value, binding: { ...value.binding }, filter: value.filter ? { ...value.filter } : undefined } : undefined
  }

  forget(scope: string, binding: IdentityBinding = {}): boolean {
    return this.#entries.delete(`${scope}|${bindingKey(validateBinding(binding))}`)
  }

  prune(now = Date.now()): number {
    let removed = 0
    for (const [key, value] of this.#entries) {
      if (value.expiresAt < now) {
        this.#entries.delete(key)
        removed += 1
      }
    }
    return removed
  }

  clear(): void {
    this.#entries.clear()
  }

  snapshot(): CursorPayload[] {
    return [...this.#entries.values()].map((value) => ({
      ...value,
      binding: { ...value.binding },
      filter: value.filter ? { ...value.filter } : undefined,
    }))
  }
}
