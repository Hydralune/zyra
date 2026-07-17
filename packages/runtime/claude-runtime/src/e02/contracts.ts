import type { JsonObject, JsonValue, RuntimeEvent, ToolSpecContract } from "../contracts.ts";

export type E02Domain =
  | "permission"
  | "mcp-config"
  | "mcp-connection"
  | "mcp-auth"
  | "mcp-capability"
  | "mcp-request"
  | "mcp-elicitation"
  | "mcp-task"
  | "skill"
  | "plugin"
  | "command";

export type TransitionPhase =
  | "prepared"
  | "effect_started"
  | "effect_recorded"
  | "receipt_recorded"
  | "committed"
  | "acknowledged"
  | "rejected"
  | "cancelled";

export type E02Clock = () => string;

export interface E02RuntimeIdentity extends JsonObject {
  runtimeId: string;
  runId: string;
  taskId: string;
  sessionId: string;
  workerRequestId: string;
  epoch: number;
}

export interface TransitionBinding {
  runId: string;
  taskId: string;
  sessionId: string;
  workerRequestId: string;
  toolCallId: string | null;
  requestId: string;
  entityId: string;
  expectedRevision: number;
}

export interface TransitionPrepareInput {
  domain: E02Domain;
  operation: string;
  binding: TransitionBinding;
  payload: JsonObject;
  idempotencyKey?: string;
  replayPolicy: "return-committed" | "reject-duplicate" | "resume-pending" | "resume-pending-or-return-committed";
  nonIdempotentEffect: boolean;
  metadata?: JsonObject;
}

export interface EffectReceipt {
  effectId: string;
  transitionId: string;
  effectKind: string;
  requestHash: string;
  responseHash: string;
  ok: boolean;
  output: JsonObject;
  error: string | null;
  performedAt: string;
  providerReceiptId: string | null;
  metadata: JsonObject;
}

export interface CommitReceipt {
  transitionId: string;
  entityId: string;
  domain: E02Domain;
  operation: string;
  revisionBefore: number;
  revisionAfter: number;
  stateHashBefore: string;
  stateHashAfter: string;
  payloadHash: string;
  effectReceiptHash: string | null;
  outputHash: string;
  output: JsonObject;
  committedAt: string;
  commitHash: string;
}

export interface TransitionRecord {
  transitionId: string;
  idempotencyKey: string;
  domain: E02Domain;
  operation: string;
  binding: TransitionBinding;
  payload: JsonObject;
  payloadHash: string;
  replayPolicy: TransitionPrepareInput["replayPolicy"];
  nonIdempotentEffect: boolean;
  phase: TransitionPhase;
  revisionBefore: number;
  revisionAfter: number | null;
  stateHashBefore: string;
  stateHashAfter: string | null;
  effectReceipt: EffectReceipt | null;
  commitReceipt: CommitReceipt | null;
  rejectionCode: string | null;
  rejectionMessage: string | null;
  preparedAt: string;
  updatedAt: string;
  acknowledgedAt: string | null;
  epochPrepared: number;
  epochCommitted: number | null;
  metadata: JsonObject;
}

export interface EntityRevisionState {
  entityId: string;
  revision: number;
  stateHash: string;
  lastTransitionId: string | null;
  updatedAt: string;
}

export interface TransitionJournalSnapshot {
  version: "zyra.e02-transition-journal/v1";
  runtime: E02RuntimeIdentity;
  sequence: number;
  entities: EntityRevisionState[];
  pending: TransitionRecord[];
  committed: TransitionRecord[];
  rejected: TransitionRecord[];
  idempotency: Array<[string, string]>;
  transitionHashes: Array<[string, string]>;
  snapshotHash: string;
}

export interface TransitionPrepareResult {
  kind: "prepared" | "resume_pending" | "return_committed";
  transition: TransitionRecord;
  committedReceipt: CommitReceipt | null;
}

export interface TransitionCommitInput {
  transitionId: string;
  nextState: JsonObject;
  output: JsonObject;
  expectedRevision?: number;
  metadata?: JsonObject;
}

export interface E02EventEnvelope extends RuntimeEvent {
  event_id: string;
  event_type: string;
  occurred_at: string;
  run_id: string;
  session_id: string;
  task_id: string;
  worker_request_id: string;
  transition_id: string;
  domain: E02Domain;
  phase: TransitionPhase | "observation";
  revision: number;
  payload: JsonObject;
  cause_ids: string[];
  payload_hash: string;
  previous_event_hash: string;
  event_hash: string;
}

export interface E02EventSnapshot {
  version: "zyra.e02-events/v1";
  sequence: number;
  previousEventHash: string;
  events: E02EventEnvelope[];
  snapshotHash: string;
}

