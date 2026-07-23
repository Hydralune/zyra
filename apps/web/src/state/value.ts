import type {
  IngressArtifactRef,
  IngressEvent,
  JsonObject,
  JsonValue,
} from "../events/ingress/index.ts"
import {
  EntityLifecycle,
  ProjectionDomain,
  ProjectionStatus,
  type EntityLifecycleValue,
  type ProjectionDomainValue,
  type ProjectionEntity,
  type ProjectionStatusValue,
} from "./contracts.ts"

const encoder = new TextEncoder()

export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

export function jsonRecord(value: unknown): JsonObject {
  if (!isRecord(value)) return {}
  const result: JsonObject = {}
  for (const [key, item] of Object.entries(value)) {
    const normalized = jsonValue(item)
    if (normalized !== undefined) result[key] = normalized
  }
  return result
}

export function jsonValue(value: unknown): JsonValue | undefined {
  if (value === null) return null
  if (typeof value === "string") return value
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "boolean") return value
  if (Array.isArray(value)) {
    const result: JsonValue[] = []
    for (const item of value) {
      const normalized = jsonValue(item)
      if (normalized !== undefined) result.push(normalized)
    }
    return result
  }
  if (isRecord(value)) return jsonRecord(value)
  return undefined
}

export function stringValue(
  value: JsonValue | undefined,
  fallback = "",
): string {
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value)
  }
  return fallback
}

export function optionalString(value: JsonValue | undefined): string | undefined {
  const normalized = stringValue(value).trim()
  return normalized || undefined
}

export function numberValue(
  value: JsonValue | undefined,
  fallback = 0,
): number {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string") {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return fallback
}

export function optionalNumber(value: JsonValue | undefined): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return undefined
}

export function booleanValue(
  value: JsonValue | undefined,
  fallback = false,
): boolean {
  if (typeof value === "boolean") return value
  if (typeof value === "number") return value !== 0
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase()
    if (["true", "yes", "1", "on", "allow", "allowed"].includes(normalized)) {
      return true
    }
    if (["false", "no", "0", "off", "deny", "denied"].includes(normalized)) {
      return false
    }
  }
  return fallback
}

export function arrayValue(value: JsonValue | undefined): readonly JsonValue[] {
  return Array.isArray(value) ? value : []
}

export function stringArray(value: JsonValue | undefined): string[] {
  if (Array.isArray(value)) {
    return uniqueStrings(value.map((item) => stringValue(item)))
  }
  const single = stringValue(value).trim()
  return single ? [single] : []
}

export function objectValue(value: JsonValue | undefined): JsonObject {
  return isRecord(value) ? (value as JsonObject) : {}
}

const DOMAIN_PAYLOAD_KEYS: Readonly<
  Record<ProjectionDomainValue, readonly string[]>
> = Object.freeze({
  [ProjectionDomain.TASK]: Object.freeze([
    "task",
    "task_state",
    "taskState",
  ]),
  [ProjectionDomain.NODE]: Object.freeze([
    "node",
    "plan_node",
    "planNode",
    "topology_node",
  ]),
  [ProjectionDomain.WORKER]: Object.freeze([
    "worker",
    "agent",
    "subagent",
    "worker_state",
  ]),
  [ProjectionDomain.TOOL]: Object.freeze([
    "tool",
    "tool_call",
    "toolCall",
    "browser_action",
  ]),
  [ProjectionDomain.ARTIFACT]: Object.freeze([
    "artifact",
    "file",
    "deliverable",
  ]),
  [ProjectionDomain.MEMORY]: Object.freeze([
    "memory",
    "retrieval",
    "context",
  ]),
  [ProjectionDomain.SCHEDULER]: Object.freeze([
    "scheduler",
    "route",
    "placement",
    "dispatch",
    "decision",
  ]),
  [ProjectionDomain.RECOVERY]: Object.freeze([
    "recovery",
    "failure",
    "fault",
    "plan",
  ]),
  [ProjectionDomain.COMMAND]: Object.freeze([
    "command",
    "control",
    "request",
  ]),
  [ProjectionDomain.PERMISSION]: Object.freeze([
    "permission",
    "approval",
    "question",
    "request",
  ]),
  [ProjectionDomain.OVERLAY]: Object.freeze([
    "overlay",
    "modal",
    "local_jsx",
  ]),
  [ProjectionDomain.SESSION]: Object.freeze([
    "session",
    "conversation",
    "run",
  ]),
  [ProjectionDomain.EVENT]: Object.freeze(["event", "data", "payload"]),
})

