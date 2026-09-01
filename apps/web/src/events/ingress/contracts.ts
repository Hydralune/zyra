export const EVENT_INGRESS_PROTOCOL = "zyra.event-ingress/v1"
export const EVENT_INGRESS_CAPABILITIES_SCHEMA = "zyra.event-ingress-capabilities/v1"
export const EVENT_INGRESS_SNAPSHOT_SCHEMA = "zyra.event-ingress-snapshot/v1"
export const EVENT_INGRESS_DELTA_SCHEMA = "zyra.event-ingress-delta/v1"
export const EVENT_INGRESS_FRAME_SCHEMA = "zyra.event-ingress-frame/v1"
export const EVENT_INGRESS_CURSOR_SCHEMA = "zyra.event-ingress-cursor/v1"
export const RUNTIME_EVENT_SCHEMA = "zyra.runtime-event/v1"

export const TransportKind = {
  SSE: "sse",
  WEBSOCKET: "websocket",
  LONG_POLL: "long_poll",
} as const

export type TransportKindValue = (typeof TransportKind)[keyof typeof TransportKind]

export const ConnectionPhase = {
  IDLE: "idle",
  NEGOTIATING: "negotiating",
  SUBSCRIBING: "subscribing",
  SNAPSHOTTING: "snapshotting",
  CATCHING_UP: "catching_up",
  LIVE: "live",
  BACKING_OFF: "backing_off",
  RESYNCING: "resyncing",
  STOPPING: "stopping",
  STOPPED: "stopped",
  FAILED: "failed",
} as const

export type ConnectionPhaseValue = (typeof ConnectionPhase)[keyof typeof ConnectionPhase]

export const FrameKind = {
  EVENT: "event",
  LIVE: "live",
  READY: "ready",
  HEARTBEAT: "heartbeat",
  CLOSE: "close",
  ERROR: "error",
  CURSOR: "cursor",
} as const

export type FrameKindValue = (typeof FrameKind)[keyof typeof FrameKind]

export const EventSettlement = {
  ATOMIC: "atomic",
  PARTIAL: "partial",
  FINAL: "final",
  TOMBSTONE: "tombstone",
} as const

export type EventSettlementValue = (typeof EventSettlement)[keyof typeof EventSettlement]

export const BufferPriority = {
  TERMINAL: 400,
  EFFECTIVE: 300,
  CONTROL: 250,
  NORMAL: 200,
  NON_EFFECTIVE: 100,
  HEARTBEAT: 0,
} as const

export const PressureLevel = {
  NORMAL: "normal",
  ELEVATED: "elevated",
  HIGH: "high",
  CRITICAL: "critical",
  OVERFLOW: "overflow",
} as const

export type PressureLevelValue = (typeof PressureLevel)[keyof typeof PressureLevel]

export const DeliveryDisposition = {
  ACCEPTED: "accepted",
  DUPLICATE: "duplicate",
  STALE: "stale",
  BUFFERED_GAP: "buffered_gap",
  BUFFERED_ORPHAN: "buffered_orphan",
  COALESCED: "coalesced",
  TOMBSTONED: "tombstoned",
  REJECTED: "rejected",
  EVICTED: "evicted",
} as const

export type DeliveryDispositionValue =
  (typeof DeliveryDisposition)[keyof typeof DeliveryDisposition]

export interface JsonObject {
  [key: string]: JsonValue
}

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonObject
  | JsonValue[]

export interface EventIdentity {
  taskId: string
  runId: string
  sessionId?: string
  nodeId?: string
  workerId?: string
  spanId?: string
  parentSpanId?: string
  toolCallId?: string
  artifactId?: string
  checkpointId?: string
  controlCommandId?: string
}

export interface StateMutationRef {
  domain: string
  operation: string
  path: readonly string[]
  beforeDigest?: string
  afterDigest?: string
  effective: boolean
}

export interface IngressArtifactRef {
  artifactId: string
  digest: string
  mediaType: string
  sizeBytes: number
  title: string
  uri?: string
}

