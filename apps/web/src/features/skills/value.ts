import type { JsonObject, JsonValue } from "../../events/ingress/index.ts"

const encoder = new TextEncoder()
const SECRET_KEY = /(?:^|[_-])(?:access|api|auth|client|private|refresh|secret|session|token|verifier|password|passwd|credential|cookie|authorization)(?:$|[_-])/i
const SECRET_VALUE = /(?:bearer\s+[a-z0-9._~+/-]{12,}|(?:sk|pk)-[a-z0-9_-]{12,}|(?:ghp|github_pat|xox[baprs]|ya29)[-_][a-z0-9_-]{12,}|-----BEGIN [A-Z ]+PRIVATE KEY-----)/i
const IDENTITY = /^[a-zA-Z0-9][a-zA-Z0-9_.:/@+-]{0,255}$/
const HASH = /^(?:sha(?:256|384|512):)?[a-f0-9]{32,128}$/i

export function skillText(
  value: unknown,
  fallback = "",
  maximumBytes = 16_384,
): string {
  if (typeof value !== "string") return fallback
  const normalized = value.replace(/\u0000/g, "").trim()
  if (!normalized) return fallback
  if (encoder.encode(normalized).byteLength > maximumBytes) {
    throw new TypeError("Skill projection text exceeds its byte budget.")
  }
  return normalized
}

export function optionalSkillText(
  value: unknown,
  maximumBytes = 16_384,
): string | undefined {
  const normalized = skillText(value, "", maximumBytes)
  return normalized || undefined
}

export function skillIdentity(value: unknown, label = "skill identity"): string {
  const normalized = skillText(value, "", 512)
  if (!normalized || !IDENTITY.test(normalized) || normalized.includes("..")) {
    throw new TypeError(`${label} is invalid.`)
  }
  return normalized
}

export function optionalSkillIdentity(
  value: unknown,
  label = "skill identity",
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return skillIdentity(value, label)
}

export function skillHash(
  value: unknown,
  label = "skill content hash",
): string {
  const normalized = skillText(value, "", 256).toLowerCase()
  if (!HASH.test(normalized)) throw new TypeError(`${label} is invalid.`)
  return normalized.includes(":") ? normalized : `sha256:${normalized}`
}

export function optionalSkillHash(
  value: unknown,
  label = "skill content hash",
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return skillHash(value, label)
}

export function skillInteger(
  value: unknown,
  fallback = 0,
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  const candidate = value === undefined || value === null || value === ""
    ? fallback
    : Number(value)
  if (!Number.isFinite(candidate)) throw new TypeError("Skill projection number must be finite.")
  return Math.min(maximum, Math.max(minimum, Math.trunc(candidate)))
}

export function skillNumber(
  value: unknown,
  fallback = 0,
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  const candidate = value === undefined || value === null || value === ""
    ? fallback
    : Number(value)
  if (!Number.isFinite(candidate)) throw new TypeError("Skill projection number must be finite.")
  return Math.min(maximum, Math.max(minimum, candidate))
}

export function skillBoolean(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") return value
  if (typeof value === "number") return value !== 0
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase()
    if (["true", "yes", "1", "on", "enabled", "allow", "allowed"].includes(normalized)) {
      return true
    }
    if (["false", "no", "0", "off", "disabled", "deny", "denied"].includes(normalized)) {
      return false
    }
  }
  return fallback
}

export function skillRecord(value: unknown): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  return value as JsonObject
}

export function optionalSkillRecord(value: unknown): JsonObject | undefined {
  const result = skillRecord(value)
  return Object.keys(result).length ? result : undefined
}

export function skillArray(value: unknown): readonly JsonValue[] {
  return Array.isArray(value) ? value : []
}

export function skillRecords(value: unknown): readonly JsonObject[] {
  return skillArray(value).map(skillRecord).filter((entry) => Object.keys(entry).length > 0)
}

export function skillStrings(
  value: unknown,
  maximumItems = 512,
  maximumItemBytes = 2_048,
): readonly string[] {
  const values = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(/[,\n]/)
      : []
  const result = new Set<string>()
  for (const item of values) {
    if (result.size >= maximumItems) break
    const normalized = skillText(item, "", maximumItemBytes)
    if (normalized) result.add(normalized)
  }
  return Object.freeze([...result])
}

export function skillTimestamp(value: unknown, fallback?: string): string {
  const normalized = skillText(value, fallback ?? "", 128)
  const parsed = Date.parse(normalized)
  if (!normalized || !Number.isFinite(parsed)) {
    if (fallback) return fallback
    throw new TypeError("Skill projection timestamp is invalid.")
  }
  return new Date(parsed).toISOString()
}

export function optionalSkillTimestamp(value: unknown): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return skillTimestamp(value)
}

export function stableSkillStringify(value: unknown): string {
  return JSON.stringify(canonicalSkillValue(value))
}

export function canonicalSkillValue(value: unknown): JsonValue {
  if (value === null || value === undefined) return null
  if (typeof value === "string" || typeof value === "boolean") return value
  if (typeof value === "number") return Number.isFinite(value) ? value : 0
  if (Array.isArray(value)) return value.map(canonicalSkillValue)
  if (typeof value === "object") {
    const result: JsonObject = {}
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      const item = (value as Record<string, unknown>)[key]
      if (item !== undefined) result[key] = canonicalSkillValue(item)
    }
    return result
  }
  return String(value)
}

