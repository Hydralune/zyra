import type { JsonObject, JsonValue } from "../../events/ingress/index.ts"

const SECRET_KEY =
  /(^|[_-])(access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|client[_-]?secret|secret|password|authorization|cookie|code[_-]?verifier|callback[_-]?code|private[_-]?key|credential|env(?:ironment)?[_-]?value)($|[_-])/i
const SECRET_VALUE =
  /(?:bearer\s+[a-z0-9._~+/-]+=*|(?:sk|pk|ghp|github_pat|xox[baprs])[-_][a-z0-9_-]{12,}|authorization[=:]|client_secret[=:]|refresh_token[=:])/i
const CONTROL = /[\u0000-\u001f\u007f]/

export function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

export function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

export function scalar(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value.trim()
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return fallback
}

export function numberValue(value: unknown, fallback = 0): number {
  const candidate = typeof value === "number" ? value : Number(value)
  return Number.isFinite(candidate) ? candidate : fallback
}

export function integerValue(value: unknown, fallback = 0): number {
  const candidate = numberValue(value, Number.NaN)
  return Number.isSafeInteger(candidate) ? candidate : fallback
}

export function booleanValue(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") return value
  if (typeof value === "number") return value !== 0
  const candidate = scalar(value).toLowerCase()
  if (["true", "yes", "on", "1", "enabled", "present", "available"].includes(candidate)) return true
  if (["false", "no", "off", "0", "disabled", "absent", "unavailable"].includes(candidate)) return false
  return fallback
}

export function textList(value: unknown): string[] {
  const values = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(",")
      : []
  return unique(values.map((item) => scalar(item)).filter(Boolean))
}

export function first(
  source: Record<string, unknown>,
  keys: readonly string[],
): unknown {
  for (const key of keys) {
    const value = source[key]
    if (value !== undefined && value !== null) return value
  }
  return undefined
}

export function textFrom(
  source: Record<string, unknown>,
  keys: readonly string[],
  fallback = "",
): string {
  return scalar(first(source, keys), fallback)
}

export function numberFrom(
  source: Record<string, unknown>,
  keys: readonly string[],
  fallback = 0,
): number {
  return numberValue(first(source, keys), fallback)
}

export function integerFrom(
  source: Record<string, unknown>,
  keys: readonly string[],
  fallback = 0,
): number {
  return integerValue(first(source, keys), fallback)
}

export function booleanFrom(
  source: Record<string, unknown>,
  keys: readonly string[],
  fallback = false,
): boolean {
  return booleanValue(first(source, keys), fallback)
}

export function listFrom(
  source: Record<string, unknown>,
  keys: readonly string[],
): string[] {
  return textList(first(source, keys))
}

export function merge(...sources: readonly unknown[]): Record<string, unknown> {
  const result: Record<string, unknown> = {}
  for (const source of sources) Object.assign(result, object(source))
  return result
}

export function nested(
  source: Record<string, unknown>,
  paths: readonly (readonly string[])[],
): Record<string, unknown> {
  for (const path of paths) {
    let current = source
    let present = true
    for (const part of path) {
      const next = object(current[part])
      if (!Object.keys(next).length) {
        present = false
        break
      }
      current = next
    }
    if (present) return current
  }
  return {}
}

export function isoDate(value: unknown): string | undefined {
  const candidate = scalar(value)
  if (!candidate) return undefined
  const timestamp = Date.parse(candidate)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : undefined
}

export function clamp(value: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) return minimum
  return Math.min(maximum, Math.max(minimum, value))
}

export function unique<T>(values: readonly T[]): T[] {
  return [...new Set(values)]
}

