export const COMMAND_PROTOCOL = "zyra.control/v1" as const
export const COMMAND_QUEUE_PROTOCOL = "zyra.command-queue/v1" as const
export const COMMAND_RECEIPT_PROTOCOL = "zyra.command-receipt/v1" as const

export type CommandName =
  | "/status"
  | "/graph"
  | "/trace"
  | "/artifacts"
  | "/permissions"
  | "/context"
  | "/compact"
  | "/memory"
  | "/model"
  | "/resume"
  | "/rewind"
  | "/export"
  | "/btw"
  | "/inject"
  | "/change"
  | "/verify"
  | "/eval"
  | "/doctor"

export type CommandCategory =
  | "observability"
  | "artifact"
  | "session"
  | "memory"
  | "provider"
  | "permission"
  | "side-question"
  | "intervention"
  | "verification"
  | "health"

export type CommandDeliveryMode = "enqueue" | "steer" | "interrupt"
export type CommandPriority = "now" | "next" | "later"
export type CommandMutation = "read-only" | "session" | "permission" | "task-graph"
export type CommandAvailability = "enabled" | "disabled" | "active-task" | "terminal-task"
export type CommandReceiptPhase =
  | "received"
  | "validated"
  | "queued"
  | "running"
  | "applied"
  | "rejected"
  | "expired"
  | "cancelled"
export type CommandQueuePhase =
  | "queued"
  | "reserved"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "expired"
export type ArgumentKind =
  | "string"
  | "integer"
  | "boolean"
  | "enum"
  | "json"
  | "duration"
  | "identity"

export interface CommandArgumentSpec {
  name: string
  kind: ArgumentKind
  required: boolean
  variadic?: boolean
  flag?: string
  aliases?: readonly string[]
  choices?: readonly string[]
  minimum?: number
  maximum?: number
  maximumBytes?: number
  description: string
  placeholder?: string
}

export interface CommandDescriptor {
  id: string
  name: CommandName
  aliases: readonly string[]
  title: string
  description: string
  category: CommandCategory
  mutation: CommandMutation
  availability: CommandAvailability
  queueable: boolean
  remoteSafe: boolean
  immediate: boolean
  sealedAllowed: boolean
  overlay: "status" | "graph" | "trace" | "artifacts" | "permissions" | "btw" | "doctor" | "receipt"
  arguments: readonly CommandArgumentSpec[]
  keywords: readonly string[]
}

export interface CommandTaskContext {
  taskId?: string
  runId?: string
  sessionId?: string
  taskStatus?: string
  active: boolean
  terminal: boolean
  transportEnabled: boolean
  sealed: boolean
  remote: boolean
  expectedRevision?: number
}

export interface CommandToken {
  value: string
  raw: string
  start: number
  end: number
  quoted: boolean
  quote?: "'" | '"'
}

export interface CommandParseError {
  code:
    | "empty"
    | "not-command"
    | "unknown-command"
    | "unclosed-quote"
    | "invalid-escape"
    | "invalid-flag"
    | "duplicate-flag"
    | "missing-value"
    | "unexpected-argument"
    | "invalid-value"
    | "input-too-large"
  message: string
  start: number
  end: number
  argument?: string
}

export interface BoundCommandArguments {
  values: Readonly<Record<string, unknown>>
  positional: readonly string[]
  flags: Readonly<Record<string, string | boolean>>
  errors: readonly CommandParseError[]
  hints: readonly string[]
}

export interface ParsedCommand {
  raw: string
  normalized: string
  trigger: string
  descriptor?: CommandDescriptor
  tokens: readonly CommandToken[]
  arguments: BoundCommandArguments
  errors: readonly CommandParseError[]
}

export interface CommandCompletion {
  kind: "command" | "argument" | "flag" | "value"
  query: string
  replaceStart: number
  replaceEnd: number
  argumentIndex: number
  argumentName?: string
}

export interface CommandSuggestion {
  id: string
  descriptor: CommandDescriptor
  label: string
  description: string
  category: CommandCategory
  matched: readonly [number, number][]
  score: number
  disabled: boolean
  disabledReason?: string
}

export interface ArgumentSuggestion {
  id: string
  value: string
  label: string
  description: string
  kind: "choice" | "flag" | "identity" | "history"
  matched: readonly [number, number][]
  disabled?: boolean
}

export interface CommandAvailabilityDecision {
  allowed: boolean
  reason?: string
  code:
    | "allow"
    | "disabled"
    | "transport-unavailable"
    | "task-required"
    | "active-task-required"
    | "terminal-task-required"
    | "remote-unsafe"
    | "sealed-forbidden"
}

