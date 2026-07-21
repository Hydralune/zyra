import { createHash, randomUUID } from "node:crypto";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export interface JsonObject { [key: string]: JsonValue }

export const SKILL_MEMORY_PROTOCOL = "zyra.skill-memory/v1" as const;
export const SKILL_OUTCOME_PROTOCOL = "zyra.skill-invocation-outcome-memory/v1" as const;
export const PROCEDURE_PROTOCOL = "zyra.reusable-procedure/v1" as const;
export const COMPACT_PROJECTION_PROTOCOL = "zyra.compact-restore-projection/v1" as const;
export const MEMORY_SIGNAL_PROTOCOL = "zyra.memory-signal/v1" as const;
export const SNAPCOMPACT_EXPERIMENT_PROTOCOL = "zyra.snapcompact-experiment/v1" as const;

export type SkillInvocationStatus = "completed" | "failed" | "cancelled" | "background";
export type OutcomeMemoryState = "observed" | "accepted" | "quarantined" | "superseded";
export type EvidenceKind = "tool_call" | "tool_result" | "artifact" | "event" | "permission" | "checkpoint";
export type ProcedureValidationStatus = "candidate" | "validated" | "rejected" | "superseded";
export type ProcedureConsumer = "routing" | "recovery" | "context" | "audit";
export type CompactTriggerKind = "threshold" | "overflow" | "mid_turn" | "idle" | "manual" | "reactive";
export type CompactTriggerDecision = "compact" | "defer" | "reject" | "circuit_open";
export type ContextWorkerKind = "code" | "browser";
export type RestoreProjectionState = "prepared" | "applied" | "rejected" | "expired";
export type MemorySignalKind =
  | "skill_memory_updated"
  | "skill_memory_rejected"
  | "procedure_mined"
  | "procedure_rejected"
  | "compact_triggered"
  | "compact_deferred"
  | "compact_restored"
  | "compact_restore_rejected"
  | "context_epoch_advanced";

export interface RuntimeIdentity {
  runId: string;
  taskId: string;
  sessionId: string;
  workerRequestId: string;
  epoch: number;
}

export interface SkillVersionReference {
  skillId: string;
  skillName: string;
  registryRevision: number;
  descriptorDigest: string;
  bodyDigest: string;
  resourceDigests: Record<string, string>;
  sourceRevision: string;
}

export interface PolicyDecisionReference {
  decisionId: string;
  effect: "allow" | "deny" | "ask";
  policyRevision: string;
  policyDigest: string;
  requestedTools: string[];
  effectiveTools: string[];
  deniedTools: string[];
  approvalId: string | null;
}

export interface OutcomeEvidenceReference {
  evidenceId: string;
  kind: EvidenceKind;
  sourceId: string;
  sourceDigest: string;
  sequence: number;
  occurredAt: string;
  toolName: string | null;
  toolCallId: string | null;
  artifactId: string | null;
  trustedRuntime: boolean;
  metadata: JsonObject;
}

export interface ReuseCondition {
  conditionId: string;
  kind: "workspace" | "language" | "tool" | "artifact" | "failure" | "goal" | "provider" | "custom";
  operator: "equals" | "contains" | "matches" | "present" | "absent" | "in";
  key: string;
  value: JsonValue;
  required: boolean;
  sourceEvidenceIds: string[];
}

export interface SkillInvocationOutcomeInput {
  protocol: typeof SKILL_OUTCOME_PROTOCOL;
  identity: RuntimeIdentity;
  invocationId: string;
  toolCallId: string;
  compositionId: string;
  status: SkillInvocationStatus;
  version: SkillVersionReference;
  policy: PolicyDecisionReference;
  evidence: OutcomeEvidenceReference[];
  artifacts: JsonObject[];
  outputDigest: string;
  summary: string;
  successReason: string | null;
  failureReason: string | null;
  reuseConditions: ReuseCondition[];
  inputTokens: number;
  outputTokens: number;
  toolCalls: number;
  costMicros: number;
  startedAt: string;
  completedAt: string | null;
  sourceRecordDigest: string;
  metadata: JsonObject;
}

