import {
  assertJsonValue,
  boundedText,
  cloneJson,
  digestJson,
  isDigest,
  isPlainObject,
  newId,
  normalizeIdentifier,
  optionalInteger,
  optionalRecord,
  optionalString,
  parseTimestamp,
  requireBoolean,
  requireFinite,
  requireInteger,
  requireRecord,
  requireString,
  stringArray,
  utcNow,
  type JsonValue,
} from "./canonical.ts";
import { EnvelopeValidationError } from "./errors.ts";

export const EVENT_SCHEMA_VERSION = "zyra.runtime-event/v1";
export const MESSAGE_SCHEMA_VERSION = "zyra.agent-message/v1";
export const ARTIFACT_REF_SCHEMA_VERSION = "zyra.artifact-ref/v1";
export const EVIDENCE_REF_SCHEMA_VERSION = "zyra.evidence-ref/v1";

export const EventDurability = {
  DURABLE: "durable",
  LIVE_ONLY: "live_only",
} as const;
export type EventDurabilityValue = (typeof EventDurability)[keyof typeof EventDurability];

export const EventEffect = {
  EFFECTIVE: "effective",
  NON_EFFECTIVE: "non_effective",
  UNKNOWN: "unknown",
} as const;
export type EventEffectValue = (typeof EventEffect)[keyof typeof EventEffect];

export const MessageIntent = {
  QUERY: "query",
  PLAN: "plan",
  TOOL_CALL: "tool_call",
  TOOL_RESULT: "tool_result",
  OBSERVATION: "observation",
  PERMISSION: "permission",
  CONTROL: "control",
  MCP: "mcp",
  SKILL: "skill",
  SUBAGENT: "subagent",
  COMPACT: "compact",
  DISPATCH: "dispatch",
  ARTIFACT: "artifact",
  RECOVERY: "recovery",
  STATUS: "status",
  AUDIT: "audit",
} as const;
export type MessageIntentValue = (typeof MessageIntent)[keyof typeof MessageIntent];

export const SenderKind = {
  USER: "user",
  API: "api",
  RUNTIME: "runtime",
  WORKER: "worker",
  SCHEDULER: "scheduler",
  GATEWAY: "gateway",
  TOOL: "tool",
  MCP: "mcp",
  SKILL: "skill",
  SUBAGENT: "subagent",
  ARTIFACT_STORE: "artifact_store",
  RECOVERY: "recovery",
  CONTROL: "control",
  SYSTEM: "system",
} as const;
export type SenderKindValue = (typeof SenderKind)[keyof typeof SenderKind];

export const RecipientKind = {
  API: "api",
  WORKER: "worker",
  SCHEDULER: "scheduler",
  ARTIFACT_STORE: "artifact_store",
  RECOVERY: "recovery",
  MCP: "mcp",
  CONTROL: "control",
  MEMORY: "memory",
  UI_PROJECTOR: "ui_projector",
  AUDITOR: "auditor",
} as const;
export type RecipientKindValue = (typeof RecipientKind)[keyof typeof RecipientKind];

export const TrustLevel = {
  UNTRUSTED: "untrusted",
  EXTERNAL: "external",
  INTERNAL: "internal",
  VERIFIED: "verified",
  SYSTEM: "system",
} as const;
export type TrustLevelValue = (typeof TrustLevel)[keyof typeof TrustLevel];

export const DeliveryState = {
  PENDING: "pending",
  LEASED: "leased",
  ACKNOWLEDGED: "acknowledged",
  RETRY_WAIT: "retry_wait",
  DEAD_LETTER: "dead_letter",
  CANCELLED: "cancelled",
} as const;
export type DeliveryStateValue = (typeof DeliveryState)[keyof typeof DeliveryState];

export const BackpressureMode = {
  REJECT: "reject",
  DEAD_LETTER_OLDEST: "dead_letter_oldest",
  COALESCE_NON_EFFECTIVE: "coalesce_non_effective",
} as const;
export type BackpressureModeValue = (typeof BackpressureMode)[keyof typeof BackpressureMode];

export interface RuntimeIdentity {
  runId: string;
  sessionId?: string;
  taskId: string;
  nodeId?: string;
  workerId?: string;
  toolCallId?: string;
  commandId?: string;
}

export interface SenderRef {
  kind: SenderKindValue;
  id: string;
  role?: string;
  capabilityRefs: readonly string[];
}

