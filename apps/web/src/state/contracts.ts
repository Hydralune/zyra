import type {
  ConnectionSnapshot,
  IngressBatch,
  IngressEvent,
  JsonObject,
  JsonValue,
} from "../events/ingress/index.ts"

export const PROJECTION_SCHEMA = "zyra.ui-projection/v1" as const
export const PROJECTION_SNAPSHOT_SCHEMA = "zyra.ui-projection-snapshot/v1" as const
export const PROJECTION_STORAGE_PREFIX = "zyra.ui-projection"

export const ProjectionDomain = {
  TASK: "task",
  NODE: "node",
  WORKER: "worker",
  TOOL: "tool",
  ARTIFACT: "artifact",
  MEMORY: "memory",
  SCHEDULER: "scheduler",
  RECOVERY: "recovery",
  COMMAND: "command",
  PERMISSION: "permission",
  OVERLAY: "overlay",
  SESSION: "session",
  EVENT: "event",
} as const

export type ProjectionDomainValue =
  (typeof ProjectionDomain)[keyof typeof ProjectionDomain]

export const ProjectionStatus = {
  OPTIMISTIC: "optimistic",
  PARTIAL: "partial",
  AUTHORITATIVE: "authoritative",
  FINAL: "final",
  TOMBSTONED: "tombstoned",
  ORPHANED: "orphaned",
  STALE: "stale",
} as const

export type ProjectionStatusValue =
  (typeof ProjectionStatus)[keyof typeof ProjectionStatus]

export const EntityLifecycle = {
  UNKNOWN: "unknown",
  QUEUED: "queued",
  ADMITTED: "admitted",
  STARTING: "starting",
  RUNNING: "running",
  WAITING_TOOL: "waiting_tool",
  WAITING_POLICY: "waiting_policy",
  PAUSED: "paused",
  RECOVERING: "recovering",
  COMPLETED: "completed",
  FAILED: "failed",
  CANCELLED: "cancelled",
  REJECTED: "rejected",
  EXPIRED: "expired",
  DELETED: "deleted",
} as const

export type EntityLifecycleValue =
  (typeof EntityLifecycle)[keyof typeof EntityLifecycle]

export interface ProjectionEntity {
  id: string
  domain: ProjectionDomainValue
  taskId: string
  runId: string
  sessionId?: string
  nodeId?: string
  workerId?: string
  lifecycle: EntityLifecycleValue
  status: ProjectionStatusValue
  revision: number
  sequence: number
  aggregateSequence: number
  createdAt: string
  updatedAt: string
  firstEventId: string
  lastEventId: string
  correlationId: string
  causationId?: string
  spanId?: string
  parentSpanId?: string
  toolCallId?: string
  artifactIds: readonly string[]
  checkpointId?: string
  controlCommandId?: string
  title: string
  summary: string
  progress?: number
  terminal: boolean
  effective: boolean
  optimisticKey?: string
  parentId?: string
  attributes: Readonly<JsonObject>
  metadata: Readonly<JsonObject>
}

export interface TaskProjection extends ProjectionEntity {
  domain: "task"
  activeNodeIds: readonly string[]
  workerIds: readonly string[]
  sessionIds: readonly string[]
  artifactIds: readonly string[]
  pendingPermissionIds: readonly string[]
  commandIds: readonly string[]
  recoveryIds: readonly string[]
  lastCheckpointId?: string
}

export interface NodeProjection extends ProjectionEntity {
  domain: "node"
  role?: string
  capabilityRefs: readonly string[]
  dependencyIds: readonly string[]
  childNodeIds: readonly string[]
  placementId?: string
  routeId?: string
}

export interface WorkerProjection extends ProjectionEntity {
  domain: "worker"
  role?: string
  capabilityRefs: readonly string[]
  leaseId?: string
  routeId?: string
  placementId?: string
  activeToolCallId?: string
  health?: string
}

export interface ToolProjection extends ProjectionEntity {
  domain: "tool"
  toolName?: string
  inputDigest?: string
  resultDigest?: string
  resultArtifactIds: readonly string[]
  permissionId?: string
  durationMs?: number
  errorCode?: string
}

export interface ArtifactProjection extends ProjectionEntity {
  domain: "artifact"
  digest: string
  mediaType: string
  sizeBytes: number
  uri?: string
  producerEventId: string
  producerToolCallId?: string
  version: number
  deleted: boolean
}

export interface MemoryProjection extends ProjectionEntity {
  domain: "memory"
  memoryKind?: string
  namespace?: string
  key?: string
  score?: number
  tokenCount?: number
  sourceArtifactIds: readonly string[]
}