export interface SkillInvocationOutcomeMemory {
  memoryId: string;
  protocol: typeof SKILL_OUTCOME_PROTOCOL;
  identity: RuntimeIdentity;
  invocationId: string;
  toolCallId: string;
  compositionId: string;
  status: SkillInvocationStatus;
  state: OutcomeMemoryState;
  version: SkillVersionReference;
  policy: PolicyDecisionReference;
  evidence: OutcomeEvidenceReference[];
  artifactIds: string[];
  outputDigest: string;
  summary: string;
  successReason: string | null;
  failureReason: string | null;
  reuseConditions: ReuseCondition[];
  metrics: {
    inputTokens: number;
    outputTokens: number;
    toolCalls: number;
    costMicros: number;
    durationMs: number | null;
  };
  startedAt: string;
  completedAt: string | null;
  observedAt: string;
  sourceRecordDigest: string;
  recordDigest: string;
  revision: number;
  metadata: JsonObject;
}

export interface ProcedureStep {
  stepId: string;
  ordinal: number;
  action: string;
  toolName: string | null;
  inputShapeDigest: string | null;
  expectedEffect: string;
  successEvidenceIds: string[];
  artifactKinds: string[];
  retryable: boolean;
  failureRoutes: string[];
  metadata: JsonObject;
}

export interface ProcedureApplicability {
  languages: string[];
  workspaceKinds: string[];
  goalPatterns: string[];
  requiredTools: string[];
  forbiddenTools: string[];
  requiredArtifactKinds: string[];
  providerCapabilities: string[];
  minimumTrust: "internal" | "verified";
  constraints: ReuseCondition[];
}

export interface ProcedureProvenance {
  curatorOutcomeId: string;
  curatorJobId: string;
  curatorDecisionId: string;
  evidenceBundleId: string;
  evidenceDigest: string;
  memoryId: string;
  memoryRevision: number;
  runId: string;
  taskId: string;
  sessionIds: string[];
  toolCallIds: string[];
  artifactIds: string[];
  skillVersions: SkillVersionReference[];
  memoryEventIds: string[];
}

export interface ReusableProcedure {
  procedureId: string;
  protocol: typeof PROCEDURE_PROTOCOL;
  name: string;
  summary: string;
  state: ProcedureValidationStatus;
  revision: number;
  steps: ProcedureStep[];
  applicability: ProcedureApplicability;
  provenance: ProcedureProvenance;
  consumers: ProcedureConsumer[];
  confidence: number;
  successCount: number;
  failureCount: number;
  validatedAt: string | null;
  createdAt: string;
  updatedAt: string;
  supersedesProcedureId: string | null;
  procedureDigest: string;
  metadata: JsonObject;
}

export interface CompactMessageBlock {
  blockId: string;
  messageId: string;
  role: "system" | "user" | "assistant" | "tool";
  kind: "text" | "tool_call" | "tool_result" | "attachment" | "notice";
  text: string;
  toolPairId: string | null;
  turnIndex: number | null;
  apiRound: number | null;
  tokenEstimate: number;
  createdAt: string;
  sourceDigest: string;
  metadata: JsonObject;
}

export interface CompactTriggerObservation {
  triggerId?: string;
  identity: RuntimeIdentity;
  kind: CompactTriggerKind;
  currentTokens: number;
  contextWindow: number;
  reservedOutputTokens: number;
  thresholdTokens: number;
  activeToolCallIds: string[];
  pendingToolResultIds: string[];
  idleMilliseconds: number;
  compactGeneration: number;
  consecutiveFailures: number;
  querySource: string;
  observedAt: string;
  metadata: JsonObject;
}

export interface CompactTriggerReceipt {
  triggerId: string;
  decision: CompactTriggerDecision;
  reason: string;
  effectiveTokens: number;
  headroomTokens: number;
  safeToCut: boolean;
  deferredToolPairIds: string[];
  compactGeneration: number;
  observedAt: string;
  receiptDigest: string;
  metadata: JsonObject;
}

export interface SafeCutPlan {
  planId: string;
  triggerId: string;
  compactGeneration: number;
  sourceMessageIds: string[];
  summarizedMessageIds: string[];
  preservedMessageIds: string[];
  preservedToolPairIds: string[];
  unresolvedToolPairIds: string[];
  cutAfterIndex: number;
  sourceTokens: number;
  summarizedTokens: number;
  preservedTokens: number;
  targetTokens: number;
  minimumRecentTurns: number;
  valid: boolean;
  reason: string;
  planDigest: string;
  createdAt: string;
  metadata: JsonObject;
}

