import type { JsonObject, JsonValue } from "../../../events/ingress/index.ts"
import type {
  CanonicalProjectionState,
  ProjectionEntity,
  TombstoneRecord,
} from "../../../state/contracts.ts"
import type {
  NormalizedTopologyProjectionOptions,
  PlacementLocation,
  ProjectionRecord,
  ResourceVectorView,
  TopologyEntityState,
  TopologyProjectionOptions,
} from "./contracts.ts"

const SENSITIVE_KEY =
  /(?:secret|token|authorization|cookie|password|api[_-]?key|credential[_-]?(?:fingerprint|secret|value)|request[_-]?headers|base[_-]?url)/i
const IDENTIFIER_KEY =
  /(?:^|_)(?:id|ref|key|name|role|kind|type|status|phase|owner|revision|sequence)$/i
const LOCATION_ALIASES = new Map<string, PlacementLocation>([
  ["device", "device"],
  ["terminal", "device"],
  ["desktop", "device"],
  ["browser_device", "device"],
  ["local", "local"],
  ["local_process", "local"],
  ["workspace", "local"],
  ["host", "local"],
  ["edge", "edge"],
  ["edge_only", "edge"],
  ["simulated_edge", "edge"],
  ["isolated_edge", "edge"],
  ["cloud", "cloud"],
  ["cloud_model", "cloud"],
  ["remote", "cloud"],
  ["provider", "cloud"],
])

export function normalizeTopologyOptions(
  options: TopologyProjectionOptions = {},
): NormalizedTopologyProjectionOptions {
  return Object.freeze({
    disabled: options.disabled === true,
    includeRemoved: options.includeRemoved !== false,
    includeRejected: options.includeRejected !== false,
    includeNonEffective: options.includeNonEffective !== false,
    maximumEntities: clampInteger(options.maximumEntities, 25_000, 100, 250_000),
    maximumEvidenceEvents: clampInteger(
      options.maximumEvidenceEvents,
      5_000,
      100,
      100_000,
    ),
    strict: options.strict === true,
  })
}

export function collectProjectionRecords(
  state: CanonicalProjectionState,
  taskId: string,
  options: NormalizedTopologyProjectionOptions,
): ProjectionRecord[] {
  const records: ProjectionRecord[] = []
  appendTable(records, state.tasks, "task", taskId)
  appendTable(records, state.nodes, "node", taskId)
  appendTable(records, state.workers, "worker", taskId)
  appendTable(records, state.schedulers, "scheduler", taskId)
  appendTable(records, state.recoveries, "recovery", taskId)
  appendTable(records, state.sessions, "session", taskId)
  appendTable(records, state.commands, "command", taskId)
  appendTable(records, state.permissions, "permission", taskId)
  appendTable(records, state.tools, "tool", taskId)
  appendTable(records, state.artifacts, "artifact", taskId)
  appendTable(records, state.memories, "memory", taskId)
  appendTombstones(records, state.tombstones, state, taskId)
  const filtered = records.filter(
    (record) =>
      (options.includeRemoved || !record.removed) &&
      (options.includeNonEffective || record.effective),
  )
  filtered.sort(compareProjectionRecords)
  if (filtered.length <= options.maximumEntities) return filtered
  return filtered.slice(filtered.length - options.maximumEntities)
}

function appendTable(
  records: ProjectionRecord[],
  table: Readonly<Record<string, ProjectionEntity>>,
  domain: string,
  taskId: string,
): void {
  for (const entity of Object.values(table)) {
    if (entity.taskId !== taskId) continue
    records.push(recordFromEntity(entity, domain))
  }
}

function recordFromEntity(
  entity: ProjectionEntity,
  domain: string,
): ProjectionRecord {
  return {
    id: `${domain}:${entity.id}`,
    domain,
    entityId: entity.id,
    taskId: entity.taskId,
    runId: entity.runId,
    sequence: entity.sequence,
    revision: entity.revision,
    lifecycle: entity.lifecycle,
    terminal: entity.terminal,
    effective: entity.effective,
    removed: entity.status === "tombstoned" || entity.lifecycle === "deleted",
    title: entity.title,
    summary: entity.summary,
    eventId: entity.lastEventId,
    checkpointId: entity.checkpointId,
    correlationId: entity.correlationId,
    causationId: entity.causationId,
    spanId: entity.spanId,
    parentSpanId: entity.parentSpanId,
    nodeId: entity.nodeId,
    workerId: entity.workerId,
    attributes: entity.attributes,
    metadata: entity.metadata,
    raw: entity,
  }
}

