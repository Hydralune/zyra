import {
  EVENT_INGRESS_CAPABILITIES_SCHEMA,
  EVENT_INGRESS_DELTA_SCHEMA,
  EVENT_INGRESS_FRAME_SCHEMA,
  EVENT_INGRESS_PROTOCOL,
  EVENT_INGRESS_SNAPSHOT_SCHEMA,
  EventSettlement,
  FrameKind,
  RUNTIME_EVENT_SCHEMA,
  TransportKind,
  encodedJsonBytes,
  freezeEvent,
  type EventIdentity,
  type IngressArtifactRef,
  type IngressCapabilities,
  type IngressCloseFrame,
  type IngressErrorFrame,
  type IngressEvent,
  type IngressEventFrame,
  type IngressFrame,
  type IngressHeartbeatFrame,
  type IngressPage,
  type IngressReadyFrame,
  type JsonObject,
  type JsonValue,
  type StateMutationRef,
  type TransportDescriptor,
  type TransportKindValue,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
  crossTaskError,
  schemaError,
  staleGenerationError,
} from "./errors.ts"

const encoder = new TextEncoder()
const MAX_TEXT_BYTES = 256 * 1024
const MAX_IDENTIFIER_BYTES = 1024
const MAX_OBJECT_DEPTH = 64
const MAX_OBJECT_KEYS = 100_000
const MAX_ARRAY_ITEMS = 1_000_000
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/
const DIGEST = /^(?:sha256:)?[a-fA-F0-9]{32,128}$/

interface ValidationBudget {
  keys: number
  arrays: number
  depth: number
}

function objectValue(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} must be an object.`,
      { context: { details: { label, actualType: typeof value } } },
    )
  }
  return value as Record<string, unknown>
}

function optionalObject(value: unknown, label: string): Record<string, unknown> {
  if (value === undefined || value === null) return {}
  return objectValue(value, label)
}

function arrayValue(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} must be an array.`,
      { context: { details: { label, actualType: typeof value } } },
    )
  }
  return value
}

function requiredString(
  value: unknown,
  label: string,
  maximum = MAX_TEXT_BYTES,
  allowEmpty = false,
): string {
  if (typeof value !== "string") {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} must be a string.`,
      { context: { details: { label, actualType: typeof value } } },
    )
  }
  const normalized = allowEmpty ? value : value.trim()
  if (!allowEmpty && !normalized) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} must not be empty.`,
      { context: { details: { label } } },
    )
  }
  if (encoder.encode(normalized).byteLength > maximum) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} exceeds ${maximum} bytes.`,
      { context: { details: { label, maximum } } },
    )
  }
  if (label.toLowerCase().includes("id") && /[\u0000\r\n]/.test(normalized)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} contains control characters.`,
      { context: { details: { label } } },
    )
  }
  return normalized
}

function identifier(value: unknown, label: string): string {
  return requiredString(value, label, MAX_IDENTIFIER_BYTES)
}

function optionalString(value: unknown, label: string, maximum = MAX_TEXT_BYTES): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return requiredString(value, label, maximum)
}

function integer(
  value: unknown,
  label: string,
  minimum = Number.MIN_SAFE_INTEGER,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} must be a safe integer.`,
      { context: { details: { label, value: String(value) } } },
    )
  }
  if (value < minimum || value > maximum) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} must be between ${minimum} and ${maximum}.`,
      { context: { details: { label, value, minimum, maximum } } },
    )
  }
  return value
}

function finite(
  value: unknown,
  label: string,
  minimum = Number.NEGATIVE_INFINITY,
  maximum = Number.POSITIVE_INFINITY,
): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} must be finite.`,
      { context: { details: { label, value: String(value) } } },
    )
  }
  if (value < minimum || value > maximum) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} must be between ${minimum} and ${maximum}.`,
      { context: { details: { label, value, minimum, maximum } } },
    )
  }
  return value
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      `${label} must be a boolean.`,
      { context: { details: { label, actualType: typeof value } } },
    )
  }
  return value
}

function timestamp(value: unknown, label: string): string {
  const rendered = requiredString(value, label, 128)
  const parsed = Date.parse(rendered)
  if (!ISO_TIMESTAMP.test(rendered) || !Number.isFinite(parsed)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_EVENT,
      `${label} must be an ISO timestamp.`,
      { context: { details: { label, value: rendered } } },
    )
  }
  return new Date(parsed).toISOString()
}