export interface RevisionedEntry<T extends JsonObject = JsonObject> {
  id: string;
  revision: number;
  value: T;
  valueHash: string;
  enabled: boolean;
  source: string;
  sourceRevision: string;
  createdAt: string;
  updatedAt: string;
  tombstonedAt: string | null;
  metadata: JsonObject;
}

export interface RevisionedRegistrySnapshot<T extends JsonObject = JsonObject> {
  version: "zyra.e02-revisioned-registry/v1";
  name: string;
  revision: number;
  entries: RevisionedEntry<T>[];
  staged: RegistryStage<T>[];
  snapshotHash: string;
}

export interface RegistryMutation<T extends JsonObject = JsonObject> {
  kind: "put" | "delete" | "enable" | "disable";
  id: string;
  value?: T;
  source?: string;
  sourceRevision?: string;
  expectedEntryRevision?: number | null;
  metadata?: JsonObject;
}

export interface RegistryStage<T extends JsonObject = JsonObject> {
  stageId: string;
  expectedRegistryRevision: number;
  mutations: RegistryMutation<T>[];
  mutationHash: string;
  createdAt: string;
  source: string;
  metadata: JsonObject;
}

export interface RegistryCommit<T extends JsonObject = JsonObject> {
  stageId: string;
  revisionBefore: number;
  revisionAfter: number;
  added: RevisionedEntry<T>[];
  updated: RevisionedEntry<T>[];
  removed: RevisionedEntry<T>[];
  unchanged: RevisionedEntry<T>[];
  commitHash: string;
  committedAt: string;
}

export type PermissionEffect = "allow" | "deny" | "ask";
export type PermissionMode =
  | "default"
  | "acceptEdits"
  | "dontAsk"
  | "plan"
  | "bypassPermissions"
  | "auto"
  | "sealed";
export type PermissionScopeKind =
  | "tool"
  | "command"
  | "resource"
  | "server"
  | "session"
  | "workspace";
export type PermissionRuleSource =
  | "managed"
  | "policy"
  | "user"
  | "project"
  | "workspace"
  | "plugin"
  | "command"
  | "session"
  | "standing-grant";

export interface PermissionScope extends JsonObject {
  kind: PermissionScopeKind;
  toolPattern: string;
  namespacePattern: string;
  serverPattern: string;
  commandPattern: string;
  resourcePattern: string;
  operationPattern: string;
  workspacePattern: string;
  sessionPattern: string;
  argumentPattern: string;
}

export interface PermissionRuleRecord extends JsonObject {
  ruleId: string;
  effect: PermissionEffect;
  source: PermissionRuleSource;
  scope: PermissionScope;
  priority: number;
  enabled: boolean;
  reason: string;
  expiresAt: string | null;
  maxUses: number | null;
  useCount: number;
  revision: number;
  createdAt: string;
  updatedAt: string;
  metadata: JsonObject;
}

export interface PermissionRequestContext extends JsonObject {
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  toolName: string;
  namespace: string;
  serverId: string;
  commandName: string;
  resourceUri: string;
  operation: string;
  workspaceRoot: string;
  arguments: JsonObject;
  metadata: JsonObject;
}

export interface PermissionRiskAssessment extends JsonObject {
  level: "low" | "medium" | "high" | "unknown";
  score: number;
  reasons: string[];
  deterministicSignals: string[];
  classifierSuggestion: PermissionEffect | null;
  classifierConfidence: number | null;
  classifierExplanation: string | null;
  classifierCanOverride: false;
  assessedArgumentsHash: string;
}

export interface PermissionHookAudit extends JsonObject {
  hookId: string;
  pluginId: string | null;
  order: number;
  beforeDigest: string;
  afterDigest: string;
  changed: boolean;
  outcome: "pass" | "mutate" | "deny" | "ask" | "error";
  reason: string;
  durationMs: number;
  errorCode: string | null;
  errorMessage: string | null;
  outputMetadata: JsonObject;
}

export interface PermissionDecisionRecord extends JsonObject {
  decisionId: string;
  canonicalOwner: "typescript";
  effect: PermissionEffect;
  reasonCode: string;
  reason: string;
  mode: PermissionMode;
  modeRevision: number;
  policyRevision: number;
  policyDigest: string;
  requestFingerprint: string;
  originalArgumentsDigest: string;
  finalArgumentsDigest: string;
  finalArguments: JsonObject;
  matchedRuleIds: string[];
  matchedRuleSources: string[];
  risk: PermissionRiskAssessment;
  hooks: PermissionHookAudit[];
  requestBinding: JsonObject;
  recoveryInput: JsonObject | null;
  replanRequired: boolean;
  humanInterventionCount: number;
  continuationRequestId: string | null;
  evaluatedAt: string;
  metadata: JsonObject;
}

