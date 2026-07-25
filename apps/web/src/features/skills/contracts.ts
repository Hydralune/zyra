import type { JsonObject, JsonValue } from "../../events/ingress/index.ts"
import type { CommandReceipt } from "../../../../../packages/commands/src/index.ts"

export type SkillAvailability =
  | "available"
  | "disabled"
  | "shadowed"
  | "invalid"
  | "missing"

export type SkillControlPhase =
  | "admitting"
  | "submitting"
  | "queued"
  | "permission_pending"
  | "reconciling"
  | "committed"
  | "failed"
  | "quarantined"
  | "disconnected"
  | "disabled"

export type SkillOperationKind = "update" | "invoke"

export type SkillRiskSeverity = "info" | "low" | "medium" | "high" | "critical"

export type SkillApprovalStage =
  | "unreviewed"
  | "provenance_reviewed"
  | "dependencies_reviewed"
  | "supply_chain_reviewed"
  | "approved"
  | "rejected"
  | "expired"

export interface SkillBodySection {
  id: string
  level: number
  heading: string
  body: string
  lineStart: number
  lineEnd: number
  codeBlocks: number
  linkTargets: readonly string[]
  resourceRefs: readonly string[]
  digest: string
}

export interface SkillBodyProjection {
  markdown: string
  summary: string
  sections: readonly SkillBodySection[]
  headings: readonly string[]
  codeBlockCount: number
  linkTargets: readonly string[]
  resourceRefs: readonly string[]
  suspiciousFragments: readonly string[]
  truncated: boolean
  byteLength: number
  digest: string
}

export interface SkillResourceProjection {
  resourceId: string
  path: string
  kind: string
  required: boolean
  maximumBytes?: number
  sizeBytes?: number
  mediaType?: string
  charset?: string
  digest?: string
  available: boolean
  private: boolean
  source: string
  artifactId?: string
  warnings: readonly string[]
}

export interface SkillToolScopeProjection {
  allowed: readonly string[]
  denied: readonly string[]
  namespaces: readonly string[]
  mcpServers: readonly string[]
  requireApproval: readonly string[]
  readOnly: boolean
  inheritParent: boolean
  maximumCalls?: number
  maximumParallel: number
  warnings: readonly string[]
}

export interface SkillProvenanceProjection {
  sourceId: string
  sourceKind: string
  sourcePriority: number
  pluginId?: string
  relativePath?: string
  rootCategory: string
  canonicalOwner: string
  capabilityOwner?: string
  registryOwner?: string
  signatureStatus: string
  signer?: string
  repository?: string
  revision?: string
  discoveredAt?: string
  parsedAt?: string
  modifiedAt?: string
  provenanceDigest: string
  trustworthy: boolean
  warnings: readonly string[]
}

export interface SkillVersionProjection {
  version: string
  registryRevision: number
  revisionId?: string
  parentRevisionId?: string
  contentHash: string
  descriptorDigest: string
  bodyDigest: string
  frontmatterDigest?: string
  resourceDigest?: string
  observedAt: string
  eventId: string
}

export interface SkillDependencyProjection {
  dependencyId: string
  name: string
  kind: "skill" | "plugin" | "mcp" | "tool" | "resource" | "package" | "unknown"
  versionRange?: string
  resolvedVersion?: string
  resolvedId?: string
  required: boolean
  optional: boolean
  approved: boolean
  available: boolean
  digest?: string
  source?: string
  warnings: readonly string[]
}

export interface SkillDependencyEdge {
  from: string
  to: string
  kind: SkillDependencyProjection["kind"]
  required: boolean
  approved: boolean
}

export interface SkillDependencyGraph {
  rootId: string
  nodes: readonly SkillDependencyProjection[]
  edges: readonly SkillDependencyEdge[]
  topologicalOrder: readonly string[]
  missing: readonly string[]
  unapproved: readonly string[]
  versionConflicts: readonly string[]
  cycles: readonly (readonly string[])[]
  maximumDepth: number
  complete: boolean
  digest: string
}

export interface SkillSupplyChainFinding {
  findingId: string
  code: string
  severity: SkillRiskSeverity
  message: string
  source: string
  subject?: string
  evidenceRefs: readonly string[]
  blocking: boolean
  canonical: boolean
}

export interface SkillSupplyChainAudit {
  policyRevision?: string
  receiptId?: string
  scannedAt?: string
  manifestDigest?: string
  allowedByOwner?: boolean
  browserDisposition: "pass" | "review" | "block" | "unknown"
  riskScore: number
  findings: readonly SkillSupplyChainFinding[]
  blockingFindingIds: readonly string[]
  warnings: readonly string[]
  digest: string
}

export interface SkillStagedApproval {
  stage: SkillApprovalStage
  approvalId?: string
  approvalRevision?: number
  approvedHash?: string
  approvedDependencyDigest?: string
  approvedSupplyDigest?: string
  reviewerClass?: string
  reviewedAt?: string
  expiresAt?: string
  rejectedReason?: string
  current: boolean
  updateEligible: boolean
  invokeEligible: boolean
  reasons: readonly string[]
}

export interface SkillInvocationProjection {
  invocationId: string
  skillId: string
  taskId: string
  runId: string
  sessionId?: string
  toolCallId: string
  workerId?: string
  commandId?: string
  permissionId?: string
  registryRevision: number
  descriptorDigest?: string
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | "background" | "unknown"
  mode?: string
  startedAt: string
  completedAt?: string
  durationMs?: number
  toolCalls?: number
  inputTokens?: number
  outputTokens?: number
  costMicros?: number
  resultSummary?: string
  resultDigest?: string
  resultArtifactIds: readonly string[]
  errorCode?: string
  errorMessage?: string
  eventIds: readonly string[]
  canonical: boolean
  quarantined: boolean
  quarantineReason?: string
}

