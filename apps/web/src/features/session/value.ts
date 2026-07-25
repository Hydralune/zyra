import type { JsonObject, JsonValue } from "../../events/ingress/index.ts"

export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

export function values(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

export function text(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value.trim()
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  return fallback
}

export function integer(value: unknown, fallback = 0): number {
  const selected = typeof value === "number" ? value : Number(value)
  return Number.isSafeInteger(selected) ? selected : fallback
}

export function decimal(value: unknown, fallback = 0): number {
  const selected = typeof value === "number" ? value : Number(value)
  return Number.isFinite(selected) ? selected : fallback
}

export function truth(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") return value
  if (typeof value === "number") return value !== 0
  const selected = text(value).toLowerCase()
  if (["true", "yes", "1", "on", "enabled", "available"].includes(selected)) return true
  if (["false", "no", "0", "off", "disabled", "unavailable"].includes(selected)) return false
  return fallback
}

export function list(value: unknown): string[] {
  if (Array.isArray(value)) {
    return [...new Set(value.map((entry) => text(entry)).filter(Boolean))]
  }
  const selected = text(value)
  if (!selected) return []
  return [...new Set(selected.split(",").map((entry) => entry.trim()).filter(Boolean))]
}

export function date(value: unknown): string | undefined {
  const selected = text(value)
  if (!selected) return undefined
  const timestamp = Date.parse(selected)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : undefined
}

export function first(source: Record<string, unknown>, keys: readonly string[]): unknown {
  for (const key of keys) {
    if (source[key] !== undefined && source[key] !== null) return source[key]
  }
  return undefined
}

export function textFrom(source: Record<string, unknown>, keys: readonly string[], fallback = ""): string {
  return text(first(source, keys), fallback)
}

export function integerFrom(source: Record<string, unknown>, keys: readonly string[], fallback = 0): number {
  return integer(first(source, keys), fallback)
}

export function decimalFrom(source: Record<string, unknown>, keys: readonly string[], fallback = 0): number {
  return decimal(first(source, keys), fallback)
}

export function truthFrom(source: Record<string, unknown>, keys: readonly string[], fallback = false): boolean {
  return truth(first(source, keys), fallback)
}

export function listFrom(source: Record<string, unknown>, keys: readonly string[]): string[] {
  return list(first(source, keys))
}

export function nested(source: Record<string, unknown>, path: readonly string[]): Record<string, unknown> {
  let current = source
  for (const key of path) current = record(current[key])
  return current
}

export function bounded(value: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) return minimum
  return Math.min(maximum, Math.max(minimum, value))
}

export function ratio(numerator: number, denominator: number): number {
  if (!Number.isFinite(numerator) || !Number.isFinite(denominator) || denominator <= 0) return 0
  return bounded(numerator / denominator, 0, 1)
}

export function compareText(left: string | undefined, right: string | undefined): number {
  return (left ?? "").localeCompare(right ?? "")
}

export function compareNumber(left: number | undefined, right: number | undefined): number {
  return (left ?? 0) - (right ?? 0)
}

export function unique<T>(items: readonly T[]): T[] {
  return [...new Set(items)]
}

export function groupBy<T>(items: readonly T[], key: (item: T) => string): Record<string, T[]> {
  const groups: Record<string, T[]> = {}
  for (const item of items) {
    const selected = key(item)
    const group = groups[selected] ?? []
    group.push(item)
    groups[selected] = group
  }
  return groups
}

export function sum(items: readonly number[]): number {
  return items.reduce((total, item) => total + (Number.isFinite(item) ? item : 0), 0)
}

export function mean(items: readonly number[]): number {
  return items.length ? sum(items) / items.length : 0
}

export function percentile(items: readonly number[], target: number): number {
  if (!items.length) return 0
  const sorted = [...items].filter(Number.isFinite).sort((left, right) => left - right)
  if (!sorted.length) return 0
  const offset = bounded(target, 0, 1) * (sorted.length - 1)
  const lower = Math.floor(offset)
  const upper = Math.ceil(offset)
  if (lower === upper) return sorted[lower] ?? 0
  const weight = offset - lower
  return (sorted[lower] ?? 0) * (1 - weight) + (sorted[upper] ?? 0) * weight
}