export interface CompactArchiveReference {
  archiveId: string;
  artifactId: string;
  boundaryId: string;
  sessionId: string;
  compactGeneration: number;
  contentDigest: string;
  summaryDigest: string;
  summarizedMessageIds: string[];
  preservedMessageIds: string[];
  tokenCountBefore: number;
  tokenCountAfter: number;
  createdAt: string;
  metadata: JsonObject;
}

export interface RestoreAttachmentReference {
  candidateId: string;
  kind: "file" | "skill" | "memory" | "agent" | "plan" | "artifact" | "other";
  name: string;
  sourceId: string;
  sourceDigest: string;
  content: string;
  tokenEstimate: number;
  selected: boolean;
  required: boolean;
  metadata: JsonObject;
}

export interface CompactRestoreProjection {
  projectionId: string;
  protocol: typeof COMPACT_PROJECTION_PROTOCOL;
  identity: RuntimeIdentity;
  workerKind: ContextWorkerKind;
  state: RestoreProjectionState;
  boundaryId: string;
  archive: CompactArchiveReference;
  contextEpochBefore: number;
  contextEpochAfter: number;
  summary: string;
  attachments: RestoreAttachmentReference[];
  skillMemoryIds: string[];
  procedureIds: string[];
  allowedToolsBefore: string[];
  allowedToolsAfter: string[];
  deniedTools: string[];
  contextSectionIds: string[];
  providerMessageDigest: string | null;
  preparedAt: string;
  appliedAt: string | null;
  expiresAt: string | null;
  rejectionReason: string | null;
  projectionDigest: string;
  metadata: JsonObject;
}

export interface MemorySignal {
  signalId: string;
  protocol: typeof MEMORY_SIGNAL_PROTOCOL;
  kind: MemorySignalKind;
  identity: RuntimeIdentity;
  aggregateId: string;
  causationId: string;
  correlationId: string;
  sequence: number;
  priority: "low" | "normal" | "high" | "critical";
  payload: JsonObject;
  payloadDigest: string;
  createdAt: string;
  publishedAt: string | null;
  acknowledgedBy: string[];
  signalDigest: string;
}

export interface SnapcompactCapability {
  protocol: typeof SNAPCOMPACT_EXPERIMENT_PROTOCOL;
  capabilityId: string;
  enabled: boolean;
  providerIds: string[];
  modelPatterns: string[];
  maximumFrameBytes: number;
  maximumFrames: number;
  maximumTotalBytes: number;
  acceptedMediaTypes: string[];
  rendererRevision: string;
  metadata: JsonObject;
}

export interface SnapcompactFrame {
  frameId: string;
  sequence: number;
  mediaType: string;
  width: number;
  height: number;
  byteSize: number;
  frameHash: string;
  sourceMessageIds: string[];
  metadata: JsonObject;
}

export interface SnapcompactEvaluation {
  evaluationId: string;
  capabilityId: string;
  eligible: boolean;
  providerCompatible: boolean;
  modelCompatible: boolean;
  withinFrameBudget: boolean;
  frames: SnapcompactFrame[];
  totalBytes: number;
  fallbackReason: string | null;
  evaluatedAt: string;
  evaluationDigest: string;
  metadata: JsonObject;
}

export interface SkillMemoryPolicy {
  maximumRecords: number;
  maximumRecordsPerSkill: number;
  maximumEvidencePerRecord: number;
  maximumReuseConditions: number;
  maximumSummaryCharacters: number;
  requireCompletedForReuse: boolean;
  requireTrustedEvidence: boolean;
  minimumSuccessfulToolCalls: number;
  retentionMilliseconds: number;
}

export interface CompactPolicy {
  contextWindow: number;
  reservedOutputTokens: number;
  thresholdRatio: number;
  overflowRatio: number;
  minimumCompactTokens: number;
  targetTokens: number;
  minimumRecentTurns: number;
  idleTriggerMilliseconds: number;
  maximumConsecutiveFailures: number;
  maximumRestoreTokens: number;
  maximumSkillMemoryTokens: number;
  maximumProcedureTokens: number;
  projectionTtlMilliseconds: number;
}

