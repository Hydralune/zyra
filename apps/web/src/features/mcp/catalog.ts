import type {
  McpCatalogCursor,
  McpCatalogEntry,
  McpCatalogKind,
  McpCatalogPage,
  McpConsoleProjection,
  McpServerProjection,
} from "./contracts.ts"
import {
  clamp,
  decodeBase64Url,
  encodeBase64Url,
  fingerprint,
  matchesSearch,
  object,
  scalar,
  stable,
} from "./value.ts"

export class McpCatalogError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "McpCatalogError"
    this.code = code
  }
}

export interface McpCatalogQuery {
  serverId: string
  kind: McpCatalogKind
  query?: string
  cursor?: string
  limit?: number
}

export class McpCatalogPager {
  readonly #maximumLimit: number
  readonly #cursorTtlMs: number
  readonly #now: () => number
  #enabled = true
  #disabledReason = "MCP catalog pager is disabled."

  constructor(options: {
    maximumLimit?: number
    cursorTtlMs?: number
    now?: () => number
  } = {}) {
    this.#maximumLimit = clamp(
      Math.trunc(options.maximumLimit ?? 200),
      1,
      1000,
    )
    this.#cursorTtlMs = clamp(
      Math.trunc(options.cursorTtlMs ?? 15 * 60_000),
      1_000,
      24 * 60 * 60_000,
    )
    this.#now = options.now ?? Date.now
  }

  page(
    projection: McpConsoleProjection,
    input: McpCatalogQuery,
  ): McpCatalogPage {
    this.#assertEnabled()
    const server = projection.byId[input.serverId]
    if (!server) {
      throw new McpCatalogError(
        "SERVER_MISSING",
        "MCP server is absent from the canonical projection.",
      )
    }
    const kind = normalizeKind(input.kind)
    const query = normalizeQuery(input.query)
    const queryDigest = fingerprint(query)
    let offset = 0
    if (input.cursor) {
      const cursor = decodeCursor(input.cursor)
      this.#admitCursor(cursor, projection, server, kind, queryDigest)
      offset = cursor.offset
    }
    const limit = clamp(Math.trunc(input.limit ?? 50), 1, this.#maximumLimit)
    const source = catalogFor(server, kind)
    const filtered = source.filter((item) =>
      matchesSearch(query, [
        item.id,
        item.name,
        item.title,
        item.description,
        item.uri ?? "",
        item.mimeType ?? "",
        ...item.annotations,
        ...item.capabilityFlags,
        ...item.argumentNames,
      ]))
    if (offset > filtered.length) {
      throw new McpCatalogError(
        "CURSOR_OFFSET_STALE",
        "MCP catalog cursor points beyond the current revision.",
      )
    }
    const items = filtered.slice(offset, offset + limit)
    const nextOffset = offset + items.length
    const hasNext = nextOffset < filtered.length
    const nextCursor = hasNext
      ? encodeCursor({
          version: 1,
          taskId: projection.authority.taskId,
          runId: projection.authority.runId,
          sessionId: projection.authority.sessionId,
          serverId: server.id,
          kind,
          capabilityRevision: server.capabilities.revision,
          projectionRevision: projection.authority.projectionRevision,
          queryDigest,
          offset: nextOffset,
          issuedAtMs: this.#now(),
          checksum: "",
        })
      : undefined
    return Object.freeze({
      serverId: server.id,
      kind,
      capabilityRevision: server.capabilities.revision,
      projectionRevision: projection.authority.projectionRevision,
      items: Object.freeze(items),
      total: filtered.length,
      offset,
      limit,
      hasNext,
      nextCursor,
      query,
    })
  }

  invalidate(): void {
    this.#assertEnabled()
  }

  disable(reason = "MCP catalog pager is disabled."): void {
    this.#enabled = false
    this.#disabledReason = scalar(reason, "MCP catalog pager is disabled.")
  }

  enable(): void {
    this.#enabled = true
    this.#disabledReason = "MCP catalog pager is disabled."
  }

  #admitCursor(
    cursor: McpCatalogCursor,
    projection: McpConsoleProjection,
    server: McpServerProjection,
    kind: McpCatalogKind,
    queryDigest: string,
  ): void {
    if (cursor.taskId !== projection.authority.taskId) {
      throw new McpCatalogError(
        "CURSOR_TASK_MISMATCH",
        "MCP catalog cursor belongs to another task.",
      )
    }
    if (cursor.runId !== projection.authority.runId) {
      throw new McpCatalogError(
        "CURSOR_RUN_MISMATCH",
        "MCP catalog cursor belongs to another run.",
      )
    }
    if (cursor.sessionId !== projection.authority.sessionId) {
      throw new McpCatalogError(
        "CURSOR_SESSION_MISMATCH",
        "MCP catalog cursor belongs to another session.",
      )
    }
    if (cursor.serverId !== server.id) {
      throw new McpCatalogError(
        "CURSOR_SERVER_MISMATCH",
        "MCP catalog cursor belongs to another server.",
      )
    }
    if (cursor.kind !== kind) {
      throw new McpCatalogError(
        "CURSOR_KIND_MISMATCH",
        "MCP catalog cursor belongs to another capability kind.",
      )
    }
    if (cursor.capabilityRevision !== server.capabilities.revision) {
      throw new McpCatalogError(
        "CURSOR_CAPABILITY_STALE",
        "MCP capability revision changed; restart pagination.",
      )
    }
    if (cursor.projectionRevision !== projection.authority.projectionRevision) {
      throw new McpCatalogError(
        "CURSOR_PROJECTION_STALE",
        "Canonical projection changed; restart pagination.",
      )
    }
    if (cursor.queryDigest !== queryDigest) {
      throw new McpCatalogError(
        "CURSOR_QUERY_MISMATCH",
        "MCP catalog cursor belongs to another search query.",
      )
    }
    if (this.#now() - cursor.issuedAtMs > this.#cursorTtlMs) {
      throw new McpCatalogError(
        "CURSOR_EXPIRED",
        "MCP catalog cursor has expired.",
      )
    }
    if (cursor.issuedAtMs > this.#now() + 30_000) {
      throw new McpCatalogError(
        "CURSOR_FUTURE",
        "MCP catalog cursor timestamp is invalid.",
      )
    }
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new McpCatalogError("PAGER_DISABLED", this.#disabledReason)
  }
}