export interface SchedulerProjection extends ProjectionEntity {
  domain: "scheduler"
  routeId?: string
  placementId?: string
  modelId?: string
  providerId?: string
  resourceClass?: string
  privacyClass?: string
  costEstimate?: number
  latencyEstimateMs?: number
  candidateIds: readonly string[]
}

export interface RecoveryProjection extends ProjectionEntity {
  domain: "recovery"
  failureId?: string
  planId?: string
  attempt: number
  strategy?: string
  previousWorkerId?: string
  replacementWorkerId?: string
  resumedCheckpointId?: string
  reason?: string
}

export interface CommandProjection extends ProjectionEntity {
  domain: "command"
  commandName?: string
  mode?: string
  scope?: string
  priority: "now" | "next" | "later"
  queuePosition?: number
  argsDigest?: string
  receiptId?: string
  errorCode?: string
}

export interface PermissionProjection extends ProjectionEntity {
  domain: "permission"
  requestId: string
  permissionKind?: string
  toolName?: string
  argsDigest?: string
  policyRevision?: string
  ownerId?: string
  decision?: "allow" | "deny" | "ask"
  expiresAt?: string
  resolvedAt?: string
  reason?: string
}

export interface OverlayProjection extends ProjectionEntity {
  domain: "overlay"
  overlayKind?: string
  modal: boolean
  commandId?: string
  permissionId?: string
  openedAt: string
  closedAt?: string
}

export interface SessionProjection extends ProjectionEntity {
  domain: "session"
  parentSessionId?: string
  contextTokens?: number
  contextLimit?: number
  compactCount: number
  restoredCheckpointId?: string
  childSessionIds: readonly string[]
}

export type DomainProjection =
  | TaskProjection
  | NodeProjection
  | WorkerProjection
  | ToolProjection
  | ArtifactProjection
  | MemoryProjection
  | SchedulerProjection
  | RecoveryProjection
  | CommandProjection
  | PermissionProjection
  | OverlayProjection
  | SessionProjection

export type ProjectionTable<T extends ProjectionEntity = ProjectionEntity> =
  Readonly<Record<string, T>>

export interface TombstoneRecord {
  identity: string
  domain: ProjectionDomainValue
  taskId: string
  targetId: string
  eventId: string
  sequence: number
  generation: number
  observedAtMs: number
  expiresAtMs: number
  reason: string
}

export interface OrphanRecord {
  identity: string
  taskId: string
  parentId: string
  eventIds: readonly string[]
  firstSequence: number
  lastSequence: number
  observedAtMs: number
  expiresAtMs: number
}

export interface OptimisticRecord {
  key: string
  taskId: string
  domain: ProjectionDomainValue
  entityId: string
  eventId: string
  createdAtMs: number
  expiresAtMs: number
  confirmedEventId?: string
}

export interface PartialRecord {
  key: string
  taskId: string
  entityId: string
  eventIds: readonly string[]
  parts: Readonly<Record<string, JsonValue>>
  firstSequence: number
  lastSequence: number
  updatedAtMs: number
  expiresAtMs: number
}

export interface MutationProjection {
  id: string
  taskId: string
  eventId: string
  domain: string
  operation: string
  path: readonly string[]
  beforeDigest?: string
  afterDigest?: string
  effective: boolean
  sequence: number
  createdAt: string
  entityRefs: readonly string[]
}

export interface CausalEventProjection {
  eventId: string
  eventType: string
  taskId: string
  runId: string
  sessionId?: string
  nodeId?: string
  workerId?: string
  spanId?: string
  parentSpanId?: string
  toolCallId?: string
  artifactIds: readonly string[]
  checkpointId?: string
  controlCommandId?: string
  correlationId: string
  causationId?: string
  mutationId: string
  failureId?: string
  recoveryId?: string
  sequence: number
  aggregateSequence: number
  createdAt: string
  committedAt: string
  summary: string
  terminal: boolean
  effective: boolean
  entityRefs: readonly string[]
}