export interface SkillMemoryRuntimeSnapshot {
  version: typeof SKILL_MEMORY_PROTOCOL;
  identity: RuntimeIdentity;
  revision: number;
  contextEpoch: number;
  signalSequence: number;
  policy: SkillMemoryPolicy;
  compactPolicy: CompactPolicy;
  outcomes: SkillInvocationOutcomeMemory[];
  procedures: ReusableProcedure[];
  projections: CompactRestoreProjection[];
  triggers: CompactTriggerReceipt[];
  cutPlans: SafeCutPlan[];
  archives: CompactArchiveReference[];
  signals: MemorySignal[];
  snapcompactCapabilities: SnapcompactCapability[];
  capturedAt: string;
  checksum: string;
}

export class SkillMemoryContractError extends Error {
  readonly code: string;
  readonly details: JsonObject;

  constructor(code: string, message: string, details: JsonObject = {}) {
    super(message);
    this.name = "SkillMemoryContractError";
    this.code = code;
    this.details = cloneJson(details);
  }
}

export function normalizeIdentity(value: RuntimeIdentity): RuntimeIdentity {
  return {
    runId: required(value.runId, "run id"),
    taskId: required(value.taskId, "task id"),
    sessionId: required(value.sessionId, "session id"),
    workerRequestId: required(value.workerRequestId, "worker request id"),
    epoch: nonNegativeInteger(value.epoch, "runtime epoch"),
  };
}

export function normalizeSkillVersion(value: SkillVersionReference): SkillVersionReference {
  const resourceDigests: Record<string, string> = {};
  for (const [path, valueDigest] of Object.entries(value.resourceDigests ?? {})) {
    const normalizedPath = required(path, "skill resource path");
    resourceDigests[normalizedPath] = sha256Digest(valueDigest, `skill resource digest ${normalizedPath}`);
  }
  return {
    skillId: required(value.skillId, "skill id"),
    skillName: required(value.skillName, "skill name"),
    registryRevision: positiveInteger(value.registryRevision, "skill registry revision"),
    descriptorDigest: sha256Digest(value.descriptorDigest, "skill descriptor digest"),
    bodyDigest: sha256Digest(value.bodyDigest, "skill body digest"),
    resourceDigests: sortRecord(resourceDigests),
    sourceRevision: required(value.sourceRevision, "skill source revision"),
  };
}

export function normalizePolicyDecision(value: PolicyDecisionReference): PolicyDecisionReference {
  const requestedTools = uniqueStrings(value.requestedTools);
  const effectiveTools = uniqueStrings(value.effectiveTools);
  const deniedTools = uniqueStrings(value.deniedTools);
  if (value.effect === "deny" && effectiveTools.length > 0) {
    throw contractError("skill_policy_denied_scope_nonempty", "denied skill outcome cannot expose effective tools");
  }
  if (value.effect !== "allow" && value.approvalId && value.effect !== "ask") {
    throw contractError("skill_policy_approval_invalid", "approval reference is only valid for ask decisions");
  }
  for (const tool of effectiveTools) {
    if (!requestedTools.includes("*") && !requestedTools.includes(tool)) {
      throw contractError("skill_policy_scope_widened", `effective tool ${tool} was not requested`);
    }
    if (deniedTools.includes(tool)) {
      throw contractError("skill_policy_denied_tool_effective", `denied tool ${tool} cannot be effective`);
    }
  }
  return {
    decisionId: required(value.decisionId, "policy decision id"),
    effect: policyEffect(value.effect),
    policyRevision: required(value.policyRevision, "policy revision"),
    policyDigest: sha256Digest(value.policyDigest, "policy digest"),
    requestedTools,
    effectiveTools,
    deniedTools,
    approvalId: nullableString(value.approvalId),
  };
}