export interface IngressEvent {
  schema: typeof RUNTIME_EVENT_SCHEMA
  eventId: string
  eventType: string
  eventVersion: number
  aggregateId: string
  aggregateSequence: number
  globalSequence: number
  producerSequence: number
  idempotencyKey: string
  correlationId: string
  causationId?: string
  createdAt: string
  committedAt: string
  durability: string
  effect: string
  identity: EventIdentity
  senderKind: string
  senderId: string
  intent: string
  targetKind?: string
  targetId?: string
  summary: string
  stateMutation: StateMutationRef
  artifactRefs: readonly IngressArtifactRef[]
  inline: Readonly<JsonObject>
  metadata: Readonly<JsonObject>
  contentDigest: string
  sourceBytes: number
  inlineBytes: number
  envelopeBytes: number
  settlement: EventSettlementValue
  partId?: string
  parentEventId?: string
  tombstoneTargetId?: string
  terminal: boolean
  effective: boolean
  raw: Readonly<JsonObject>
}

export interface IngressEventFrame {
  schema: typeof EVENT_INGRESS_FRAME_SCHEMA
  kind: typeof FrameKind.EVENT
  source: string
  generation: number
  taskId: string
  sequence: number
  previousSequence: number
  eventId: string
  eventType: string
  correlationId: string
  causationId?: string
  observedAtMs: number
  cursor?: string
  event: IngressEvent
  /**
   * Strict user-facing projection admitted by the API.  It is deliberately
   * separate from the canonical runtime envelope: consumers may render this
   * value, but must never infer task state from it.
   */
  presentation?: Readonly<JsonObject>
  encodedBytes: number
}

export interface IngressLiveFrame {
  schema: typeof EVENT_INGRESS_FRAME_SCHEMA
  kind: typeof FrameKind.LIVE
  source: string
  generation: number
  taskId: string
  sequence: number
  liveSequence: number
  eventId: string
  eventType: "runtime.text.delta"
  observedAtMs: number
  presentation: Readonly<JsonObject>
}

export interface IngressReadyFrame {
  schema: typeof EVENT_INGRESS_FRAME_SCHEMA
  kind: typeof FrameKind.READY
  taskId: string
  generation: number
  sequence: number
  streamId?: string
  observedAtMs: number
}

export interface IngressHeartbeatFrame {
  schema: typeof EVENT_INGRESS_FRAME_SCHEMA
  kind: typeof FrameKind.HEARTBEAT
  taskId: string
  generation: number
  sequence: number
  cursor?: string
  observedAtMs: number
}

export interface IngressCloseFrame {
  schema: typeof EVENT_INGRESS_FRAME_SCHEMA
  kind: typeof FrameKind.CLOSE
  taskId: string
  generation: number
  sequence: number
  cursor?: string
  reason: string
  retryable: boolean
  observedAtMs: number
}

export interface IngressErrorFrame {
  schema: typeof EVENT_INGRESS_FRAME_SCHEMA
  kind: typeof FrameKind.ERROR
  taskId: string
  generation: number
  sequence: number
  code: string
  message: string
  retryable: boolean
  resyncRequired: boolean
  observedAtMs: number
}

export type IngressFrame =
  | IngressEventFrame
  | IngressLiveFrame
  | IngressReadyFrame
  | IngressHeartbeatFrame
  | IngressCloseFrame
  | IngressErrorFrame

export interface TransportDescriptor {
  kind: TransportKindValue
  available: boolean
  priority: number
  path: string
  cursorMode: "query" | "header" | "message"
  customHeaders: boolean
  reason?: string
  protocols?: readonly string[]
}

export interface IngressCapabilities {
  schema: typeof EVENT_INGRESS_CAPABILITIES_SCHEMA
  protocol: typeof EVENT_INGRESS_PROTOCOL
  taskId: string
  canonicalOwner: string
  ingressOwner: string
  canonicalWriteAllowed: false
  snapshotRequired: boolean
  subscribeBeforeSnapshot: boolean
  subscriptionCursor: string
  subscriptionSequence: number
  subscriptionBoundary: number
  generation: number
  transports: readonly TransportDescriptor[]
  endpoints: {
    capabilities: string
    snapshot: string
    delta: string
    sse: string
    websocket: string
  }
  limits: {
    defaultPage: number
    maxPage: number
    defaultWaitMs: number
    maxWaitMs: number
    defaultStreamMs: number
    maxStreamMs: number
    cursorTtlMs: number
  }
  schemas: {
    cursor: string
    frame: string
    snapshot: string
    delta: string
    event: string
  }
  filterDigest: string
}

