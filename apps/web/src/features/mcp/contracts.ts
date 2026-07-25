import type { CommandReceipt } from "../../../../../packages/commands/src/index.ts"

export type McpCatalogKind = "tool" | "resource" | "prompt"
export type McpConnectionState =
  | "connected"
  | "connecting"
  | "needs-auth"
  | "backoff"
  | "disabled"
  | "failed"
  | "disconnected"
  | "unknown"
export type McpAuthState =
  | "valid"
  | "expiring"
  | "expired"
  | "missing"
  | "refreshing"
  | "failed"
  | "not-required"
  | "unknown"
export type McpTransportKind = "stdio" | "sse" | "streamable-http" | "websocket" | "in-process" | "unknown"
export type McpConfigSource =
  | "project"
  | "user"
  | "managed"
  | "plugin"
  | "runtime"
  | "environment"
  | "unknown"

export interface McpProjectionAuthority {
  taskId: string
  runId: string
  sessionId: string
  ownerId: string
  projectionRevision: number
  committedSequence: number
  generation: number
  connected: boolean
  snapshotComplete: boolean
}

export interface McpConfigProvenance {
  source: McpConfigSource
  sourceId?: string
  declaredAt?: string
  configRevision: number
  configDigest?: string
  managed: boolean
  inherited: boolean
  overridden: boolean
  overrideSource?: McpConfigSource
  transport: McpTransportKind
  endpointClass?: "local" | "private" | "public" | "managed" | "unknown"
  environmentKeys: readonly string[]
  argumentCount: number
  headerNames: readonly string[]
  secretFieldPresence: Readonly<Record<string, boolean>>
}

export interface McpAuthProjection {
  required: boolean
  state: McpAuthState
  provider?: string
  accountLabel?: string
  scopeClasses: readonly string[]
  credentialPresent: boolean
  accessTokenPresent: boolean
  refreshTokenPresent: boolean
  clientSecretPresent: boolean
  expiresAt?: string
  expiresInMs?: number
  refreshEligible: boolean
  refreshInFlight: boolean
  lastRefreshAt?: string
  lastRefreshEventId?: string
  lastFailureCode?: string
  lastFailureMessage?: string
  authRevision: number
  warnings: readonly string[]
}

export interface McpCapabilitySummary {
  revision: number
  tools: number
  resources: number
  prompts: number
  elicitation: boolean
  resourceSubscriptions: boolean
  promptListChanged: boolean
  toolListChanged: boolean
  resourceListChanged: boolean
  changedAt?: string
  changedEventId?: string
  added: readonly string[]
  removed: readonly string[]
  warnings: readonly string[]
}

export interface McpCatalogEntry {
  id: string
  serverId: string
  kind: McpCatalogKind
  name: string
  title: string
  description: string
  revision: number
  sequence: number
  eventId?: string
  toolCallId?: string
  uri?: string
  mimeType?: string
  argumentNames: readonly string[]
  requiredArguments: readonly string[]
  annotations: readonly string[]
  capabilityFlags: readonly string[]
  deprecated: boolean
  enabled: boolean
  readOnly: boolean
  sensitive: boolean
  inputSchemaDigest?: string
  outputSchemaDigest?: string
  provenanceDigest?: string
}

export interface McpElicitationProjection {
  id: string
  serverId: string
  taskId: string
  runId: string
  sessionId: string
  ownerId: string
  requestRevision: number
  schemaDigest: string
  title: string
  message: string
  requestedAt: string
  expiresAt?: string
  state: "pending" | "permission-pending" | "submitted" | "accepted" | "declined" | "cancelled" | "expired" | "failed"
  fieldNames: readonly string[]
  requiredFields: readonly string[]
  sensitiveFields: readonly string[]
  permissionId?: string
  commandId?: string
  responseDigest?: string
  settledAt?: string
  settledEventId?: string
  failureCode?: string
}