const GENERIC_PAYLOAD_KEYS = Object.freeze([
  "payload",
  "data",
  "value",
  "result",
  "event",
])

export function projectionPayload(
  event: IngressEvent,
  domain: ProjectionDomainValue,
): JsonObject {
  let flattened = mergeJson({}, event.inline)
  const candidates = uniqueStrings([
    ...GENERIC_PAYLOAD_KEYS,
    ...DOMAIN_PAYLOAD_KEYS[domain],
  ])
  for (const key of candidates) {
    const nested = objectValue(event.inline[key])
    if (Object.keys(nested).length > 0) {
      flattened = mergeJson(flattened, nested)
    }
  }
  return flattened
}

export function projectionPayloadCandidates(
  event: IngressEvent,
): readonly JsonObject[] {
  const candidates: JsonObject[] = [event.inline, event.metadata]
  const keys = uniqueStrings([
    ...GENERIC_PAYLOAD_KEYS,
    ...Object.values(DOMAIN_PAYLOAD_KEYS).flat(),
  ])
  for (const key of keys) {
    const nestedInline = objectValue(event.inline[key])
    if (Object.keys(nestedInline).length > 0) candidates.push(nestedInline)
    const nestedMetadata = objectValue(event.metadata[key])
    if (Object.keys(nestedMetadata).length > 0) {
      candidates.push(nestedMetadata)
    }
  }
  return candidates
}

export function firstValue(
  source: JsonObject,
  ...keys: readonly string[]
): JsonValue | undefined {
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(source, key)) return source[key]
  }
  return undefined
}

export function firstString(
  source: JsonObject,
  ...keys: readonly string[]
): string | undefined {
  return optionalString(firstValue(source, ...keys))
}

export function firstNumber(
  source: JsonObject,
  ...keys: readonly string[]
): number | undefined {
  return optionalNumber(firstValue(source, ...keys))
}

export function firstBoolean(
  source: JsonObject,
  fallback: boolean,
  ...keys: readonly string[]
): boolean {
  return booleanValue(firstValue(source, ...keys), fallback)
}

export function mergeJson(
  left: Readonly<JsonObject>,
  right: Readonly<JsonObject>,
): JsonObject {
  const result: JsonObject = { ...left }
  for (const [key, value] of Object.entries(right)) {
    if (
      isRecord(result[key]) &&
      isRecord(value)
    ) {
      result[key] = mergeJson(result[key] as JsonObject, value as JsonObject)
      continue
    }
    if (Array.isArray(value)) {
      result[key] = value.map((item) => cloneJson(item))
      continue
    }
    result[key] = cloneJson(value)
  }
  return result
}

export function cloneJson<T extends JsonValue>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => cloneJson(item)) as T
  }
  if (isRecord(value)) {
    const result: JsonObject = {}
    for (const [key, item] of Object.entries(value)) {
      result[key] = cloneJson(item as JsonValue)
    }
    return result as T
  }
  return value
}

export function stableJson(value: unknown): string {
  return stableSerialize(jsonValue(value) ?? null)
}