export interface RecipientRef {
  kind: RecipientKindValue;
  id: string;
  requiredCapabilities: readonly string[];
}

export interface ArtifactPointer {
  schema: typeof ARTIFACT_REF_SCHEMA_VERSION;
  artifactId: string;
  digest: string;
  mediaType: string;
  sizeBytes: number;
  title: string;
  uri?: string;
  producerNodeId?: string;
  metadata: Readonly<Record<string, JsonValue>>;
}

export interface EvidencePointer {
  schema: typeof EVIDENCE_REF_SCHEMA_VERSION;
  evidenceId: string;
  source: string;
  summary: string;
  confidence?: number;
  artifactId?: string;
  metadata: Readonly<Record<string, JsonValue>>;
}

export interface Provenance {
  sourceRepository: string;
  sourceModule: string;
  migrationRole: "primary" | "supplementary" | "conformance" | "reference" | "zyra_owned";
  producerVersion: string;
  trust: TrustLevelValue;
  normalizedFrom?: string;
  sourceEventId?: string;
  sourceDigest?: string;
}

export interface StateDelta {
  domain: string;
  operation: "set" | "merge" | "append" | "remove" | "transition" | "none";
  path: readonly string[];
  beforeDigest?: string;
  afterDigest?: string;
  value?: JsonValue;
  effective: boolean;
}

export interface RuntimeEventEnvelope {
  schema: typeof EVENT_SCHEMA_VERSION;
  eventId: string;
  eventType: string;
  eventVersion: number;
  aggregateId: string;
  aggregateSequence: number;
  producerSequence: number;
  idempotencyKey: string;
  correlationId: string;
  causationId?: string;
  createdAt: string;
  committedAt: string;
  durability: EventDurabilityValue;
  effect: EventEffectValue;
  identity: RuntimeIdentity;
  sender: SenderRef;
  intent: MessageIntentValue;
  target?: RecipientRef;
  topKRecipients: readonly RecipientRef[];
  summary: string;
  stateDelta: StateDelta;
  evidenceRefs: readonly EvidencePointer[];
  artifactRefs: readonly ArtifactPointer[];
  uncertainty?: number;
  provenance: Provenance;
  inline: Readonly<Record<string, JsonValue>>;
  sourceBytes: number;
  inlineBytes: number;
  envelopeBytes: number;
  contentDigest: string;
  metadata: Readonly<Record<string, JsonValue>>;
}

export interface RuntimeEventDraft {
  eventId?: string;
  eventType: string;
  eventVersion?: number;
  aggregateId: string;
  expectedSequence?: number;
  producerSequence?: number;
  idempotencyKey: string;
  correlationId: string;
  causationId?: string;
  createdAt?: string;
  durability?: EventDurabilityValue;
  effect?: EventEffectValue;
  identity: RuntimeIdentity;
  sender: SenderRef;
  intent: MessageIntentValue;
  target?: RecipientRef;
  topKRecipients?: readonly RecipientRef[];
  summary?: string;
  stateDelta?: StateDelta;
  evidenceRefs?: readonly EvidencePointer[];
  artifactRefs?: readonly ArtifactPointer[];
  uncertainty?: number;
  provenance: Provenance;
  inline?: Readonly<Record<string, JsonValue>>;
  sourceBytes?: number;
  metadata?: Readonly<Record<string, JsonValue>>;
}

export interface AgentMessageEnvelope {
  schema: typeof MESSAGE_SCHEMA_VERSION;
  messageId: string;
  eventId: string;
  eventType: string;
  aggregateId: string;
  aggregateSequence: number;
  producerSequence: number;
  idempotencyKey: string;
  correlationId: string;
  causationId?: string;
  identity: RuntimeIdentity;
  sender: SenderRef;
  intent: MessageIntentValue;
  target?: RecipientRef;
  topKRecipients: readonly RecipientRef[];
  summary: string;
  stateDelta: StateDelta;
  evidenceRefs: readonly EvidencePointer[];
  artifactRefs: readonly ArtifactPointer[];
  uncertainty?: number;
  provenance: Provenance;
  eventDigest: string;
  createdAt: string;
  metadata: Readonly<Record<string, JsonValue>>;
}

export interface LegacyEventRecord {
  event_id?: string;
  eventId?: string;
  run_id?: string;
  runId?: string;
  task_id?: string;
  taskId?: string;
  node_id?: string | null;
  nodeId?: string | null;
  event_type?: string;
  eventType?: string;
  created_at?: string;
  createdAt?: string;
  payload?: Record<string, unknown>;
}

