import type { JsonValue } from "../../events/ingress/contracts.ts"

export type UnknownRecord = Readonly<Record<string, unknown>>

const SECRET_KEY =
  /(?:^|_)(?:api_?key|access_?token|refresh_?token|authorization|cookie|password|passwd|secret|client_?secret|private_?key|session_?token|code_?verifier)(?:$|_)/i
const SECRET_TEXT = [
  /\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*/gi,
  /\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b/gi,
  /\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\s*[:=]\s*["']?[^\s"',;]{6,}/gi,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/gi,
] as const

export function record(value: unknown): UnknownRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return Object.freeze({})
  }
  return value as UnknownRecord
}

export function records(value: unknown): readonly UnknownRecord[] {
  if (!Array.isArray(value)) return Object.freeze([])
  return Object.freeze(
    value
      .filter((entry) => entry && typeof entry === "object" && !Array.isArray(entry))
      .map((entry) => record(entry)),
  )
}

export function candidateRecords(
  ...values: readonly unknown[]
): readonly UnknownRecord[] {
  const output: UnknownRecord[] = []
  for (const value of values) {
    const selected = record(value)
    if (Object.keys(selected).length > 0) output.push(selected)
  }
  return Object.freeze(output)
}

export function nested(
  source: UnknownRecord,
  ...paths: readonly (readonly string[])[]
): UnknownRecord {
  for (const path of paths) {
    let current = source
    let valid = true
    for (const key of path) {
      const next = record(current[key])
      if (Object.keys(next).length === 0) {
        valid = false
        break
      }
      current = next
    }
    if (valid) return current
  }
  return Object.freeze({})
}

export function firstRecord(
  sources: readonly UnknownRecord[],
  ...keys: readonly string[]
): UnknownRecord {
  for (const source of sources) {
    for (const key of keys) {
      const selected = record(source[key])
      if (Object.keys(selected).length > 0) return selected
    }
  }
  return Object.freeze({})
}

export function text(
  value: unknown,
  fallback = "",
  maximumBytes = 16_384,
): string {
  if (typeof value !== "string" && typeof value !== "number") return fallback
  const selected = String(value).trim()
  if (!selected) return fallback
  if (/[\u0000]/.test(selected)) return fallback
  return truncateUtf8(selected, maximumBytes)
}

export function firstText(
  sources: readonly UnknownRecord[],
  ...keys: readonly string[]
): string | undefined {
  for (const source of sources) {
    for (const key of keys) {
      const selected = text(source[key])
      if (selected) return selected
    }
  }
  return undefined
}

export function identity(
  value: unknown,
  label = "identity",
): string {
  const selected = text(value, "", 512)
  if (!selected) throw new TypeError(`${label} is required.`)
  if (
    selected.length > 256 ||
    !/^[A-Za-z0-9][A-Za-z0-9._:/@+\-=]*$/.test(selected)
  ) {
    throw new TypeError(`${label} is not a safe canonical identity.`)
  }
  return selected
}

export function optionalIdentity(
  value: unknown,
): string | undefined {
  const selected = text(value, "", 512)
  if (!selected) return undefined
  try {
    return identity(selected)
  } catch {
    return undefined
  }
}

export function firstIdentity(
  sources: readonly UnknownRecord[],
  ...keys: readonly string[]
): string | undefined {
  for (const source of sources) {
    for (const key of keys) {
      const selected = optionalIdentity(source[key])
      if (selected) return selected
    }
  }
  return undefined
}

export function numberValue(
  value: unknown,
  fallback?: number,
): number | undefined {
  if (value === null || value === undefined || value === "") return fallback
  const selected = Number(value)
  return Number.isFinite(selected) ? selected : fallback
}

export function firstNumber(
  sources: readonly UnknownRecord[],
  ...keys: readonly string[]
): number | undefined {
  for (const source of sources) {
    for (const key of keys) {
      const selected = numberValue(source[key])
      if (selected !== undefined) return selected
    }
  }
  return undefined
}

export function integer(
  value: unknown,
  fallback = 0,
  minimum = Number.MIN_SAFE_INTEGER,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  const selected = Math.trunc(numberValue(value, fallback) ?? fallback)
  return Math.max(minimum, Math.min(maximum, selected))
}

export function firstInteger(
  sources: readonly UnknownRecord[],
  fallback: number,
  minimum: number,
  maximum: number,
  ...keys: readonly string[]
): number {
  return integer(firstNumber(sources, ...keys), fallback, minimum, maximum)
}

export function booleanValue(
  value: unknown,
): boolean | undefined {
  if (typeof value === "boolean") return value
  if (typeof value === "number" && (value === 0 || value === 1)) return value === 1
  const selected = text(value).toLowerCase()
  if (["true", "yes", "on", "enabled", "allow", "allowed"].includes(selected)) {
    return true
  }
  if (["false", "no", "off", "disabled", "deny", "denied"].includes(selected)) {
    return false
  }
  return undefined
}