function jsonValue(
  value: unknown,
  label: string,
  budget: ValidationBudget = { keys: 0, arrays: 0, depth: 0 },
): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    if (typeof value === "string" && encoder.encode(value).byteLength > MAX_TEXT_BYTES) {
      throw new EventIngressError(
        IngressErrorCode.INVALID_EVENT,
        `${label} contains a string larger than ${MAX_TEXT_BYTES} bytes.`,
      )
    }
    return value
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new EventIngressError(
        IngressErrorCode.INVALID_EVENT,
        `${label} contains a non-finite number.`,
      )
    }
    return value
  }
  if (budget.depth >= MAX_OBJECT_DEPTH) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_EVENT,
      `${label} exceeds maximum object depth.`,
    )
  }
  if (Array.isArray(value)) {
    budget.arrays += value.length
    if (budget.arrays > MAX_ARRAY_ITEMS) {
      throw new EventIngressError(
        IngressErrorCode.INVALID_EVENT,
        `${label} exceeds the aggregate array item limit.`,
      )
    }
    return value.map((item, index) =>
      jsonValue(item, `${label}[${index}]`, { ...budget, depth: budget.depth + 1 }),
    )
  }
  if (value && typeof value === "object") {
    const result: JsonObject = {}
    const entries = Object.entries(value as Record<string, unknown>)
    budget.keys += entries.length
    if (budget.keys > MAX_OBJECT_KEYS) {
      throw new EventIngressError(
        IngressErrorCode.INVALID_EVENT,
        `${label} exceeds the aggregate object key limit.`,
      )
    }
    for (const [key, item] of entries) {
      if (encoder.encode(key).byteLength > MAX_IDENTIFIER_BYTES) {
        throw new EventIngressError(
          IngressErrorCode.INVALID_EVENT,
          `${label} contains an oversized key.`,
        )
      }
      result[key] = jsonValue(item, `${label}.${key}`, {
        ...budget,
        depth: budget.depth + 1,
      })
    }
    return result
  }
  throw new EventIngressError(
    IngressErrorCode.INVALID_EVENT,
    `${label} contains a non-JSON value.`,
    { context: { details: { label, actualType: typeof value } } },
  )
}

function jsonObject(value: unknown, label: string): JsonObject {
  const normalized = jsonValue(objectValue(value, label), label)
  if (!normalized || Array.isArray(normalized) || typeof normalized !== "object") {
    throw new EventIngressError(IngressErrorCode.INVALID_EVENT, `${label} must be an object.`)
  }
  return normalized as JsonObject
}

function stringArray(value: unknown, label: string): readonly string[] {
  if (value === undefined || value === null) return Object.freeze([])
  const result = arrayValue(value, label).map((item, index) =>
    requiredString(item, `${label}[${index}]`, MAX_IDENTIFIER_BYTES),
  )
  return Object.freeze(result)
}

function firstString(record: Record<string, unknown>, keys: readonly string[]): string | undefined {
  for (const key of keys) {
    const value = record[key]
    if (typeof value === "string" && value.trim()) return value.trim()
  }
  return undefined
}

function nestedString(
  record: Record<string, unknown>,
  container: string,
  keys: readonly string[],
): string | undefined {
  return firstString(optionalObject(record[container], container), keys)
}

function normalizeIdentity(
  value: Record<string, unknown>,
  raw: Record<string, unknown>,
  expectedTaskId: string,
): EventIdentity {
  const inline = optionalObject(raw.inline, "event.inline")
  const metadata = optionalObject(raw.metadata, "event.metadata")
  const taskId =
    firstString(value, ["taskId", "task_id"]) ??
    firstString(raw, ["taskId", "task_id"]) ??
    expectedTaskId
  if (taskId !== expectedTaskId) throw crossTaskError(expectedTaskId, taskId)
  const runId =
    firstString(value, ["runId", "run_id"]) ??
    firstString(raw, ["runId", "run_id"]) ??
    firstString(inline, ["run_id", "runId"])
  if (!runId) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_EVENT,
      "Runtime event identity omitted runId.",
      { context: { taskId: expectedTaskId } },
    )
  }
  return Object.freeze({
    taskId,
    runId: identifier(runId, "event.identity.runId"),
    sessionId: firstString(value, ["sessionId", "session_id"]),
    nodeId: firstString(value, ["nodeId", "node_id"]),
    workerId: firstString(value, ["workerId", "worker_id"]),
    spanId:
      firstString(value, ["spanId", "span_id"]) ??
      firstString(inline, ["span_id", "spanId"]) ??
      firstString(metadata, ["span_id", "spanId"]),
    parentSpanId:
      firstString(value, ["parentSpanId", "parent_span_id"]) ??
      firstString(inline, ["parent_span_id", "parentSpanId"]) ??
      firstString(metadata, ["parent_span_id", "parentSpanId"]),
    toolCallId:
      firstString(value, ["toolCallId", "tool_call_id"]) ??
      firstString(inline, ["tool_call_id", "toolCallId"]),
    artifactId:
      firstString(value, ["artifactId", "artifact_id"]) ??
      firstString(inline, ["artifact_id", "artifactId"]),
    checkpointId:
      firstString(value, ["checkpointId", "checkpoint_id"]) ??
      firstString(inline, ["checkpoint_id", "checkpointId"]),
    controlCommandId:
      firstString(value, ["commandId", "controlCommandId", "command_id", "control_command_id"]) ??
      firstString(inline, ["command_id", "control_command_id", "controlCommandId"]),
  })
}

function normalizeStateMutation(value: unknown): StateMutationRef {
  const record = optionalObject(value, "event.stateDelta")
  const operation = firstString(record, ["operation"]) ?? "none"
  const domain = firstString(record, ["domain"]) ?? "runtime"
  const path = stringArray(record.path ?? [], "event.stateDelta.path")
  const effective =
    typeof record.effective === "boolean"
      ? record.effective
      : operation !== "none"
  return Object.freeze({
    domain: requiredString(domain, "event.stateDelta.domain", MAX_IDENTIFIER_BYTES),
    operation: requiredString(operation, "event.stateDelta.operation", MAX_IDENTIFIER_BYTES),
    path,
    beforeDigest: optionalString(record.beforeDigest ?? record.before_digest, "event.stateDelta.beforeDigest", 256),
    afterDigest: optionalString(record.afterDigest ?? record.after_digest, "event.stateDelta.afterDigest", 256),
    effective,
  })
}