export function normalizeEvidence(value: OutcomeEvidenceReference): OutcomeEvidenceReference {
  return {
    evidenceId: required(value.evidenceId, "evidence id"),
    kind: evidenceKind(value.kind),
    sourceId: required(value.sourceId, "evidence source id"),
    sourceDigest: sha256Digest(value.sourceDigest, "evidence source digest"),
    sequence: nonNegativeInteger(value.sequence, "evidence sequence"),
    occurredAt: timestamp(value.occurredAt, "evidence timestamp"),
    toolName: nullableString(value.toolName),
    toolCallId: nullableString(value.toolCallId),
    artifactId: nullableString(value.artifactId),
    trustedRuntime: Boolean(value.trustedRuntime),
    metadata: sanitizeJsonObject(value.metadata),
  };
}

export function normalizeReuseCondition(value: ReuseCondition): ReuseCondition {
  const kind = value.kind;
  if (!["workspace", "language", "tool", "artifact", "failure", "goal", "provider", "custom"].includes(kind)) {
    throw contractError("skill_reuse_condition_kind", `unsupported reuse condition kind ${kind}`);
  }
  const operator = value.operator;
  if (!["equals", "contains", "matches", "present", "absent", "in"].includes(operator)) {
    throw contractError("skill_reuse_condition_operator", `unsupported reuse condition operator ${operator}`);
  }
  return {
    conditionId: value.conditionId?.trim() || stableId("reuse-condition", kind, value.key, value.operator, value.value),
    kind,
    operator,
    key: required(value.key, "reuse condition key"),
    value: cloneJson(value.value),
    required: Boolean(value.required),
    sourceEvidenceIds: uniqueStrings(value.sourceEvidenceIds),
  };
}

export function normalizeSkillMemoryPolicy(value: Partial<SkillMemoryPolicy> = {}): SkillMemoryPolicy {
  return {
    maximumRecords: boundedInteger(value.maximumRecords ?? 25_000, 1, 1_000_000, "maximum records"),
    maximumRecordsPerSkill: boundedInteger(value.maximumRecordsPerSkill ?? 2_000, 1, 100_000, "maximum records per skill"),
    maximumEvidencePerRecord: boundedInteger(value.maximumEvidencePerRecord ?? 512, 1, 10_000, "maximum evidence per record"),
    maximumReuseConditions: boundedInteger(value.maximumReuseConditions ?? 128, 0, 10_000, "maximum reuse conditions"),
    maximumSummaryCharacters: boundedInteger(value.maximumSummaryCharacters ?? 16_000, 128, 1_000_000, "maximum summary characters"),
    requireCompletedForReuse: value.requireCompletedForReuse ?? true,
    requireTrustedEvidence: value.requireTrustedEvidence ?? true,
    minimumSuccessfulToolCalls: boundedInteger(value.minimumSuccessfulToolCalls ?? 0, 0, 1_000_000, "minimum successful tool calls"),
    retentionMilliseconds: boundedInteger(value.retentionMilliseconds ?? 180 * 24 * 60 * 60 * 1_000, 60_000, 10 * 365 * 24 * 60 * 60 * 1_000, "retention milliseconds"),
  };
}

export function normalizeCompactPolicy(value: Partial<CompactPolicy> = {}): CompactPolicy {
  const contextWindow = boundedInteger(value.contextWindow ?? 200_000, 8_192, 10_000_000, "context window");
  const reservedOutputTokens = boundedInteger(value.reservedOutputTokens ?? 13_000, 1, contextWindow - 1, "reserved output tokens");
  const thresholdRatio = boundedNumber(value.thresholdRatio ?? 0.8, 0.1, 0.99, "threshold ratio");
  const overflowRatio = boundedNumber(value.overflowRatio ?? 0.98, thresholdRatio, 1.5, "overflow ratio");
  return {
    contextWindow,
    reservedOutputTokens,
    thresholdRatio,
    overflowRatio,
    minimumCompactTokens: boundedInteger(value.minimumCompactTokens ?? 8_000, 1_000, contextWindow, "minimum compact tokens"),
    targetTokens: boundedInteger(value.targetTokens ?? Math.max(4_096, Math.floor(contextWindow * 0.45)), 1_000, contextWindow, "target compact tokens"),
    minimumRecentTurns: boundedInteger(value.minimumRecentTurns ?? 3, 1, 1_000, "minimum recent turns"),
    idleTriggerMilliseconds: boundedInteger(value.idleTriggerMilliseconds ?? 30_000, 0, 24 * 60 * 60 * 1_000, "idle trigger milliseconds"),
    maximumConsecutiveFailures: boundedInteger(value.maximumConsecutiveFailures ?? 5, 1, 1_000, "maximum consecutive failures"),
    maximumRestoreTokens: boundedInteger(value.maximumRestoreTokens ?? 32_000, 1_000, contextWindow, "maximum restore tokens"),
    maximumSkillMemoryTokens: boundedInteger(value.maximumSkillMemoryTokens ?? 8_000, 256, contextWindow, "maximum skill memory tokens"),
    maximumProcedureTokens: boundedInteger(value.maximumProcedureTokens ?? 8_000, 256, contextWindow, "maximum procedure tokens"),
    projectionTtlMilliseconds: boundedInteger(value.projectionTtlMilliseconds ?? 24 * 60 * 60 * 1_000, 60_000, 30 * 24 * 60 * 60 * 1_000, "projection ttl milliseconds"),
  };
}