export interface PermissionContinuationRecord extends JsonObject {
  requestId: string;
  decisionId: string;
  requestFingerprint: string;
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  policyRevision: number;
  modeRevision: number;
  argumentsDigest: string;
  status: "pending" | "approved" | "denied" | "expired" | "cancelled" | "consumed";
  createdAt: string;
  expiresAt: string;
  respondedAt: string | null;
  consumedAt: string | null;
  responseId: string | null;
  responder: string | null;
  responseDigest: string | null;
  metadata: JsonObject;
}

export interface PermissionApprovalResponse extends JsonObject {
  responseId: string;
  requestId: string;
  runId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  effect: "allow" | "deny";
  responder: string;
  respondedAt: string;
  metadata: JsonObject;
}

export interface PermissionStandingGrant extends JsonObject {
  grantId: string;
  sessionId: string;
  workspaceRoot: string;
  scope: PermissionScope;
  issuedForDecisionId: string;
  issuedAt: string;
  expiresAt: string;
  revision: number;
  maxUses: number;
  useCount: number;
  revokedAt: string | null;
  revocationReason: string | null;
  metadata: JsonObject;
}

export interface E02PermissionSnapshot extends JsonObject {
  version: "zyra.e02-permission/v1";
  mode: PermissionMode;
  modeRevision: number;
  policyRevision: number;
  policyDigest: string;
  rules: PermissionRuleRecord[];
  grants: PermissionStandingGrant[];
  continuations: PermissionContinuationRecord[];
  decisions: PermissionDecisionRecord[];
  denialCount: number;
  journal: JsonObject;
  snapshotHash: string;
}

export interface CapabilityDescriptor extends JsonObject {
  id: string;
  kind: "tool" | "resource" | "prompt" | "instruction" | "skill" | "plugin" | "command";
  name: string;
  namespace: string;
  serverId: string;
  sourceId: string;
  sourceKind: string;
  version: string;
  revision: number;
  schema: JsonObject;
  annotations: JsonObject;
  description: string;
  enabled: boolean;
  contentDigest: string;
  provenance: JsonObject;
  metadata: JsonObject;
}

export interface CapabilityRevision extends JsonObject {
  revisionId: string;
  namespace: string;
  revision: number;
  expectedRevision: number;
  sourceId: string;
  sourceRevision: string;
  added: string[];
  updated: string[];
  removed: string[];
  unchanged: string[];
  stagedAt: string;
  committedAt: string | null;
  status: "staged" | "committed" | "rejected";
  digest: string;
  metadata: JsonObject;
}

export interface E02CapabilityResult {
  summary: string;
  output: JsonObject;
  metadata: Record<string, string>;
  contextDelta?: JsonObject;
  toolScopeDelta?: JsonObject;
  events?: E02EventEnvelope[];
}

export interface E02CapabilityRuntime {
  open(): Promise<void>;
  owns(toolName: string): boolean;
  owner(toolName: string): string;
  toolSpecs(): ToolSpecContract[];
  execute(toolName: string, argumentsValue: JsonObject): Promise<E02CapabilityResult>;
  snapshot(): JsonObject;
  close(): Promise<void>;
}

export interface E02Snapshot extends JsonObject {
  version: "zyra.e02-runtime/v1";
  runtime: E02RuntimeIdentity;
  permission: E02PermissionSnapshot;
  mcp: JsonObject;
  skills: JsonObject;
  plugins: JsonObject;
  commands: JsonObject;
  journal: JsonObject;
  events: JsonObject;
  snapshotHash: string;
}

export interface E02RestoreEnvelope extends JsonObject {
  snapshot: E02Snapshot;
  snapshotHash: string;
  restoredAt: string;
  sourceEpoch: number;
  targetEpoch: number;
}

export interface E02RuntimePorts {
  emit(event: E02EventEnvelope): Promise<void>;
  checkpoint(snapshot: E02Snapshot): Promise<void>;
  persistSecret(handle: string, value: string, metadata: JsonObject): Promise<void>;
  readSecret(handle: string): Promise<string | null>;
  deleteSecret(handle: string): Promise<void>;
  now?: E02Clock;
}

export interface E02MutationObservation extends JsonObject {
  mutationId: string;
  targetPath: string;
  targetSymbol: string;
  killed: boolean;
  killerTestIds: string[];
  observedStateHash: string;
  baselineStateHash: string;
  error: string | null;
}

export interface E02HealthReport extends JsonObject {
  status: "ready" | "candidate_pending_independent_review" | "failed";
  canonicalPermissionOwner: "typescript";
  canonicalMcpOwner: "typescript";
  canonicalSkillOwner: "typescript";
  canonicalPluginOwner: "typescript";
  canonicalCommandOwner: "typescript";
  pythonDecisionFallback: false;
  restoredBeforeBootstrap: boolean;
  pendingTransitions: number;
  committedTransitions: number;
  capabilityRevision: number;
  permissionPolicyRevision: number;
  snapshotHash: string;
  details: JsonObject;
}

export type E02JsonValue = JsonValue;
