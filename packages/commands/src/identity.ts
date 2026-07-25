import type {
  CommandRequestIdentity,
  CommandTransportRequest,
} from "./contracts.ts"

const encoder = new TextEncoder()

export function commandBytes(value: string): number {
  return encoder.encode(value).byteLength
}

export function commandHash(value: string): string {
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  let third = 0x85ebca6b
  for (const byte of encoder.encode(value)) {
    first ^= byte
    first = Math.imul(first, 0x01000193) >>> 0
    second ^= first + byte + ((second << 6) >>> 0) + (second >>> 2)
    second = Math.imul(second, 0x27d4eb2d) >>> 0
    third ^= second + byte
    third = Math.imul(third ^ (third >>> 16), 0x7feb352d) >>> 0
  }
  return [first, second, third]
    .map((part) => part.toString(16).padStart(8, "0"))
    .join("")
}

export function stableJson(value: unknown): string {
  return JSON.stringify(normalizeJson(value))
}

function normalizeJson(value: unknown): unknown {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError("Command identity cannot include a non-finite number.")
    }
    return value
  }
  if (Array.isArray(value)) {
    return value.map(normalizeJson)
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([, entry]) => entry !== undefined)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, entry]) => [key, normalizeJson(entry)]),
    )
  }
  if (value === undefined) return null
  throw new TypeError(`Unsupported command identity value: ${typeof value}`)
}

function randomToken(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID().replace(/-/g, "")
  }
  const time = Date.now().toString(36)
  const random = Math.random().toString(36).slice(2)
  return `${time}${random}${commandHash(`${time}:${random}`).slice(0, 12)}`
}

export function createCommandIdentity(input: {
  taskId: string
  runId: string
  sessionId: string
  text: string
  arguments: Readonly<Record<string, unknown>>
  mode: string
  retryOf?: string
  nonce?: string
}): CommandRequestIdentity {
  const fingerprint = commandHash(
    stableJson({
      taskId: input.taskId,
      runId: input.runId,
      sessionId: input.sessionId,
      text: input.text.trim(),
      arguments: input.arguments,
      mode: input.mode,
      retryOf: input.retryOf,
    }),
  )
  const token = input.nonce?.trim() || randomToken()
  const suffix = commandHash(`${fingerprint}:${token}`)
  return {
    requestId: `controlreq_${suffix.slice(0, 24)}`,
    commandId: `cmd_${suffix.slice(8, 32)}`,
    idempotencyKey: `zyra-command:${fingerprint}:${suffix.slice(0, 12)}`,
    fingerprint,
  }
}

export function requestFingerprint(
  request: Omit<CommandTransportRequest, "signal">,
): string {
  return commandHash(
    stableJson({
      taskId: request.taskId,
      runId: request.runId,
      sessionId: request.sessionId,
      text: request.text,
      arguments: request.arguments,
      requestId: request.requestId,
      commandId: request.commandId,
      idempotencyKey: request.idempotencyKey,
      expectedRevision: request.expectedRevision,
      sealed: request.sealed,
      priority: request.priority,
      deliveryMode: request.deliveryMode,
      retryOf: request.retryOf,
    }),
  )
}

export function assertIdentity(value: string, label: string): string {
  const normalized = String(value || "").trim()
  if (
    normalized.length < 1 ||
    normalized.length > 255 ||
    !/^[A-Za-z0-9][A-Za-z0-9._:@/+~-]*$/.test(normalized)
  ) {
    throw new TypeError(`${label} is not a valid command identity.`)
  }
  return normalized
}

export function normalizeIdempotencyKey(value: string): string {
  const normalized = String(value || "").trim()
  if (normalized.length < 8 || normalized.length > 512) {
    throw new TypeError(
      "Command idempotency key must contain 8 through 512 characters.",
    )
  }
  if (/[\u0000-\u001f\u007f]/.test(normalized)) {
    throw new TypeError("Command idempotency key contains control characters.")
  }
  return normalized
}