export function assertSameRuntime(expected: RuntimeIdentity, actual: RuntimeIdentity, label: string): void {
  const left = normalizeIdentity(expected);
  const right = normalizeIdentity(actual);
  if (left.taskId !== right.taskId || left.sessionId !== right.sessionId) {
    throw contractError("skill_memory_runtime_binding_mismatch", `${label} belongs to a different task/session`, {
      expected_task_id: left.taskId,
      actual_task_id: right.taskId,
      expected_session_id: left.sessionId,
      actual_session_id: right.sessionId,
    });
  }
  if (right.epoch < left.epoch) {
    throw contractError("skill_memory_epoch_regression", `${label} epoch regressed`, {
      expected_epoch: left.epoch,
      actual_epoch: right.epoch,
    });
  }
}

export function intersectToolScopes(parentAllowed: readonly string[], childAllowed: readonly string[], denied: readonly string[]): string[] {
  const parent = new Set(uniqueStrings(parentAllowed));
  const child = new Set(uniqueStrings(childAllowed));
  const forbidden = new Set(uniqueStrings(denied));
  const wildcardParent = parent.has("*");
  const wildcardChild = child.has("*");
  const candidates = wildcardParent
    ? [...child]
    : wildcardChild
      ? [...parent]
      : [...parent].filter((tool) => child.has(tool));
  if (wildcardParent && wildcardChild) candidates.push("*");
  return uniqueStrings(candidates.filter((tool) => tool === "*" || !forbidden.has(tool)));
}

export function toolScopeIsSubset(parentAllowed: readonly string[], candidateAllowed: readonly string[], denied: readonly string[] = []): boolean {
  const parent = new Set(uniqueStrings(parentAllowed));
  const forbidden = new Set(uniqueStrings(denied));
  for (const tool of uniqueStrings(candidateAllowed)) {
    if (forbidden.has(tool)) return false;
    if (!parent.has("*") && !parent.has(tool)) return false;
  }
  return true;
}

export function stableId(prefix: string, ...parts: unknown[]): string {
  return `${prefix}-${digest(parts).slice(0, 32)}`;
}

export function digest(value: unknown): string {
  return createHash("sha256").update(canonicalJson(value)).digest("hex");
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

export function canonicalize(value: unknown, depth = 0): JsonValue {
  if (depth > 64) return "[depth-limited]";
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
  if (typeof value === "bigint") return value.toString();
  if (value instanceof Date) return value.toISOString();
  if (Array.isArray(value)) return value.map((item) => canonicalize(item, depth + 1));
  if (value && typeof value === "object") {
    const output: JsonObject = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      const item = (value as Record<string, unknown>)[key];
      if (item === undefined || typeof item === "function" || typeof item === "symbol") continue;
      output[key] = canonicalize(item, depth + 1);
    }
    return output;
  }
  return String(value);
}

export function cloneJson<T>(value: T): T {
  return structuredClone(value);
}

export function sanitizeJsonObject(value: unknown): JsonObject {
  const canonical = canonicalize(value);
  return canonical && typeof canonical === "object" && !Array.isArray(canonical)
    ? canonical as JsonObject
    : {};
}

