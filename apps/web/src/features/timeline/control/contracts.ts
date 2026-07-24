import type { TimelineRow, WorkerCausalTimelineProjection } from "../projection/index.ts"

export const RecoveryControlAction = Object.freeze({
  KILL: "kill",
  STEER: "steer",
  RETRY: "retry",
  REASSIGN: "reassign",
  RESUME: "resume",
} as const)

export type RecoveryControlActionValue =
  (typeof RecoveryControlAction)[keyof typeof RecoveryControlAction]

export const RecoveryControlPhase = Object.freeze({
  VALIDATING: "validating",
  SUBMITTING: "submitting",
  PENDING: "pending",
  DENIED: "denied",
  APPLIED: "applied",
  FAILED: "failed",
  TIMED_OUT: "timed-out",
  SUPERSEDED: "superseded",
  CANCELLED: "cancelled",
} as const)

export type RecoveryControlPhaseValue =
  (typeof RecoveryControlPhase)[keyof typeof RecoveryControlPhase]

export const RecoveryControlConnection = Object.freeze({
  ONLINE: "online",
  DEGRADED: "degraded",
  OFFLINE: "offline",
  RECONNECTING: "reconnecting",
} as const)

export type RecoveryControlConnectionValue =
  (typeof RecoveryControlConnection)[keyof typeof RecoveryControlConnection]

export type RecoveryControlTargetKind =
  | "task"
  | "worker"
  | "node"
  | "recovery"
  | "checkpoint"
  | "session"

export interface RecoveryControlOwnerExpectation {
  taskId: string
  runId: string
  sessionId?: string
  workerId?: string
  leaseId?: string
  attemptId?: string
  nodeId?: string
  graphId?: string
  checkpointId?: string
  ownerRevision?: number
  graphRevision?: number
  sessionRevision?: number
}

export interface RecoveryControlRequest {
  action: RecoveryControlActionValue
  taskId: string
  runId: string
  actorId: string
  reason: string
  instruction?: string
  targetKind?: RecoveryControlTargetKind
  targetId?: string
  sealed: boolean
  timeoutMs?: number
  maximumAttempts?: number
  owner: RecoveryControlOwnerExpectation
  metadata?: Readonly<Record<string, string | number | boolean>>
}

export interface NormalizedRecoveryControlRequest
  extends RecoveryControlRequest {
  targetKind: RecoveryControlTargetKind
  requestId: string
  commandId: string
  commandName: string
  commandText: string
  commandArguments: Readonly<Record<string, unknown>>
  idempotencyKey: string
  timeoutMs: number
  normalizedAt: number
  requestDigest: string
}

export interface RecoveryControlTransportInput {
  taskId: string
  runId: string
  text: string
  arguments: Readonly<Record<string, unknown>>
  requestId: string
  commandId: string
  idempotencyKey: string
  actorId: string
  sessionId?: string
  sealed: boolean
  expectedRevision?: number
  timeoutMs: number
  signal?: AbortSignal
}

export interface RecoveryControlTransport {
  submit(
    input: RecoveryControlTransportInput,
  ): Promise<Readonly<Record<string, unknown>>>
}

export interface RecoveryControlOwnerEvidence {
  owner: string
  operation?: string
  receiptId?: string
  accepted?: boolean
  changed?: boolean
  workerId?: string
  leaseId?: string
  attemptId?: string
  nodeId?: string
  graphId?: string
  checkpointId?: string
  revision?: number
  before?: Readonly<Record<string, unknown>>
  after?: Readonly<Record<string, unknown>>
  raw: Readonly<Record<string, unknown>>
}

export interface RecoveryControlObservation {
  phase: RecoveryControlPhaseValue
  observedAt: number
  observedRevision?: number
  eventIds: readonly string[]
  rowKeys: readonly string[]
  ownerEvidence: readonly RecoveryControlOwnerEvidence[]
  summary: string
  source:
    | "response"
    | "canonical-event"
    | "projection"
    | "timeout"
    | "transport"
    | "sealed-policy"
  terminal: boolean
  consistent: boolean
  conflictReason?: string
}