export interface RouteCandidate {
  recipient: RecipientRef;
  score: number;
  reasons: readonly string[];
  rejectedReasons: readonly string[];
}

export interface RouteDecision {
  eventId: string;
  policyId: string;
  explicitTarget: boolean;
  broadcast: boolean;
  fanoutReason?: string;
  recipients: readonly RecipientRef[];
  candidates: readonly RouteCandidate[];
  routeDensity: number;
  availableRecipientCount: number;
  createdAt: string;
}

export interface SubscriptionSpec {
  subscriptionId: string;
  recipient: RecipientRef;
  eventTypes: readonly string[];
  intents: readonly MessageIntentValue[];
  aggregatePrefixes: readonly string[];
  taskIds: readonly string[];
  capabilityRefs: readonly string[];
  capacity: number;
  maxAttempts: number;
  ackTimeoutMs: number;
  backpressureMode: BackpressureModeValue;
  enabled: boolean;
  priority: number;
  metadata: Readonly<Record<string, JsonValue>>;
}

export interface DeliveryRecord {
  deliveryId: string;
  eventId: string;
  subscriptionId: string;
  recipient: RecipientRef;
  state: DeliveryStateValue;
  attempt: number;
  availableAt: string;
  leasedAt?: string;
  leaseExpiresAt?: string;
  leaseToken?: string;
  acknowledgedAt?: string;
  lastError?: string;
  createdAt: string;
  updatedAt: string;
  redelivered: boolean;
  routePolicyId: string;
  routeReason: string;
}

export interface LeasedDelivery {
  delivery: DeliveryRecord;
  message: AgentMessageEnvelope;
}

export interface AppendOptions {
  ownerId?: string;
  strictOwner?: boolean;
  route?: boolean;
  project?: boolean;
  publishLegacyProjection?: boolean;
  replay?: boolean;
}

export interface AppendReceipt {
  event: RuntimeEventEnvelope;
  committed: boolean;
  duplicate: boolean;
  projected: boolean;
  routed: boolean;
  route?: RouteDecision;
  deliveryIds: readonly string[];
  artifactSpillCount: number;
  warnings: readonly string[];
}

export interface EventQuery {
  aggregateId?: string;
  runId?: string;
  sessionId?: string;
  taskId?: string;
  workerId?: string;
  eventTypes?: readonly string[];
  intents?: readonly MessageIntentValue[];
  effects?: readonly EventEffectValue[];
  afterSequence?: number;
  beforeSequence?: number;
  createdAtGte?: string;
  createdAtLt?: string;
  correlationId?: string;
  causationId?: string;
  artifactId?: string;
  cursor?: string;
  limit?: number;
  descending?: boolean;
}

export interface EventPage {
  items: readonly RuntimeEventEnvelope[];
  nextCursor?: string;
  hasMore: boolean;
  scanned: number;
}

export interface ProjectionCursor {
  projectorId: string;
  aggregateId: string;
  sequence: number;
  eventId: string;
  digest: string;
  updatedAt: string;
}

export interface ProjectedSession {
  aggregateId: string;
  runId: string;
  sessionId?: string;
  taskId: string;
  status: string;
  lastEventId: string;
  lastSequence: number;
  lastIntent: string;
  effectiveTransitions: number;
  nonEffectiveEvents: number;
  toolCallsStarted: number;
  toolCallsSettled: number;
  permissionPending: number;
  artifactCount: number;
  workerRoutes: number;
  failureCount: number;
  compactCount: number;
  metadata: Readonly<Record<string, JsonValue>>;
  updatedAt: string;
}

export interface ProjectedToolCall {
  aggregateId: string;
  toolCallId: string;
  toolName: string;
  status: "input" | "called" | "running" | "succeeded" | "failed" | "cancelled";
  calledEventId?: string;
  settledEventId?: string;
  inputDigest?: string;
  resultDigest?: string;
  artifactRefs: readonly ArtifactPointer[];
  progressSequence: number;
  providerExecuted: boolean;
  malformedResult: boolean;
  errorCode?: string;
  updatedAt: string;
}

export interface ProjectedControlState {
  aggregateId: string;
  controlId: string;
  controlKind: string;
  status: string;
  requestedEventId: string;
  resolvedEventId?: string;
  decision?: string;
  actorId?: string;
  updatedAt: string;
}

