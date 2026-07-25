import type { CommandReceipt } from "../../../../../packages/commands/src/contracts.ts"
import type {
  CanonicalProjectionState,
  CausalEventProjection,
  WorkerProjection,
} from "../../state/contracts.ts"

export const SUBAGENT_PANEL_SCHEMA = "zyra.subagent-panel/v1" as const

export type SubagentLifecycle =
  | "created"
  | "validating"
  | "ready"
  | "dispatched"
  | "starting"
  | "running"
  | "waiting"
  | "paused"
  | "recovering"
  | "resuming"
  | "completed"
  | "failed"
  | "cancelled"
  | "killed"
  | "rejected"
  | "unknown"

export type HeartbeatPhase =
  | "healthy"
  | "due"
  | "stale"
  | "timed_out"
  | "missing"
  | "terminal"

export type AdmissionSeverity = "info" | "warning" | "error"

export interface SubagentAdmissionIssue {
  code: string
  severity: AdmissionSeverity
  message: string
  childId?: string
  eventId?: string
  field?: string
}

export interface SubagentScopeProjection {
  digest?: string
  permissionCeilingDigest?: string
  toolCatalogDigest?: string
  permissionMode?: string
  parentTools: readonly string[]
  childTools: readonly string[]
  deniedTools: readonly string[]
  skills: readonly string[]
  mcpServers: readonly string[]
  memoryScope?: string
  isolation?: string
  workspaceId?: string
  worktreeId?: string
  remoteId?: string
  lineage: readonly string[]
  cycleKey?: string
  allowKill: boolean
  allowSteer: boolean
  bounded: boolean
}

export interface BudgetMetric {
  key:
    | "turns"
    | "tool_calls"
    | "input_tokens"
    | "output_tokens"
    | "result_chars"
    | "wall_time_ms"
    | "children"
    | "depth"
  used: number
  limit: number
  remaining: number
  ratio: number
  exceeded: boolean
  nearLimit: boolean
}

export interface SubagentBudgetProjection {
  metrics: readonly BudgetMetric[]
  maxTurns: number
  turnsUsed: number
  maxToolCalls: number
  toolCallsUsed: number
  maxInputTokens: number
  inputTokensUsed: number
  maxOutputTokens: number
  outputTokensUsed: number
  maxResultChars: number
  resultCharsUsed: number
  maxWallTimeMs: number
  wallTimeMs: number
  maxChildren: number
  childCount: number
  maxDepth: number
  depth: number
  highestRatio: number
  exceeded: boolean
  warnings: readonly string[]
}

export interface SubagentHeartbeatProjection {
  phase: HeartbeatPhase
  lastAt?: string
  expectedEveryMs: number
  timeoutMs: number
  ageMs?: number
  dueAt?: string
  timeoutAt?: string
  sequence?: number
  sourceEventId?: string
  connected: boolean
}

export interface SubagentResultProjection {
  digest?: string
  summary?: string
  artifactIds: readonly string[]
  usage: Readonly<Record<string, number>>
  committedEventId?: string
  committedAt?: string
}

export interface SubagentErrorProjection {
  code?: string
  message?: string
  failureId?: string
  eventId?: string
  retryable?: boolean
  crashed: boolean
}

export interface LateResultRecord {
  id: string
  childId: string
  attempt: number
  terminalEventId: string
  resultEventId: string
  terminalSequence: number
  resultSequence: number
  resultDigest?: string
  artifactIds: readonly string[]
  reason: string
  quarantinedAt: string
  releasable: false
}

export interface SubagentRow {
  id: string
  taskId: string
  runId: string
  sessionId?: string
  parentId: string
  parentTaskId?: string
  parentRunId?: string
  parentSessionId?: string
  workerId?: string
  ownerId?: string
  routeId?: string
  placementId?: string
  leaseId?: string
  checkpointId?: string
  definitionName?: string
  definitionDigest?: string
  contextDigest?: string
  promptDigest?: string
  idempotencyKey?: string
  executionMode?: string
  lifecycle: SubagentLifecycle
  depth: number
  attempt: number
  sequence: number
  revision: number
  createdAt: string
  updatedAt: string
  terminal: boolean
  admitted: boolean
  controlEligible: boolean
  reconnected: boolean
  crashed: boolean
  quarantined: boolean
  connected: boolean
  childIds: readonly string[]
  scope: SubagentScopeProjection
  budget: SubagentBudgetProjection
  heartbeat: SubagentHeartbeatProjection
  result?: SubagentResultProjection
  error?: SubagentErrorProjection
  lateResults: readonly LateResultRecord[]
  eventIds: readonly string[]
  artifactIds: readonly string[]
  issues: readonly SubagentAdmissionIssue[]
  fingerprint: string
}

export interface SubagentHierarchyNode {
  id: string
  row: SubagentRow
  parentId: string
  depth: number
  declaredDepth: number
  childIds: readonly string[]
  descendantIds: readonly string[]
  activeDescendants: number
  terminalDescendants: number
  budgetExceededDescendants: number
  heartbeatRiskDescendants: number
  cyclic: boolean
  orphaned: boolean
  path: readonly string[]
}

export interface SubagentHierarchy {
  nodes: Readonly<Record<string, SubagentHierarchyNode>>
  roots: readonly string[]
  order: readonly string[]
  orphanIds: readonly string[]
  cycleIds: readonly string[]
  maximumDepth: number
  fingerprint: string
}