export function firstBoolean(
  sources: readonly UnknownRecord[],
  ...keys: readonly string[]
): boolean | undefined {
  for (const source of sources) {
    for (const key of keys) {
      const selected = booleanValue(source[key])
      if (selected !== undefined) return selected
    }
  }
  return undefined
}

export function textList(
  value: unknown,
  maximum = 1_000,
): readonly string[] {
  const output: string[] = []
  const append = (candidate: unknown) => {
    const selected = text(candidate, "", 4_096)
    if (selected && !output.includes(selected) && output.length < maximum) {
      output.push(selected)
    }
  }
  if (Array.isArray(value)) {
    for (const entry of value) append(entry)
  } else if (typeof value === "string") {
    for (const entry of value.split(/[,\n]/)) append(entry)
  }
  return Object.freeze(output)
}

export function firstTextList(
  sources: readonly UnknownRecord[],
  ...keys: readonly string[]
): readonly string[] {
  for (const source of sources) {
    for (const key of keys) {
      const selected = textList(source[key])
      if (selected.length > 0) return selected
    }
  }
  return Object.freeze([])
}

export function identityList(
  value: unknown,
  maximum = 1_000,
): readonly string[] {
  return Object.freeze(
    textList(value, maximum)
      .map((entry) => optionalIdentity(entry))
      .filter((entry): entry is string => Boolean(entry)),
  )
}

export function firstIdentityList(
  sources: readonly UnknownRecord[],
  ...keys: readonly string[]
): readonly string[] {
  for (const source of sources) {
    for (const key of keys) {
      const selected = identityList(source[key])
      if (selected.length > 0) return selected
    }
  }
  return Object.freeze([])
}

export function isoTime(
  value: unknown,
): string | undefined {
  const selected = text(value)
  if (!selected) return undefined
  const parsed = Date.parse(selected)
  if (!Number.isFinite(parsed)) return undefined
  return new Date(parsed).toISOString()
}

export function firstTime(
  sources: readonly UnknownRecord[],
  ...keys: readonly string[]
): string | undefined {
  for (const source of sources) {
    for (const key of keys) {
      const selected = isoTime(source[key])
      if (selected) return selected
    }
  }
  return undefined
}

export function elapsedMs(
  earlier: string | undefined,
  laterMs: number,
): number | undefined {
  if (!earlier) return undefined
  const parsed = Date.parse(earlier)
  if (!Number.isFinite(parsed)) return undefined
  return Math.max(0, laterMs - parsed)
}

export function ratio(
  used: number,
  limit: number,
): number {
  if (!Number.isFinite(used) || used <= 0) return 0
  if (!Number.isFinite(limit) || limit <= 0) return used > 0 ? 1 : 0
  return Math.max(0, used / limit)
}

export function clamp(
  value: number,
  minimum: number,
  maximum: number,
): number {
  return Math.max(minimum, Math.min(maximum, value))
}

export function unique(
  values: readonly (string | undefined | null)[],
): readonly string[] {
  return Object.freeze(
    [...new Set(values.filter((value): value is string => Boolean(value)))],
  )
}

export function compareText(
  left: string | undefined,
  right: string | undefined,
): number {
  return (left ?? "").localeCompare(right ?? "")
}

export function compareNumber(
  left: number | undefined,
  right: number | undefined,
): number {
  return (left ?? 0) - (right ?? 0)
}

export function sortStable<T>(
  values: Iterable<T>,
  compare: (left: T, right: T) => number,
): readonly T[] {
  return Object.freeze(
    [...values]
      .map((value, index) => ({ value, index }))
      .sort((left, right) => compare(left.value, right.value) || left.index - right.index)
      .map((entry) => entry.value),
  )
}

export function redactText(
  value: unknown,
  maximumBytes = 16_384,
): string {
  let selected = text(value, "", maximumBytes * 2)
  if (!selected) return ""
  for (const matcher of SECRET_TEXT) {
    selected = selected.replace(matcher, "[REDACTED]")
  }
  return truncateUtf8(selected, maximumBytes)
}

export function safeError(
  value: unknown,
): string | undefined {
  const selected = redactText(value, 8_192)
  return selected || undefined
}