export function skillFingerprint(parts: readonly unknown[]): string {
  const input = stableSkillStringify(parts)
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (let index = 0; index < input.length; index += 1) {
    const code = input.charCodeAt(index)
    first ^= code
    first = Math.imul(first, 0x01000193)
    second ^= first + code + Math.imul(second, 33)
    second = Math.imul(second ^ (second >>> 16), 0x85ebca6b)
  }
  const left = (first >>> 0).toString(16).padStart(8, "0")
  const right = (second >>> 0).toString(16).padStart(8, "0")
  return `skill:${left}${right}`
}

export function uniqueSkillStrings(values: readonly (string | undefined)[]): readonly string[] {
  return Object.freeze([...new Set(values.filter((value): value is string => Boolean(value)))].sort())
}

export function compareSkillText(left: unknown, right: unknown): number {
  return String(left ?? "").localeCompare(String(right ?? ""), undefined, {
    numeric: true,
    sensitivity: "base",
  })
}

export function compareSkillNumber(left: unknown, right: unknown): number {
  return skillNumber(left) - skillNumber(right)
}

export function containsSecretMaterial(
  value: unknown,
  path: readonly string[] = [],
): readonly string[] {
  const findings: string[] = []
  const visit = (current: unknown, keys: readonly string[], depth: number): void => {
    if (depth > 12 || findings.length >= 128) return
    if (typeof current === "string") {
      if (SECRET_VALUE.test(current)) findings.push(keys.join(".") || "<root>")
      return
    }
    if (Array.isArray(current)) {
      current.slice(0, 1_024).forEach((item, index) =>
        visit(item, [...keys, String(index)], depth + 1))
      return
    }
    if (!current || typeof current !== "object") return
    for (const [key, item] of Object.entries(current as Record<string, unknown>)) {
      const next = [...keys, key]
      if (SECRET_KEY.test(key) && item !== undefined && item !== null && item !== false) {
        findings.push(next.join("."))
        continue
      }
      visit(item, next, depth + 1)
    }
  }
  visit(value, path, 0)
  return Object.freeze([...new Set(findings)].sort())
}

export function redactSkillValue(
  value: unknown,
  depth = 0,
): JsonValue {
  if (depth > 12) return "[depth-limit]"
  if (value === null || value === undefined) return null
  if (typeof value === "boolean") return value
  if (typeof value === "number") return Number.isFinite(value) ? value : 0
  if (typeof value === "string") {
    if (SECRET_VALUE.test(value)) return "[redacted]"
    return value.length > 64_000 ? `${value.slice(0, 64_000)}…` : value
  }
  if (Array.isArray(value)) {
    return value.slice(0, 2_048).map((item) => redactSkillValue(item, depth + 1))
  }
  if (typeof value === "object") {
    const output: JsonObject = {}
    for (const [key, item] of Object.entries(value as Record<string, unknown>).slice(0, 2_048)) {
      output[key] = SECRET_KEY.test(key)
        ? "[redacted]"
        : redactSkillValue(item, depth + 1)
    }
    return output
  }
  return String(value)
}

export function assertPublicSkillArguments(value: unknown): JsonObject {
  const record = skillRecord(value)
  const findings = containsSecretMaterial(record)
  if (findings.length) {
    throw new TypeError(
      `Skill arguments contain secret-like material at ${findings.slice(0, 5).join(", ")}.`,
    )
  }
  const encoded = stableSkillStringify(record)
  if (encoder.encode(encoded).byteLength > 64 * 1024) {
    throw new TypeError("Skill arguments exceed the 64 KiB command handoff budget.")
  }
  return canonicalSkillValue(record) as JsonObject
}

export function quoteSkillCommand(value: string): string {
  if (/[\u0000\r\n]/.test(value)) throw new TypeError("Command value contains control characters.")
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`
}

export function classifySkillStatus(value: unknown): string {
  return skillText(value, "unknown", 128).toLowerCase().replace(/[\s-]+/g, "_")
}

export function mergeSkillRecords(...values: readonly unknown[]): JsonObject {
  const output: JsonObject = {}
  for (const value of values) {
    const record = skillRecord(value)
    for (const [key, item] of Object.entries(record)) {
      if (item !== undefined && item !== null) output[key] = item
    }
  }
  return output
}

export function firstSkillValue(
  records: readonly JsonObject[],
  ...keys: readonly string[]
): JsonValue | undefined {
  for (const record of records) {
    for (const key of keys) {
      const value = record[key]
      if (value !== undefined && value !== null && value !== "") return value
    }
  }
  return undefined
}

export function firstSkillText(
  records: readonly JsonObject[],
  keys: readonly string[],
  fallback = "",
  maximumBytes = 16_384,
): string {
  return skillText(firstSkillValue(records, ...keys), fallback, maximumBytes)
}

export function firstSkillRecord(
  records: readonly JsonObject[],
  keys: readonly string[],
): JsonObject {
  return skillRecord(firstSkillValue(records, ...keys))
}

export function firstSkillArray(
  records: readonly JsonObject[],
  keys: readonly string[],
): readonly JsonValue[] {
  return skillArray(firstSkillValue(records, ...keys))
}

export function encodedSkillBytes(value: unknown): number {
  return encoder.encode(stableSkillStringify(value)).byteLength
}