export interface ProjectedArtifact {
  aggregateId: string;
  artifact: ArtifactPointer;
  sourceEventId: string;
  intent: MessageIntentValue;
  updatedAt: string;
}

export interface SpineMetrics {
  eventCount: number;
  effectiveEventCount: number;
  nonEffectiveEventCount: number;
  duplicateEventCount: number;
  deliveryCount: number;
  acknowledgedDeliveryCount: number;
  redeliveryCount: number;
  deadLetterCount: number;
  artifactRefCount: number;
  artifactSpillCount: number;
  inlineBytesTotal: number;
  sourceBytesTotal: number;
  envelopeBytes: readonly number[];
  routeRecipientCounts: readonly number[];
  availableRecipientCounts: readonly number[];
  broadcastCount: number;
  messageCount: number;
  tokenEstimate: number;
}

export interface LowEntropyReport {
  eventCount: number;
  envelopeBytes: { p50: number; p95: number; max: number };
  inlineToSourceByteRatio: number;
  artifactRefRate: number;
  artifactRefOffloadRatio: number;
  duplicateRate: number;
  redeliveryRate: number;
  routeDensity: number;
  broadcastRatio: number;
  messagesPerThousandTokens: number;
  duplicateFactRate: number;
  taskSuccess: number;
  passedHardLimits: boolean;
  findings: readonly string[];
}

export interface BaselineComparison {
  strategy: "targeted_artifact_ref" | "static_route" | "full_broadcast" | "full_text_inline";
  routeDensity: number;
  broadcastRatio: number;
  messageCount: number;
  messagesPerThousandTokens: number;
  duplicateFactRate: number;
  artifactRefOffloadRatio: number;
  inlineTokenEstimate: number;
  taskSuccess: number;
}

const EVENT_DURABILITY_VALUES = new Set<string>(Object.values(EventDurability));
const EVENT_EFFECT_VALUES = new Set<string>(Object.values(EventEffect));
const INTENT_VALUES = new Set<string>(Object.values(MessageIntent));
const SENDER_VALUES = new Set<string>(Object.values(SenderKind));
const RECIPIENT_VALUES = new Set<string>(Object.values(RecipientKind));
const TRUST_VALUES = new Set<string>(Object.values(TrustLevel));
const BACKPRESSURE_VALUES = new Set<string>(Object.values(BackpressureMode));

function enumValue<T extends string>(value: unknown, field: string, allowed: ReadonlySet<string>): T {
  const selected = requireString(value, field, 128);
  if (!allowed.has(selected)) throw new EnvelopeValidationError(`${field} has unsupported value`, { field, value: selected });
  return selected as T;
}

export function parseIdentity(value: unknown): RuntimeIdentity {
  const input = requireRecord(value, "identity");
  return {
    runId: normalizeIdentifier(input.runId, "identity.runId"),
    sessionId: optionalString(input.sessionId, "identity.sessionId", 256),
    taskId: normalizeIdentifier(input.taskId, "identity.taskId"),
    nodeId: optionalString(input.nodeId, "identity.nodeId", 256),
    workerId: optionalString(input.workerId, "identity.workerId", 256),
    toolCallId: optionalString(input.toolCallId, "identity.toolCallId", 256),
    commandId: optionalString(input.commandId, "identity.commandId", 256),
  };
}

export function parseSender(value: unknown): SenderRef {
  const input = requireRecord(value, "sender");
  return {
    kind: enumValue(input.kind, "sender.kind", SENDER_VALUES),
    id: normalizeIdentifier(input.id, "sender.id"),
    role: optionalString(input.role, "sender.role", 128),
    capabilityRefs: stringArray(input.capabilityRefs, "sender.capabilityRefs", 128),
  };
}

export function parseRecipient(value: unknown, field = "recipient"): RecipientRef {
  const input = requireRecord(value, field);
  return {
    kind: enumValue(input.kind, `${field}.kind`, RECIPIENT_VALUES),
    id: normalizeIdentifier(input.id, `${field}.id`),
    requiredCapabilities: stringArray(input.requiredCapabilities, `${field}.requiredCapabilities`, 128),
  };
}

