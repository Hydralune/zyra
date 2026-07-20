import {
  EventDurability,
  EventEffect,
  RecipientKind,
  SenderKind,
  TrustLevel,
  type ArtifactPointer,
  type EventEffectValue,
  type MessageIntentValue,
  type RecipientRef,
  type RuntimeEventDraft,
  type RuntimeEventEnvelope,
  type RuntimeIdentity,
  type SenderKindValue,
  type SenderRef,
  type StateDelta,
} from "./contracts.ts";
import {
  assertJsonValue,
  boundedText,
  canonicalJson,
  cloneJson,
  digestJson,
  isPlainObject,
  normalizeIdentifier,
  optionalInteger,
  optionalRecord,
  optionalString,
  parseTimestamp,
  requireBoolean,
  requireInteger,
  requireRecord,
  requireString,
  stringArray,
  utcNow,
  type JsonValue,
} from "./canonical.ts";
import { eventDefinition } from "./event-catalog.ts";
import { EnvelopeValidationError } from "./errors.ts";

/**
 * Versioned contract shared by runtime source adapters, the durable consumer
 * runner, the API stream, and the M2 projector clients.  The canonical event
 * remains the only fact; every object in this module is an ingress command or
 * a read-side cursor and therefore cannot be written back as canonical state.
 */
export const RUNTIME_INTEGRATION_SCHEMA = "zyra.runtime-event-integration/v1";
export const SOURCE_RECORD_SCHEMA = "zyra.runtime-source-record/v1";
export const SOURCE_BATCH_SCHEMA = "zyra.runtime-source-batch/v1";
export const CONSUMER_CHECKPOINT_SCHEMA = "zyra.runtime-consumer-checkpoint/v1";
export const PROJECTOR_STREAM_SCHEMA = "zyra.runtime-projector-stream/v1";
export const RECONCILIATION_SCHEMA = "zyra.runtime-event-reconciliation/v1";
export const DISABLE_MATRIX_SCHEMA = "zyra.runtime-event-disable-matrix/v1";
export const ARTIFACT_READ_SCHEMA = "zyra.runtime-artifact-read/v1";
export const BASELINE_REPORT_SCHEMA = "zyra.runtime-message-baseline/v1";

export const SourceDomain = {
  CODEWORKER: "codeworker",
  PERMISSION: "permission",
  MCP: "mcp",
  SKILL: "skill",
  SUBAGENT: "subagent",
  BROWSER: "browser",
  GATEWAY: "gateway",
  ARTIFACT: "artifact",
  CONTROL: "control",
  COMPACT: "compact",
  API_TRANSPORT: "api_transport",
  BACKEND: "backend",
  RECOVERY: "recovery",
} as const;
export type SourceDomainValue = (typeof SourceDomain)[keyof typeof SourceDomain];

export const SourceRecordKind = {
  QUERY_ADMITTED: "query_admitted",
  QUERY_QUEUED: "query_queued",
  QUERY_PROMOTED: "query_promoted",
  TURN_STARTED: "turn_started",
  TURN_COMPLETED: "turn_completed",
  TURN_FAILED: "turn_failed",
  TEXT_STARTED: "text_started",
  TEXT_DELTA: "text_delta",
  TEXT_ENDED: "text_ended",
  REASONING_STARTED: "reasoning_started",
  REASONING_DELTA: "reasoning_delta",
  REASONING_ENDED: "reasoning_ended",
  TOOL_INPUT_STARTED: "tool_input_started",
  TOOL_INPUT_DELTA: "tool_input_delta",
  TOOL_INPUT_ENDED: "tool_input_ended",
  TOOL_CALLED: "tool_called",
  TOOL_PROGRESS: "tool_progress",
  TOOL_SUCCEEDED: "tool_succeeded",
  TOOL_FAILED: "tool_failed",
  TOOL_CANCELLED: "tool_cancelled",
  PERMISSION_REQUESTED: "permission_requested",
  PERMISSION_PENDING: "permission_pending",
  PERMISSION_ALLOWED: "permission_allowed",
  PERMISSION_DENIED: "permission_denied",
  PERMISSION_GRANT_CONSUMED: "permission_grant_consumed",
  MCP_CONNECTION: "mcp_connection",
  MCP_AUTH_REQUESTED: "mcp_auth_requested",
  MCP_AUTH_RESOLVED: "mcp_auth_resolved",
  MCP_ELICITATION_REQUESTED: "mcp_elicitation_requested",
  MCP_ELICITATION_RESOLVED: "mcp_elicitation_resolved",
  MCP_TOOL_CALLED: "mcp_tool_called",
  MCP_TOOL_RESULT: "mcp_tool_result",
  MCP_RESOURCE_READ: "mcp_resource_read",
  MCP_PROMPT_LOADED: "mcp_prompt_loaded",
  SKILL_STARTED: "skill_started",
  SKILL_COMPLETED: "skill_completed",
  SKILL_FAILED: "skill_failed",
  SUBAGENT_CREATED: "subagent_created",
  SUBAGENT_DISPATCHED: "subagent_dispatched",
  SUBAGENT_PROGRESS: "subagent_progress",
  SUBAGENT_YIELD: "subagent_yield",
  SUBAGENT_COMPLETED: "subagent_completed",
  SUBAGENT_FAILED: "subagent_failed",
  SUBAGENT_CANCELLED: "subagent_cancelled",
  SUBAGENT_LATE_RESULT: "subagent_late_result",
  BROWSER_OBSERVATION: "browser_observation",
  BROWSER_FAILURE: "browser_failure",
  WORKER_HEALTH: "worker_health",
  ARTIFACT_COMMITTED: "artifact_committed",
  ARTIFACT_QUARANTINED: "artifact_quarantined",
  CONTROL_REQUESTED: "control_requested",
  CONTROL_ACCEPTED: "control_accepted",
  CONTROL_REJECTED: "control_rejected",
  CONTROL_COMPLETED: "control_completed",
  COMPACT_STARTED: "compact_started",
  COMPACT_COMPLETED: "compact_completed",
  COMPACT_RESTORE: "compact_restore",
  API_STREAM_RETRY: "api_stream_retry",
  API_STREAM_DISCONNECTED: "api_stream_disconnected",
  BACKEND_DISPATCH_REQUESTED: "backend_dispatch_requested",
  BACKEND_DISPATCH_ACCEPTED: "backend_dispatch_accepted",
  BACKEND_DISPATCH_FAILED: "backend_dispatch_failed",
  BACKEND_FAILOVER: "backend_failover",
  RECOVERY_REQUESTED: "recovery_requested",
  RECOVERY_PLANNED: "recovery_planned",
  RECOVERY_COMPLETED: "recovery_completed",
} as const;
export type SourceRecordKindValue = (typeof SourceRecordKind)[keyof typeof SourceRecordKind];