function appendTombstones(
  records: ProjectionRecord[],
  tombstones: Readonly<Record<string, TombstoneRecord>>,
  state: CanonicalProjectionState,
  taskId: string,
): void {
  for (const tombstone of Object.values(tombstones)) {
    if (tombstone.taskId !== taskId) continue
    const event = state.causality.byEvent[tombstone.eventId]
    records.push({
      id: `tombstone:${tombstone.identity}`,
      domain: tombstone.domain,
      entityId: tombstone.targetId,
      taskId,
      runId: event?.runId ?? "",
      sequence: tombstone.sequence,
      revision: tombstone.generation,
      lifecycle: "deleted",
      terminal: true,
      effective: true,
      removed: true,
      title: `Removed ${tombstone.domain} ${tombstone.targetId}`,
      summary: tombstone.reason,
      eventId: tombstone.eventId,
      mutationId: event?.mutationId,
      checkpointId: event?.checkpointId,
      correlationId: event?.correlationId,
      causationId: event?.causationId,
      spanId: event?.spanId,
      parentSpanId: event?.parentSpanId,
      nodeId: event?.nodeId,
      workerId: event?.workerId,
      attributes: Object.freeze({
        target_id: tombstone.targetId,
        target_domain: tombstone.domain,
        reason: tombstone.reason,
      }),
      metadata: Object.freeze({ tombstone: true }),
      raw: tombstone,
    })
  }
}

export function compareProjectionRecords(
  left: ProjectionRecord,
  right: ProjectionRecord,
): number {
  return (
    left.sequence - right.sequence ||
    left.revision - right.revision ||
    left.domain.localeCompare(right.domain) ||
    left.entityId.localeCompare(right.entityId) ||
    left.id.localeCompare(right.id)
  )
}

export function recordCandidates(record: ProjectionRecord): JsonObject[] {
  const output: JsonObject[] = []
  const seen = new Set<JsonObject>()
  const visit = (value: JsonValue | undefined, depth: number) => {
    if (depth > 8 || !isJsonObject(value) || seen.has(value)) return
    seen.add(value)
    output.push(value)
    for (const item of Object.values(value)) {
      if (isJsonObject(item)) visit(item, depth + 1)
      if (Array.isArray(item)) {
        for (const child of item) {
          if (isJsonObject(child)) visit(child, depth + 1)
        }
      }
    }
  }
  visit(record.attributes, 0)
  visit(record.metadata, 0)
  return output
}

export function namedRecords(
  record: ProjectionRecord,
  names: readonly string[],
): JsonObject[] {
  const normalized = new Set(names.map(normalizeKey))
  const output: JsonObject[] = []
  const seen = new Set<JsonObject>()
  const visit = (value: JsonValue | undefined, depth: number) => {
    if (depth > 10 || !isJsonObject(value) || seen.has(value)) return
    seen.add(value)
    for (const [key, child] of Object.entries(value)) {
      if (normalized.has(normalizeKey(key))) {
        if (isJsonObject(child)) output.push(child)
        if (Array.isArray(child)) {
          for (const item of child) {
            if (isJsonObject(item)) output.push(item)
          }
        }
      }
      if (isJsonObject(child)) visit(child, depth + 1)
      if (Array.isArray(child)) {
        for (const item of child) visit(item, depth + 1)
      }
    }
  }
  visit(record.attributes, 0)
  visit(record.metadata, 0)
  return uniqueObjects(output)
}

export function arrayRecords(
  source: JsonObject,
  ...keys: readonly string[]
): JsonObject[] {
  for (const key of keys) {
    const value = valueForAliases(source, [key])
    if (!Array.isArray(value)) continue
    return value.filter(isJsonObject)
  }
  return []
}

export function objectForAliases(
  source: JsonObject,
  aliases: readonly string[],
): JsonObject | undefined {
  const value = valueForAliases(source, aliases)
  return isJsonObject(value) ? value : undefined
}

export function objectsForAliases(
  source: JsonObject,
  aliases: readonly string[],
): JsonObject[] {
  const value = valueForAliases(source, aliases)
  if (isJsonObject(value)) return [value]
  if (Array.isArray(value)) return value.filter(isJsonObject)
  return []
}