function stableSerialize(value: JsonValue): string {
  if (value === null) return "null"
  if (typeof value === "string") return JSON.stringify(value)
  if (typeof value === "number" || typeof value === "boolean") {
    return JSON.stringify(value)
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableSerialize(item)).join(",")}]`
  }
  const entries = Object.entries(value).sort(([left], [right]) =>
    left.localeCompare(right),
  )
  return `{${entries
    .map(([key, item]) => `${JSON.stringify(key)}:${stableSerialize(item)}`)
    .join(",")}}`
}

export function hashString(value: string): string {
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  const bytes = encoder.encode(value)
  for (const byte of bytes) {
    first ^= byte
    first = Math.imul(first, 0x01000193)
    second ^= byte + ((second << 6) >>> 0) + (second >>> 2)
    second = Math.imul(second, 0x85ebca6b)
  }
  const left = (first >>> 0).toString(16).padStart(8, "0")
  const right = (second >>> 0).toString(16).padStart(8, "0")
  return `fnv64:${left}${right}`
}

export function checksumJson(value: unknown): string {
  return hashString(stableJson(value))
}

export function uniqueStrings(values: readonly (string | undefined)[]): string[] {
  const result: string[] = []
  const seen = new Set<string>()
  for (const value of values) {
    const normalized = String(value ?? "").trim()
    if (!normalized || seen.has(normalized)) continue
    seen.add(normalized)
    result.push(normalized)
  }
  return result
}

export function insertSortedUnique(
  values: readonly string[],
  value: string,
): string[] {
  if (!value) return [...values]
  let low = 0
  let high = values.length
  while (low < high) {
    const middle = (low + high) >>> 1
    if (values[middle]!.localeCompare(value) < 0) low = middle + 1
    else high = middle
  }
  if (values[low] === value) return [...values]
  const next = [...values]
  next.splice(low, 0, value)
  return next
}

export function removeValue(
  values: readonly string[],
  value: string,
): string[] {
  const index = values.indexOf(value)
  if (index < 0) return [...values]
  const next = [...values]
  next.splice(index, 1)
  return next
}

export function appendBounded(
  values: readonly string[],
  value: string,
  limit: number,
): string[] {
  const next = values.includes(value)
    ? [...values.filter((item) => item !== value), value]
    : [...values, value]
  if (next.length <= limit) return next
  return next.slice(next.length - limit)
}

export function orderedUnion(
  left: readonly string[],
  right: readonly string[],
): string[] {
  return uniqueStrings([...left, ...right])
}

export function parseTimestamp(value: string | undefined, fallback: number): number {
  if (!value) return fallback
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

export function isoTimestamp(value: number): string {
  return new Date(value).toISOString()
}

export function clamp(value: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) return minimum
  return Math.min(maximum, Math.max(minimum, value))
}

export function normalizeLifecycle(value: string | undefined): EntityLifecycleValue {
  const normalized = String(value ?? "").trim().toLowerCase().replaceAll("-", "_")
  switch (normalized) {
    case "queued":
    case "pending":
    case "created":
      return EntityLifecycle.QUEUED
    case "admitted":
    case "accepted":
      return EntityLifecycle.ADMITTED
    case "starting":
    case "initializing":
    case "booting":
      return EntityLifecycle.STARTING
    case "running":
    case "active":
    case "in_progress":
    case "executing":
    case "applied":
      return EntityLifecycle.RUNNING
    case "waiting_tool":
    case "waiting_for_tool":
    case "tool_pending":
      return EntityLifecycle.WAITING_TOOL
    case "waiting_policy":
    case "waiting_permission":
    case "waiting_for_permission":
    case "ask":
      return EntityLifecycle.WAITING_POLICY
    case "paused":
    case "suspended":
      return EntityLifecycle.PAUSED
    case "recovering":
    case "retrying":
    case "replanning":
      return EntityLifecycle.RECOVERING
    case "completed":
    case "complete":
    case "succeeded":
    case "success":
    case "done":
    case "resolved":
    case "allowed":
      return EntityLifecycle.COMPLETED
    case "failed":
    case "error":
    case "errored":
      return EntityLifecycle.FAILED
    case "cancelled":
    case "canceled":
    case "aborted":
      return EntityLifecycle.CANCELLED
    case "rejected":
    case "denied":
      return EntityLifecycle.REJECTED
    case "expired":
    case "timed_out":
    case "timeout":
      return EntityLifecycle.EXPIRED
    case "deleted":
    case "removed":
    case "tombstoned":
      return EntityLifecycle.DELETED
    default:
      return EntityLifecycle.UNKNOWN
  }
}

export function inferLifecycle(event: IngressEvent): EntityLifecycleValue {
  for (const candidate of projectionPayloadCandidates(event)) {
    const explicit = firstString(
      candidate,
      "lifecycle",
      "lifecycle_state",
      "lifecycleState",
      "status",
      "state",
      "phase",
      "result",
      "outcome",
    )
    const normalized = normalizeLifecycle(explicit)
    if (normalized !== EntityLifecycle.UNKNOWN) return normalized
  }
  const haystack = [
    event.eventType,
    event.intent,
    event.stateMutation.operation,
    event.summary,
  ]
    .join(" ")
    .toLowerCase()
  if (/tombstone|delete|removed/.test(haystack)) return EntityLifecycle.DELETED
  if (/reject|denied/.test(haystack)) return EntityLifecycle.REJECTED
  if (/cancel|abort/.test(haystack)) return EntityLifecycle.CANCELLED
  if (/recover|retry|replan|resume/.test(haystack)) return EntityLifecycle.RECOVERING
  if (/fail|error|fault/.test(haystack)) return EntityLifecycle.FAILED
  if (/complete|success|finish|resolved|applied/.test(haystack)) {
    return EntityLifecycle.COMPLETED
  }
  if (/permission|approval|question|policy/.test(haystack)) {
    return EntityLifecycle.WAITING_POLICY
  }
  if (/tool.*wait|waiting.*tool/.test(haystack)) return EntityLifecycle.WAITING_TOOL
  if (/pause|suspend/.test(haystack)) return EntityLifecycle.PAUSED
  if (/start|boot|initial/.test(haystack)) return EntityLifecycle.STARTING
  if (/queue|pending|created/.test(haystack)) return EntityLifecycle.QUEUED
  if (event.terminal) {
    return event.effective ? EntityLifecycle.COMPLETED : EntityLifecycle.FAILED
  }
  return EntityLifecycle.RUNNING
}

export function lifecycleIsTerminal(
  lifecycle: EntityLifecycleValue,
): boolean {
  return (
    lifecycle === EntityLifecycle.COMPLETED ||
    lifecycle === EntityLifecycle.FAILED ||
    lifecycle === EntityLifecycle.CANCELLED ||
    lifecycle === EntityLifecycle.REJECTED ||
    lifecycle === EntityLifecycle.EXPIRED ||
    lifecycle === EntityLifecycle.DELETED
  )
}

export function inferProjectionStatus(event: IngressEvent): ProjectionStatusValue {
  if (event.tombstoneTargetId) return ProjectionStatus.TOMBSTONED
  const settlement = event.settlement.toLowerCase()
  const optimistic =
    booleanValue(event.metadata.optimistic) ||
    booleanValue(event.inline.optimistic) ||
    false
  if (optimistic) return ProjectionStatus.OPTIMISTIC
  if (settlement === "partial") return ProjectionStatus.PARTIAL
  if (event.terminal || settlement === "final") return ProjectionStatus.FINAL
  return ProjectionStatus.AUTHORITATIVE
}

export function inferDomain(event: IngressEvent): ProjectionDomainValue {
  const declared = event.stateMutation.domain.trim().toLowerCase()
  const type = event.eventType.toLowerCase()
  const intent = event.intent.toLowerCase()
  const candidates = [declared, type, intent]
  for (const value of candidates) {
    if (/permission|approval|question/.test(value)) return ProjectionDomain.PERMISSION
    if (/overlay|modal|local-jsx|local_jsx/.test(value)) return ProjectionDomain.OVERLAY
    if (/command|control/.test(value)) return ProjectionDomain.COMMAND
    if (/recovery|recover|fault|failure|retry|replan/.test(value)) {
      return ProjectionDomain.RECOVERY
    }
    if (/scheduler|route|placement|dispatch|resource|model/.test(value)) {
      return ProjectionDomain.SCHEDULER
    }
    if (/memory|retrieval|compact|context/.test(value)) return ProjectionDomain.MEMORY
    if (/artifact|file|diff|output|deliverable/.test(value)) {
      return ProjectionDomain.ARTIFACT
    }
    if (/tool|browser|terminal|mcp|skill/.test(value)) return ProjectionDomain.TOOL
    if (/worker|agent|subagent/.test(value)) return ProjectionDomain.WORKER
    if (/node|topology|graph|edge/.test(value)) return ProjectionDomain.NODE
    if (/session|conversation|run/.test(value)) return ProjectionDomain.SESSION
    if (/task|requirement|goal/.test(value)) return ProjectionDomain.TASK
  }
  if (event.identity.controlCommandId) return ProjectionDomain.COMMAND
  if (event.identity.toolCallId) return ProjectionDomain.TOOL
  if (event.identity.workerId) return ProjectionDomain.WORKER
  if (event.identity.nodeId) return ProjectionDomain.NODE
  if (event.identity.sessionId) return ProjectionDomain.SESSION
  return ProjectionDomain.TASK
}

export function inferEntityId(
  event: IngressEvent,
  domain: ProjectionDomainValue,
): string {
  const inline = event.inline
  const metadata = event.metadata
  const declared =
    firstString(
      inline,
      `${domain}_id`,
      `${domain}Id`,
      "entity_id",
      "entityId",
      "id",
    ) ??
    firstString(
      metadata,
      `${domain}_id`,
      `${domain}Id`,
      "entity_id",
      "entityId",
      "id",
    )
  if (declared) return declared
  switch (domain) {
    case ProjectionDomain.TASK:
      return event.identity.taskId
    case ProjectionDomain.NODE:
      return event.identity.nodeId ?? event.targetId ?? event.aggregateId
    case ProjectionDomain.WORKER:
      return event.identity.workerId ?? event.senderId
    case ProjectionDomain.TOOL:
      return event.identity.toolCallId ?? event.targetId ?? event.eventId
    case ProjectionDomain.ARTIFACT:
      return (
        event.identity.artifactId ??
        event.artifactRefs[0]?.artifactId ??
        event.targetId ??
        event.eventId
      )
    case ProjectionDomain.MEMORY:
      return event.targetId ?? event.aggregateId ?? event.eventId
    case ProjectionDomain.SCHEDULER:
      return (
        firstString(inline, "route_id", "routeId", "placement_id", "placementId") ??
        event.targetId ??
        event.identity.taskId
      )
    case ProjectionDomain.RECOVERY:
      return (
        firstString(inline, "recovery_id", "recoveryId", "failure_id", "failureId") ??
        event.targetId ??
        event.eventId
      )
    case ProjectionDomain.COMMAND:
      return event.identity.controlCommandId ?? event.targetId ?? event.eventId
    case ProjectionDomain.PERMISSION:
      return (
        firstString(inline, "request_id", "requestId", "permission_id", "permissionId") ??
        event.targetId ??
        event.eventId
      )
    case ProjectionDomain.OVERLAY:
      return (
        firstString(inline, "overlay_id", "overlayId") ??
        event.targetId ??
        event.eventId
      )
    case ProjectionDomain.SESSION:
      return event.identity.sessionId ?? event.identity.runId
    default:
      return event.eventId
  }
}

export function eventTitle(event: IngressEvent, fallback: string): string {
  for (const candidate of projectionPayloadCandidates(event)) {
    const title = firstString(candidate, "title", "name", "label", "goal")
    if (title) return title
  }
  return fallback
}

export function eventProgress(event: IngressEvent): number | undefined {
  let value: number | undefined
  for (const candidate of projectionPayloadCandidates(event)) {
    value = firstNumber(
      candidate,
      "progress",
      "progress_percent",
      "progressPercent",
      "percent",
    )
    if (value !== undefined) break
  }
  if (value === undefined) return undefined
  return clamp(value > 1 ? value / 100 : value, 0, 1)
}

export function artifactIds(
  refs: readonly IngressArtifactRef[],
  existing: readonly string[] = [],
): string[] {
  return orderedUnion(
    existing,
    refs.map((item) => item.artifactId),
  )
}

export function baseEntity(
  event: IngressEvent,
  domain: ProjectionDomainValue,
  id: string,
  existing?: ProjectionEntity,
): ProjectionEntity {
  const lifecycle = inferLifecycle(event)
  const status = inferProjectionStatus(event)
  const optimisticKey =
    firstString(event.inline, "optimistic_key", "optimisticKey") ??
    firstString(event.metadata, "optimistic_key", "optimisticKey")
  const parentId =
    event.parentEventId ??
    firstString(event.inline, "parent_id", "parentId") ??
    firstString(event.metadata, "parent_id", "parentId")
  return {
    id,
    domain,
    taskId: event.identity.taskId,
    runId: event.identity.runId,
    sessionId: event.identity.sessionId ?? existing?.sessionId,
    nodeId: event.identity.nodeId ?? existing?.nodeId,
    workerId: event.identity.workerId ?? existing?.workerId,
    lifecycle:
      lifecycle === EntityLifecycle.UNKNOWN
        ? existing?.lifecycle ?? EntityLifecycle.UNKNOWN
        : lifecycle,
    status,
    revision: (existing?.revision ?? 0) + 1,
    sequence: Math.max(existing?.sequence ?? 0, event.globalSequence),
    aggregateSequence: Math.max(
      existing?.aggregateSequence ?? 0,
      event.aggregateSequence,
    ),
    createdAt: existing?.createdAt ?? event.createdAt,
    updatedAt: event.committedAt,
    firstEventId: existing?.firstEventId ?? event.eventId,
    lastEventId: event.eventId,
    correlationId: event.correlationId,
    causationId: event.causationId ?? existing?.causationId,
    spanId: event.identity.spanId ?? existing?.spanId,
    parentSpanId: event.identity.parentSpanId ?? existing?.parentSpanId,
    toolCallId: event.identity.toolCallId ?? existing?.toolCallId,
    artifactIds: artifactIds(event.artifactRefs, existing?.artifactIds),
    checkpointId: event.identity.checkpointId ?? existing?.checkpointId,
    controlCommandId:
      event.identity.controlCommandId ?? existing?.controlCommandId,
    title: eventTitle(event, existing?.title ?? `${domain} ${id}`),
    summary: event.summary || existing?.summary || `${domain} update`,
    progress: eventProgress(event) ?? existing?.progress,
    terminal: event.terminal || lifecycleIsTerminal(lifecycle),
    effective: event.effective,
    optimisticKey: optimisticKey ?? existing?.optimisticKey,
    parentId: parentId ?? existing?.parentId,
    attributes: mergeJson(existing?.attributes ?? {}, event.inline),
    metadata: mergeJson(existing?.metadata ?? {}, event.metadata),
  }
}

export function entityKey(domain: ProjectionDomainValue, id: string): string {
  return `${domain}:${id}`
}

export function tombstoneKey(
  domain: ProjectionDomainValue,
  taskId: string,
  targetId: string,
): string {
  return `${taskId}:${domain}:${targetId}`
}

export function partialKey(event: IngressEvent, entityId: string): string {
  return `${event.identity.taskId}:${entityId}:${event.partId ?? event.eventId}`
}

export function optimisticKey(event: IngressEvent, entityId: string): string {
  return (
    firstString(event.inline, "optimistic_key", "optimisticKey") ??
    firstString(event.metadata, "optimistic_key", "optimisticKey") ??
    `${event.identity.taskId}:${entityId}`
  )
}

export function failureId(event: IngressEvent): string | undefined {
  return (
    firstString(event.inline, "failure_id", "failureId", "fault_id", "faultId") ??
    firstString(event.metadata, "failure_id", "failureId", "fault_id", "faultId") ??
    (/fail|fault|error/.test(event.eventType.toLowerCase())
      ? event.targetId ?? event.eventId
      : undefined)
  )
}

export function recoveryId(event: IngressEvent): string | undefined {
  return (
    firstString(event.inline, "recovery_id", "recoveryId", "plan_id", "planId") ??
    firstString(event.metadata, "recovery_id", "recoveryId", "plan_id", "planId") ??
    (/recover|retry|replan|resume/.test(event.eventType.toLowerCase())
      ? event.targetId ?? event.eventId
      : undefined)
  )
}

export function statePathId(event: IngressEvent): string {
  const path = event.stateMutation.path.join("/")
  return `${event.identity.taskId}:${event.stateMutation.domain}:${path}:${event.globalSequence}`
}