export function uniqueStrings(values: readonly unknown[] | null | undefined): string[] {
  return [...new Set((values ?? []).map((item) => String(item).trim()).filter(Boolean))].sort();
}

export function required(value: unknown, label: string): string {
  const text = String(value ?? "").trim();
  if (!text) throw contractError("skill_memory_required_value", `${label} is required`, { label });
  return text;
}

export function nullableString(value: unknown): string | null {
  const text = String(value ?? "").trim();
  return text || null;
}

export function sha256Digest(value: unknown, label: string): string {
  const text = required(value, label).replace(/^sha256:/, "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(text)) throw contractError("skill_memory_digest_invalid", `${label} must be a sha256 digest`, { label });
  return text;
}

export function timestamp(value: unknown, label: string): string {
  const text = required(value, label);
  const parsed = Date.parse(text);
  if (!Number.isFinite(parsed)) throw contractError("skill_memory_timestamp_invalid", `${label} must be an ISO timestamp`, { label });
  return new Date(parsed).toISOString();
}

export function nonNegativeInteger(value: unknown, label: string): number {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 0) throw contractError("skill_memory_integer_invalid", `${label} must be a non-negative integer`, { label });
  return number;
}

export function positiveInteger(value: unknown, label: string): number {
  const number = nonNegativeInteger(value, label);
  if (number < 1) throw contractError("skill_memory_integer_invalid", `${label} must be positive`, { label });
  return number;
}

export function boundedInteger(value: unknown, minimum: number, maximum: number, label: string): number {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < minimum || number > maximum) {
    throw contractError("skill_memory_integer_out_of_range", `${label} must be between ${minimum} and ${maximum}`, { label, minimum, maximum });
  }
  return number;
}

export function boundedNumber(value: unknown, minimum: number, maximum: number, label: string): number {
  const number = Number(value);
  if (!Number.isFinite(number) || number < minimum || number > maximum) {
    throw contractError("skill_memory_number_out_of_range", `${label} must be between ${minimum} and ${maximum}`, { label, minimum, maximum });
  }
  return number;
}

export function durationMilliseconds(startedAt: string, completedAt: string | null): number | null {
  if (!completedAt) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(completedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return end - start;
}

export function randomId(prefix: string): string {
  return `${prefix}-${randomUUID()}`;
}

export function nowIso(now: () => Date = () => new Date()): string {
  return timestamp(now().toISOString(), "runtime clock");
}

export function estimateTokens(text: string): number {
  if (!text) return 0;
  const unicode = [...text].length;
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  return Math.max(1, Math.ceil(Math.max(unicode / 4, words * 1.25)));
}

export function truncateToTokens(text: string, maximumTokens: number): { text: string; tokens: number; truncated: boolean } {
  const normalized = text.replace(/\r\n/g, "\n").trim();
  const tokens = estimateTokens(normalized);
  if (tokens <= maximumTokens) return { text: normalized, tokens, truncated: false };
  const maximumCharacters = Math.max(32, maximumTokens * 4);
  const headCharacters = Math.floor(maximumCharacters * 0.7);
  const tailCharacters = maximumCharacters - headCharacters;
  const truncated = `${normalized.slice(0, headCharacters)}\n...[restore projection truncated]...\n${normalized.slice(-tailCharacters)}`;
  return { text: truncated, tokens: estimateTokens(truncated), truncated: true };
}

export function sortRecord<T>(value: Record<string, T>): Record<string, T> {
  return Object.fromEntries(Object.entries(value).sort(([left], [right]) => left.localeCompare(right)));
}

export function contractError(code: string, message: string, details: JsonObject = {}): SkillMemoryContractError {
  return new SkillMemoryContractError(code, message, details);
}

function policyEffect(value: string): PolicyDecisionReference["effect"] {
  if (value === "allow" || value === "deny" || value === "ask") return value;
  throw contractError("skill_policy_effect_invalid", `unsupported policy effect ${value}`);
}

function evidenceKind(value: string): EvidenceKind {
  if (["tool_call", "tool_result", "artifact", "event", "permission", "checkpoint"].includes(value)) return value as EvidenceKind;
  throw contractError("skill_evidence_kind_invalid", `unsupported evidence kind ${value}`);
}