export function valueForAliases(
  source: JsonObject,
  aliases: readonly string[],
): JsonValue | undefined {
  const wanted = new Set(aliases.map(normalizeKey))
  for (const [key, value] of Object.entries(source)) {
    if (wanted.has(normalizeKey(key))) return value
  }
  return undefined
}

export function firstString(
  sources: readonly JsonObject[],
  ...aliases: readonly string[]
): string | undefined {
  for (const source of sources) {
    const value = valueForAliases(source, aliases)
    const result = stringFrom(value)
    if (result !== undefined) return result
  }
  return undefined
}

export function firstNumber(
  sources: readonly JsonObject[],
  ...aliases: readonly string[]
): number | undefined {
  for (const source of sources) {
    const value = valueForAliases(source, aliases)
    const result = numberFrom(value)
    if (result !== undefined) return result
  }
  return undefined
}

export function firstInteger(
  sources: readonly JsonObject[],
  ...aliases: readonly string[]
): number | undefined {
  const value = firstNumber(sources, ...aliases)
  return value === undefined ? undefined : Math.trunc(value)
}

export function firstBoolean(
  sources: readonly JsonObject[],
  ...aliases: readonly string[]
): boolean | undefined {
  for (const source of sources) {
    const value = valueForAliases(source, aliases)
    const result = booleanFrom(value)
    if (result !== undefined) return result
  }
  return undefined
}

export function firstStringArray(
  sources: readonly JsonObject[],
  ...aliases: readonly string[]
): string[] {
  for (const source of sources) {
    const value = valueForAliases(source, aliases)
    const output = stringsFrom(value)
    if (output.length > 0) return output
  }
  return []
}

export function mergedStringArrays(
  sources: readonly JsonObject[],
  ...aliases: readonly string[]
): string[] {
  const output: string[] = []
  for (const source of sources) {
    output.push(...stringsFrom(valueForAliases(source, aliases)))
  }
  return uniqueStrings(output)
}

export function nestedString(
  sources: readonly JsonObject[],
  path: readonly string[],
): string | undefined {
  for (const source of sources) {
    let value: JsonValue | undefined = source
    for (const segment of path) {
      if (!isJsonObject(value)) {
        value = undefined
        break
      }
      value = valueForAliases(value, [segment])
    }
    const normalized = stringFrom(value)
    if (normalized !== undefined) return normalized
  }
  return undefined
}

export function nestedNumber(
  sources: readonly JsonObject[],
  path: readonly string[],
): number | undefined {
  for (const source of sources) {
    let value: JsonValue | undefined = source
    for (const segment of path) {
      if (!isJsonObject(value)) {
        value = undefined
        break
      }
      value = valueForAliases(value, [segment])
    }
    const normalized = numberFrom(value)
    if (normalized !== undefined) return normalized
  }
  return undefined
}

export function findRecords(
  roots: readonly JsonObject[],
  predicate: (record: JsonObject, path: readonly string[]) => boolean,
  maximum = 10_000,
): JsonObject[] {
  const output: JsonObject[] = []
  const visited = new Set<JsonObject>()
  const stack = roots.map((value) => ({ value, path: [] as string[], depth: 0 }))
  while (stack.length > 0 && output.length < maximum) {
    const current = stack.pop()!
    if (visited.has(current.value) || current.depth > 12) continue
    visited.add(current.value)
    if (predicate(current.value, current.path)) output.push(current.value)
    const entries = Object.entries(current.value)
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      const [key, value] = entries[index]!
      if (isJsonObject(value)) {
        stack.push({
          value,
          path: [...current.path, key],
          depth: current.depth + 1,
        })
      } else if (Array.isArray(value)) {
        for (let childIndex = value.length - 1; childIndex >= 0; childIndex -= 1) {
          const child = value[childIndex]
          if (!isJsonObject(child)) continue
          stack.push({
            value: child,
            path: [...current.path, key, String(childIndex)],
            depth: current.depth + 1,
          })
        }
      }
    }
  }
  return output
}

export function recordLooksLike(
  record: JsonObject,
  requiredAny: readonly string[],
  requiredAll: readonly string[] = [],
): boolean {
  const keys = new Set(Object.keys(record).map(normalizeKey))
  if (requiredAll.some((key) => !keys.has(normalizeKey(key)))) return false
  return requiredAny.length === 0 || requiredAny.some((key) => keys.has(normalizeKey(key)))
}