export interface McpServerProjection {
  id: string
  name: string
  title: string
  taskId: string
  runId: string
  sessionId: string
  ownerId: string
  ownerRevision: number
  revision: number
  sequence: number
  connectionEpoch: number
  state: McpConnectionState
  enabled: boolean
  mutable: boolean
  disabledReason?: string
  lastErrorCode?: string
  lastErrorMessage?: string
  lastConnectedAt?: string
  lastDisconnectedAt?: string
  lastHeartbeatAt?: string
  retryAttempt: number
  retryAt?: string
  retryAfterMs?: number
  reconnectEligible: boolean
  config: McpConfigProvenance
  auth: McpAuthProjection
  capabilities: McpCapabilitySummary
  tools: readonly McpCatalogEntry[]
  resources: readonly McpCatalogEntry[]
  prompts: readonly McpCatalogEntry[]
  elicitations: readonly McpElicitationProjection[]
  eventIds: readonly string[]
  warnings: readonly string[]
  fingerprint: string
}

export interface McpRejectedProjection {
  subject: string
  code: string
  reason: string
  eventId?: string
  entityId?: string
}

export interface McpConsoleProjection {
  authority: McpProjectionAuthority
  servers: readonly McpServerProjection[]
  byId: Readonly<Record<string, McpServerProjection>>
  selectedId?: string
  selected?: McpServerProjection
  pendingElicitations: number
  needsAuth: number
  connectedServers: number
  rejected: readonly McpRejectedProjection[]
  warnings: readonly string[]
  fingerprint: string
}

export interface McpCatalogCursor {
  version: 1
  taskId: string
  runId: string
  sessionId: string
  serverId: string
  kind: McpCatalogKind
  capabilityRevision: number
  projectionRevision: number
  queryDigest: string
  offset: number
  issuedAtMs: number
  checksum: string
}

export interface McpCatalogPage {
  serverId: string
  kind: McpCatalogKind
  capabilityRevision: number
  projectionRevision: number
  items: readonly McpCatalogEntry[]
  total: number
  offset: number
  limit: number
  hasNext: boolean
  nextCursor?: string
  query: string
}

export type McpControlAction =
  | "enable"
  | "disable"
  | "reconnect"
  | "refresh"
  | "auth-refresh"
  | "elicit"

export type McpControlPhase =
  | "submitting"
  | "queued"
  | "permission-pending"
  | "running"
  | "reconciling"
  | "committed"
  | "failed"
  | "quarantined"
  | "disconnected"
  | "disabled"
  | "sealed-denied"

export interface McpEffectBaseline {
  id: string
  action: McpControlAction
  taskId: string
  runId: string
  sessionId: string
  serverId: string
  ownerId: string
  projectionRevision: number
  ownerRevision: number
  serverRevision: number
  configRevision: number
  authRevision: number
  capabilityRevision: number
  connectionEpoch: number
  state: McpConnectionState
  enabled: boolean
  expiresAt?: string
  catalogFingerprint: string
  elicitationId?: string
  elicitationState?: string
}

export interface McpEffectVerification {
  satisfied: boolean
  terminal: boolean
  quarantined: boolean
  code: string
  message: string
  observedRevision: number
  changed: readonly string[]
  evidenceEventIds: readonly string[]
}

export interface McpControlOperation {
  id: string
  nonce: string
  idempotencyKey: string
  action: McpControlAction
  phase: McpControlPhase
  taskId: string
  runId: string
  sessionId: string
  serverId: string
  ownerId: string
  expectedOwnerRevision: number
  expectedProjectionRevision: number
  commandText: string
  startedAt: string
  updatedAt: string
  baseline: McpEffectBaseline
  receipt?: CommandReceipt
  requestId?: string
  commandId?: string
  permissionId?: string
  elicitationId?: string
  responseDigest?: string
  error?: string
  eventIds: readonly string[]
  verification?: McpEffectVerification
}

export interface McpReconnectPlan {
  serverId: string
  ownerId: string
  connectionEpoch: number
  capabilityRevision: number
  attempt: number
  createdAtMs: number
  delayMs: number
  readyAtMs: number
  maximumAttempts: number
  reason: string
  state: "scheduled" | "ready" | "cancelled" | "exhausted"
}

export interface McpControllerSnapshot {
  taskId?: string
  runId?: string
  sessionId?: string
  selectedServerId?: string
  connected: boolean
  enabled: boolean
  sealed: boolean
  closed: boolean
  disabledReason?: string
  projection?: McpConsoleProjection
  operations: readonly McpControlOperation[]
  activeOperation?: McpControlOperation
  reconnectPlans: readonly McpReconnectPlan[]
  lastReceipt?: CommandReceipt
  error?: string
  revision: number
}