export function stable(value: unknown, depth = 0): string {
  if (depth > 32) return '"[depth-limit]"'
  if (value === undefined || value === null) return "null"
  if (typeof value === "string") return JSON.stringify(value)
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "null"
  if (typeof value === "boolean") return String(value)
  if (Array.isArray(value)) return `[${value.map((entry) => stable(entry, depth + 1)).join(",")}]`
  const source = object(value)
  return `{${Object.keys(source)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stable(source[key], depth + 1)}`)
    .join(",")}}`
}

export function fingerprint(value: unknown): string {
  const input = stable(value)
  let left = 2166136261
  let right = 2246822507
  for (let index = 0; index < input.length; index += 1) {
    const code = input.charCodeAt(index)
    left = Math.imul(left ^ code, 16777619)
    right = Math.imul(right ^ code, 3266489909)
  }
  return `mcp_${(left >>> 0).toString(16).padStart(8, "0")}${(right >>> 0)
    .toString(16)
    .padStart(8, "0")}`
}

export function identity(label: string, value: unknown): string {
  const candidate = scalar(value)
  const bytes = new TextEncoder().encode(candidate).byteLength
  if (!candidate || bytes > 512 || CONTROL.test(candidate)) {
    throw new TypeError(`${label} identity is invalid.`)
  }
  return candidate
}

export function safeLabel(value: unknown, fallback = ""): string {
  const candidate = scalar(value, fallback).replace(/\s+/g, " ")
  if (!candidate || CONTROL.test(candidate)) return fallback
  if (SECRET_VALUE.test(candidate)) return "[redacted]"
  return candidate.slice(0, 2048)
}

export function safeDescription(value: unknown, fallback = ""): string {
  const candidate = scalar(value, fallback)
  if (!candidate) return fallback
  const clean = candidate
    .replace(/bearer\s+[a-z0-9._~+/-]+=*/gi, "Bearer [redacted]")
    .replace(/((?:token|secret|password|authorization|credential)\s*[=:]\s*)\S+/gi, "$1[redacted]")
  return clean.slice(0, 16384)
}

export function secretKey(key: string): boolean {
  return SECRET_KEY.test(key)
}

export function secretPresent(value: unknown): boolean {
  if (value === undefined || value === null || value === false) return false
  if (typeof value === "string") {
    const candidate = value.trim().toLowerCase()
    return Boolean(candidate && !["absent", "none", "false", "0", "[absent]"].includes(candidate))
  }
  if (Array.isArray(value)) return value.some(secretPresent)
  if (typeof value === "object") return Object.keys(object(value)).length > 0
  return Boolean(value)
}

export function collectSecretPresence(
  value: unknown,
  prefix = "",
  result: Record<string, boolean> = {},
  depth = 0,
): Record<string, boolean> {
  if (depth > 12) return result
  if (Array.isArray(value)) {
    for (const item of value.slice(0, 1000)) {
      collectSecretPresence(item, prefix, result, depth + 1)
    }
    return result
  }
  if (!value || typeof value !== "object") return result
  for (const [key, item] of Object.entries(object(value))) {
    const path = prefix ? `${prefix}.${key}` : key
    if (secretKey(key)) {
      result[path] = secretPresent(item)
      continue
    }
    collectSecretPresence(item, path, result, depth + 1)
  }
  return result
}

export function redactSecrets(
  value: unknown,
  depth = 0,
): unknown {
  if (depth > 12) return "[depth-limit]"
  if (Array.isArray(value)) {
    return value.slice(0, 1000).map((item) => redactSecrets(item, depth + 1))
  }
  if (!value || typeof value !== "object") {
    if (typeof value === "string" && SECRET_VALUE.test(value)) return "[redacted]"
    return value
  }
  const result: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(object(value))) {
    if (secretKey(key)) {
      result[key] = secretPresent(item) ? "[present]" : "[absent]"
      continue
    }
    result[key] = redactSecrets(item, depth + 1)
  }
  return result
}

export function assertSecretSafe(value: unknown, path = "$", depth = 0): void {
  if (depth > 32) throw new Error(`MCP projection exceeds safe depth at ${path}.`)
  if (typeof value === "string" && SECRET_VALUE.test(value)) {
    throw new Error(`MCP projection contains credential material at ${path}.`)
  }
  if (Array.isArray(value)) {
    value.forEach((entry, index) => assertSecretSafe(entry, `${path}[${index}]`, depth + 1))
    return
  }
  if (!value || typeof value !== "object") return
  for (const [key, item] of Object.entries(object(value))) {
    if (secretKey(key) && !["[present]", "[absent]", true, false].includes(item as never)) {
      throw new Error(`MCP projection contains unsafe secret field ${path}.${key}.`)
    }
    assertSecretSafe(item, `${path}.${key}`, depth + 1)
  }
}

export function safeJsonObject(value: unknown): JsonObject {
  const converted = jsonValue(redactSecrets(value))
  return object(converted) as JsonObject
}

function jsonValue(value: unknown): JsonValue | undefined {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value
  if (typeof value === "number") return Number.isFinite(value) ? value : undefined
  if (Array.isArray(value)) {
    return value
      .map(jsonValue)
      .filter((item): item is JsonValue => item !== undefined)
  }
  if (value && typeof value === "object") {
    const result: Record<string, JsonValue> = {}
    for (const [key, item] of Object.entries(object(value))) {
      const converted = jsonValue(item)
      if (converted !== undefined) result[key] = converted
    }
    return result
  }
  return undefined
}

export function searchTokens(query: string): string[] {
  const normalized = query
    .normalize("NFKC")
    .toLocaleLowerCase()
    .split(/[^a-z0-9_.:/-]+/i)
    .filter(Boolean)
    .slice(0, 32)
  return unique(normalized)
}

export function matchesSearch(query: string, fields: readonly string[]): boolean {
  const tokens = searchTokens(query)
  if (!tokens.length) return true
  const haystack = fields.join("\n").normalize("NFKC").toLocaleLowerCase()
  return tokens.every((token) => haystack.includes(token))
}

export function compareText(left: string | undefined, right: string | undefined): number {
  return (left ?? "").localeCompare(right ?? "")
}

export function compareNumber(left: number | undefined, right: number | undefined): number {
  return (left ?? 0) - (right ?? 0)
}

export function quote(value: string): string {
  return JSON.stringify(identity("command argument", value))
}

export function encodeBase64Url(value: string): string {
  const bytes = new TextEncoder().encode(value)
  let binary = ""
  for (const byte of bytes) binary += String.fromCharCode(byte)
  const encoded =
    typeof btoa === "function"
      ? btoa(binary)
      : Buffer.from(bytes).toString("base64")
  return encoded.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "")
}

export function decodeBase64Url(value: string): string {
  if (!/^[A-Za-z0-9_-]+$/.test(value) || value.length > 8192) {
    throw new TypeError("MCP cursor encoding is invalid.")
  }
  const padded = value.replace(/-/g, "+").replace(/_/g, "/")
    .padEnd(Math.ceil(value.length / 4) * 4, "=")
  const binary =
    typeof atob === "function"
      ? atob(padded)
      : Buffer.from(padded, "base64").toString("binary")
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0))
  return new TextDecoder().decode(bytes)
}