export const DeliveryMode = {
  TARGETED: "targeted",
  DEPENDENCY: "dependency",
  CRITICAL_BROADCAST: "critical_broadcast",
  STORE_ONLY: "store_only",
  LIVE_ONLY: "live_only",
} as const;
export type DeliveryModeValue = (typeof DeliveryMode)[keyof typeof DeliveryMode];

export const ConsumerRole = {
  API_PROJECTION: "api_projection",
  UI_PROJECTION: "ui_projection",
  WORKER: "worker",
  SCHEDULER: "scheduler",
  CONTROL: "control",
  MCP: "mcp",
  MEMORY: "memory",
  ARTIFACT: "artifact",
  RECOVERY: "recovery",
  AUDITOR: "auditor",
} as const;
export type ConsumerRoleValue = (typeof ConsumerRole)[keyof typeof ConsumerRole];

export const ConsumerOutcome = {
  ACKNOWLEDGED: "acknowledged",
  RETRY: "retry",
  DEAD_LETTER: "dead_letter",
  DUPLICATE: "duplicate",
  SKIPPED: "skipped",
} as const;
export type ConsumerOutcomeValue = (typeof ConsumerOutcome)[keyof typeof ConsumerOutcome];

export const StreamFrameKind = {
  SNAPSHOT: "snapshot",
  EVENT: "event",
  GAP: "gap",
  RESET: "reset",
  HEARTBEAT: "heartbeat",
  END: "end",
} as const;
export type StreamFrameKindValue = (typeof StreamFrameKind)[keyof typeof StreamFrameKind];

export interface SourceOwnerRef {
  domain: SourceDomainValue;
  ownerId: string;
  transactionId?: string;
  storeRef?: string;
  committed: boolean;
  committedAt?: string;
}

export interface SourceCausality {
  correlationId: string;
  causationEventId?: string;
  requestId?: string;
  parentId?: string;
  producerSequence: number;
}

export interface SourceDelivery {
  mode: DeliveryModeValue;
  target?: RecipientRef;
  dependencyRecipients: readonly string[];
  topK?: number;
  broadcastPolicyId?: string;
  broadcastReason?: string;
}

export interface SourceRecord {
  schema: typeof SOURCE_RECORD_SCHEMA;
  sourceId: string;
  kind: SourceRecordKindValue;
  domain: SourceDomainValue;
  occurredAt: string;
  identity: RuntimeIdentity;
  owner: SourceOwnerRef;
  causality: SourceCausality;
  delivery: SourceDelivery;
  subjectId?: string;
  summary: string;
  effective?: boolean;
  uncertainty?: number;
  payload: Readonly<Record<string, JsonValue>>;
  evidenceRefs: NonNullable<RuntimeEventDraft["evidenceRefs"]>;
  artifactRefs: readonly ArtifactPointer[];
  metadata: Readonly<Record<string, JsonValue>>;
}

export interface SourceBatch {
  schema: typeof SOURCE_BATCH_SCHEMA;
  batchId: string;
  producerId: string;
  records: readonly SourceRecord[];
  atomic: boolean;
  expectedCount: number;
  createdAt: string;
}