export interface CommandPolicyDecision {
  allowed: boolean
  queue: boolean
  priority: CommandPriority
  mode: CommandDeliveryMode
  code:
    | "allow"
    | "invalid"
    | "unavailable"
    | "busy-conflict"
    | "interrupt-unsafe"
    | "disabled"
  reason: string
}

export interface CommandRequestIdentity {
  requestId: string
  commandId: string
  idempotencyKey: string
  fingerprint: string
}

export interface CommandTransportRequest {
  taskId: string
  runId: string
  sessionId: string
  text: string
  arguments: Readonly<Record<string, unknown>>
  requestId: string
  commandId: string
  idempotencyKey: string
  actorId: string
  expectedRevision?: number
  sealed: boolean
  priority: CommandPriority
  deliveryMode: CommandDeliveryMode
  retryOf?: string
  signal?: AbortSignal
  timeoutMs?: number
}

export interface CommandErrorReceipt {
  code: string
  message: string
  retryable: boolean
  details: Readonly<Record<string, unknown>>
}

export interface CommandUsageReceipt {
  inputTokens: number
  outputTokens: number
  cachedTokens: number
  costUsd?: number
  durationMs?: number
}

export interface CommandReceipt {
  schema: typeof COMMAND_RECEIPT_PROTOCOL
  requestId: string
  commandId: string
  queueId?: string
  taskId: string
  runId: string
  sessionId?: string
  name: CommandName
  scope: string
  mode: CommandDeliveryMode
  priority: CommandPriority
  idempotencyKey: string
  retryOf?: string
  phase: CommandReceiptPhase
  summary: string
  displayText: string
  data: Readonly<Record<string, unknown>>
  error?: CommandErrorReceipt
  usage?: CommandUsageReceipt
  eventIds: readonly string[]
  checkpointRef?: string
  revisionBefore?: number
  revisionAfter?: number
  replayed: boolean
  durable: boolean
  executed: boolean
  interventionCounted: boolean
  humanInterventionCount: number
  operatorInterventionAttemptCount: number
  createdAt: string
  finishedAt?: string
  raw: Readonly<Record<string, unknown>>
}

export interface CommandQueueItem {
  queueId: string
  requestId?: string
  commandId?: string
  taskId?: string
  runId?: string
  sessionId: string
  name?: CommandName
  text?: string
  phase: CommandQueuePhase
  priority: CommandPriority
  sequence: number
  position: number
  idempotencyKey?: string
  retryOf?: string
  createdAt: string
  updatedAt: string
  claimExpiresAt?: string
  error?: string
  cancellable: boolean
  retryable: boolean
  terminal: boolean
  source: "backend-queue" | "canonical-projection" | "receipt"
}

export interface CommandQueueSnapshot {
  schema: typeof COMMAND_QUEUE_PROTOCOL
  taskId: string
  sessionId?: string
  sequence: number
  revision: number
  restored: boolean
  items: readonly CommandQueueItem[]
  pending: readonly CommandQueueItem[]
  running: readonly CommandQueueItem[]
  settled: readonly CommandQueueItem[]
}

export interface CommandCorrelation {
  commandId: string
  requestId?: string
  queueId?: string
  eventIds: readonly string[]
  spanIds: readonly string[]
  mutationIds: readonly string[]
  checkpointIds: readonly string[]
  artifactIds: readonly string[]
  toolCallIds: readonly string[]
  failureIds: readonly string[]
  recoveryIds: readonly string[]
  complete: boolean
  missing: readonly string[]
}

export interface CommandCoordinatorRecord {
  operationId: string
  identity: CommandRequestIdentity
  descriptor: CommandDescriptor
  parsed: ParsedCommand
  context: CommandTaskContext
  mode: CommandDeliveryMode
  priority: CommandPriority
  phase: CommandReceiptPhase
  createdAt: number
  updatedAt: number
  attempt: number
  retryOf?: string
  receipt?: CommandReceipt
  error?: string
}

export interface CommandCoordinatorSnapshot {
  enabled: boolean
  revision: number
  busy: boolean
  active?: CommandCoordinatorRecord
  records: readonly CommandCoordinatorRecord[]
  lastReceipt?: CommandReceipt
  lastError?: string
}

export interface CommandTransport {
  submit(request: CommandTransportRequest): Promise<unknown>
  queue(input: {
    taskId: string
    sessionId?: string
    includeTerminal?: boolean
    signal?: AbortSignal
  }): Promise<unknown>
  cancel(input: {
    taskId: string
    requestId: string
    reason: string
    idempotencyKey: string
    signal?: AbortSignal
  }): Promise<unknown>
}
