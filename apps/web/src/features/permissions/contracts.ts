import type { PermissionSessionBinding } from "../../api/permission-api.ts"

export const PERMISSION_CONSOLE_SCHEMA = "zyra.permission-console/v1" as const
export const PERMISSION_RESPONSE_VERSION = "zyra.permission-response/v2" as const
export const LEGACY_PERMISSION_RESPONSE_VERSION = "zyra.permission-response/v1" as const
export type PermissionResponseVersion = typeof PERMISSION_RESPONSE_VERSION | typeof LEGACY_PERMISSION_RESPONSE_VERSION
export const PERMISSION_CANONICAL_OWNER =
  "typescript.PermissionCoordinator" as const

export type PermissionEffect = "allow" | "deny" | "ask"
export type PermissionResponseEffect = Exclude<PermissionEffect, "ask">
export type PermissionRequestStatus =
  | "pending_delivery"
  | "delivered"
  | "delivery_failed"
  | "responded"
  | "expired"
  | "cancelled"
  | "unknown"
export type PermissionConsolePhase =
  | "idle"
  | "binding"
  | "loading"
  | "ready"
  | "responding"
  | "reconnecting"
  | "error"
  | "disabled"
  | "closed"
export type PermissionProductMode = "interactive" | "sealed"
export type PermissionRiskLevel = "low" | "medium" | "high" | "unknown"
export type PermissionWarningSeverity = "info" | "warning" | "danger"
export type PermissionSourceSurface =
  | "tool"
  | "browser"
  | "terminal"
  | "mcp"
  | "command"
  | "plugin"
  | "skill"
  | "agent"
  | "unknown"

export interface PermissionResponseChallenge {
  version: PermissionResponseVersion
  nonce: string
  canonicalOwner: typeof PERMISSION_CANONICAL_OWNER
  challengeDigest: string
}

export interface PermissionRequestIdentity extends PermissionSessionBinding {
  envelopeId: string
  requestId: string
  decisionId: string
  sessionRevision: number
  workerRequestId: string
  toolCallId: string
  requestFingerprint: string
  argumentsDigest: string
  policyRevision: number
  modeRevision: number
  expiresAt: string
  canonicalOwner: typeof PERMISSION_CANONICAL_OWNER
}

export interface PermissionRequestBinding {
  toolName: string
  namespace: string
  serverId: string
  operation: string
  commandName: string
  resourceUri: string
  workspaceRoot: string
  capabilityRevision?: number
  pluginRevision?: number
  skillRevision?: number
  schemaDigest?: string
}

export interface PermissionRequestProjection
  extends PermissionRequestIdentity,
    PermissionRequestBinding {
  status: PermissionRequestStatus
  prompt: string
  reason: string
  createdAt: string
  updatedAt: string
  deliveredAt?: string
  deliveryAttempt: number
  deliveryReceiptId?: string
  responseId?: string
  responseEffect?: PermissionResponseEffect
  responseAccepted: boolean
  responder?: string
  sourceSurface: PermissionSourceSurface
  riskLevel: PermissionRiskLevel
  responseChallenge: PermissionResponseChallenge
  redactedPreview: PermissionArgumentPreview
  warnings: readonly PermissionWarning[]
  terminal: boolean
  expired: boolean
  stale: boolean
  selectable: boolean
  raw: Readonly<Record<string, unknown>>
}

export interface PermissionArgumentPreview {
  summary: string
  rows: readonly PermissionArgumentRow[]
  redactedFields: readonly string[]
  omittedFields: readonly string[]
  secretCount: number
  byteCount: number
  digest: string
}

export interface PermissionArgumentRow {
  key: string
  label: string
  value: string
  kind:
    | "text"
    | "path"
    | "url"
    | "command"
    | "identifier"
    | "count"
    | "boolean"
    | "redacted"
    | "omitted"
  sensitive: boolean
  truncated: boolean
}

export interface PermissionWarning {
  code: string
  severity: PermissionWarningSeverity
  title: string
  detail: string
  evidence: readonly string[]
  source: "binding" | "preview" | "policy" | "runtime"
  blocksPersistentGrant: boolean
}

export interface PermissionModeProjection {
  mode: string
  productMode: PermissionProductMode
  revision: number
  policyRevision: number
  policyHash: string
  frozen: boolean
  noHuman: boolean
  askWillDeny: boolean
  humanInterventionCount: number
  canonicalOwner: string
}

export interface PermissionRuleProjection {
  ruleId: string
  effect: PermissionEffect
  source: string
  priority: number
  enabled: boolean
  reason: string
  revision: number
  expiresAt?: string
  scopeSummary: string
}

export interface PermissionDecisionProjection {
  decisionId: string
  requestId?: string
  toolCallId?: string
  effect: PermissionEffect
  reasonCode: string
  reason: string
  policyRevision: number
  modeRevision: number
  evaluatedAt: string
  exactBinding: boolean
  responseId?: string
  permitId?: string
  humanInterventionCount: number
}

export interface PermissionDecisionReceipt {
  requestId: string
  responseId: string
  effect: PermissionResponseEffect
  accepted: boolean
  replayed: boolean
  responseProofVerified: boolean
  responseProofDigest?: string
  responseChallengeDigest?: string
  permitId?: string
  decisionId?: string
  finalArgumentsDigest?: string
  stateDigest?: string
  canonicalOwner: string
  resumed: boolean
  receivedAt: string
  raw: Readonly<Record<string, unknown>>
}