export interface IntegrationAppendOptions {
  route: boolean;
  project: boolean;
  publishLegacyProjection: boolean;
  strictOwner: boolean;
  ownerId?: string;
  dependencyRecipients: readonly string[];
  topK?: number;
  broadcast: boolean;
  fanoutReason?: string;
}

export interface MappedSourceEvent {
  schema: typeof RUNTIME_INTEGRATION_SCHEMA;
  source: SourceRecord;
  draft: RuntimeEventDraft;
  options: IntegrationAppendOptions;
  warnings: readonly string[];
}

export interface ConsumerCheckpoint {
  schema: typeof CONSUMER_CHECKPOINT_SCHEMA;
  consumerId: string;
  subscriptionId: string;
  role: ConsumerRoleValue;
  lastGlobalSequence: number;
  lastEventId?: string;
  lastDeliveryId?: string;
  lastEventDigest?: string;
  handledCount: number;
  duplicateCount: number;
  retryCount: number;
  deadLetterCount: number;
  generation: number;
  startedAt: string;
  updatedAt: string;
}

export interface ConsumerResult {
  consumerId: string;
  subscriptionId: string;
  deliveryId: string;
  eventId: string;
  globalSequence: number;
  outcome: ConsumerOutcomeValue;
  attempt: number;
  startedAt: string;
  completedAt: string;
  durationMs: number;
  errorCode?: string;
  errorMessage?: string;
  checkpoint: ConsumerCheckpoint;
}

export interface ProjectorStreamCursor {
  aggregateId: string;
  globalSequence: number;
  projectionSequence: number;
  eventId?: string;
  projectionDigest?: string;
  generation: number;
}

export interface ProjectorStreamFrame {
  schema: typeof PROJECTOR_STREAM_SCHEMA;
  frameId: string;
  kind: StreamFrameKindValue;
  aggregateId: string;
  cursor: ProjectorStreamCursor;
  event?: RuntimeEventEnvelope;
  projection?: Readonly<Record<string, JsonValue>>;
  missingSequences: readonly number[];
  retryAfterMs?: number;
  createdAt: string;
  metadata: Readonly<Record<string, JsonValue>>;
}

export interface ReconciliationFinding {
  code: string;
  severity: "info" | "warning" | "error";
  aggregateId?: string;
  eventId?: string;
  deliveryId?: string;
  expected?: JsonValue;
  actual?: JsonValue;
  repairable: boolean;
  repaired: boolean;
  summary: string;
}

export interface ReconciliationReport {
  schema: typeof RECONCILIATION_SCHEMA;
  startedAt: string;
  completedAt: string;
  highWatermark: number;
  scannedEvents: number;
  scannedAggregates: number;
  repairedDeliveries: number;
  rebuiltAggregates: number;
  findings: readonly ReconciliationFinding[];
  equivalentAfterRepair: boolean;
}

export interface DisableMatrixCell {
  component: "event_store" | "message_bus" | "projector" | "omp_mapper";
  canonicalWrite: boolean;
  liveDelivery: boolean;
  replay: boolean;
  queryView: boolean;
  typedFrameAdmission: boolean;
  expectedFailure: string;
  observedFailure?: string;
  passed: boolean;
}

export interface DisableMatrixReport {
  schema: typeof DISABLE_MATRIX_SCHEMA;
  aggregateId: string;
  cells: readonly DisableMatrixCell[];
  passed: boolean;
  checkedAt: string;
}

export interface ArtifactReadRequest {
  artifactId: string;
  expectedDigest?: string;
  offset: number;
  length?: number;
  encoding: "base64" | "utf8";
}

export interface ArtifactReadResult {
  schema: typeof ARTIFACT_READ_SCHEMA;
  artifact: ArtifactPointer;
  verified: boolean;
  offset: number;
  returnedBytes: number;
  totalBytes: number;
  truncated: boolean;
  encoding: "base64" | "utf8";
  data: string;
}

const SOURCE_DOMAINS = new Set<string>(Object.values(SourceDomain));
const SOURCE_KINDS = new Set<string>(Object.values(SourceRecordKind));
const DELIVERY_MODES = new Set<string>(Object.values(DeliveryMode));
const CONSUMER_ROLES = new Set<string>(Object.values(ConsumerRole));
const STREAM_KINDS = new Set<string>(Object.values(StreamFrameKind));

function enumString<T extends string>(value: unknown, field: string, allowed: ReadonlySet<string>): T {
  const selected = requireString(value, field, 128);
  if (!allowed.has(selected)) {
    throw new EnvelopeValidationError(`${field} has unsupported value`, { field, value: selected });
  }
  return selected as T;
}

function jsonRecord(value: unknown, field: string): Readonly<Record<string, JsonValue>> {
  const input = value === undefined ? {} : requireRecord(value, field);
  assertJsonValue(input, field);
  return cloneJson(input) as Readonly<Record<string, JsonValue>>;
}

