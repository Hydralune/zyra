import type {
  McpAuthProjection,
  McpServerProjection,
} from "./contracts.ts"
import {
  assertSecretSafe,
  collectSecretPresence,
  fingerprint,
  object,
  redactSecrets,
  scalar,
  secretKey,
  secretPresent,
  stable,
} from "./value.ts"

export interface McpAuthDecision {
  allowed: boolean
  code:
    | "allow"
    | "not-required"
    | "server-disabled"
    | "refresh-running"
    | "refresh-unavailable"
    | "projection-disconnected"
    | "owner-mismatch"
    | "stale-revision"
    | "sealed"
  reason: string
  serverId: string
  ownerId: string
  authRevision: number
  expiresInMs?: number
}

export interface McpAuthAudit {
  safe: boolean
  credentialFields: readonly string[]
  presentFields: readonly string[]
  absentFields: readonly string[]
  unsafePaths: readonly string[]
  digest: string
}

export function admitAuthRefresh(input: {
  server: McpServerProjection
  connected: boolean
  sealed: boolean
  expectedOwnerId?: string
  expectedAuthRevision?: number
}): McpAuthDecision {
  const auth = input.server.auth
  const decision = (
    allowed: boolean,
    code: McpAuthDecision["code"],
    reason: string,
  ): McpAuthDecision => Object.freeze({
    allowed,
    code,
    reason,
    serverId: input.server.id,
    ownerId: input.server.ownerId,
    authRevision: auth.authRevision,
    expiresInMs: auth.expiresInMs,
  })
  if (input.sealed) {
    return decision(false, "sealed", "Human MCP authentication refresh is forbidden in sealed mode.")
  }
  if (!input.connected) {
    return decision(false, "projection-disconnected", "MCP controls require a connected canonical projection.")
  }
  if (!input.server.enabled || input.server.state === "disabled") {
    return decision(false, "server-disabled", "Disabled MCP servers cannot refresh authentication.")
  }
  if (input.expectedOwnerId && input.expectedOwnerId !== input.server.ownerId) {
    return decision(false, "owner-mismatch", "MCP authentication owner changed.")
  }
  if (
    input.expectedAuthRevision !== undefined &&
    input.expectedAuthRevision !== auth.authRevision
  ) {
    return decision(false, "stale-revision", "MCP authentication revision changed.")
  }
  if (!auth.required) {
    return decision(false, "not-required", "MCP server does not require authentication.")
  }
  if (auth.refreshInFlight || auth.state === "refreshing") {
    return decision(false, "refresh-running", "MCP authentication refresh is already running.")
  }
  if (!auth.refreshEligible) {
    return decision(false, "refresh-unavailable", "MCP authentication cannot be refreshed with current owner state.")
  }
  return decision(true, "allow", "MCP authentication refresh may be handed to permission runtime.")
}

export function authUrgency(
  auth: McpAuthProjection,
  nowMs = Date.now(),
): {
  level: "none" | "info" | "warning" | "critical"
  dueAt?: string
  remainingMs?: number
  reason: string
} {
  if (!auth.required) {
    return Object.freeze({
      level: "none",
      reason: "Authentication is not required.",
    })
  }
  if (auth.state === "missing") {
    return Object.freeze({
      level: "critical",
      reason: "Authentication is missing.",
    })
  }
  if (auth.state === "failed") {
    return Object.freeze({
      level: "critical",
      reason: auth.lastFailureMessage ?? "Authentication failed.",
    })
  }
  const expiryMs = auth.expiresAt ? Date.parse(auth.expiresAt) : Number.NaN
  if (!Number.isFinite(expiryMs)) {
    return Object.freeze({
      level: auth.state === "unknown" ? "warning" : "info",
      reason: "Authentication expiry is not projected.",
    })
  }
  const remainingMs = expiryMs - nowMs
  if (remainingMs <= 0 || auth.state === "expired") {
    return Object.freeze({
      level: "critical",
      dueAt: auth.expiresAt,
      remainingMs,
      reason: "Authentication has expired.",
    })
  }
  if (remainingMs <= 5 * 60_000) {
    return Object.freeze({
      level: "critical",
      dueAt: auth.expiresAt,
      remainingMs,
      reason: "Authentication expires within five minutes.",
    })
  }
  if (remainingMs <= 30 * 60_000 || auth.state === "expiring") {
    return Object.freeze({
      level: "warning",
      dueAt: auth.expiresAt,
      remainingMs,
      reason: "Authentication expires soon.",
    })
  }
  return Object.freeze({
    level: "info",
    dueAt: auth.expiresAt,
    remainingMs,
    reason: "Authentication is valid.",
  })
}