function normalizeArtifact(value: unknown, index: number): IngressArtifactRef {
  const record = objectValue(value, `event.artifactRefs[${index}]`)
  const digest = requiredString(
    record.digest ?? record.sha256,
    `event.artifactRefs[${index}].digest`,
    256,
  )
  if (!DIGEST.test(digest)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_EVENT,
      `event.artifactRefs[${index}].digest is invalid.`,
    )
  }
  return Object.freeze({
    artifactId: identifier(
      record.artifactId ?? record.artifact_id,
      `event.artifactRefs[${index}].artifactId`,
    ),
    digest,
    mediaType: requiredString(
      record.mediaType ?? record.media_type ?? "application/octet-stream",
      `event.artifactRefs[${index}].mediaType`,
      1024,
    ),
    sizeBytes: integer(
      record.sizeBytes ?? record.byteLength ?? record.size_bytes ?? 0,
      `event.artifactRefs[${index}].sizeBytes`,
      0,
    ),
    title: requiredString(
      record.title ?? record.role ?? "artifact",
      `event.artifactRefs[${index}].title`,
      8192,
    ),
    uri: optionalString(record.uri, `event.artifactRefs[${index}].uri`, 64 * 1024),
  })
}

function normalizeArtifacts(value: unknown): readonly IngressArtifactRef[] {
  if (value === undefined || value === null) return Object.freeze([])
  const result = arrayValue(value, "event.artifactRefs").map(normalizeArtifact)
  return Object.freeze(result)
}

function inferSettlement(
  eventType: string,
  inline: Record<string, unknown>,
  metadata: Record<string, unknown>,
): {
  settlement: IngressEvent["settlement"]
  partId?: string
  parentEventId?: string
  tombstoneTargetId?: string
} {
  const explicit = firstString(metadata, ["settlement", "stream_settlement"])
  const lower = eventType.toLowerCase()
  const partial =
    explicit === EventSettlement.PARTIAL ||
    lower.endsWith(".partial") ||
    lower.endsWith(".delta") ||
    lower.includes("stream_delta") ||
    metadata.partial === true
  const final =
    explicit === EventSettlement.FINAL ||
    lower.endsWith(".final") ||
    lower.endsWith(".completed") ||
    metadata.final === true
  const tombstone =
    explicit === EventSettlement.TOMBSTONE ||
    lower.endsWith(".removed") ||
    lower.endsWith(".deleted") ||
    lower.includes("tombstone") ||
    metadata.tombstone === true
  const settlement = tombstone
    ? EventSettlement.TOMBSTONE
    : final
      ? EventSettlement.FINAL
      : partial
        ? EventSettlement.PARTIAL
        : EventSettlement.ATOMIC
  return {
    settlement,
    partId:
      firstString(metadata, ["part_id", "partId", "chunk_id", "chunkId"]) ??
      firstString(inline, ["part_id", "partId", "chunk_id", "chunkId"]),
    parentEventId:
      firstString(metadata, ["parent_event_id", "parentEventId"]) ??
      firstString(inline, ["parent_event_id", "parentEventId"]),
    tombstoneTargetId:
      firstString(metadata, ["tombstone_target_id", "tombstoneTargetId", "removed_event_id"]) ??
      firstString(inline, ["tombstone_target_id", "tombstoneTargetId", "removed_event_id"]),
  }
}

function terminalEvent(
  eventType: string,
  stateMutation: StateMutationRef,
  inline: Record<string, unknown>,
  settlement: IngressEvent["settlement"],
): boolean {
  if (settlement === EventSettlement.FINAL) return true
  if (inline.terminal === true) return true
  const lower = eventType.toLowerCase()
  if (
    [
      ".completed",
      ".failed",
      ".cancelled",
      ".denied",
      ".rejected",
      ".timed_out",
      ".settled",
    ].some((suffix) => lower.endsWith(suffix))
  ) return true
  if (stateMutation.operation === "transition") {
    const value = String(inline.status ?? inline.state ?? "").toLowerCase()
    return ["completed", "failed", "cancelled", "rejected", "timed_out"].includes(value)
  }
  return false
}