function nonNegative(value: unknown, field: string, fallback = 0): number {
  const parsed = optionalInteger(value, field) ?? fallback;
  if (parsed < 0) throw new EnvelopeValidationError(`${field} must be non-negative`, { field, value: parsed });
  return parsed;
}

function optionalProbability(value: unknown, field: string): number | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    throw new EnvelopeValidationError(`${field} must be between zero and one`, { field, value: String(value) });
  }
  return value;
}

function parseRecipientRef(value: unknown, field: string): RecipientRef {
  const input = requireRecord(value, field);
  const kind = requireString(input.kind, `${field}.kind`, 128);
  if (!Object.values(RecipientKind).includes(kind as never)) {
    throw new EnvelopeValidationError(`${field}.kind is unsupported`, { field, kind });
  }
  return {
    kind: kind as RecipientRef["kind"],
    id: normalizeIdentifier(input.id, `${field}.id`),
    requiredCapabilities: stringArray(input.requiredCapabilities, `${field}.requiredCapabilities`, 64),
  };
}

function parseIdentityRecord(value: unknown): RuntimeIdentity {
  const input = requireRecord(value, "source.identity");
  return {
    runId: normalizeIdentifier(input.runId, "source.identity.runId"),
    sessionId: optionalString(input.sessionId, "source.identity.sessionId", 256),
    taskId: normalizeIdentifier(input.taskId, "source.identity.taskId"),
    nodeId: optionalString(input.nodeId, "source.identity.nodeId", 256),
    workerId: optionalString(input.workerId, "source.identity.workerId", 256),
    toolCallId: optionalString(input.toolCallId, "source.identity.toolCallId", 256),
    commandId: optionalString(input.commandId, "source.identity.commandId", 256),
  };
}

function parseArtifact(value: unknown, field: string): ArtifactPointer {
  const input = requireRecord(value, field);
  const digest = requireString(input.digest, `${field}.digest`, 80);
  if (!/^sha256:[0-9a-f]{64}$/u.test(digest)) {
    throw new EnvelopeValidationError(`${field}.digest must be a sha256 digest`, { digest });
  }
  return {
    schema: "zyra.artifact-ref/v1",
    artifactId: normalizeIdentifier(input.artifactId, `${field}.artifactId`),
    digest,
    mediaType: requireString(input.mediaType, `${field}.mediaType`, 256),
    sizeBytes: nonNegative(input.sizeBytes, `${field}.sizeBytes`),
    title: boundedText(input.title, 512),
    uri: optionalString(input.uri, `${field}.uri`, 2048),
    producerNodeId: optionalString(input.producerNodeId, `${field}.producerNodeId`, 256),
    metadata: jsonRecord(input.metadata, `${field}.metadata`),
  };
}

function omitOptionalUndefined(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => omitOptionalUndefined(item));
  if (!isPlainObject(value)) return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([, item]) => item !== undefined)
      .map(([key, item]) => [key, omitOptionalUndefined(item)]),
  );
}

export function parseSourceOwner(value: unknown): SourceOwnerRef {
  const input = requireRecord(value, "source.owner");
  const domain = enumString<SourceDomainValue>(input.domain, "source.owner.domain", SOURCE_DOMAINS);
  const committed = requireBoolean(input.committed, "source.owner.committed");
  const committedAt = optionalString(input.committedAt, "source.owner.committedAt", 128);
  if (committedAt) parseTimestamp(committedAt, "source.owner.committedAt");
  return {
    domain,
    ownerId: normalizeIdentifier(input.ownerId, "source.owner.ownerId"),
    transactionId: optionalString(input.transactionId, "source.owner.transactionId", 256),
    storeRef: optionalString(input.storeRef, "source.owner.storeRef", 2048),
    committed,
    committedAt,
  };
}

export function parseSourceCausality(value: unknown): SourceCausality {
  const input = requireRecord(value, "source.causality");
  return {
    correlationId: normalizeIdentifier(input.correlationId, "source.causality.correlationId"),
    causationEventId: optionalString(input.causationEventId, "source.causality.causationEventId", 256),
    requestId: optionalString(input.requestId, "source.causality.requestId", 256),
    parentId: optionalString(input.parentId, "source.causality.parentId", 256),
    producerSequence: nonNegative(input.producerSequence, "source.causality.producerSequence"),
  };
}