export interface SkillProjection {
  skillId: string
  name: string
  displayName: string
  description: string
  taskId: string
  runId: string
  sessionId?: string
  availability: SkillAvailability
  disabledReason?: string
  canonicalOwner: string
  projectionRevision: number
  sequence: number
  lastEventId: string
  lastUpdatedAt: string
  body: SkillBodyProjection
  resources: readonly SkillResourceProjection[]
  toolScope: SkillToolScopeProjection
  provenance: SkillProvenanceProjection
  version: SkillVersionProjection
  dependencies: SkillDependencyGraph
  supplyChain: SkillSupplyChainAudit
  approval: SkillStagedApproval
  invocations: readonly SkillInvocationProjection[]
  activeInvocationIds: readonly string[]
  resultCount: number
  errorCount: number
  warnings: readonly string[]
  ready: boolean
  fingerprint: string
}

export interface SkillCatalogProjection {
  taskId: string
  runId?: string
  sessionId?: string
  revision: number
  skills: readonly SkillProjection[]
  selectedSkillId?: string
  totalSkills: number
  availableSkills: number
  blockedSkills: number
  activeInvocations: number
  warnings: readonly string[]
  rejectedCandidates: readonly SkillProjectionRejection[]
  fingerprint: string
}

export interface SkillProjectionRejection {
  sourceId: string
  code: string
  reason: string
  eventId?: string
  skillId?: string
}

export interface SkillProjectionCandidate {
  sourceId: string
  sourceToolCallId: string
  sourceEventId: string
  sourceSequence: number
  sourceRevision: number
  sourceUpdatedAt: string
  taskId: string
  runId: string
  sessionId?: string
  workerId?: string
  canonicalOwner: string
  payload: Readonly<JsonObject>
  metadata: Readonly<JsonObject>
  artifactIds: readonly string[]
  commandId?: string
  permissionId?: string
  errorCode?: string
}

export interface SkillProjectionAdmission {
  accepted: boolean
  candidate?: SkillProjectionCandidate
  rejection?: SkillProjectionRejection
}

export interface SkillControlEffectBaseline {
  operationId: string
  kind: SkillOperationKind
  taskId: string
  runId: string
  sessionId?: string
  skillId: string
  projectionRevision: number
  skillProjectionRevision: number
  contentHash: string
  registryRevision: number
  dependencyDigest: string
  supplyDigest: string
  invocationIds: readonly string[]
  eventCount: number
  fingerprint: string
}

export interface SkillControlEffectVerification {
  operationId: string
  kind: SkillOperationKind
  satisfied: boolean
  pending: boolean
  quarantined: boolean
  observedProjectionRevision: number
  changedFields: readonly string[]
  evidenceEventIds: readonly string[]
  evidenceInvocationIds: readonly string[]
  reasons: readonly string[]
  verifiedAt: string
}

export interface SkillControlOperation {
  operationId: string
  kind: SkillOperationKind
  phase: SkillControlPhase
  taskId: string
  runId: string
  sessionId?: string
  skillId: string
  expectedHash: string
  expectedRegistryRevision: number
  nonce: string
  idempotencyKey: string
  argumentsDigest?: string
  startedAt: string
  updatedAt: string
  commandId?: string
  requestId?: string
  permissionId?: string
  receipt?: CommandReceipt
  errorCode?: string
  errorMessage?: string
  canonicalEventIds: readonly string[]
  effect?: SkillControlEffectVerification
}

export interface SkillWorkbenchSnapshot {
  taskId?: string
  runId?: string
  sessionId?: string
  selectedSkillId?: string
  connected: boolean
  enabled: boolean
  viewerOpen: boolean
  sealed: boolean
  closed: boolean
  disabledReason?: string
  catalog?: SkillCatalogProjection
  activeOperation?: SkillControlOperation
  operations: readonly SkillControlOperation[]
  lastReceipt?: CommandReceipt
  revision: number
}

export interface SkillCatalogQuery {
  text?: string
  availability?: readonly SkillAvailability[]
  sourceKinds?: readonly string[]
  tools?: readonly string[]
  findingSeverities?: readonly SkillRiskSeverity[]
  approvalStages?: readonly SkillApprovalStage[]
  activeOnly?: boolean
  blockedOnly?: boolean
  sort?: "name" | "updated" | "risk" | "invocations" | "source"
  direction?: "asc" | "desc"
  limit?: number
  cursor?: string
}

export interface SkillCatalogPage {
  rows: readonly SkillProjection[]
  total: number
  offset: number
  limit: number
  nextCursor?: string
  previousCursor?: string
  revision: number
  queryFingerprint: string
  facets: {
    availability: Readonly<Record<string, number>>
    sourceKinds: Readonly<Record<string, number>>
    approvalStages: Readonly<Record<string, number>>
    severities: Readonly<Record<string, number>>
    tools: Readonly<Record<string, number>>
  }
}

export interface SkillCommandPort {
  submit(
    value: string,
    options?: { mode?: "enqueue" | "steer" | "interrupt"; sealed?: boolean },
  ): Promise<CommandReceipt>
  subscribe(listener: () => void): () => void
  getSnapshot(): {
    lastOverlayReceipt?: CommandReceipt
  }
}

export interface SkillPermissionPort {
  getSnapshot(): { productMode: "interactive" | "sealed" }
  recordSealedAction(input: {
    action: "steer" | "retry" | "mode_change"
    actorId: string
    requestId?: string
    reason: string
    signal?: AbortSignal
  }): Promise<unknown>
}

export type SkillArguments = Readonly<Record<string, JsonValue>>