export function parseArtifactPointer(value: unknown, field = "artifactRef"): ArtifactPointer {
  const input = requireRecord(value, field);
  const digest = requireString(input.digest, `${field}.digest`, 80);
  if (!isDigest(digest)) throw new EnvelopeValidationError(`${field}.digest must be sha256`, { digest });
  return {
    schema: ARTIFACT_REF_SCHEMA_VERSION,
    artifactId: normalizeIdentifier(input.artifactId, `${field}.artifactId`),
    digest,
    mediaType: requireString(input.mediaType, `${field}.mediaType`, 256),
    sizeBytes: requireInteger(input.sizeBytes, `${field}.sizeBytes`),
    title: boundedText(input.title, 512),
    uri: optionalString(input.uri, `${field}.uri`, 2048),
    producerNodeId: optionalString(input.producerNodeId, `${field}.producerNodeId`, 256),
    metadata: optionalRecord(input.metadata, `${field}.metadata`),
  };
}

export function parseEvidencePointer(value: unknown, field = "evidenceRef"): EvidencePointer {
  const input = requireRecord(value, field);
  return {
    schema: EVIDENCE_REF_SCHEMA_VERSION,
    evidenceId: normalizeIdentifier(input.evidenceId, `${field}.evidenceId`),
    source: requireString(input.source, `${field}.source`, 512),
    summary: boundedText(input.summary, 1024),
    confidence: input.confidence === undefined ? undefined : requireFinite(input.confidence, `${field}.confidence`, 0, 1),
    artifactId: optionalString(input.artifactId, `${field}.artifactId`, 256),
    metadata: optionalRecord(input.metadata, `${field}.metadata`),
  };
}

export function parseProvenance(value: unknown): Provenance {
  const input = requireRecord(value, "provenance");
  const role = requireString(input.migrationRole, "provenance.migrationRole", 64);
  if (!["primary", "supplementary", "conformance", "reference", "zyra_owned"].includes(role)) {
    throw new EnvelopeValidationError("unsupported migration role", { role });
  }
  const sourceDigest = optionalString(input.sourceDigest, "provenance.sourceDigest", 80);
  if (sourceDigest && !isDigest(sourceDigest)) throw new EnvelopeValidationError("source digest must be sha256", { sourceDigest });
  return {
    sourceRepository: requireString(input.sourceRepository, "provenance.sourceRepository", 256),
    sourceModule: requireString(input.sourceModule, "provenance.sourceModule", 1024),
    migrationRole: role as Provenance["migrationRole"],
    producerVersion: requireString(input.producerVersion, "provenance.producerVersion", 128),
    trust: enumValue(input.trust, "provenance.trust", TRUST_VALUES),
    normalizedFrom: optionalString(input.normalizedFrom, "provenance.normalizedFrom", 512),
    sourceEventId: optionalString(input.sourceEventId, "provenance.sourceEventId", 256),
    sourceDigest,
  };
}

export function parseStateDelta(value: unknown): StateDelta {
  const input = value === undefined || value === null ? {} : requireRecord(value, "stateDelta");
  const operation = String(input.operation ?? "none");
  if (!["set", "merge", "append", "remove", "transition", "none"].includes(operation)) {
    throw new EnvelopeValidationError("unsupported state delta operation", { operation });
  }
  const beforeDigest = optionalString(input.beforeDigest, "stateDelta.beforeDigest", 80);
  const afterDigest = optionalString(input.afterDigest, "stateDelta.afterDigest", 80);
  if (beforeDigest && !isDigest(beforeDigest)) throw new EnvelopeValidationError("before digest must be sha256", { beforeDigest });
  if (afterDigest && !isDigest(afterDigest)) throw new EnvelopeValidationError("after digest must be sha256", { afterDigest });
  if (input.value !== undefined) assertJsonValue(input.value, "stateDelta.value");
  return {
    domain: requireString(input.domain ?? "runtime", "stateDelta.domain", 128),
    operation: operation as StateDelta["operation"],
    path: stringArray(input.path, "stateDelta.path", 64),
    beforeDigest,
    afterDigest,
    value: input.value as JsonValue | undefined,
    effective: input.effective === undefined ? operation !== "none" : requireBoolean(input.effective, "stateDelta.effective"),
  };
}