export function parseSourceDelivery(value: unknown): SourceDelivery {
  const input = value === undefined ? {} : requireRecord(value, "source.delivery");
  const mode = enumString<DeliveryModeValue>(input.mode ?? DeliveryMode.TARGETED, "source.delivery.mode", DELIVERY_MODES);
  const target = input.target === undefined ? undefined : parseRecipientRef(input.target, "source.delivery.target");
  const topK = optionalInteger(input.topK, "source.delivery.topK");
  if (topK !== undefined && (topK < 1 || topK > 64)) {
    throw new EnvelopeValidationError("source.delivery.topK must be between 1 and 64", { topK });
  }
  const broadcastPolicyId = optionalString(input.broadcastPolicyId, "source.delivery.broadcastPolicyId", 256);
  const broadcastReason = optionalString(input.broadcastReason, "source.delivery.broadcastReason", 1024);
  if (mode === DeliveryMode.CRITICAL_BROADCAST && (!broadcastPolicyId || !broadcastReason)) {
    throw new EnvelopeValidationError("critical broadcast requires policy id and reason", { mode });
  }
  if (mode !== DeliveryMode.CRITICAL_BROADCAST && (broadcastPolicyId || broadcastReason)) {
    throw new EnvelopeValidationError("broadcast metadata is only valid for critical broadcast", { mode });
  }
  return {
    mode,
    target,
    dependencyRecipients: stringArray(input.dependencyRecipients, "source.delivery.dependencyRecipients", 64),
    topK,
    broadcastPolicyId,
    broadcastReason,
  };
}

export function parseSourceRecord(value: unknown): SourceRecord {
  const input = requireRecord(omitOptionalUndefined(value), "source record");
  const schema = input.schema ?? SOURCE_RECORD_SCHEMA;
  if (schema !== SOURCE_RECORD_SCHEMA) {
    throw new EnvelopeValidationError("source record schema is unsupported", { schema: String(schema) });
  }
  const payload = jsonRecord(input.payload, "source.payload");
  const evidenceInput = input.evidenceRefs ?? [];
  if (!Array.isArray(evidenceInput)) throw new EnvelopeValidationError("source.evidenceRefs must be an array");
  const evidenceRefs = evidenceInput.map((item, index) => {
    const evidence = requireRecord(item, `source.evidenceRefs[${index}]`);
    const output = {
      schema: "zyra.evidence-ref/v1" as const,
      evidenceId: normalizeIdentifier(evidence.evidenceId, `source.evidenceRefs[${index}].evidenceId`),
      source: requireString(evidence.source, `source.evidenceRefs[${index}].source`, 512),
      summary: boundedText(evidence.summary, 1024),
      confidence: optionalProbability(evidence.confidence, `source.evidenceRefs[${index}].confidence`),
      artifactId: optionalString(evidence.artifactId, `source.evidenceRefs[${index}].artifactId`, 256),
      metadata: jsonRecord(evidence.metadata, `source.evidenceRefs[${index}].metadata`),
    };
    return output;
  });
  const artifactInput = input.artifactRefs ?? [];
  if (!Array.isArray(artifactInput)) throw new EnvelopeValidationError("source.artifactRefs must be an array");
  if (artifactInput.length + evidenceRefs.length > 64) {
    throw new EnvelopeValidationError("source record exceeds reference limit", { count: artifactInput.length + evidenceRefs.length });
  }
  const record: SourceRecord = {
    schema: SOURCE_RECORD_SCHEMA,
    sourceId: normalizeIdentifier(input.sourceId, "source.sourceId"),
    kind: enumString<SourceRecordKindValue>(input.kind, "source.kind", SOURCE_KINDS),
    domain: enumString<SourceDomainValue>(input.domain, "source.domain", SOURCE_DOMAINS),
    occurredAt: parseTimestamp(input.occurredAt ?? utcNow(), "source.occurredAt"),
    identity: parseIdentityRecord(input.identity),
    owner: parseSourceOwner(input.owner),
    causality: parseSourceCausality(input.causality),
    delivery: parseSourceDelivery(input.delivery),
    subjectId: optionalString(input.subjectId, "source.subjectId", 256),
    summary: boundedText(input.summary, 1024),
    effective: input.effective === undefined ? undefined : requireBoolean(input.effective, "source.effective"),
    uncertainty: optionalProbability(input.uncertainty, "source.uncertainty"),
    payload,
    evidenceRefs,
    artifactRefs: artifactInput.map((item, index) => parseArtifact(item, `source.artifactRefs[${index}]`)),
    metadata: jsonRecord(input.metadata, "source.metadata"),
  };
  if (record.owner.domain !== record.domain && record.domain !== SourceDomain.API_TRANSPORT) {
    throw new EnvelopeValidationError("source owner domain does not match record domain", {
      owner_domain: record.owner.domain,
      record_domain: record.domain,
    });
  }
  if (!record.owner.committed && record.delivery.mode !== DeliveryMode.LIVE_ONLY) {
    throw new EnvelopeValidationError("durable source record must be committed by its domain owner", {
      source_id: record.sourceId,
      owner_id: record.owner.ownerId,
    });
  }
  return record;
}