export function encodeCursor(
  cursor: Omit<McpCatalogCursor, "checksum"> & { checksum?: string },
): string {
  const payload = {
    version: 1,
    taskId: cursor.taskId,
    runId: cursor.runId,
    sessionId: cursor.sessionId,
    serverId: cursor.serverId,
    kind: cursor.kind,
    capabilityRevision: cursor.capabilityRevision,
    projectionRevision: cursor.projectionRevision,
    queryDigest: cursor.queryDigest,
    offset: cursor.offset,
    issuedAtMs: cursor.issuedAtMs,
  }
  const envelope = {
    ...payload,
    checksum: fingerprint(["mcp-cursor/v1", payload]),
  }
  return encodeBase64Url(stable(envelope))
}

export function decodeCursor(value: string): McpCatalogCursor {
  let parsed: unknown
  try {
    parsed = JSON.parse(decodeBase64Url(value))
  } catch (error) {
    throw new McpCatalogError(
      "CURSOR_INVALID",
      error instanceof Error ? error.message : "MCP catalog cursor is invalid.",
    )
  }
  const source = object(parsed)
  const kind = normalizeKind(source.kind)
  const cursor: McpCatalogCursor = {
    version: 1,
    taskId: requiredString(source.taskId, "task"),
    runId: requiredString(source.runId, "run"),
    sessionId: requiredString(source.sessionId, "session"),
    serverId: requiredString(source.serverId, "server"),
    kind,
    capabilityRevision: requiredRevision(source.capabilityRevision, "capability"),
    projectionRevision: requiredRevision(source.projectionRevision, "projection"),
    queryDigest: requiredString(source.queryDigest, "query digest"),
    offset: requiredOffset(source.offset),
    issuedAtMs: requiredTimestamp(source.issuedAtMs),
    checksum: requiredString(source.checksum, "checksum"),
  }
  const expected = fingerprint([
    "mcp-cursor/v1",
    {
      version: cursor.version,
      taskId: cursor.taskId,
      runId: cursor.runId,
      sessionId: cursor.sessionId,
      serverId: cursor.serverId,
      kind: cursor.kind,
      capabilityRevision: cursor.capabilityRevision,
      projectionRevision: cursor.projectionRevision,
      queryDigest: cursor.queryDigest,
      offset: cursor.offset,
      issuedAtMs: cursor.issuedAtMs,
    },
  ])
  if (cursor.checksum !== expected) {
    throw new McpCatalogError(
      "CURSOR_CHECKSUM",
      "MCP catalog cursor integrity check failed.",
    )
  }
  return Object.freeze(cursor)
}

function catalogFor(
  server: McpServerProjection,
  kind: McpCatalogKind,
): readonly McpCatalogEntry[] {
  if (kind === "tool") return server.tools
  if (kind === "resource") return server.resources
  return server.prompts
}

function normalizeKind(value: unknown): McpCatalogKind {
  if (value === "tool" || value === "resource" || value === "prompt") return value
  throw new McpCatalogError(
    "CATALOG_KIND_INVALID",
    "MCP catalog kind must be tool, resource, or prompt.",
  )
}

function normalizeQuery(value: unknown): string {
  const query = scalar(value).normalize("NFKC").trim()
  if (new TextEncoder().encode(query).byteLength > 4096) {
    throw new McpCatalogError(
      "QUERY_TOO_LARGE",
      "MCP catalog query exceeds 4 KiB.",
    )
  }
  return query
}

function requiredString(value: unknown, label: string): string {
  const candidate = scalar(value)
  if (!candidate || candidate.length > 512 || /[\u0000-\u001f\u007f]/.test(candidate)) {
    throw new McpCatalogError(
      "CURSOR_FIELD_INVALID",
      `MCP catalog cursor ${label} is invalid.`,
    )
  }
  return candidate
}

function requiredRevision(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new McpCatalogError(
      "CURSOR_FIELD_INVALID",
      `MCP catalog cursor ${label} revision is invalid.`,
    )
  }
  return Number(value)
}

function requiredOffset(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0 || Number(value) > 10_000_000) {
    throw new McpCatalogError(
      "CURSOR_FIELD_INVALID",
      "MCP catalog cursor offset is invalid.",
    )
  }
  return Number(value)
}

function requiredTimestamp(value: unknown): number {
  if (!Number.isSafeInteger(value) || Number(value) <= 0) {
    throw new McpCatalogError(
      "CURSOR_FIELD_INVALID",
      "MCP catalog cursor timestamp is invalid.",
    )
  }
  return Number(value)
}