export function normalizeIngressEvent(
  value: unknown,
  expectedTaskId: string,
): IngressEvent {
  const raw = objectValue(value, "event")
  if (raw.schema !== RUNTIME_EVENT_SCHEMA) {
    throw schemaError(raw.schema, RUNTIME_EVENT_SCHEMA, { taskId: expectedTaskId })
  }
  const eventId = identifier(raw.eventId, "event.eventId")
  const eventType = requiredString(raw.eventType, "event.eventType", MAX_IDENTIFIER_BYTES)
  const identity = normalizeIdentity(
    optionalObject(raw.identity, "event.identity"),
    raw,
    expectedTaskId,
  )
  const sender = optionalObject(raw.sender, "event.sender")
  const target = optionalObject(raw.target, "event.target")
  const provenance = optionalObject(raw.provenance, "event.provenance")
  const inline = jsonObject(raw.inline ?? raw.payload ?? {}, "event.inline")
  const metadata = jsonObject(raw.metadata ?? {}, "event.metadata")
  const stateMutation = normalizeStateMutation(raw.stateDelta ?? raw.stateMutation ?? {})
  const artifactRefs = normalizeArtifacts(raw.artifactRefs ?? raw.artifacts ?? [])
  const settlement = inferSettlement(
    eventType,
    inline as Record<string, unknown>,
    metadata as Record<string, unknown>,
  )
  const effect = requiredString(raw.effect ?? "unknown", "event.effect", 128)
  const effective =
    effect === "effective" ||
    stateMutation.effective ||
    (effect === "unknown" && stateMutation.operation !== "none")
  const contentDigest = requiredString(raw.contentDigest, "event.contentDigest", 256)
  if (!DIGEST.test(contentDigest)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_EVENT,
      "event.contentDigest is invalid.",
      { context: { taskId: expectedTaskId, eventId } },
    )
  }
  const event: IngressEvent = {
    schema: RUNTIME_EVENT_SCHEMA,
    eventId,
    eventType,
    eventVersion: integer(raw.eventVersion ?? raw.schemaVersion ?? 1, "event.eventVersion", 1),
    aggregateId: identifier(raw.aggregateId, "event.aggregateId"),
    aggregateSequence: integer(raw.aggregateSequence, "event.aggregateSequence", 0),
    globalSequence: integer(raw.globalSequence, "event.globalSequence", 1),
    producerSequence: integer(raw.producerSequence ?? 0, "event.producerSequence", 0),
    idempotencyKey: identifier(raw.idempotencyKey, "event.idempotencyKey"),
    correlationId: identifier(raw.correlationId, "event.correlationId"),
    causationId: optionalString(raw.causationId, "event.causationId", MAX_IDENTIFIER_BYTES),
    createdAt: timestamp(raw.createdAt ?? raw.occurredAt, "event.createdAt"),
    committedAt: timestamp(raw.committedAt, "event.committedAt"),
    durability: requiredString(raw.durability ?? "durable", "event.durability", 128),
    effect,
    identity,
    senderKind: requiredString(sender.kind ?? "runtime", "event.sender.kind", 128),
    senderId: identifier(sender.id ?? provenance.sourceModule ?? "runtime", "event.sender.id"),
    intent: requiredString(raw.intent ?? "status", "event.intent", 128),
    targetKind: optionalString(target.kind, "event.target.kind", 128),
    targetId: optionalString(target.id, "event.target.id", MAX_IDENTIFIER_BYTES),
    summary: requiredString(raw.summary ?? "", "event.summary", MAX_TEXT_BYTES, true),
    stateMutation,
    artifactRefs,
    inline,
    metadata,
    contentDigest,
    sourceBytes: integer(raw.sourceBytes ?? 0, "event.sourceBytes", 0),
    inlineBytes: integer(raw.inlineBytes ?? encodedJsonBytes(inline), "event.inlineBytes", 0),
    envelopeBytes: integer(
      raw.envelopeBytes ?? encodedJsonBytes(raw),
      "event.envelopeBytes",
      1,
    ),
    ...settlement,
    terminal: terminalEvent(
      eventType,
      stateMutation,
      inline as Record<string, unknown>,
      settlement.settlement,
    ),
    effective,
    raw: jsonObject(raw, "event.raw"),
  }
  if (event.committedAt < event.createdAt) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_EVENT,
      "event.committedAt precedes event.createdAt.",
      { context: { taskId: expectedTaskId, eventId } },
    )
  }
  if (
    event.settlement === EventSettlement.TOMBSTONE &&
    !event.tombstoneTargetId
  ) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_EVENT,
      "Tombstone event omitted its target identity.",
      { context: { taskId: expectedTaskId, eventId } },
    )
  }
  return freezeEvent(event)
}

function transportKind(value: unknown, label: string): TransportKindValue {
  const kind = requiredString(value, label, 128) as TransportKindValue
  if (!Object.values(TransportKind).includes(kind)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_CAPABILITIES,
      `${label} is unsupported.`,
      { context: { details: { kind } } },
    )
  }
  return kind
}

function pathValue(value: unknown, label: string): string {
  const path = requiredString(value, label, 4096)
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("\\") || path.includes("\0")) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_CAPABILITIES,
      `${label} must be a safe absolute API path.`,
    )
  }
  const segments = path.split("/")
  if (segments.includes(".") || segments.includes("..")) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_CAPABILITIES,
      `${label} contains path traversal.`,
    )
  }
  return path
}

function normalizeTransport(value: unknown, index: number): TransportDescriptor {
  const item = objectValue(value, `capabilities.transports[${index}]`)
  const cursorMode = requiredString(
    item.cursorMode ?? "query",
    `capabilities.transports[${index}].cursorMode`,
    128,
  )
  if (!["query", "header", "message"].includes(cursorMode)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_CAPABILITIES,
      "Transport cursorMode is unsupported.",
    )
  }
  return Object.freeze({
    kind: transportKind(item.kind, `capabilities.transports[${index}].kind`),
    available: booleanValue(item.available, `capabilities.transports[${index}].available`),
    priority: integer(item.priority, `capabilities.transports[${index}].priority`, 0, 10_000),
    path: pathValue(item.path, `capabilities.transports[${index}].path`),
    cursorMode: cursorMode as TransportDescriptor["cursorMode"],
    customHeaders: item.customHeaders === undefined
      ? false
      : booleanValue(item.customHeaders, `capabilities.transports[${index}].customHeaders`),
    reason: optionalString(item.reason, `capabilities.transports[${index}].reason`, 8192),
    protocols: stringArray(item.protocols ?? [], `capabilities.transports[${index}].protocols`),
  })
}