export function parseSourceBatch(value: unknown): SourceBatch {
  const input = requireRecord(value, "source batch");
  const schema = input.schema ?? SOURCE_BATCH_SCHEMA;
  if (schema !== SOURCE_BATCH_SCHEMA) throw new EnvelopeValidationError("source batch schema is unsupported", { schema: String(schema) });
  if (!Array.isArray(input.records)) throw new EnvelopeValidationError("source batch records must be an array");
  const records = input.records.map(parseSourceRecord);
  const expectedCount = requireInteger(input.expectedCount ?? records.length, "source batch expectedCount");
  if (expectedCount !== records.length) {
    throw new EnvelopeValidationError("source batch expected count mismatch", { expected: expectedCount, actual: records.length });
  }
  const identities = new Set(records.map((record) => `${record.identity.runId}:${record.identity.taskId}`));
  if (requireBoolean(input.atomic ?? false, "source batch atomic") && identities.size > 1) {
    throw new EnvelopeValidationError("atomic source batch cannot span task identities", { identities: [...identities] });
  }
  return {
    schema: SOURCE_BATCH_SCHEMA,
    batchId: normalizeIdentifier(input.batchId, "source batch batchId"),
    producerId: normalizeIdentifier(input.producerId, "source batch producerId"),
    records,
    atomic: requireBoolean(input.atomic ?? false, "source batch atomic"),
    expectedCount,
    createdAt: parseTimestamp(input.createdAt ?? utcNow(), "source batch createdAt"),
  };
}

export function parseConsumerCheckpoint(value: unknown): ConsumerCheckpoint {
  const input = requireRecord(value, "consumer checkpoint");
  const schema = input.schema ?? CONSUMER_CHECKPOINT_SCHEMA;
  if (schema !== CONSUMER_CHECKPOINT_SCHEMA) throw new EnvelopeValidationError("consumer checkpoint schema is unsupported", { schema: String(schema) });
  const startedAt = parseTimestamp(input.startedAt, "consumer checkpoint startedAt");
  const updatedAt = parseTimestamp(input.updatedAt, "consumer checkpoint updatedAt");
  return {
    schema: CONSUMER_CHECKPOINT_SCHEMA,
    consumerId: normalizeIdentifier(input.consumerId, "consumer checkpoint consumerId"),
    subscriptionId: normalizeIdentifier(input.subscriptionId, "consumer checkpoint subscriptionId"),
    role: enumString<ConsumerRoleValue>(input.role, "consumer checkpoint role", CONSUMER_ROLES),
    lastGlobalSequence: nonNegative(input.lastGlobalSequence, "consumer checkpoint lastGlobalSequence"),
    lastEventId: optionalString(input.lastEventId, "consumer checkpoint lastEventId", 256),
    lastDeliveryId: optionalString(input.lastDeliveryId, "consumer checkpoint lastDeliveryId", 256),
    lastEventDigest: optionalString(input.lastEventDigest, "consumer checkpoint lastEventDigest", 80),
    handledCount: nonNegative(input.handledCount, "consumer checkpoint handledCount"),
    duplicateCount: nonNegative(input.duplicateCount, "consumer checkpoint duplicateCount"),
    retryCount: nonNegative(input.retryCount, "consumer checkpoint retryCount"),
    deadLetterCount: nonNegative(input.deadLetterCount, "consumer checkpoint deadLetterCount"),
    generation: nonNegative(input.generation, "consumer checkpoint generation"),
    startedAt,
    updatedAt,
  };
}

export function emptyConsumerCheckpoint(
  consumerId: string,
  subscriptionId: string,
  role: ConsumerRoleValue,
  generation = 1,
): ConsumerCheckpoint {
  const now = utcNow();
  return {
    schema: CONSUMER_CHECKPOINT_SCHEMA,
    consumerId: normalizeIdentifier(consumerId, "consumerId"),
    subscriptionId: normalizeIdentifier(subscriptionId, "subscriptionId"),
    role: enumString<ConsumerRoleValue>(role, "role", CONSUMER_ROLES),
    lastGlobalSequence: 0,
    handledCount: 0,
    duplicateCount: 0,
    retryCount: 0,
    deadLetterCount: 0,
    generation: Math.max(1, generation),
    startedAt: now,
    updatedAt: now,
  };
}

export function sourceFingerprint(record: SourceRecord): string {
  return digestJson(JSON.parse(JSON.stringify({
    schema: record.schema,
    sourceId: record.sourceId,
    kind: record.kind,
    domain: record.domain,
    identity: record.identity,
    owner: record.owner,
    causality: record.causality,
    payload: record.payload,
    artifactRefs: record.artifactRefs,
  })) as JsonValue);
}

export function sourceAggregateId(record: SourceRecord): string {
  const identity = record.identity;
  return `run:${identity.runId}:task:${identity.taskId}`;
}

export function sourceStateDelta(
  record: SourceRecord,
  domain: string,
  operation: StateDelta["operation"],
  path: readonly string[],
  value?: JsonValue,
): StateDelta {
  const effective = record.effective ?? operation !== "none";
  const afterDigest = effective
    ? digestJson({ source: record.sourceId, domain, operation, path, value: value ?? null })
    : undefined;
  return {
    domain,
    operation,
    path,
    afterDigest,
    value,
    effective,
  };
}