export interface SubagentProjection {
  schema: typeof SUBAGENT_PANEL_SCHEMA
  taskId: string
  runId?: string
  sessionId?: string
  canonicalRevision: number
  connected: boolean
  ready: boolean
  rows: readonly SubagentRow[]
  rowById: Readonly<Record<string, SubagentRow>>
  hierarchy: SubagentHierarchy
  activeIds: readonly string[]
  settledIds: readonly string[]
  failedIds: readonly string[]
  quarantinedIds: readonly string[]
  lateResults: readonly LateResultRecord[]
  issues: readonly SubagentAdmissionIssue[]
  sourceEventIds: readonly string[]
  rejectedWorkerIds: readonly string[]
  fingerprint: string
}

export interface SubagentProjectionOptions {
  runId?: string
  sessionId?: string
  nowMs?: number
  heartbeatIntervalMs?: number
  heartbeatTimeoutMs?: number
  maximumRows?: number
  includeRejected?: boolean
}

export interface SubagentCandidate {
  childId: string
  worker?: WorkerProjection
  events: readonly CausalEventProjection[]
}

export type SubagentControlAction = "kill" | "steer"

export type SubagentControlPhase =
  | "admitted"
  | "permission_pending"
  | "queued"
  | "running"
  | "reconciling"
  | "committed"
  | "denied"
  | "failed"
  | "quarantined"
  | "disconnected"
  | "disabled"

export interface SubagentControlBinding {
  taskId: string
  runId: string
  sessionId?: string
  parentId: string
  childId: string
  attempt: number
  leaseId: string
  ownerId?: string
  expectedRevision: number
}

export interface SubagentControlBaseline {
  projectionRevision: number
  childRevision: number
  lifecycle: SubagentLifecycle
  checkpointId?: string
  resultDigest?: string
  errorCode?: string
  heartbeatSequence?: number
  eventSequence: number
  fingerprint: string
}

export interface SubagentEffectSettlement {
  satisfied: boolean
  pending: boolean
  semanticEffect:
    | "terminal_cancel"
    | "steering_applied"
    | "permission_denied"
    | "late_result_quarantined"
  baselineRevision: number
  observedRevision: number
  changedFields: readonly string[]
  evidenceEventIds: readonly string[]
  evidenceArtifactIds: readonly string[]
  reasons: readonly string[]
  checkedAt: string
}

export interface SubagentControlOperation {
  id: string
  action: SubagentControlAction
  phase: SubagentControlPhase
  binding: SubagentControlBinding
  nonce: string
  idempotencyKey: string
  payloadFingerprint: string
  reason: string
  instruction?: string
  sealed: boolean
  startedAt: string
  updatedAt: string
  baseline: SubagentControlBaseline
  receipt?: CommandReceipt
  requestId?: string
  commandId?: string
  permissionId?: string
  ownerReceiptId?: string
  error?: string
  effect?: SubagentEffectSettlement
  canonicalEventIds: readonly string[]
  replayCount: number
}

export interface SealedSubagentIntervention {
  id: string
  action: SubagentControlAction
  binding: SubagentControlBinding
  actorId: string
  nonce: string
  reason: string
  denied: true
  noHumanWait: true
  interventionCounted: boolean
  humanInterventionCount: 0
  automaticRecoveryAction?: string
  canonicalEventIds: readonly string[]
  createdAt: string
}

export interface SubagentInterventionPort {
  record(input: {
    action: SubagentControlAction
    binding: SubagentControlBinding
    actorId: string
    nonce: string
    reason: string
  }): Promise<{
    id: string
    interventionCounted: boolean
    humanInterventionCount: number
    operatorInterventionAttemptCount: number
    automaticRecoveryAction?: string
    eventIds?: readonly string[]
  }>
}

export interface SubagentCommandPort {
  submit(
    value: string,
    options?: {
      mode?: "enqueue" | "steer" | "interrupt"
      sealed?: boolean
    },
  ): Promise<CommandReceipt>
  subscribe?(listener: () => void): () => void
  getSnapshot?(): unknown
}

export interface SubagentControlRuntimeOptions {
  commands: SubagentCommandPort
  state: () => CanonicalProjectionState
  projection: () => SubagentProjection | undefined
  interventions?: SubagentInterventionPort
  online?: () => boolean
  now?: () => Date
  nonce?: () => string
  maximumOperations?: number
  actorId?: () => string
}

export interface SubagentControlSnapshot {
  enabled: boolean
  connected: boolean
  sealed: boolean
  operations: readonly SubagentControlOperation[]
  active?: SubagentControlOperation
  interventions: readonly SealedSubagentIntervention[]
  revision: number
}

export interface SubagentLifecycleIncident {
  id: string
  childId: string
  attempt: number
  kind:
    | "heartbeat_due"
    | "heartbeat_stale"
    | "heartbeat_timeout"
    | "crashed"
    | "reconnected"
    | "budget_exceeded"
    | "late_result"
  severity: "info" | "warning" | "error"
  message: string
  firstObservedAt: string
  lastObservedAt: string
  occurrences: number
  eventIds: readonly string[]
  acknowledged: boolean
}

export type SubagentPanelFilter =
  | "active"
  | "settled"
  | "failed"
  | "quarantined"
  | "all"

export interface SubagentPanelSnapshot {
  taskId?: string
  runId?: string
  sessionId?: string
  projection?: SubagentProjection
  selectedId?: string
  selected?: SubagentRow
  visibleRows: readonly SubagentRow[]
  expandedIds: readonly string[]
  filter: SubagentPanelFilter
  query: string
  viewerOpen: boolean
  connected: boolean
  enabled: boolean
  sealed: boolean
  disabledReason?: string
  incidents: readonly SubagentLifecycleIncident[]
  controls: SubagentControlSnapshot
  restoredAt?: string
  restoreGeneration: number
  revision: number
}