export function normalizeCapabilities(
  value: unknown,
  expectedTaskId: string,
): IngressCapabilities {
  const body = objectValue(value, "event ingress capabilities")
  if (body.schema !== EVENT_INGRESS_CAPABILITIES_SCHEMA) {
    throw schemaError(body.schema, EVENT_INGRESS_CAPABILITIES_SCHEMA, {
      taskId: expectedTaskId,
    })
  }
  if (body.protocol !== EVENT_INGRESS_PROTOCOL) {
    throw schemaError(body.protocol, EVENT_INGRESS_PROTOCOL, {
      taskId: expectedTaskId,
    })
  }
  const taskId = identifier(body.taskId, "capabilities.taskId")
  if (taskId !== expectedTaskId) throw crossTaskError(expectedTaskId, taskId)
  if (body.canonicalWriteAllowed !== false) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_CAPABILITIES,
      "Event ingress capabilities must deny canonical writes.",
      { context: { taskId } },
    )
  }
  const endpoints = objectValue(body.endpoints, "capabilities.endpoints")
  const limits = objectValue(body.limits, "capabilities.limits")
  const schemas = objectValue(body.schemas, "capabilities.schemas")
  const filter = optionalObject(body.filter, "capabilities.filter")
  const transports = arrayValue(body.transports, "capabilities.transports")
    .map(normalizeTransport)
    .sort((left, right) => left.priority - right.priority || left.kind.localeCompare(right.kind))
  const available = transports.filter((item) => item.available)
  if (!available.length) {
    throw new EventIngressError(
      IngressErrorCode.TRANSPORT_UNAVAILABLE,
      "Server advertised no available event ingress transport.",
      { context: { taskId }, retryable: true },
    )
  }
  if (!available.some((item) => item.kind === TransportKind.LONG_POLL)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_CAPABILITIES,
      "Server must advertise long polling as the protocol-equivalent fallback.",
      { context: { taskId } },
    )
  }
  const result: IngressCapabilities = {
    schema: EVENT_INGRESS_CAPABILITIES_SCHEMA,
    protocol: EVENT_INGRESS_PROTOCOL,
    taskId,
    canonicalOwner: requiredString(body.canonicalOwner, "capabilities.canonicalOwner", 1024),
    ingressOwner: requiredString(body.ingressOwner, "capabilities.ingressOwner", 1024),
    canonicalWriteAllowed: false,
    snapshotRequired: booleanValue(body.snapshotRequired, "capabilities.snapshotRequired"),
    subscribeBeforeSnapshot: booleanValue(
      body.subscribeBeforeSnapshot,
      "capabilities.subscribeBeforeSnapshot",
    ),
    subscriptionCursor: requiredString(
      body.subscriptionCursor,
      "capabilities.subscriptionCursor",
      4096,
    ),
    subscriptionSequence: integer(
      body.subscriptionSequence,
      "capabilities.subscriptionSequence",
      0,
    ),
    subscriptionBoundary: integer(
      body.subscriptionBoundary,
      "capabilities.subscriptionBoundary",
      0,
    ),
    generation: integer(body.generation, "capabilities.generation", 1),
    transports: Object.freeze(transports),
    endpoints: Object.freeze({
      capabilities: pathValue(endpoints.capabilities, "capabilities.endpoints.capabilities"),
      snapshot: pathValue(endpoints.snapshot, "capabilities.endpoints.snapshot"),
      delta: pathValue(endpoints.delta, "capabilities.endpoints.delta"),
      sse: pathValue(endpoints.sse, "capabilities.endpoints.sse"),
      websocket: pathValue(endpoints.websocket, "capabilities.endpoints.websocket"),
    }),
    limits: Object.freeze({
      defaultPage: integer(limits.defaultPage, "capabilities.limits.defaultPage", 1, 1000),
      maxPage: integer(limits.maxPage, "capabilities.limits.maxPage", 1, 1000),
      defaultWaitMs: integer(limits.defaultWaitMs, "capabilities.limits.defaultWaitMs", 0, 60_000),
      maxWaitMs: integer(limits.maxWaitMs, "capabilities.limits.maxWaitMs", 0, 60_000),
      defaultStreamMs: integer(
        limits.defaultStreamMs,
        "capabilities.limits.defaultStreamMs",
        1,
        10 * 60_000,
      ),
      maxStreamMs: integer(
        limits.maxStreamMs,
        "capabilities.limits.maxStreamMs",
        1,
        10 * 60_000,
      ),
      cursorTtlMs: integer(
        limits.cursorTtlMs,
        "capabilities.limits.cursorTtlMs",
        1_000,
        24 * 60 * 60_000,
      ),
    }),
    schemas: Object.freeze({
      cursor: requiredString(schemas.cursor, "capabilities.schemas.cursor", 256),
      frame: requiredString(schemas.frame, "capabilities.schemas.frame", 256),
      snapshot: requiredString(schemas.snapshot, "capabilities.schemas.snapshot", 256),
      delta: requiredString(schemas.delta, "capabilities.schemas.delta", 256),
      event: requiredString(schemas.event, "capabilities.schemas.event", 256),
    }),
    filterDigest: requiredString(filter.digest ?? "", "capabilities.filter.digest", 256, true),
  }
  if (!result.snapshotRequired || !result.subscribeBeforeSnapshot) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_CAPABILITIES,
      "Server must require subscribe-before-snapshot ingestion.",
      { context: { taskId } },
    )
  }
  if (
    result.schemas.frame !== EVENT_INGRESS_FRAME_SCHEMA ||
    result.schemas.snapshot !== EVENT_INGRESS_SNAPSHOT_SCHEMA ||
    result.schemas.delta !== EVENT_INGRESS_DELTA_SCHEMA ||
    result.schemas.event !== RUNTIME_EVENT_SCHEMA
  ) {
    throw new EventIngressError(
      IngressErrorCode.SCHEMA_UNSUPPORTED,
      "Server advertised an unsupported event ingress schema set.",
      {
        resyncRequired: true,
        context: { taskId, details: { schemas: { ...result.schemas } } },
      },
    )
  }
  return Object.freeze(result)
}