export interface CausalIndex {
  byEvent: Readonly<Record<string, CausalEventProjection>>
  byCorrelation: Readonly<Record<string, readonly string[]>>
  byCausation: Readonly<Record<string, readonly string[]>>
  bySpan: Readonly<Record<string, readonly string[]>>
  byParentSpan: Readonly<Record<string, readonly string[]>>
  byToolCall: Readonly<Record<string, readonly string[]>>
  byArtifact: Readonly<Record<string, readonly string[]>>
  byCheckpoint: Readonly<Record<string, readonly string[]>>
  byControlCommand: Readonly<Record<string, readonly string[]>>
  byMutation: Readonly<Record<string, readonly string[]>>
  byFailure: Readonly<Record<string, readonly string[]>>
  byRecovery: Readonly<Record<string, readonly string[]>>
  byTask: Readonly<Record<string, readonly string[]>>
  byRun: Readonly<Record<string, readonly string[]>>
  bySession: Readonly<Record<string, readonly string[]>>
  byNode: Readonly<Record<string, readonly string[]>>
  byWorker: Readonly<Record<string, readonly string[]>>
  eventOrder: readonly string[]
}

export interface ProjectionCursor {
  taskId: string
  generation: number
  cursor?: string
  committedSequence: number
  highWatermark: number
  snapshotComplete: boolean
  lastEventId?: string
  committedAtMs: number
}

export interface ProjectionTaskRuntime {
  taskId: string
  generation: number
  firstSequence: number
  lastSequence: number
  eventCount: number
  duplicateCount: number
  staleCount: number
  orphanCount: number
  tombstoneCount: number
  lastEventAtMs: number
  pinned: boolean
  connected: boolean
}

export interface ProjectionDiagnostics {
  appliedTransactions: number
  rejectedTransactions: number
  duplicateEvents: number
  staleEvents: number
  migratedSnapshots: number
  rejectedSnapshots: number
  retentionRuns: number
  evictedEvents: number
  evictedEntities: number
  selectorNotifications: number
  persistedSnapshots: number
  persistenceFailures: number
  lastError?: {
    code: string
    message: string
    atMs: number
    taskId?: string
    eventId?: string
  }
}

export interface CanonicalProjectionState {
  schema: typeof PROJECTION_SCHEMA
  revision: number
  committedAtMs: number
  tasks: ProjectionTable<TaskProjection>
  nodes: ProjectionTable<NodeProjection>
  workers: ProjectionTable<WorkerProjection>
  tools: ProjectionTable<ToolProjection>
  artifacts: ProjectionTable<ArtifactProjection>
  memories: ProjectionTable<MemoryProjection>
  schedulers: ProjectionTable<SchedulerProjection>
  recoveries: ProjectionTable<RecoveryProjection>
  commands: ProjectionTable<CommandProjection>
  permissions: ProjectionTable<PermissionProjection>
  overlays: ProjectionTable<OverlayProjection>
  sessions: ProjectionTable<SessionProjection>
  mutations: Readonly<Record<string, MutationProjection>>
  tombstones: Readonly<Record<string, TombstoneRecord>>
  orphans: Readonly<Record<string, OrphanRecord>>
  optimistic: Readonly<Record<string, OptimisticRecord>>
  partials: Readonly<Record<string, PartialRecord>>
  causality: CausalIndex
  cursors: Readonly<Record<string, ProjectionCursor>>
  runtimes: Readonly<Record<string, ProjectionTaskRuntime>>
  diagnostics: ProjectionDiagnostics
}

export interface MutableProjectionState {
  schema: typeof PROJECTION_SCHEMA
  revision: number
  committedAtMs: number
  tasks: Record<string, TaskProjection>
  nodes: Record<string, NodeProjection>
  workers: Record<string, WorkerProjection>
  tools: Record<string, ToolProjection>
  artifacts: Record<string, ArtifactProjection>
  memories: Record<string, MemoryProjection>
  schedulers: Record<string, SchedulerProjection>
  recoveries: Record<string, RecoveryProjection>
  commands: Record<string, CommandProjection>
  permissions: Record<string, PermissionProjection>
  overlays: Record<string, OverlayProjection>
  sessions: Record<string, SessionProjection>
  mutations: Record<string, MutationProjection>
  tombstones: Record<string, TombstoneRecord>
  orphans: Record<string, OrphanRecord>
  optimistic: Record<string, OptimisticRecord>
  partials: Record<string, PartialRecord>
  causality: {
    byEvent: Record<string, CausalEventProjection>
    byCorrelation: Record<string, string[]>
    byCausation: Record<string, string[]>
    bySpan: Record<string, string[]>
    byParentSpan: Record<string, string[]>
    byToolCall: Record<string, string[]>
    byArtifact: Record<string, string[]>
    byCheckpoint: Record<string, string[]>
    byControlCommand: Record<string, string[]>
    byMutation: Record<string, string[]>
    byFailure: Record<string, string[]>
    byRecovery: Record<string, string[]>
    byTask: Record<string, string[]>
    byRun: Record<string, string[]>
    bySession: Record<string, string[]>
    byNode: Record<string, string[]>
    byWorker: Record<string, string[]>
    eventOrder: string[]
  }
  cursors: Record<string, ProjectionCursor>
  runtimes: Record<string, ProjectionTaskRuntime>
  diagnostics: ProjectionDiagnostics
}