export function sourceSender(record: SourceRecord, eventType: string): SenderRef {
  const definition = eventDefinition(eventType);
  const id = record.subjectId ?? record.identity.workerId ?? record.owner.ownerId;
  return {
    kind: definition.senderKind,
    id: normalizeIdentifier(id, "source sender id"),
    role: record.domain,
    capabilityRefs: sourceCapabilities(record.domain, definition.senderKind),
  };
}

export function sourceCapabilities(domain: SourceDomainValue, senderKind: SenderKindValue): readonly string[] {
  const capabilities = new Set<string>([`runtime.event.emit.${domain}`, `runtime.sender.${senderKind}`]);
  if (domain === SourceDomain.CODEWORKER) capabilities.add("worker.execute");
  if (domain === SourceDomain.PERMISSION || domain === SourceDomain.CONTROL) capabilities.add("control.decide");
  if (domain === SourceDomain.MCP) capabilities.add("mcp.invoke");
  if (domain === SourceDomain.SUBAGENT) capabilities.add("subagent.execute");
  if (domain === SourceDomain.BROWSER) capabilities.add("browser.observe");
  if (domain === SourceDomain.GATEWAY) capabilities.add("gateway.dispatch");
  if (domain === SourceDomain.ARTIFACT) capabilities.add("artifact.commit");
  if (domain === SourceDomain.RECOVERY) capabilities.add("recovery.plan");
  return [...capabilities].sort();
}

export function sourceProvenance(record: SourceRecord): RuntimeEventDraft["provenance"] {
  const supplementary = record.domain === SourceDomain.SUBAGENT;
  return {
    sourceRepository: supplementary ? "oh-my-pi" : "opencode",
    sourceModule: supplementary
      ? "packages/agent/src/agent-loop.ts + packages/wire/src/index.ts"
      : "packages/opencode/src/session + packages/opencode/src/bus",
    migrationRole: supplementary ? "supplementary" : "primary",
    producerVersion: "M1-S05C-02",
    trust: record.domain === SourceDomain.MCP ? TrustLevel.EXTERNAL : TrustLevel.INTERNAL,
    normalizedFrom: `${record.domain}:${record.kind}`,
    sourceEventId: record.sourceId,
    sourceDigest: sourceFingerprint(record),
  };
}

export function deliveryOptions(record: SourceRecord): IntegrationAppendOptions {
  const delivery = record.delivery;
  const route = delivery.mode !== DeliveryMode.STORE_ONLY && delivery.mode !== DeliveryMode.LIVE_ONLY;
  const broadcast = delivery.mode === DeliveryMode.CRITICAL_BROADCAST;
  return {
    route,
    project: delivery.mode !== DeliveryMode.LIVE_ONLY,
    publishLegacyProjection: true,
    strictOwner: true,
    // Source owners prove that a fact was committed in its originating domain.
    // Aggregate custody is deliberately stable across CodeWorker, permission,
    // MCP, subagent and recovery emitters so one task cannot become unwritable
    // when its next canonical fact comes from another subsystem.
    ownerId: "RuntimeEventIntegration",
    dependencyRecipients: delivery.dependencyRecipients,
    topK: delivery.topK,
    broadcast,
    fanoutReason: broadcast
      ? `${delivery.broadcastPolicyId}:${delivery.broadcastReason}`
      : undefined,
  };
}

export function buildSourceDraft(
  record: SourceRecord,
  eventType: string,
  inline: Readonly<Record<string, JsonValue>>,
  stateDelta: StateDelta,
  overrides: Partial<RuntimeEventDraft> = {},
): RuntimeEventDraft {
  const definition = eventDefinition(eventType);
  const target = record.delivery.target;
  const effect: EventEffectValue = record.effective === undefined
    ? definition.defaultEffect
    : record.effective ? EventEffect.EFFECTIVE : EventEffect.NON_EFFECTIVE;
  const draft: RuntimeEventDraft = {
    eventId: `evt:${record.sourceId}`,
    eventType,
    eventVersion: definition.version,
    aggregateId: sourceAggregateId(record),
    producerSequence: record.causality.producerSequence,
    idempotencyKey: `source:${record.owner.ownerId}:${record.sourceId}`,
    correlationId: record.causality.correlationId,
    causationId: record.causality.causationEventId,
    createdAt: record.occurredAt,
    durability: definition.durability,
    effect,
    identity: JSON.parse(JSON.stringify(record.identity)) as RuntimeIdentity,
    sender: sourceSender(record, eventType),
    intent: definition.intent,
    target,
    topKRecipients: target ? [target] : [],
    summary: record.summary,
    stateDelta,
    evidenceRefs: record.evidenceRefs.flatMap((items) => items ?? []),
    artifactRefs: record.artifactRefs,
    uncertainty: record.uncertainty,
    provenance: sourceProvenance(record),
    inline,
    sourceBytes: Buffer.byteLength(canonicalJson(record.payload), "utf8"),
    metadata: {
      integration_schema: RUNTIME_INTEGRATION_SCHEMA,
      source_domain: record.domain,
      source_kind: record.kind,
      source_owner: record.owner.ownerId,
      source_transaction_id: record.owner.transactionId ?? null,
      source_store_ref: record.owner.storeRef ?? null,
      source_committed_at: record.owner.committedAt ?? null,
      request_id: record.causality.requestId ?? null,
      parent_id: record.causality.parentId ?? null,
      delivery_mode: record.delivery.mode,
      ...record.metadata,
    },
    ...overrides,
  };
  return draft;
}