export interface IngressPage {
  schema:
    | typeof EVENT_INGRESS_SNAPSHOT_SCHEMA
    | typeof EVENT_INGRESS_DELTA_SCHEMA
  protocol: typeof EVENT_INGRESS_PROTOCOL
  taskId: string
  generation: number
  fromSequence: number
  nextSequence: number
  cursor: string
  cursorKind: "snapshot" | "delta" | "stream"
  frames: readonly IngressEventFrame[]
  observedAtMs: number
  canonicalOwner: string
  canonicalWriteAllowed: false
  snapshotId?: string
  boundary?: number
  complete?: boolean
  caughtUp?: boolean
  hasMore: boolean
  highWatermark?: number
  waitedMs?: number
}

export interface IngressSubscriptionFilter {
  eventTypes?: readonly string[]
  intents?: readonly string[]
  correlationIds?: readonly string[]
  artifactIds?: readonly string[]
  includeNonEffective?: boolean
  includeHeartbeats?: boolean
}

export interface NormalizedIngressFilter {
  eventTypes: readonly string[]
  intents: readonly string[]
  correlationIds: readonly string[]
  artifactIds: readonly string[]
  includeNonEffective: boolean
  includeHeartbeats: boolean
  digestMaterial: string
}

export interface IngressSubscriptionOptions {
  cursor?: string
  filter?: IngressSubscriptionFilter
  signal?: AbortSignal
  transportPreference?: readonly TransportKindValue[]
  snapshotPageSize?: number
  deltaPageSize?: number
  longPollMs?: number
  heartbeatTimeoutMs?: number
  reconnect?: Partial<ReconnectPolicy>
  capacity?: Partial<IngressCapacity>
}

export interface IngressCapacity {
  maxItems: number
  maxBytes: number
  highWatermarkRatio: number
  criticalWatermarkRatio: number
  terminalReserveItems: number
  terminalReserveBytes: number
  maxGapItems: number
  maxGapBytes: number
  maxOrphans: number
  maxOrphanAgeMs: number
  identityWindow: number
  diagnosticsLimit: number
  maxBatchItems: number
  maxBatchBytes: number
}

export interface ReconnectPolicy {
  attempts: number
  baseDelayMs: number
  maxDelayMs: number
  factor: number
  jitter: number
  stableResetMs: number
  heartbeatTimeoutMs: number
  resyncAttempts: number
  transportFailureThreshold: number
  transportCooldownMs: number
}

export interface DeliveryReceipt {
  eventId: string
  sequence: number
  generation: number
  disposition: DeliveryDispositionValue
  reason: string
  observedAtMs: number
  committedAtMs?: number
  encodedBytes: number
}

export interface IngressBatch {
  taskId: string
  generation: number
  events: readonly IngressEvent[]
  /** Product projections carried by the exact durable frames in `events`. */
  presentations?: readonly IngressBatchPresentation[]
  receipts: readonly DeliveryReceipt[]
  cursor?: string
  fromSequence: number
  sequence: number
  highWatermark: number
  receivedAt: number
  transport: TransportKindValue
  snapshot: boolean
  caughtUp: boolean
}

export interface IngressBatchPresentation {
  eventId: string
  eventType: string
  sequence: number
  presentation: Readonly<JsonObject>
}

export interface GapRange {
  expectedPrevious: number
  observedPrevious: number
  observedSequence: number
  firstObservedAtMs: number
  lastObservedAtMs: number
  attempts: number
  eventIds: readonly string[]
}

export interface PressureSnapshot {
  level: PressureLevelValue
  items: number
  bytes: number
  maxItems: number
  maxBytes: number
  itemRatio: number
  byteRatio: number
  reservedItemsAvailable: number
  reservedBytesAvailable: number
  coalesced: number
  evicted: number
  rejected: number
}

export interface CursorSnapshot {
  taskId: string
  generation: number
  cursor?: string
  committedSequence: number
  observedSequence: number
  snapshotBoundary: number
  snapshotComplete: boolean
  highWatermark: number
  lastEventId?: string
  updatedAtMs: number
}

export interface ConnectionSnapshot {
  taskId: string
  phase: ConnectionPhaseValue
  generation: number
  transport?: TransportKindValue
  startedAtMs?: number
  connectedAtMs?: number
  lastFrameAtMs?: number
  lastHeartbeatAtMs?: number
  lastDeliveryAtMs?: number
  reconnectAttempt: number
  resyncAttempt: number
  subscribers: number
  cursor: CursorSnapshot
  pressure: PressureSnapshot
  gap?: GapRange
  error?: {
    code: string
    message: string
    retryable: boolean
    resyncRequired: boolean
    observedAtMs: number
  }
}