export interface RecoveryControlReceipt {
  id: string
  action: RecoveryControlActionValue
  phase: RecoveryControlPhaseValue
  taskId: string
  runId: string
  actorId: string
  targetKind: RecoveryControlTargetKind
  targetId?: string
  requestId: string
  commandId: string
  commandName: string
  commandText: string
  requestDigest: string
  idempotencyKey: string
  expectedOwner: RecoveryControlOwnerExpectation
  submittedAt: number
  updatedAt: number
  deadlineAt: number
  attempt: number
  summary: string
  denied: boolean
  sealed: boolean
  interventionCounted: boolean
  operatorInterventionAttemptCount?: number
  humanInterventionCount?: number
  noHumanWait: boolean
  replayed: boolean
  timedOut: boolean
  detached: boolean
  errorCode?: string
  errorMessage?: string
  observedRevision?: number
  observedEventIds: readonly string[]
  observedRowKeys: readonly string[]
  ownerEvidence: readonly RecoveryControlOwnerEvidence[]
  observations: readonly RecoveryControlObservation[]
  response?: Readonly<Record<string, unknown>>
  metadata: Readonly<Record<string, string | number | boolean>>
}

export interface RecoveryControlLedgerSnapshot {
  revision: number
  receipts: readonly RecoveryControlReceipt[]
  inFlight: readonly RecoveryControlReceipt[]
  terminal: readonly RecoveryControlReceipt[]
  deniedCount: number
  failedCount: number
  timedOutCount: number
  pendingCount: number
  latest?: RecoveryControlReceipt
}

export interface RecoveryControlRuntimeSnapshot {
  taskId: string
  revision: number
  closed: boolean
  detached: boolean
  disabled: boolean
  connection: RecoveryControlConnectionValue
  connectionReason?: string
  disconnectedAt?: number
  reconnectedAt?: number
  ledger: RecoveryControlLedgerSnapshot
  announcements: readonly string[]
}

export interface RecoveryControlRuntimeOptions {
  taskId: string
  runId: string
  transport: RecoveryControlTransport
  disabled?: boolean
  now?: () => number
  requestId?: () => string
  commandId?: () => string
  defaultTimeoutMs?: number
  maximumTimeoutMs?: number
  maximumReceipts?: number
  maximumObservations?: number
  detachOnClose?: boolean
}

export interface RecoveryControlSubmission {
  request: NormalizedRecoveryControlRequest
  receipt: RecoveryControlReceipt
  completion: Promise<RecoveryControlReceipt>
}

export interface RecoveryControlProjectionContext {
  projection: WorkerCausalTimelineProjection
  rows: readonly TimelineRow[]
  now?: number
}

export interface RecoveryControlRaceResult {
  accepted: RecoveryControlReceipt
  superseded: readonly RecoveryControlReceipt[]
  pending: readonly RecoveryControlReceipt[]
}

export interface RecoveryControlAudit {
  disabled: boolean
  closed: boolean
  detached: boolean
  submitted: number
  applied: number
  denied: number
  failed: number
  timedOut: number
  replayed: number
  staleObservations: number
  conflictingObservations: number
  detachedCompletions: number
  lastRequestId?: string
  lastCommandId?: string
  lastError?: string
}

export class RecoveryControlError extends Error {
  readonly code: string
  readonly retryable: boolean
  readonly details: Readonly<Record<string, string | number | boolean>>

  constructor(
    code: string,
    message: string,
    options: {
      retryable?: boolean
      details?: Readonly<Record<string, string | number | boolean>>
    } = {},
  ) {
    super(message)
    this.name = "RecoveryControlError"
    this.code = code
    this.retryable = options.retryable ?? false
    this.details = Object.freeze({ ...(options.details ?? {}) })
  }
}

export function terminalControlPhase(
  phase: RecoveryControlPhaseValue,
): boolean {
  return (
    phase === RecoveryControlPhase.DENIED ||
    phase === RecoveryControlPhase.APPLIED ||
    phase === RecoveryControlPhase.FAILED ||
    phase === RecoveryControlPhase.SUPERSEDED ||
    phase === RecoveryControlPhase.CANCELLED
  )
}

export function activeControlPhase(
  phase: RecoveryControlPhaseValue,
): boolean {
  return (
    phase === RecoveryControlPhase.VALIDATING ||
    phase === RecoveryControlPhase.SUBMITTING ||
    phase === RecoveryControlPhase.PENDING ||
    phase === RecoveryControlPhase.TIMED_OUT
  )
}