export function normalizeEventFrame(
  value: unknown,
  expectedTaskId: string,
  expectedGeneration?: number,
): IngressEventFrame {
  const frame = objectValue(value, "event frame")
  if (frame.schema !== EVENT_INGRESS_FRAME_SCHEMA) {
    throw schemaError(frame.schema, EVENT_INGRESS_FRAME_SCHEMA, {
      taskId: expectedTaskId,
    })
  }
  if (frame.kind !== FrameKind.EVENT) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      "Expected an event frame.",
      { context: { taskId: expectedTaskId } },
    )
  }
  const taskId = identifier(frame.taskId, "frame.taskId")
  if (taskId !== expectedTaskId) throw crossTaskError(expectedTaskId, taskId)
  const generation = integer(frame.generation, "frame.generation", 1)
  if (expectedGeneration !== undefined && generation !== expectedGeneration) {
    throw staleGenerationError(expectedGeneration, generation, { taskId })
  }
  const event = normalizeIngressEvent(frame.event, taskId)
  const sequence = integer(frame.sequence, "frame.sequence", 1)
  if (event.globalSequence !== sequence) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      "Frame sequence does not match the canonical event sequence.",
      {
        resyncRequired: true,
        context: {
          taskId,
          eventId: event.eventId,
          sequence,
          details: { eventSequence: event.globalSequence },
        },
      },
    )
  }
  const eventId = identifier(frame.eventId, "frame.eventId")
  if (event.eventId !== eventId) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      "Frame eventId does not match its canonical event.",
      {
        resyncRequired: true,
        context: { taskId, eventId, sequence },
      },
    )
  }
  const eventType = requiredString(frame.eventType, "frame.eventType", MAX_IDENTIFIER_BYTES)
  if (event.eventType !== eventType) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      "Frame eventType does not match its canonical event.",
      {
        resyncRequired: true,
        context: { taskId, eventId, sequence },
      },
    )
  }
  const correlationId = identifier(frame.correlationId, "frame.correlationId")
  if (event.correlationId !== correlationId) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      "Frame correlationId does not match its canonical event.",
      {
        resyncRequired: true,
        context: { taskId, eventId, sequence },
      },
    )
  }
  const causationId = optionalString(frame.causationId, "frame.causationId", MAX_IDENTIFIER_BYTES)
  if ((event.causationId ?? undefined) !== causationId) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      "Frame causationId does not match its canonical event.",
      {
        resyncRequired: true,
        context: { taskId, eventId, sequence },
      },
    )
  }
  const normalized: IngressEventFrame = {
    schema: EVENT_INGRESS_FRAME_SCHEMA,
    kind: FrameKind.EVENT,
    source: requiredString(frame.source ?? "unknown", "frame.source", 256),
    generation,
    taskId,
    sequence,
    previousSequence: integer(frame.previousSequence, "frame.previousSequence", 0),
    eventId,
    eventType,
    correlationId,
    causationId,
    observedAtMs: integer(frame.observedAtMs, "frame.observedAtMs", 0),
    cursor: optionalString(frame.cursor, "frame.cursor", 4096),
    event,
    encodedBytes: encodedJsonBytes(frame),
  }
  if (normalized.previousSequence >= normalized.sequence) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_FRAME,
      "Frame previousSequence must precede its sequence.",
      {
        resyncRequired: true,
        context: {
          taskId,
          eventId,
          sequence,
          previousSequence: normalized.previousSequence,
        },
      },
    )
  }
  return Object.freeze(normalized)
}