export function parseRuntimeEventDraft(value: unknown): RuntimeEventDraft {
  const input = requireRecord(value, "event");
  const topKInput = input.topKRecipients ?? [];
  if (!Array.isArray(topKInput)) throw new EnvelopeValidationError("topKRecipients must be an array");
  if (topKInput.length > 64) throw new EnvelopeValidationError("too many top-k recipients", { count: topKInput.length });
  const evidenceInput = input.evidenceRefs ?? [];
  const artifactInput = input.artifactRefs ?? [];
  if (!Array.isArray(evidenceInput) || !Array.isArray(artifactInput)) {
    throw new EnvelopeValidationError("evidenceRefs and artifactRefs must be arrays");
  }
  return {
    eventId: optionalString(input.eventId, "event.eventId", 256),
    eventType: requireString(input.eventType, "event.eventType", 256),
    eventVersion: optionalInteger(input.eventVersion, "event.eventVersion", 1),
    aggregateId: normalizeIdentifier(input.aggregateId, "event.aggregateId", 512),
    expectedSequence: optionalInteger(input.expectedSequence, "event.expectedSequence", 0),
    producerSequence: optionalInteger(input.producerSequence, "event.producerSequence", 0),
    idempotencyKey: requireString(input.idempotencyKey, "event.idempotencyKey", 512),
    correlationId: normalizeIdentifier(input.correlationId, "event.correlationId", 512),
    causationId: optionalString(input.causationId, "event.causationId", 256),
    createdAt: input.createdAt === undefined ? undefined : parseTimestamp(input.createdAt, "event.createdAt"),
    durability: input.durability === undefined
      ? undefined
      : enumValue<EventDurabilityValue>(input.durability, "event.durability", EVENT_DURABILITY_VALUES),
    effect: input.effect === undefined
      ? undefined
      : enumValue<EventEffectValue>(input.effect, "event.effect", EVENT_EFFECT_VALUES),
    identity: parseIdentity(input.identity),
    sender: parseSender(input.sender),
    intent: enumValue(input.intent, "event.intent", INTENT_VALUES),
    target: input.target === undefined ? undefined : parseRecipient(input.target, "event.target"),
    topKRecipients: topKInput.map((item, index) => parseRecipient(item, `event.topKRecipients[${index}]`)),
    summary: optionalString(input.summary, "event.summary", 4096),
    stateDelta: parseStateDelta(input.stateDelta),
    evidenceRefs: evidenceInput.map((item, index) => parseEvidencePointer(item, `event.evidenceRefs[${index}]`)),
    artifactRefs: artifactInput.map((item, index) => parseArtifactPointer(item, `event.artifactRefs[${index}]`)),
    uncertainty: input.uncertainty === undefined ? undefined : requireFinite(input.uncertainty, "event.uncertainty", 0, 1),
    provenance: parseProvenance(input.provenance),
    inline: optionalRecord(input.inline, "event.inline"),
    sourceBytes: optionalInteger(input.sourceBytes, "event.sourceBytes", 0),
    metadata: optionalRecord(input.metadata, "event.metadata"),
  };
}

export function parseSubscriptionSpec(value: unknown): SubscriptionSpec {
  const input = requireRecord(value, "subscription");
  const intents = stringArray(input.intents, "subscription.intents", 64).map((intent) =>
    enumValue<MessageIntentValue>(intent, "subscription.intent", INTENT_VALUES),
  );
  return {
    subscriptionId: normalizeIdentifier(input.subscriptionId, "subscription.subscriptionId", 256),
    recipient: parseRecipient(input.recipient, "subscription.recipient"),
    eventTypes: stringArray(input.eventTypes, "subscription.eventTypes", 256),
    intents,
    aggregatePrefixes: stringArray(input.aggregatePrefixes, "subscription.aggregatePrefixes", 64),
    taskIds: stringArray(input.taskIds, "subscription.taskIds", 256),
    capabilityRefs: stringArray(input.capabilityRefs, "subscription.capabilityRefs", 128),
    capacity: requireInteger(input.capacity ?? 1024, "subscription.capacity", 1),
    maxAttempts: requireInteger(input.maxAttempts ?? 5, "subscription.maxAttempts", 1),
    ackTimeoutMs: requireInteger(input.ackTimeoutMs ?? 30_000, "subscription.ackTimeoutMs", 100),
    backpressureMode: enumValue(input.backpressureMode ?? BackpressureMode.REJECT, "subscription.backpressureMode", BACKPRESSURE_VALUES),
    enabled: input.enabled === undefined ? true : requireBoolean(input.enabled, "subscription.enabled"),
    priority: requireInteger(input.priority ?? 100, "subscription.priority", 0),
    metadata: optionalRecord(input.metadata, "subscription.metadata"),
  };
}