export interface ProjectionLimits {
  maxEvents: number
  maxEventsPerTask: number
  maxEntitiesPerDomain: number
  maxInactiveTasks: number
  maxTombstones: number
  maxOrphans: number
  maxOptimistic: number
  maxPartials: number
  maxMutations: number
  maxIndexEntries: number
  maxIndexValues: number
  tombstoneTtlMs: number
  orphanTtlMs: number
  optimisticTtlMs: number
  partialTtlMs: number
  inactiveTaskTtlMs: number
  persistenceDebounceMs: number
}

export interface ProjectionChangeSet {
  revision: number
  taskIds: ReadonlySet<string>
  domains: ReadonlySet<ProjectionDomainValue>
  entityKeys: ReadonlySet<string>
  eventIds: ReadonlySet<string>
  causalKeys: ReadonlySet<string>
  cursorTaskIds: ReadonlySet<string>
  diagnostic: boolean
}

export interface ProjectionTransactionResult {
  state: CanonicalProjectionState
  changes: ProjectionChangeSet
  appliedEventIds: readonly string[]
  duplicateEventIds: readonly string[]
  staleEventIds: readonly string[]
  orphanEventIds: readonly string[]
  tombstonedEntityIds: readonly string[]
}

export interface ProjectionRestoreResult {
  state: CanonicalProjectionState
  migratedFrom?: number
  taskIds: readonly string[]
  cursorByTask: Readonly<Record<string, ProjectionCursor>>
}

export interface ProjectionSnapshotEnvelope {
  schema: typeof PROJECTION_SNAPSHOT_SCHEMA
  version: number
  storeId: string
  createdAtMs: number
  revision: number
  checksum: string
  state: JsonObject
}

export interface ProjectionPersistence {
  load(storeId: string): Promise<string | undefined>
  save(storeId: string, value: string): Promise<void>
  remove(storeId: string): Promise<void>
  keys?(): Promise<readonly string[]>
}

export interface ProjectionSelector<T> {
  key: string
  dependencies: readonly string[]
  select(state: CanonicalProjectionState): T
  equals?(left: T, right: T): boolean
}

export interface ProjectionSubscription<T> {
  readonly id: number
  readonly selector: ProjectionSelector<T>
  readonly value: T
  close(): void
}

export interface ProjectionStoreOptions {
  id?: string
  limits?: Partial<ProjectionLimits>
  persistence?: ProjectionPersistence
  now?: () => number
  autoPersist?: boolean
  restore?: boolean
  disabled?: boolean
}

export interface ProjectionIngressBinding {
  taskId: string
  close(): void
  connection(): ConnectionSnapshot | undefined
}

export interface ProjectionStoreAudit {
  storeId: string
  revision: number
  closed: boolean
  disabled: boolean
  restored: boolean
  bindings: readonly string[]
  persistedRevision: number
  scheduledPersistence: boolean
  state: {
    tasks: number
    nodes: number
    workers: number
    tools: number
    artifacts: number
    memories: number
    schedulers: number
    recoveries: number
    commands: number
    permissions: number
    overlays: number
    sessions: number
    events: number
    mutations: number
    tombstones: number
    orphans: number
    optimistic: number
    partials: number
  }
}

export interface ApplyBatchOptions {
  expectedRevision?: number
  source?: "ingress" | "restore" | "optimistic" | "test"
  connection?: ConnectionSnapshot
}

export interface ProjectionEventContext {
  event: IngressEvent
  batch: IngressBatch
  now: number
  state: MutableProjectionState
  changes: {
    taskIds: Set<string>
    domains: Set<ProjectionDomainValue>
    entityKeys: Set<string>
    eventIds: Set<string>
    causalKeys: Set<string>
    cursorTaskIds: Set<string>
    diagnostic: boolean
  }
}

export class ProjectionError extends Error {
  readonly code: string
  readonly details: Readonly<Record<string, JsonValue>>

  constructor(
    code: string,
    message: string,
    details: Record<string, JsonValue> = {},
  ) {
    super(message)
    this.name = "ProjectionError"
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}