export function auditAuthPayload(value: unknown): McpAuthAudit {
  const presence = collectSecretPresence(value)
  const credentialFields = Object.keys(presence).sort()
  const presentFields = credentialFields.filter((path) => presence[path])
  const absentFields = credentialFields.filter((path) => !presence[path])
  const unsafePaths: string[] = []
  walkPayload(value, "$", unsafePaths)
  const redacted = redactSecrets(value)
  try {
    assertSecretSafe(redacted)
  } catch (error) {
    unsafePaths.push(error instanceof Error ? error.message : String(error))
  }
  return Object.freeze({
    safe: unsafePaths.length === 0,
    credentialFields: Object.freeze(credentialFields),
    presentFields: Object.freeze(presentFields),
    absentFields: Object.freeze(absentFields),
    unsafePaths: Object.freeze([...new Set(unsafePaths)].sort()),
    digest: fingerprint([
      credentialFields,
      presentFields,
      stable(redacted),
    ]),
  })
}

export function credentialPresenceProjection(
  value: unknown,
): Readonly<Record<string, boolean>> {
  const presence = collectSecretPresence(value)
  const normalized: Record<string, boolean> = {}
  for (const path of Object.keys(presence).sort()) {
    normalized[normalizeCredentialPath(path)] = presence[path] ?? false
  }
  return Object.freeze(normalized)
}

export function redactMcpAuthDiagnostic(
  value: unknown,
): Readonly<Record<string, unknown>> {
  const redacted = redactDiagnosticValue(redactSecrets(value))
  const source = object(redacted)
  const output: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(source)) {
    if (secretKey(key)) {
      output[key] = secretPresent(object(value)[key]) ? "[present]" : "[absent]"
      continue
    }
    if (/url|uri|endpoint/i.test(key) && typeof item === "string") {
      output[key] = redactUrl(item)
      continue
    }
    output[key] = item
  }
  assertSecretSafe(output)
  return Object.freeze(output)
}

function redactDiagnosticValue(value: unknown, depth = 0): unknown {
  if (depth > 16) return "[depth-limit]"
  if (typeof value === "string") {
    if (/^(?:https?|wss?):\/\//i.test(value)) return redactUrl(value)
    return value
      .replace(/bearer\s+\S+/gi, "Bearer [redacted]")
      .replace(/([?&](?:code|token|secret|key|verifier)=)[^&]+/gi, "$1[redacted]")
  }
  if (Array.isArray(value)) {
    return value.map((item) => redactDiagnosticValue(item, depth + 1))
  }
  if (!value || typeof value !== "object") return value
  const output: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(object(value))) {
    output[key] = redactDiagnosticValue(item, depth + 1)
  }
  return output
}

function walkPayload(
  value: unknown,
  path: string,
  unsafe: string[],
  depth = 0,
): void {
  if (depth > 16) {
    unsafe.push(`${path}:depth-limit`)
    return
  }
  if (typeof value === "string") {
    if (/bearer\s+\S+/i.test(value)) unsafe.push(`${path}:bearer-token`)
    if (/[?&](?:code|token|secret|key|verifier)=([^&]+)/i.test(value)) {
      unsafe.push(`${path}:secret-query`)
    }
    if (/-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/.test(value)) {
      unsafe.push(`${path}:private-key`)
    }
    return
  }
  if (Array.isArray(value)) {
    value.slice(0, 1000).forEach((item, index) =>
      walkPayload(item, `${path}[${index}]`, unsafe, depth + 1))
    return
  }
  if (!value || typeof value !== "object") return
  for (const [key, item] of Object.entries(object(value))) {
    if (secretKey(key) && typeof item === "string" && item !== "[present]" && item !== "[absent]") {
      unsafe.push(`${path}.${key}:raw-secret`)
    }
    walkPayload(item, `${path}.${key}`, unsafe, depth + 1)
  }
}

function normalizeCredentialPath(path: string): string {
  return path
    .replace(/\.\d+(?=\.|$)/g, "[]")
    .replace(/(?:access|refresh|id)[_-]?token/gi, "token")
    .replace(/api[_-]?key/gi, "api_key")
    .replace(/client[_-]?secret/gi, "client_secret")
    .toLowerCase()
}

function redactUrl(value: string): string {
  try {
    const url = new URL(value)
    for (const key of [...url.searchParams.keys()]) {
      if (/code|token|secret|key|verifier|authorization/i.test(key)) {
        url.searchParams.set(key, "[redacted]")
      }
    }
    url.username = url.username ? "[redacted]" : ""
    url.password = url.password ? "[redacted]" : ""
    return url.toString()
  } catch {
    return scalar(value)
      .replace(/([?&](?:code|token|secret|key|verifier)=)[^&]+/gi, "$1[redacted]")
      .replace(/bearer\s+\S+/gi, "Bearer [redacted]")
  }
}