export function normalizePage(
  value: unknown,
  expectedTaskId: string,
  expectedGeneration: number,
): IngressPage {
  const body = objectValue(value, "event ingress page")
  const schemas = [EVENT_INGRESS_SNAPSHOT_SCHEMA, EVENT_INGRESS_DELTA_SCHEMA] as const
  if (!schemas.includes(body.schema as (typeof schemas)[number])) {
    throw schemaError(body.schema, schemas, { taskId: expectedTaskId })
  }
  if (body.protocol !== EVENT_INGRESS_PROTOCOL) {
    throw schemaError(body.protocol, EVENT_INGRESS_PROTOCOL, { taskId: expectedTaskId })
  }
  const taskId = identifier(body.taskId, "page.taskId")
  if (taskId !== expectedTaskId) throw crossTaskError(expectedTaskId, taskId)
  const generation = integer(body.generation, "page.generation", 1)
  if (generation !== expectedGeneration) {
    throw staleGenerationError(expectedGeneration, generation, { taskId })
  }
  if (body.canonicalWriteAllowed !== false) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_PAGE,
      "Event ingress page must deny canonical writes.",
      { context: { taskId, generation } },
    )
  }
  const fromSequence = integer(body.fromSequence, "page.fromSequence", 0)
  const nextSequence = integer(body.nextSequence, "page.nextSequence", 0)
  if (nextSequence < fromSequence) {
    throw new EventIngressError(
      IngressErrorCode.CURSOR_REGRESSION,
      "Event ingress page regressed its cursor.",
      {
        resyncRequired: true,
        context: { taskId, generation, sequence: nextSequence, previousSequence: fromSequence },
      },
    )
  }
  const frames = arrayValue(body.frames, "page.frames").map((frame) =>
    normalizeEventFrame(frame, taskId, generation),
  )
  let previous = fromSequence
  const ids = new Set<string>()
  for (const frame of frames) {
    if (frame.previousSequence !== previous) {
      throw new EventIngressError(
        IngressErrorCode.GAP_DETECTED,
        "Page frame predecessor chain is not contiguous.",
        {
          resyncRequired: true,
          context: {
            taskId,
            generation,
            eventId: frame.eventId,
            sequence: frame.sequence,
            previousSequence: frame.previousSequence,
            details: { expectedPrevious: previous },
          },
        },
      )
    }
    if (ids.has(frame.eventId)) {
      throw new EventIngressError(
        IngressErrorCode.DUPLICATE_CONFLICT,
        "Page contains a duplicate event identity.",
        { resyncRequired: true, context: { taskId, generation, eventId: frame.eventId } },
      )
    }
    ids.add(frame.eventId)
    previous = frame.sequence
  }
  if (frames.length && nextSequence < frames[frames.length - 1]!.sequence) {
    throw new EventIngressError(
      IngressErrorCode.CURSOR_REGRESSION,
      "Page cursor precedes its last frame.",
      { resyncRequired: true, context: { taskId, generation, sequence: nextSequence } },
    )
  }
  const cursor = requiredString(body.cursor, "page.cursor", 4096)
  const cursorKind = requiredString(body.cursorKind, "page.cursorKind", 128)
  if (!["snapshot", "delta", "stream"].includes(cursorKind)) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_PAGE,
      "Page cursorKind is unsupported.",
      { context: { taskId, generation } },
    )
  }
  const schema = body.schema as IngressPage["schema"]
  const hasMore = booleanValue(body.hasMore, "page.hasMore")
  const complete =
    schema === EVENT_INGRESS_SNAPSHOT_SCHEMA
      ? booleanValue(body.complete, "page.complete")
      : undefined
  if (schema === EVENT_INGRESS_SNAPSHOT_SCHEMA) {
    if (complete === hasMore) {
      throw new EventIngressError(
        IngressErrorCode.INVALID_PAGE,
        "Snapshot complete and hasMore flags are inconsistent.",
        { context: { taskId, generation } },
      )
    }
    if (complete && cursorKind !== "delta") {
      throw new EventIngressError(
        IngressErrorCode.INVALID_PAGE,
        "A complete snapshot must promote its cursor to delta.",
        { context: { taskId, generation } },
      )
    }
  }
  const page: IngressPage = {
    schema,
    protocol: EVENT_INGRESS_PROTOCOL,
    taskId,
    generation,
    fromSequence,
    nextSequence,
    cursor,
    cursorKind: cursorKind as IngressPage["cursorKind"],
    frames: Object.freeze(frames),
    observedAtMs: integer(body.observedAtMs, "page.observedAtMs", 0),
    canonicalOwner: requiredString(body.canonicalOwner, "page.canonicalOwner", 1024),
    canonicalWriteAllowed: false,
    snapshotId: optionalString(body.snapshotId, "page.snapshotId", MAX_IDENTIFIER_BYTES),
    boundary:
      body.boundary === undefined
        ? undefined
        : integer(body.boundary, "page.boundary", 0),
    complete,
    caughtUp:
      body.caughtUp === undefined
        ? undefined
        : booleanValue(body.caughtUp, "page.caughtUp"),
    hasMore,
    highWatermark:
      body.highWatermark === undefined
        ? undefined
        : integer(body.highWatermark, "page.highWatermark", 0),
    waitedMs:
      body.waitedMs === undefined
        ? undefined
        : integer(body.waitedMs, "page.waitedMs", 0, 60_000),
  }
  if (page.boundary !== undefined && nextSequence > page.boundary && schema === EVENT_INGRESS_SNAPSHOT_SCHEMA) {
    throw new EventIngressError(
      IngressErrorCode.INVALID_PAGE,
      "Snapshot cursor exceeded its captured boundary.",
      { resyncRequired: true, context: { taskId, generation, sequence: nextSequence } },
    )
  }
  return Object.freeze(page)
}