export function normalizeEntityState(
  ...values: readonly (string | undefined)[]
): TopologyEntityState {
  for (const value of values) {
    const normalized = normalizeToken(value)
    if (!normalized) continue
    if (["planned", "pending"].includes(normalized)) return "planned"
    if (["ready", "admitted"].includes(normalized)) return "ready"
    if (["queued", "requested"].includes(normalized)) return "queued"
    if (["leased", "assigned", "dispatched"].includes(normalized)) return "leased"
    if (["running", "started", "active"].includes(normalized)) return "running"
    if (["blocked", "parked", "waiting_policy"].includes(normalized)) return "blocked"
    if (["waiting", "waiting_tool"].includes(normalized)) return "waiting"
    if (["interrupted", "paused"].includes(normalized)) return "interrupted"
    if (["resuming", "resume_pending"].includes(normalized)) return "resuming"
    if (["recovering", "retrying"].includes(normalized)) return "recovering"
    if (["needs_revision", "replanned"].includes(normalized)) return "needs_revision"
    if (["succeeded", "success"].includes(normalized)) return "succeeded"
    if (["completed", "complete", "final"].includes(normalized)) return "completed"
    if (["failed", "error", "lost"].includes(normalized)) return "failed"
    if (["cancelled", "canceled"].includes(normalized)) return "cancelled"
    if (["superseded", "replaced"].includes(normalized)) return "superseded"
    if (["removed", "deleted", "tombstoned"].includes(normalized)) return "removed"
    if (["conflicted", "conflict"].includes(normalized)) return "conflicted"
    if (["rejected", "replan_required"].includes(normalized)) return "rejected"
  }
  return "unknown"
}

export function normalizeLocation(
  ...values: readonly (string | undefined)[]
): PlacementLocation {
  for (const value of values) {
    const normalized = normalizeToken(value)
    if (!normalized) continue
    const direct = LOCATION_ALIASES.get(normalized)
    if (direct) return direct
    if (normalized.includes("edge")) return "edge"
    if (normalized.includes("cloud") || normalized.includes("provider")) {
      return "cloud"
    }
    if (normalized.includes("local") || normalized.includes("workspace")) {
      return "local"
    }
    if (normalized.includes("device") || normalized.includes("terminal")) {
      return "device"
    }
  }
  return "unknown"
}

export function resourceVector(
  value: JsonValue | undefined,
): ResourceVectorView {
  const source = isJsonObject(value) ? value : {}
  const customSource = objectForAliases(source, ["custom"]) ?? {}
  const custom: Record<string, number> = {}
  for (const [key, item] of Object.entries(customSource)) {
    const number = numberFrom(item)
    if (number !== undefined) custom[key] = Math.max(0, number)
  }
  return Object.freeze({
    cpuCores: nonNegative(numberForAliases(source, ["cpu_cores", "cpuCores", "cpu"])),
    memoryMb: nonNegative(numberForAliases(source, ["memory_mb", "memoryMb", "memory"])),
    gpuUnits: nonNegative(numberForAliases(source, ["gpu_units", "gpuUnits", "gpu"])),
    diskMb: nonNegative(numberForAliases(source, ["disk_mb", "diskMb", "disk"])),
    networkMbps: nonNegative(
      numberForAliases(source, ["network_mbps", "networkMbps", "network"]),
    ),
    processSlots: nonNegative(
      numberForAliases(source, ["process_slots", "processSlots"]),
    ),
    browserSlots: nonNegative(
      numberForAliases(source, ["browser_slots", "browserSlots"]),
    ),
    custom: Object.freeze(custom),
  })
}

export function vectorSubtract(
  capacity: ResourceVectorView,
  allocated: ResourceVectorView,
): ResourceVectorView {
  const custom: Record<string, number> = {}
  for (const key of uniqueStrings([
    ...Object.keys(capacity.custom),
    ...Object.keys(allocated.custom),
  ])) {
    custom[key] = Math.max(
      0,
      (capacity.custom[key] ?? 0) - (allocated.custom[key] ?? 0),
    )
  }
  return Object.freeze({
    cpuCores: Math.max(0, capacity.cpuCores - allocated.cpuCores),
    memoryMb: Math.max(0, capacity.memoryMb - allocated.memoryMb),
    gpuUnits: Math.max(0, capacity.gpuUnits - allocated.gpuUnits),
    diskMb: Math.max(0, capacity.diskMb - allocated.diskMb),
    networkMbps: Math.max(0, capacity.networkMbps - allocated.networkMbps),
    processSlots: Math.max(0, capacity.processSlots - allocated.processSlots),
    browserSlots: Math.max(0, capacity.browserSlots - allocated.browserSlots),
    custom: Object.freeze(custom),
  })
}