export function parseEventQuery(value: unknown): EventQuery {
  const input = value === undefined || value === null ? {} : requireRecord(value, "query");
  const intents = stringArray(input.intents, "query.intents", 64).map((intent) =>
    enumValue<MessageIntentValue>(intent, "query.intent", INTENT_VALUES),
  );
  const effects = stringArray(input.effects, "query.effects", 8).map((effect) =>
    enumValue<EventEffectValue>(effect, "query.effect", EVENT_EFFECT_VALUES),
  );
  return {
    aggregateId: optionalString(input.aggregateId, "query.aggregateId", 512),
    runId: optionalString(input.runId, "query.runId", 256),
    sessionId: optionalString(input.sessionId, "query.sessionId", 256),
    taskId: optionalString(input.taskId, "query.taskId", 256),
    workerId: optionalString(input.workerId, "query.workerId", 256),
    eventTypes: stringArray(input.eventTypes, "query.eventTypes", 256),
    intents,
    effects,
    afterSequence: optionalInteger(input.afterSequence, "query.afterSequence", 0),
    beforeSequence: optionalInteger(input.beforeSequence, "query.beforeSequence", 0),
    createdAtGte: input.createdAtGte === undefined ? undefined : parseTimestamp(input.createdAtGte, "query.createdAtGte"),
    createdAtLt: input.createdAtLt === undefined ? undefined : parseTimestamp(input.createdAtLt, "query.createdAtLt"),
    correlationId: optionalString(input.correlationId, "query.correlationId", 512),
    causationId: optionalString(input.causationId, "query.causationId", 256),
    artifactId: optionalString(input.artifactId, "query.artifactId", 256),
    cursor: optionalString(input.cursor, "query.cursor", 2048),
    limit: input.limit === undefined ? 100 : Math.min(1000, requireInteger(input.limit, "query.limit", 1)),
    descending: input.descending === undefined ? false : requireBoolean(input.descending, "query.descending"),
  };
}

export function buildCommittedEnvelope(
  draft: RuntimeEventDraft,
  sequence: number,
  committedAt: string,
  inlineBytes: number,
  envelopeBytes: number,
): RuntimeEventEnvelope {
  const event: Omit<RuntimeEventEnvelope, "contentDigest"> = {
    schema: EVENT_SCHEMA_VERSION,
    eventId: draft.eventId ?? newId("evt"),
    eventType: draft.eventType,
    eventVersion: draft.eventVersion ?? 1,
    aggregateId: draft.aggregateId,
    aggregateSequence: sequence,
    producerSequence: draft.producerSequence ?? sequence,
    idempotencyKey: draft.idempotencyKey,
    correlationId: draft.correlationId,
    causationId: draft.causationId,
    createdAt: draft.createdAt ?? utcNow(),
    committedAt,
    durability: draft.durability ?? EventDurability.DURABLE,
    effect: draft.effect ?? EventEffect.UNKNOWN,
    identity: draft.identity,
    sender: draft.sender,
    intent: draft.intent,
    target: draft.target,
    topKRecipients: draft.topKRecipients ?? [],
    summary: draft.summary ?? "",
    stateDelta: draft.stateDelta ?? parseStateDelta(undefined),
    evidenceRefs: draft.evidenceRefs ?? [],
    artifactRefs: draft.artifactRefs ?? [],
    uncertainty: draft.uncertainty,
    provenance: draft.provenance,
    inline: draft.inline ?? {},
    sourceBytes: draft.sourceBytes ?? inlineBytes,
    inlineBytes,
    envelopeBytes,
    metadata: draft.metadata ?? {},
  };
  return { ...event, contentDigest: digestJson(event) };
}