export interface IngressDiagnostic {
  id: number
  taskId: string
  generation: number
  atMs: number
  category:
    | "connection"
    | "transport"
    | "cursor"
    | "snapshot"
    | "delivery"
    | "gap"
    | "pressure"
    | "schema"
    | "subscription"
    | "recovery"
  code: string
  message: string
  sequence?: number
  eventId?: string
  transport?: TransportKindValue
  details: Readonly<Record<string, JsonValue>>
}

export interface IngressObserver {
  batch(batch: IngressBatch): void
  live?(frame: IngressLiveFrame): void
  status?(snapshot: ConnectionSnapshot): void
  diagnostic?(diagnostic: IngressDiagnostic): void
}

export interface TransportOpenContext {
  taskId: string
  cursor: string
  generation: number
  filter: NormalizedIngressFilter
  signal: AbortSignal
  capabilities: IngressCapabilities
  pageSize: number
  waitMs: number
  heartbeatTimeoutMs: number
}

export interface TransportSession {
  readonly kind: TransportKindValue
  readonly openedAtMs: number
  readonly closed: boolean
  frames(): AsyncIterable<unknown>
  close(reason?: unknown): void | Promise<void>
}

export interface EventIngressDataSource {
  capabilities(
    taskId: string,
    filter: NormalizedIngressFilter,
    signal?: AbortSignal,
    generation?: number,
    cursor?: string,
  ): Promise<unknown>
  snapshot(
    taskId: string,
    options: {
      cursor?: string
      generation: number
      limit: number
      filter: NormalizedIngressFilter
      signal?: AbortSignal
    },
  ): Promise<unknown>
  delta(
    taskId: string,
    options: {
      cursor: string
      generation: number
      limit: number
      waitMs: number
      filter: NormalizedIngressFilter
      signal?: AbortSignal
    },
  ): Promise<unknown>
  openSse(context: TransportOpenContext): Promise<TransportSession>
  openWebSocket(context: TransportOpenContext): Promise<TransportSession>
}

export const DEFAULT_INGRESS_CAPACITY: Readonly<IngressCapacity> = Object.freeze({
  maxItems: 20_000,
  maxBytes: 32 * 1024 * 1024,
  highWatermarkRatio: 0.72,
  criticalWatermarkRatio: 0.9,
  terminalReserveItems: 256,
  terminalReserveBytes: 2 * 1024 * 1024,
  maxGapItems: 4_096,
  maxGapBytes: 8 * 1024 * 1024,
  maxOrphans: 2_048,
  maxOrphanAgeMs: 60_000,
  identityWindow: 100_000,
  diagnosticsLimit: 8_192,
  maxBatchItems: 512,
  maxBatchBytes: 2 * 1024 * 1024,
})

export const DEFAULT_RECONNECT_POLICY: Readonly<ReconnectPolicy> = Object.freeze({
  attempts: 12,
  baseDelayMs: 125,
  maxDelayMs: 10_000,
  factor: 1.8,
  jitter: 0.15,
  stableResetMs: 30_000,
  heartbeatTimeoutMs: 20_000,
  resyncAttempts: 3,
  transportFailureThreshold: 3,
  transportCooldownMs: 30_000,
})

const encoder = new TextEncoder()

function clampInteger(value: unknown, fallback: number, minimum: number, maximum: number): number {
  const candidate = value === undefined ? fallback : Number(value)
  if (!Number.isFinite(candidate)) throw new TypeError("Ingress numeric option must be finite")
  return Math.min(maximum, Math.max(minimum, Math.floor(candidate)))
}

function clampRatio(value: unknown, fallback: number, minimum: number, maximum: number): number {
  const candidate = value === undefined ? fallback : Number(value)
  if (!Number.isFinite(candidate)) throw new TypeError("Ingress ratio option must be finite")
  return Math.min(maximum, Math.max(minimum, candidate))
}