export function vectorFits(
  available: ResourceVectorView,
  requested: ResourceVectorView,
): { fits: boolean; missing: string[]; utilization: Record<string, number> } {
  const missing: string[] = []
  const utilization: Record<string, number> = {}
  const compare = (key: string, have: number, need: number) => {
    if (have < need) missing.push(key)
    utilization[key] = need <= 0 ? 0 : clampRatio(need / Math.max(have, need))
  }
  compare("cpu_cores", available.cpuCores, requested.cpuCores)
  compare("memory_mb", available.memoryMb, requested.memoryMb)
  compare("gpu_units", available.gpuUnits, requested.gpuUnits)
  compare("disk_mb", available.diskMb, requested.diskMb)
  compare("network_mbps", available.networkMbps, requested.networkMbps)
  compare("process_slots", available.processSlots, requested.processSlots)
  compare("browser_slots", available.browserSlots, requested.browserSlots)
  for (const [key, need] of Object.entries(requested.custom)) {
    compare(`custom:${key}`, available.custom[key] ?? 0, need)
  }
  return { fits: missing.length === 0, missing, utilization }
}

export function sanitizeJson(
  value: JsonValue,
  drops: { value: number },
  depth = 0,
): JsonValue {
  if (depth > 16) return "[depth-limited]"
  if (Array.isArray(value)) {
    return value.slice(0, 10_000).map((item) => sanitizeJson(item, drops, depth + 1))
  }
  if (!isJsonObject(value)) return value
  const output: JsonObject = {}
  for (const [key, item] of Object.entries(value)) {
    if (SENSITIVE_KEY.test(key)) {
      drops.value += 1
      continue
    }
    output[key] = sanitizeJson(item, drops, depth + 1)
  }
  return output
}

export function compactAttributes(
  sources: readonly JsonObject[],
  drops: { value: number },
  maximumKeys = 200,
): JsonObject {
  const output: JsonObject = {}
  for (const source of sources) {
    for (const [key, value] of Object.entries(source)) {
      if (Object.keys(output).length >= maximumKeys && !(key in output)) continue
      if (SENSITIVE_KEY.test(key)) {
        drops.value += 1
        continue
      }
      if (IDENTIFIER_KEY.test(key) || isCompactValue(value)) {
        output[key] = sanitizeJson(value, drops)
      }
    }
  }
  return output
}

function isCompactValue(value: JsonValue): boolean {
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "number"
  ) {
    return true
  }
  if (typeof value === "string") return value.length <= 2_048
  if (Array.isArray(value)) {
    return (
      value.length <= 128 &&
      value.every((item) => !isJsonObject(item) && !Array.isArray(item))
    )
  }
  return Object.keys(value).length <= 32
}

export function deepFreeze<T>(value: T, seen = new WeakSet<object>()): T {
  if (value === null || typeof value !== "object") return value
  if (seen.has(value as object)) return value
  seen.add(value as object)
  for (const child of Object.values(value as Record<string, unknown>)) {
    deepFreeze(child, seen)
  }
  return Object.freeze(value)
}

export function stableKey(value: unknown): string {
  return stableSerialize(value, new WeakSet<object>())
}