export function assertMappedSource(mapped: MappedSourceEvent): MappedSourceEvent {
  if (mapped.schema !== RUNTIME_INTEGRATION_SCHEMA) {
    throw new EnvelopeValidationError("mapped source schema is unsupported", { schema: mapped.schema });
  }
  if (mapped.draft.provenance.sourceEventId !== mapped.source.sourceId) {
    throw new EnvelopeValidationError("mapped source event lost source identity", {
      source_id: mapped.source.sourceId,
      provenance_source_event_id: mapped.draft.provenance.sourceEventId ?? "",
    });
  }
  if (mapped.draft.correlationId !== mapped.source.causality.correlationId) {
    throw new EnvelopeValidationError("mapped source event lost correlation", {
      source_id: mapped.source.sourceId,
    });
  }
  if (mapped.draft.aggregateId !== sourceAggregateId(mapped.source)) {
    throw new EnvelopeValidationError("mapped source aggregate does not match runtime identity", {
      source_id: mapped.source.sourceId,
      aggregate_id: mapped.draft.aggregateId,
    });
  }
  const definition = eventDefinition(mapped.draft.eventType);
  if (mapped.source.delivery.mode === DeliveryMode.LIVE_ONLY && definition.durability !== EventDurability.LIVE_ONLY) {
    throw new EnvelopeValidationError("live-only source mapped to durable event", { event_type: mapped.draft.eventType });
  }
  if (mapped.source.delivery.mode !== DeliveryMode.LIVE_ONLY && definition.durability === EventDurability.LIVE_ONLY) {
    throw new EnvelopeValidationError("durable source mapped to live-only event", { event_type: mapped.draft.eventType });
  }
  if (mapped.options.broadcast && mapped.source.delivery.mode !== DeliveryMode.CRITICAL_BROADCAST) {
    throw new EnvelopeValidationError("broadcast option lacks critical source declaration", { source_id: mapped.source.sourceId });
  }
  return mapped;
}

export function sourceRecordToJson(record: SourceRecord): Record<string, JsonValue> {
  return JSON.parse(JSON.stringify(record)) as Record<string, JsonValue>;
}

export function checkpointToJson(checkpoint: ConsumerCheckpoint): Record<string, JsonValue> {
  return JSON.parse(JSON.stringify(checkpoint)) as Record<string, JsonValue>;
}

export function streamFrameToJson(frame: ProjectorStreamFrame): Record<string, JsonValue> {
  return JSON.parse(JSON.stringify(frame)) as Record<string, JsonValue>;
}

export function isSourceRecord(value: unknown): value is SourceRecord {
  if (!isPlainObject(value)) return false;
  try {
    parseSourceRecord(value);
    return true;
  } catch {
    return false;
  }
}

export function canonicalIntegrationContract(): Record<string, JsonValue> {
  return {
    schema: RUNTIME_INTEGRATION_SCHEMA,
    source_record_schema: SOURCE_RECORD_SCHEMA,
    source_batch_schema: SOURCE_BATCH_SCHEMA,
    consumer_checkpoint_schema: CONSUMER_CHECKPOINT_SCHEMA,
    projector_stream_schema: PROJECTOR_STREAM_SCHEMA,
    reconciliation_schema: RECONCILIATION_SCHEMA,
    disable_matrix_schema: DISABLE_MATRIX_SCHEMA,
    artifact_read_schema: ARTIFACT_READ_SCHEMA,
    baseline_report_schema: BASELINE_REPORT_SCHEMA,
    canonical_owner: "RuntimeEventSqliteStore",
    delivery_owner: "RuntimeMessageBus",
    projector_owner: "RuntimeEventProjector",
    predictive_ui_write_allowed: false,
    source_domains: [...SOURCE_DOMAINS].sort(),
    source_kinds: [...SOURCE_KINDS].sort(),
    delivery_modes: [...DELIVERY_MODES].sort(),
    hard_limits: {
      inline_bytes: 4 * 1024,
      envelope_bytes: 8 * 1024,
      summary_bytes: 1024,
      reference_count: 64,
      p95_envelope_bytes: 4 * 1024,
    },
    downstream_consumers: ["M1-05D", "M1-07A", "M1-07C", "M2"],
  };
}