export function secretPaths(
  value: unknown,
  prefix = "",
  output: string[] = [],
  seen = new Set<unknown>(),
): readonly string[] {
  if (!value || typeof value !== "object") return Object.freeze(output)
  if (seen.has(value)) return Object.freeze(output)
  seen.add(value)
  if (Array.isArray(value)) {
    value.slice(0, 1_000).forEach((entry, index) =>
      secretPaths(entry, `${prefix}[${index}]`, output, seen))
    return Object.freeze(output)
  }
  for (const [key, candidate] of Object.entries(value as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (SECRET_KEY.test(key)) {
      output.push(path)
      continue
    }
    if (candidate && typeof candidate === "object") {
      secretPaths(candidate, path, output, seen)
      continue
    }
    if (
      typeof candidate === "string" &&
      SECRET_TEXT.some((matcher) => {
        matcher.lastIndex = 0
        return matcher.test(candidate)
      })
    ) {
      output.push(path)
    }
  }
  return Object.freeze(output)
}

export function sanitizeRecord(
  value: unknown,
  depth = 0,
): Readonly<Record<string, JsonValue>> {
  if (depth > 6) return Object.freeze({})
  const source = record(value)
  const output: Record<string, JsonValue> = {}
  for (const [key, candidate] of Object.entries(source).slice(0, 500)) {
    if (SECRET_KEY.test(key)) continue
    if (
      candidate === null ||
      typeof candidate === "boolean" ||
      typeof candidate === "number"
    ) {
      output[key] = candidate
      continue
    }
    if (typeof candidate === "string") {
      output[key] = redactText(candidate, 8_192)
      continue
    }
    if (Array.isArray(candidate)) {
      output[key] = candidate
        .slice(0, 500)
        .map((entry) => {
          if (entry === null || typeof entry === "boolean" || typeof entry === "number") {
            return entry
          }
          if (typeof entry === "string") return redactText(entry, 4_096)
          return sanitizeRecord(entry, depth + 1)
        }) as JsonValue
      continue
    }
    if (candidate && typeof candidate === "object") {
      output[key] = sanitizeRecord(candidate, depth + 1)
    }
  }
  return Object.freeze(output)
}

export function fingerprint(
  values: readonly unknown[],
): string {
  const input = stableSerialize(values)
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (let index = 0; index < input.length; index += 1) {
    const code = input.charCodeAt(index)
    first ^= code
    first = Math.imul(first, 0x01000193)
    second ^= code + index
    second = Math.imul(second, 0x85ebca6b)
  }
  return `subagent-${(first >>> 0).toString(16).padStart(8, "0")}${(second >>> 0).toString(16).padStart(8, "0")}`
}

export function stableSerialize(value: unknown): string {
  const seen = new Set<unknown>()
  const normalize = (candidate: unknown): unknown => {
    if (
      candidate === null ||
      typeof candidate === "string" ||
      typeof candidate === "number" ||
      typeof candidate === "boolean"
    ) {
      return candidate
    }
    if (candidate === undefined) return null
    if (Array.isArray(candidate)) return candidate.map(normalize)
    if (candidate instanceof Set) return [...candidate].map(normalize).sort()
    if (candidate instanceof Map) {
      return [...candidate.entries()]
        .sort(([left], [right]) => String(left).localeCompare(String(right)))
        .map(([key, entry]) => [key, normalize(entry)])
    }
    if (typeof candidate === "object") {
      if (seen.has(candidate)) return "[circular]"
      seen.add(candidate)
      const output: Record<string, unknown> = {}
      for (const key of Object.keys(candidate as Record<string, unknown>).sort()) {
        output[key] = normalize((candidate as Record<string, unknown>)[key])
      }
      seen.delete(candidate)
      return output
    }
    return String(candidate)
  }
  return JSON.stringify(normalize(value))
}

export function truncateUtf8(
  value: string,
  maximumBytes: number,
): string {
  if (maximumBytes <= 0) return ""
  const encoder = new TextEncoder()
  if (encoder.encode(value).byteLength <= maximumBytes) return value
  let low = 0
  let high = value.length
  while (low < high) {
    const middle = Math.ceil((low + high) / 2)
    if (encoder.encode(value.slice(0, middle)).byteLength <= maximumBytes) {
      low = middle
    } else {
      high = middle - 1
    }
  }
  return `${value.slice(0, Math.max(0, low - 1))}…`
}

export function assertBoundedText(
  label: string,
  value: unknown,
  minimumBytes: number,
  maximumBytes: number,
): string {
  const selected = text(value, "", maximumBytes + 1)
  const bytes = new TextEncoder().encode(selected).byteLength
  if (bytes < minimumBytes || bytes > maximumBytes) {
    throw new TypeError(
      `${label} must contain ${minimumBytes} through ${maximumBytes} UTF-8 bytes.`,
    )
  }
  return selected
}

export function exactScopeMatch(
  expected: {
    taskId: string
    runId: string
    sessionId?: string
    parentId?: string
    childId: string
    attempt: number
    ownerId?: string
  },
  observed: {
    taskId?: string
    runId?: string
    sessionId?: string
    parentId?: string
    childId?: string
    attempt?: number
    ownerId?: string
  },
): readonly string[] {
  const conflicts: string[] = []
  if (observed.taskId && observed.taskId !== expected.taskId) conflicts.push("task")
  if (observed.runId && observed.runId !== expected.runId) conflicts.push("run")
  if (observed.sessionId && observed.sessionId !== expected.sessionId) conflicts.push("session")
  if (observed.parentId && observed.parentId !== expected.parentId) conflicts.push("parent")
  if (observed.childId && observed.childId !== expected.childId) conflicts.push("child")
  if (
    observed.attempt !== undefined &&
    observed.attempt !== expected.attempt
  ) {
    conflicts.push("attempt")
  }
  if (observed.ownerId && observed.ownerId !== expected.ownerId) conflicts.push("owner")
  return Object.freeze(conflicts)
}