function stableSerialize(value: unknown, seen: WeakSet<object>): string {
  if (value === null) return "null"
  if (typeof value === "string") return JSON.stringify(value)
  if (typeof value === "number" || typeof value === "boolean") {
    return JSON.stringify(value)
  }
  if (typeof value === "undefined") return "undefined"
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableSerialize(item, seen)).join(",")}]`
  }
  if (typeof value === "object") {
    if (seen.has(value)) return '"[circular]"'
    seen.add(value)
    const entries = Object.entries(value as Record<string, unknown>).sort(
      ([left], [right]) => left.localeCompare(right),
    )
    const serialized = `{${entries
      .map(
        ([key, item]) =>
          `${JSON.stringify(key)}:${stableSerialize(item, seen)}`,
      )
      .join(",")}}`
    seen.delete(value)
    return serialized
  }
  return JSON.stringify(String(value))
}

export function hashKey(value: unknown): string {
  const input = stableKey(value)
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (let index = 0; index < input.length; index += 1) {
    const code = input.charCodeAt(index)
    first ^= code
    first = Math.imul(first, 0x01000193)
    second ^= code + ((second << 6) >>> 0) + (second >>> 2)
    second = Math.imul(second, 0x85ebca6b)
  }
  return `topology:${(first >>> 0).toString(16).padStart(8, "0")}${(
    second >>> 0
  )
    .toString(16)
    .padStart(8, "0")}`
}

export function uniqueStrings(values: readonly (string | undefined)[]): string[] {
  const output: string[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const normalized = String(value ?? "").trim()
    if (!normalized || seen.has(normalized)) continue
    seen.add(normalized)
    output.push(normalized)
  }
  return output
}

export function uniqueNumbers(values: readonly (number | undefined)[]): number[] {
  return [...new Set(values.filter((value): value is number => Number.isFinite(value)))].sort(
    (left, right) => left - right,
  )
}

export function mergeObjects(
  left: Readonly<JsonObject>,
  right: Readonly<JsonObject>,
): JsonObject {
  const output: JsonObject = { ...left }
  for (const [key, value] of Object.entries(right)) {
    const existing = output[key]
    if (isJsonObject(existing) && isJsonObject(value)) {
      output[key] = mergeObjects(existing, value)
    } else if (Array.isArray(value)) {
      output[key] = value.map(cloneJson)
    } else {
      output[key] = cloneJson(value)
    }
  }
  return output
}

export function cloneJson<T extends JsonValue>(value: T): T {
  if (Array.isArray(value)) return value.map(cloneJson) as T
  if (isJsonObject(value)) {
    const output: JsonObject = {}
    for (const [key, child] of Object.entries(value)) {
      output[key] = cloneJson(child)
    }
    return output as T
  }
  return value
}

export function isJsonObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

export function normalizeKey(value: string): string {
  return value.replace(/[^a-zA-Z0-9]/g, "").toLowerCase()
}

export function normalizeToken(value: string | undefined): string {
  return String(value ?? "")
    .trim()
    .replace(/[\s./:-]+/g, "_")
    .toLowerCase()
}

export function stringFrom(value: JsonValue | undefined): string | undefined {
  if (typeof value === "string") {
    const normalized = value.trim()
    return normalized || undefined
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value)
  }
  return undefined
}

export function numberFrom(value: JsonValue | undefined): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return undefined
}

export function booleanFrom(value: JsonValue | undefined): boolean | undefined {
  if (typeof value === "boolean") return value
  if (typeof value === "number") return value !== 0
  if (typeof value !== "string") return undefined
  const normalized = normalizeToken(value)
  if (["true", "yes", "on", "1", "accepted", "committed"].includes(normalized)) {
    return true
  }
  if (["false", "no", "off", "0", "rejected", "discarded"].includes(normalized)) {
    return false
  }
  return undefined
}

export function stringsFrom(value: JsonValue | undefined): string[] {
  if (Array.isArray(value)) {
    return uniqueStrings(
      value.flatMap((item) => {
        const direct = stringFrom(item)
        if (direct) return [direct]
        if (isJsonObject(item)) {
          return [
            stringFrom(valueForAliases(item, ["id", "node_id", "worker_id", "key"])),
          ]
        }
        return []
      }),
    )
  }
  const single = stringFrom(value)
  return single ? [single] : []
}

function numberForAliases(
  source: JsonObject,
  aliases: readonly string[],
): number | undefined {
  return numberFrom(valueForAliases(source, aliases))
}

function nonNegative(value: number | undefined): number {
  return Math.max(0, value ?? 0)
}

function clampRatio(value: number): number {
  return Math.min(1, Math.max(0, Number.isFinite(value) ? value : 0))
}

function clampInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  if (!Number.isFinite(value)) return fallback
  return Math.min(maximum, Math.max(minimum, Math.trunc(value!)))
}

function uniqueObjects(values: readonly JsonObject[]): JsonObject[] {
  const output: JsonObject[] = []
  const seen = new Set<JsonObject>()
  for (const value of values) {
    if (seen.has(value)) continue
    seen.add(value)
    output.push(value)
  }
  return output
}