function boundedFilterValues(
  values: readonly string[] | undefined,
  label: string,
): readonly string[] {
  if (!values?.length) return Object.freeze([])
  if (values.length > 128) throw new TypeError(`${label} exceeds 128 values`)
  const normalized: string[] = []
  const seen = new Set<string>()
  for (const raw of values) {
    const value = String(raw || "").trim()
    if (!value) continue
    if (encoder.encode(value).byteLength > 8_192) throw new TypeError(`${label} value is too large`)
    if (/[\u0000\r\n]/.test(value)) throw new TypeError(`${label} value contains control characters`)
    if (seen.has(value)) continue
    seen.add(value)
    normalized.push(value)
  }
  return Object.freeze(normalized.sort())
}

function stableFilterMaterial(value: Omit<NormalizedIngressFilter, "digestMaterial">): string {
  return JSON.stringify({
    eventTypes: value.eventTypes,
    intents: value.intents,
    correlationIds: value.correlationIds,
    artifactIds: value.artifactIds,
    includeNonEffective: value.includeNonEffective,
    includeHeartbeats: value.includeHeartbeats,
  })
}

export function normalizeIngressFilter(
  value: IngressSubscriptionFilter | undefined,
): NormalizedIngressFilter {
  const core = {
    eventTypes: boundedFilterValues(value?.eventTypes, "eventTypes"),
    intents: boundedFilterValues(value?.intents, "intents"),
    correlationIds: boundedFilterValues(value?.correlationIds, "correlationIds"),
    artifactIds: boundedFilterValues(value?.artifactIds, "artifactIds"),
    includeNonEffective: value?.includeNonEffective ?? true,
    includeHeartbeats: value?.includeHeartbeats ?? false,
  }
  return Object.freeze({ ...core, digestMaterial: stableFilterMaterial(core) })
}

export function normalizeIngressCapacity(value: Partial<IngressCapacity> | undefined): IngressCapacity {
  const defaults = DEFAULT_INGRESS_CAPACITY
  const maxItems = clampInteger(value?.maxItems, defaults.maxItems, 64, 1_000_000)
  const maxBytes = clampInteger(value?.maxBytes, defaults.maxBytes, 256 * 1024, 1024 * 1024 * 1024)
  const highWatermarkRatio = clampRatio(
    value?.highWatermarkRatio,
    defaults.highWatermarkRatio,
    0.25,
    0.94,
  )
  const criticalWatermarkRatio = clampRatio(
    value?.criticalWatermarkRatio,
    defaults.criticalWatermarkRatio,
    highWatermarkRatio + 0.01,
    0.99,
  )
  const terminalReserveItems = clampInteger(
    value?.terminalReserveItems,
    defaults.terminalReserveItems,
    1,
    Math.max(1, Math.floor(maxItems / 2)),
  )
  const terminalReserveBytes = clampInteger(
    value?.terminalReserveBytes,
    defaults.terminalReserveBytes,
    1024,
    Math.max(1024, Math.floor(maxBytes / 2)),
  )
  return Object.freeze({
    maxItems,
    maxBytes,
    highWatermarkRatio,
    criticalWatermarkRatio,
    terminalReserveItems,
    terminalReserveBytes,
    maxGapItems: clampInteger(value?.maxGapItems, defaults.maxGapItems, 8, maxItems),
    maxGapBytes: clampInteger(value?.maxGapBytes, defaults.maxGapBytes, 64 * 1024, maxBytes),
    maxOrphans: clampInteger(value?.maxOrphans, defaults.maxOrphans, 8, maxItems),
    maxOrphanAgeMs: clampInteger(
      value?.maxOrphanAgeMs,
      defaults.maxOrphanAgeMs,
      1_000,
      24 * 60 * 60_000,
    ),
    identityWindow: clampInteger(
      value?.identityWindow,
      defaults.identityWindow,
      maxItems,
      5_000_000,
    ),
    diagnosticsLimit: clampInteger(
      value?.diagnosticsLimit,
      defaults.diagnosticsLimit,
      64,
      100_000,
    ),
    maxBatchItems: clampInteger(value?.maxBatchItems, defaults.maxBatchItems, 1, maxItems),
    maxBatchBytes: clampInteger(
      value?.maxBatchBytes,
      defaults.maxBatchBytes,
      16 * 1024,
      maxBytes,
    ),
  })
}