export function fingerprint(parts: readonly unknown[]): string {
  let hash = 2166136261
  const input = parts.map((part) => stable(part)).join("\u001f")
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return `fp_${(hash >>> 0).toString(16).padStart(8, "0")}`
}

export function stable(value: unknown): string {
  if (value === null || value === undefined) return "null"
  if (typeof value === "string") return JSON.stringify(value)
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`
  const selected = record(value)
  return `{${Object.keys(selected).sort().map((key) => `${JSON.stringify(key)}:${stable(selected[key])}`).join(",")}}`
}

export function jsonObject(value: unknown): JsonObject {
  const selected = record(value)
  const output: Record<string, JsonValue> = {}
  for (const [key, item] of Object.entries(selected)) {
    const converted = jsonValue(item)
    if (converted !== undefined) output[key] = converted
  }
  return output
}

function jsonValue(value: unknown): JsonValue | undefined {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value
  if (typeof value === "number") return Number.isFinite(value) ? value : undefined
  if (Array.isArray(value)) {
    return value.map(jsonValue).filter((item): item is JsonValue => item !== undefined)
  }
  if (value && typeof value === "object") return jsonObject(value)
  return undefined
}

export function secretKey(key: string): boolean {
  return /(^|[_-])(api[_-]?key|token|secret|password|credential|authorization|cookie|private[_-]?key)($|[_-])/i.test(key)
}

export function redact(value: unknown, depth = 0): unknown {
  if (depth > 12) return "[depth-limit]"
  if (Array.isArray(value)) return value.map((item) => redact(item, depth + 1))
  if (!value || typeof value !== "object") return value
  const output: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(record(value))) {
    if (secretKey(key)) {
      output[key] = truth(item) ? "[present]" : "[absent]"
      continue
    }
    output[key] = redact(item, depth + 1)
  }
  return output
}

export function searchScore(query: string, fields: readonly string[]): number {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (!terms.length) return 1
  const normalized = fields.map((field) => field.toLowerCase())
  let score = 0
  for (const term of terms) {
    let best = 0
    for (const field of normalized) {
      if (field === term) best = Math.max(best, 20)
      else if (field.startsWith(term)) best = Math.max(best, 12)
      else if (field.includes(term)) best = Math.max(best, 6)
    }
    if (!best) return 0
    score += best
  }
  return score
}

export function humanBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B"
  const units = ["B", "KiB", "MiB", "GiB", "TiB"]
  const index = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)))
  const amount = value / 1024 ** index
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`
}

export function humanTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0"
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(Math.round(value))
}

export function elapsed(from: string | undefined, to = new Date().toISOString()): number {
  const start = from ? Date.parse(from) : Number.NaN
  const finish = Date.parse(to)
  return Number.isFinite(start) && Number.isFinite(finish) ? Math.max(0, finish - start) : 0
}

export function mergeRecords(...sources: readonly unknown[]): Record<string, unknown> {
  const output: Record<string, unknown> = {}
  for (const source of sources) Object.assign(output, record(source))
  return output
}

export function pick(source: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  const output: Record<string, unknown> = {}
  for (const key of keys) if (source[key] !== undefined) output[key] = source[key]
  return output
}

export function omitSecrets(source: Record<string, unknown>): Record<string, unknown> {
  return record(redact(source))
}

export function assertIdentity(label: string, value: unknown): string {
  const selected = text(value)
  if (!selected || selected.length > 512 || /[\u0000-\u001f]/.test(selected)) {
    throw new TypeError(`${label} identity is invalid.`)
  }
  return selected
}

export function assertRevision(value: unknown): number {
  const selected = integer(value, -1)
  if (selected < 0) throw new TypeError("Expected canonical revision is invalid.")
  return selected
}

export function assertQuery(value: unknown): string {
  const selected = text(value)
  if (new TextEncoder().encode(selected).byteLength > 32768) {
    throw new TypeError("Query exceeds 32 KiB.")
  }
  return selected
}

export function sortStable<T>(items: readonly T[], compare: (left: T, right: T) => number): T[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort((left, right) => compare(left.item, right.item) || left.index - right.index)
    .map((entry) => entry.item)
}