function baseControlFrame(
  value: unknown,
  expectedTaskId: string,
  expectedGeneration: number,
): {
  body: Record<string, unknown>
  taskId: string
  generation: number
  sequence: number
  observedAtMs: number
} {
  const body = objectValue(value, "control frame")
  if (body.schema !== EVENT_INGRESS_FRAME_SCHEMA) {
    throw schemaError(body.schema, EVENT_INGRESS_FRAME_SCHEMA, { taskId: expectedTaskId })
  }
  const taskId = identifier(body.taskId, "frame.taskId")
  if (taskId !== expectedTaskId) throw crossTaskError(expectedTaskId, taskId)
  const generation = integer(body.generation, "frame.generation", 1)
  if (generation !== expectedGeneration) {
    throw staleGenerationError(expectedGeneration, generation, { taskId })
  }
  return {
    body,
    taskId,
    generation,
    sequence: integer(body.sequence, "frame.sequence", 0),
    observedAtMs: integer(body.observedAtMs, "frame.observedAtMs", 0),
  }
}

export function normalizeAnyFrame(
  value: unknown,
  expectedTaskId: string,
  expectedGeneration: number,
): IngressFrame {
  const body = objectValue(value, "ingress frame")
  if (body.kind === FrameKind.EVENT) {
    return normalizeEventFrame(body, expectedTaskId, expectedGeneration)
  }
  const base = baseControlFrame(body, expectedTaskId, expectedGeneration)
  if (body.kind === FrameKind.READY) {
    const frame: IngressReadyFrame = {
      schema: EVENT_INGRESS_FRAME_SCHEMA,
      kind: FrameKind.READY,
      taskId: base.taskId,
      generation: base.generation,
      sequence: base.sequence,
      streamId: optionalString(body.streamId, "frame.streamId", MAX_IDENTIFIER_BYTES),
      observedAtMs: base.observedAtMs,
    }
    return Object.freeze(frame)
  }
  if (body.kind === FrameKind.HEARTBEAT) {
    const frame: IngressHeartbeatFrame = {
      schema: EVENT_INGRESS_FRAME_SCHEMA,
      kind: FrameKind.HEARTBEAT,
      taskId: base.taskId,
      generation: base.generation,
      sequence: base.sequence,
      cursor: optionalString(body.cursor, "frame.cursor", 4096),
      observedAtMs: base.observedAtMs,
    }
    return Object.freeze(frame)
  }
  if (body.kind === FrameKind.CLOSE) {
    const frame: IngressCloseFrame = {
      schema: EVENT_INGRESS_FRAME_SCHEMA,
      kind: FrameKind.CLOSE,
      taskId: base.taskId,
      generation: base.generation,
      sequence: base.sequence,
      cursor: optionalString(body.cursor, "frame.cursor", 4096),
      reason: requiredString(body.reason, "frame.reason", 8192),
      retryable: booleanValue(body.retryable, "frame.retryable"),
      observedAtMs: base.observedAtMs,
    }
    return Object.freeze(frame)
  }
  if (body.kind === FrameKind.ERROR) {
    const frame: IngressErrorFrame = {
      schema: EVENT_INGRESS_FRAME_SCHEMA,
      kind: FrameKind.ERROR,
      taskId: base.taskId,
      generation: base.generation,
      sequence: base.sequence,
      code: requiredString(body.code, "frame.code", 1024),
      message: requiredString(body.message, "frame.message", 64 * 1024),
      retryable: booleanValue(body.retryable, "frame.retryable"),
      resyncRequired: booleanValue(body.resyncRequired, "frame.resyncRequired"),
      observedAtMs: base.observedAtMs,
    }
    return Object.freeze(frame)
  }
  throw new EventIngressError(
    IngressErrorCode.INVALID_FRAME,
    `Unsupported event ingress frame kind ${String(body.kind ?? "[missing]")}.`,
    { context: { taskId: expectedTaskId, generation: expectedGeneration } },
  )
}

export function assertFramePredecessor(
  frame: IngressEventFrame,
  committedSequence: number,
): void {
  if (frame.previousSequence === committedSequence) return
  throw new EventIngressError(
    IngressErrorCode.GAP_DETECTED,
    "Event frame predecessor does not match the committed cursor.",
    {
      retryable: true,
      resyncRequired: false,
      context: {
        taskId: frame.taskId,
        generation: frame.generation,
        eventId: frame.eventId,
        sequence: frame.sequence,
        previousSequence: frame.previousSequence,
        details: { committedSequence },
      },
    },
  )
}

export function assertPageStartsAt(page: IngressPage, expectedSequence: number): void {
  if (page.fromSequence === expectedSequence) return
  throw new EventIngressError(
    page.fromSequence < expectedSequence
      ? IngressErrorCode.CURSOR_STALE
      : IngressErrorCode.GAP_DETECTED,
    "Event page does not start at the requested committed cursor.",
    {
      retryable: true,
      resyncRequired: page.fromSequence < expectedSequence,
      context: {
        taskId: page.taskId,
        generation: page.generation,
        sequence: page.fromSequence,
        previousSequence: expectedSequence,
      },
    },
  )
}