export function normalizeReconnectPolicy(
  value: Partial<ReconnectPolicy> | undefined,
): ReconnectPolicy {
  const defaults = DEFAULT_RECONNECT_POLICY
  return Object.freeze({
    attempts: clampInteger(value?.attempts, defaults.attempts, 0, 10_000),
    baseDelayMs: clampInteger(value?.baseDelayMs, defaults.baseDelayMs, 0, 60_000),
    maxDelayMs: clampInteger(value?.maxDelayMs, defaults.maxDelayMs, 25, 10 * 60_000),
    factor: clampRatio(value?.factor, defaults.factor, 1, 10),
    jitter: clampRatio(value?.jitter, defaults.jitter, 0, 1),
    stableResetMs: clampInteger(
      value?.stableResetMs,
      defaults.stableResetMs,
      1_000,
      24 * 60 * 60_000,
    ),
    heartbeatTimeoutMs: clampInteger(
      value?.heartbeatTimeoutMs,
      defaults.heartbeatTimeoutMs,
      1_000,
      10 * 60_000,
    ),
    resyncAttempts: clampInteger(value?.resyncAttempts, defaults.resyncAttempts, 0, 100),
    transportFailureThreshold: clampInteger(
      value?.transportFailureThreshold,
      defaults.transportFailureThreshold,
      1,
      100,
    ),
    transportCooldownMs: clampInteger(
      value?.transportCooldownMs,
      defaults.transportCooldownMs,
      1_000,
      24 * 60 * 60_000,
    ),
  })
}

export function eventPriority(event: IngressEvent): number {
  if (event.terminal || event.settlement === EventSettlement.FINAL) return BufferPriority.TERMINAL
  if (event.intent === "control" || event.intent === "permission") return BufferPriority.CONTROL
  if (event.effective) return BufferPriority.EFFECTIVE
  if (event.effect === "non_effective") return BufferPriority.NON_EFFECTIVE
  return BufferPriority.NORMAL
}

export function eventIdentityKey(event: IngressEvent): string {
  return `${event.globalSequence}:${event.eventId}:${event.contentDigest}`
}

export function eventPartKey(event: IngressEvent): string {
  const partIdentity = event.partId ?? event.eventType
  return [
    event.identity.taskId,
    event.parentEventId ?? event.eventId,
    partIdentity,
  ].join("|")
}

export function eventMatchesFilter(
  event: IngressEvent,
  filter: NormalizedIngressFilter,
): boolean {
  if (!filter.includeNonEffective && !event.effective) return false
  if (filter.eventTypes.length && !filter.eventTypes.includes(event.eventType)) return false
  if (filter.intents.length && !filter.intents.includes(event.intent)) return false
  if (
    filter.correlationIds.length &&
    !filter.correlationIds.includes(event.correlationId)
  ) return false
  if (
    filter.artifactIds.length &&
    !event.artifactRefs.some((item) => filter.artifactIds.includes(item.artifactId))
  ) return false
  return true
}

export function transportPreference(
  requested: readonly TransportKindValue[] | undefined,
): readonly TransportKindValue[] {
  const input = requested?.length
    ? requested
    : [TransportKind.SSE, TransportKind.WEBSOCKET, TransportKind.LONG_POLL]
  const result: TransportKindValue[] = []
  for (const kind of input) {
    if (!Object.values(TransportKind).includes(kind)) {
      throw new TypeError(`Unknown event transport: ${kind}`)
    }
    if (!result.includes(kind)) result.push(kind)
  }
  if (!result.includes(TransportKind.LONG_POLL)) result.push(TransportKind.LONG_POLL)
  return Object.freeze(result)
}

export function encodedJsonBytes(value: unknown): number {
  try {
    return encoder.encode(JSON.stringify(value)).byteLength
  } catch {
    return Number.MAX_SAFE_INTEGER
  }
}

export function freezeEvent(event: IngressEvent): IngressEvent {
  Object.freeze(event.identity)
  Object.freeze(event.stateMutation.path)
  Object.freeze(event.stateMutation)
  for (const artifact of event.artifactRefs) Object.freeze(artifact)
  Object.freeze(event.artifactRefs)
  Object.freeze(event.inline)
  Object.freeze(event.metadata)
  Object.freeze(event.raw)
  return Object.freeze(event)
}

export function emptyCursorSnapshot(taskId: string, now = Date.now()): CursorSnapshot {
  return {
    taskId,
    generation: 0,
    committedSequence: 0,
    observedSequence: 0,
    snapshotBoundary: 0,
    snapshotComplete: false,
    highWatermark: 0,
    updatedAtMs: now,
  }
}