export interface PermissionResponseDraft {
  request: PermissionRequestProjection
  responseId: string
  effect: PermissionResponseEffect
  displayResponder: string
  feedback?: string
  createdAt: string
}

export interface PermissionResponseProof {
  version: PermissionResponseVersion
  decision_scope?: "once"
  nonce: string
  canonical_owner: typeof PERMISSION_CANONICAL_OWNER
  envelope_id: string
  request_id: string
  response_id: string
  effect: PermissionResponseEffect
  run_id: string
  task_id: string
  session_id: string
  session_revision: number
  worker_request_id: string
  tool_call_id: string
  request_fingerprint: string
  arguments_digest: string
  policy_revision: number
  mode_revision: number
  expires_at: string
  proof: string
}

export interface PermissionResponseAttempt {
  attemptId: string
  requestId: string
  responseId: string
  effect: PermissionResponseEffect
  source:
    | "permission-panel"
    | "browser-panel"
    | "terminal-panel"
    | "timeline"
    | "command"
    | "timeout"
    | "reconnect"
  displayResponder: string
  phase:
    | "created"
    | "claimed"
    | "proof_ready"
    | "submitted"
    | "accepted"
    | "rejected"
    | "lost_race"
    | "cancelled"
  createdAt: string
  updatedAt: string
  errorCode?: string
  errorMessage?: string
  receipt?: PermissionDecisionReceipt
}

export interface PermissionInterventionRecord {
  interventionId: string
  requestId?: string
  action: "approve" | "deny" | "steer" | "retry" | "mode_change"
  actorId: string
  productMode: PermissionProductMode
  rejected: boolean
  counted: boolean
  reasonCode: string
  reason: string
  recordedAt: string
  canonicalReceipt?: Readonly<Record<string, unknown>>
}

export interface PermissionTimelineEntry {
  entryId: string
  requestId?: string
  responseId?: string
  kind:
    | "request"
    | "delivery"
    | "response"
    | "decision"
    | "permit"
    | "timeout"
    | "restore"
    | "intervention"
    | "error"
  phase: string
  title: string
  detail: string
  createdAt: string
  severity: PermissionWarningSeverity
  sourceSurface: PermissionSourceSurface
  exactBinding: boolean
  canonical: boolean
  refs: Readonly<Record<string, string>>
}

export interface PermissionCrossViewTarget {
  kind: "permission" | "browser" | "terminal" | "timeline" | "command"
  selector: string
  label: string
  requestId: string
  available: boolean
}

export interface PermissionQueueRow {
  request: PermissionRequestProjection
  selected: boolean
  responsePending: boolean
  secondsRemaining: number
  urgency: "expired" | "critical" | "soon" | "normal"
  crossViewTargets: readonly PermissionCrossViewTarget[]
}

export interface PermissionConsoleDiagnostics {
  generation: number
  refreshCount: number
  reconnectCount: number
  lastRefreshStartedAt?: string
  lastRefreshCompletedAt?: string
  lastErrorCode?: string
  lastErrorMessage?: string
  sourceOwner: string
  sourceSchema: string
  backendAuthoritative: boolean
  localPendingStore: false
  custodyInMemoryOnly: true
  responseProofRequired: true
  eventProjectionRevision?: number
  admittedPermissionEvents?: number
  quarantinedPermissionEvents?: number
  custodySessionCount?: number
  custodyFailureCount?: number
  reconnectPhase?: string
  nextReconnectAt?: string
  nextExpiryAt?: string
  expiryClaimCount?: number
  issuedPermitCount?: number
  consumedPermitCount?: number
  permitConflictCount?: number
  disabledReason?: string
}

export interface PermissionConsoleSnapshot {
  schema: typeof PERMISSION_CONSOLE_SCHEMA
  phase: PermissionConsolePhase
  productMode: PermissionProductMode
  binding?: PermissionSessionBinding
  bindings: readonly PermissionSessionBinding[]
  selectedRequestId?: string
  requests: readonly PermissionRequestProjection[]
  rows: readonly PermissionQueueRow[]
  selected?: PermissionRequestProjection
  mode: PermissionModeProjection
  rules: readonly PermissionRuleProjection[]
  decisions: readonly PermissionDecisionProjection[]
  attempts: readonly PermissionResponseAttempt[]
  receipts: readonly PermissionDecisionReceipt[]
  interventions: readonly PermissionInterventionRecord[]
  timeline: readonly PermissionTimelineEntry[]
  pendingCount: number
  expiringCount: number
  respondingRequestIds: readonly string[]
  responseError?: {
    code: string
    message: string
    requestId?: string
    retryable: boolean
  }
  diagnostics: PermissionConsoleDiagnostics
  revision: number
  updatedAt: string
}

export interface PermissionTaskBindingInput extends PermissionSessionBinding {
  additionalSessionIds?: readonly string[]
  custodyTokens?: Readonly<Record<string, string>>
  productMode?: PermissionProductMode
  policyHash?: string
  policyRevision?: number
  humanInterventionCount?: number
}

export interface PermissionRespondOptions {
  effect: PermissionResponseEffect
  requestId: string
  source: PermissionResponseAttempt["source"]
  displayResponder: string
  feedback?: string
  signal?: AbortSignal
}

export interface PermissionConsoleOptions {
  now?: () => Date
  pollIntervalMs?: number
  requestTimeoutMs?: number
  maximumRequests?: number
  maximumTimeline?: number
  maximumAttempts?: number
  maximumReceipts?: number
  maximumInterventions?: number
  disabled?: boolean
}