export function messageFromEvent(event: RuntimeEventEnvelope, messageId = newId("msg")): AgentMessageEnvelope {
  const value: AgentMessageEnvelope = {
    schema: MESSAGE_SCHEMA_VERSION,
    messageId,
    eventId: event.eventId,
    eventType: event.eventType,
    aggregateId: event.aggregateId,
    aggregateSequence: event.aggregateSequence,
    producerSequence: event.producerSequence,
    idempotencyKey: event.idempotencyKey,
    correlationId: event.correlationId,
    causationId: event.causationId,
    identity: cloneJson(event.identity as unknown as JsonValue) as unknown as RuntimeIdentity,
    sender: cloneJson(event.sender as unknown as JsonValue) as unknown as SenderRef,
    intent: event.intent,
    target: event.target ? (cloneJson(event.target as unknown as JsonValue) as unknown as RecipientRef) : undefined,
    topKRecipients: cloneJson(event.topKRecipients as unknown as JsonValue) as unknown as RecipientRef[],
    summary: event.summary,
    stateDelta: cloneJson(event.stateDelta as unknown as JsonValue) as unknown as StateDelta,
    evidenceRefs: cloneJson(event.evidenceRefs as unknown as JsonValue) as unknown as EvidencePointer[],
    artifactRefs: cloneJson(event.artifactRefs as unknown as JsonValue) as unknown as ArtifactPointer[],
    uncertainty: event.uncertainty,
    provenance: cloneJson(event.provenance as unknown as JsonValue) as unknown as Provenance,
    eventDigest: event.contentDigest,
    createdAt: event.createdAt,
    metadata: {
      route_required: true,
      canonical_event_ref_only: true,
    },
  };
  return value;
}

export function assertEnvelopeDigest(event: RuntimeEventEnvelope): void {
  const { contentDigest, ...unsigned } = event;
  const expected = digestJson(unsigned);
  if (contentDigest !== expected) {
    throw new EnvelopeValidationError("event content digest mismatch", {
      event_id: event.eventId,
      expected,
      actual: contentDigest,
    });
  }
}

export function eventToJson(event: RuntimeEventEnvelope): Record<string, JsonValue> {
  assertJsonValue(event);
  return cloneJson(event as unknown as JsonValue) as Record<string, JsonValue>;
}

export function eventFromJson(value: unknown): RuntimeEventEnvelope {
  const input = requireRecord(value, "event");
  if (input.schema !== EVENT_SCHEMA_VERSION) throw new EnvelopeValidationError("unsupported event schema", { schema: input.schema });
  const draft = parseRuntimeEventDraft({
    ...input,
    expectedSequence: input.aggregateSequence,
  });
  const event = buildCommittedEnvelope(
    { ...draft, eventId: requireString(input.eventId, "event.eventId", 256) },
    requireInteger(input.aggregateSequence, "event.aggregateSequence", 0),
    parseTimestamp(input.committedAt, "event.committedAt"),
    requireInteger(input.inlineBytes, "event.inlineBytes", 0),
    requireInteger(input.envelopeBytes, "event.envelopeBytes", 0),
  );
  const storedDigest = requireString(input.contentDigest, "event.contentDigest", 80);
  const restored = { ...event, contentDigest: storedDigest };
  assertEnvelopeDigest(restored);
  return restored;
}

export function subscriptionToJson(spec: SubscriptionSpec): Record<string, JsonValue> {
  assertJsonValue(spec);
  return cloneJson(spec as unknown as JsonValue) as Record<string, JsonValue>;
}

export function deliveryToJson(record: DeliveryRecord): Record<string, JsonValue> {
  assertJsonValue(record);
  return cloneJson(record as unknown as JsonValue) as Record<string, JsonValue>;
}

export function assertRuntimeIdentityMatchesAggregate(identity: RuntimeIdentity, aggregateId: string): void {
  if (!aggregateId.includes(identity.taskId) && !aggregateId.includes(identity.runId)) {
    throw new EnvelopeValidationError("aggregate identity is not tied to canonical run/task", {
      aggregate_id: aggregateId,
      run_id: identity.runId,
      task_id: identity.taskId,
    });
  }
}

export function normalizeLegacyRecord(value: unknown): LegacyEventRecord {
  if (!isPlainObject(value)) throw new EnvelopeValidationError("legacy event must be an object");
  const payload = isPlainObject(value.payload) ? value.payload : {};
  assertJsonValue(payload, "legacy.payload");
  return {
    event_id: optionalString(value.event_id ?? value.eventId, "legacy.event_id", 256),
    run_id: requireString(value.run_id ?? value.runId, "legacy.run_id", 256),
    task_id: requireString(value.task_id ?? value.taskId, "legacy.task_id", 256),
    node_id: optionalString(value.node_id ?? value.nodeId, "legacy.node_id", 256),
    event_type: requireString(value.event_type ?? value.eventType, "legacy.event_type", 256),
    created_at: value.created_at ?? value.createdAt ? parseTimestamp(value.created_at ?? value.createdAt, "legacy.created_at") : utcNow(),
    payload,
  };
}
