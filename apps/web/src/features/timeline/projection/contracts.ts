import type {
  ArtifactProjection,
  CanonicalProjectionState,
  CausalEventProjection,
  CommandProjection,
  EntityLifecycleValue,
  MutationProjection,
  PermissionProjection,
  ProjectionSelector,
  RecoveryProjection,
  SchedulerProjection,
  ToolProjection,
  WorkerProjection,
} from "../../../state/contracts.ts"

export const TimelinePhase = {
  QUEUED: "queued",
  ADMITTED: "admitted",
  STARTING: "starting",
  RUNNING: "running",
  WAITING_TOOL: "waiting-tool",
  WAITING_POLICY: "waiting-policy",
  RECOVERING: "recovering",
  COMPLETED: "completed",
  FAILED: "failed",
  CANCELLED: "cancelled",
  UNKNOWN: "unknown",
} as const

export type TimelinePhaseValue =
  (typeof TimelinePhase)[keyof typeof TimelinePhase]

export const TimelineEventKind = {
  TASK: "task",
  WORKER: "worker",
  LEASE: "lease",
  ROUTE: "route",
  PLACEMENT: "placement",
  TOOL: "tool",
  POLICY: "policy",
  ARTIFACT: "artifact",
  FAILURE: "failure",
  RECOVERY: "recovery",
  CHECKPOINT: "checkpoint",
  BACKGROUND: "background",
  BROWSER_STEP: "browser-step",
  TOPOLOGY: "topology",
  MUTATION: "mutation",
  COMMAND: "command",
  SESSION: "session",
  OTHER: "other",
} as const

export type TimelineEventKindValue =
  (typeof TimelineEventKind)[keyof typeof TimelineEventKind]

export const TimelineEdgeKind = {
  CAUSATION: "causation",
  CORRELATION: "correlation",
  SPAN_PARENT: "span-parent",
  SPAN_SIBLING: "span-sibling",
  TOOL: "tool",
  ARTIFACT: "artifact",
  FAILURE: "failure",
  RECOVERY: "recovery",
  MUTATION: "mutation",
  WORKER: "worker",
  LEASE_REPLACEMENT: "lease-replacement",
  WORKER_REPLACEMENT: "worker-replacement",
  BACKGROUND_REVIVE: "background-revive",
  BROWSER_STEP: "browser-step",
  SEQUENCE: "sequence",
} as const

export type TimelineEdgeKindValue =
  (typeof TimelineEdgeKind)[keyof typeof TimelineEdgeKind]

export type TimelineEntityKind =
  | "event"
  | "task"
  | "node"
  | "worker"
  | "lease"
  | "route"
  | "placement"
  | "tool"
  | "permission"
  | "artifact"
  | "failure"
  | "recovery"
  | "checkpoint"
  | "mutation"
  | "command"
  | "session"
  | "correlation"
  | "span"
  | "background-job"
  | "browser-step"

export interface TimelineCanonicalOrder {
  sequence: number
  aggregateSequence: number
  committedAtMs: number
  createdAtMs: number
  eventId: string
}

export interface TimelineEventFacts {
  event: CausalEventProjection
  order: TimelineCanonicalOrder
  kinds: readonly TimelineEventKindValue[]
  phase?: TimelinePhaseValue
  worker?: WorkerProjection
  tool?: ToolProjection
  artifacts: readonly ArtifactProjection[]
  recovery?: RecoveryProjection
  permission?: PermissionProjection
  scheduler?: SchedulerProjection
  command?: CommandProjection
  mutation?: MutationProjection
  leaseId?: string
  routeId?: string
  placementId?: string
  failureId?: string
  recoveryId?: string
  permissionId?: string
  backgroundJobId?: string
  browserStepId?: string
  title: string
  summary: string
  partial: boolean
  missingRefs: readonly string[]
  safeAttributes: Readonly<Record<string, unknown>>
}

export interface TimelineEvidenceRef {
  kind: TimelineEntityKind
  id: string
  label: string
  eventIds: readonly string[]
  missing: boolean
  terminal: boolean
  status?: string
  metadata: Readonly<Record<string, string | number | boolean>>
}

export interface TimelineDrilldownTarget {
  id: string
  kind: TimelineEntityKind
  entityId: string
  label: string
  selector: string
  available: boolean
  eventId?: string
  workerId?: string
  nodeId?: string
  artifactId?: string
  toolCallId?: string
  failureId?: string
  recoveryId?: string
  permissionId?: string
  routeId?: string
}

export interface TimelineRow {
  key: string
  taskId: string
  runId: string
  rowKind: TimelineEventKindValue
  phase: TimelinePhaseValue
  primaryEventId: string
  eventIds: readonly string[]
  sequence: number
  endSequence: number
  occurredAt: string
  committedAt: string
  title: string
  summary: string
  workerId?: string
  workerEpochId?: string
  leaseId?: string
  nodeId?: string
  routeId?: string
  placementId?: string
  toolCallId?: string
  permissionId?: string
  failureId?: string
  recoveryId?: string
  backgroundJobId?: string
  browserStepId?: string
  spanId?: string
  correlationId: string
  causationId?: string
  depth: number
  critical: boolean
  terminal: boolean
  effective: boolean
  partial: boolean
  duplicateCount: number
  late: boolean
  evidence: readonly TimelineEvidenceRef[]
  drilldowns: readonly TimelineDrilldownTarget[]
  artifactIds: readonly string[]
  mutationIds: readonly string[]
  incomingEdgeIds: readonly string[]
  outgoingEdgeIds: readonly string[]
  tags: readonly string[]
}

export interface TimelineEdge {
  id: string
  kind: TimelineEdgeKindValue
  sourceEventId: string
  targetEventId: string
  sourceRowKey?: string
  targetRowKey?: string
  explicit: boolean
  missingSource: boolean
  missingTarget: boolean
  weight: number
  label: string
  metadata: Readonly<Record<string, string | number | boolean>>
}

export interface TimelineCausalGraph {
  eventIds: readonly string[]
  edges: readonly TimelineEdge[]
  incomingByEvent: Readonly<Record<string, readonly string[]>>
  outgoingByEvent: Readonly<Record<string, readonly string[]>>
  ancestorsByEvent: Readonly<Record<string, readonly string[]>>
  descendantsByEvent: Readonly<Record<string, readonly string[]>>
  depthByEvent: Readonly<Record<string, number>>
  components: readonly (readonly string[])[]
  roots: readonly string[]
  leaves: readonly string[]
  missingEventIds: readonly string[]
  cycleEventIds: readonly string[]
}

export interface TimelinePhaseInterval {
  id: string
  workerId: string
  workerEpochId: string
  phase: TimelinePhaseValue
  startEventId: string
  endEventId: string
  startSequence: number
  endSequence: number
  startedAt: string
  endedAt: string
  durationMs: number
  terminal: boolean
  eventIds: readonly string[]
  reason?: string
}

export interface TimelineWorkerEpoch {
  id: string
  workerId: string
  taskId: string
  runId: string
  nodeId?: string
  role?: string
  leaseId?: string
  routeId?: string
  placementId?: string
  previousEpochId?: string
  replacementEpochId?: string
  replacementReason?: string
  startEventId: string
  endEventId: string
  startSequence: number
  endSequence: number
  startedAt: string
  endedAt: string
  terminal: boolean
  finalPhase: TimelinePhaseValue
  phases: readonly TimelinePhaseInterval[]
  eventIds: readonly string[]
  toolCallIds: readonly string[]
  artifactIds: readonly string[]
  failureIds: readonly string[]
  recoveryIds: readonly string[]
  duplicateEventCount: number
  lateEventCount: number
}

export interface TimelineRecoveryAttempt {
  id: string
  recoveryId: string
  failureId?: string
  attempt: number
  strategy?: string
  workerId?: string
  previousWorkerId?: string
  replacementWorkerId?: string
  checkpointId?: string
  startEventId: string
  endEventId: string
  startSequence: number
  endSequence: number
  phase: "planned" | "running" | "completed" | "failed" | "cancelled" | "partial"
  reason?: string
  eventIds: readonly string[]
  duplicateEventIds: readonly string[]
  partial: boolean
  terminal: boolean
}

export interface TimelineRecoveryChain {
  id: string
  failureId: string
  workerIds: readonly string[]
  attempts: readonly TimelineRecoveryAttempt[]
  firstEventId: string
  lastEventId: string
  startSequence: number
  endSequence: number
  resolved: boolean
  exhausted: boolean
  duplicateTerminalCount: number
  replacementWorkerIds: readonly string[]
  checkpointIds: readonly string[]
  eventIds: readonly string[]
}

export interface TimelineBackgroundLifecycle {
  id: string
  jobId: string
  workerId?: string
  type?: string
  label?: string
  status: "running" | "parked" | "reviving" | "completed" | "failed" | "cancelled" | "unknown"
  startEventId: string
  endEventId: string
  eventIds: readonly string[]
  parkEventIds: readonly string[]
  reviveEventIds: readonly string[]
  revivalCount: number
  partial: boolean
}

export interface TimelineBrowserStep {
  id: string
  workerId?: string
  actionEventIds: readonly string[]
  resultEventIds: readonly string[]
  stateEventIds: readonly string[]
  artifactIds: readonly string[]
  actionNames: readonly string[]
  startEventId: string
  endEventId: string
  startSequence: number
  endSequence: number
  status: "running" | "completed" | "failed" | "partial"
  error?: string
  partial: boolean
}

export interface TimelineCriticalPath {
  eventIds: readonly string[]
  rowKeys: readonly string[]
  workerIds: readonly string[]
  totalWeight: number
  startedAt?: string
  endedAt?: string
  durationMs: number
  complete: boolean
  missingEventIds: readonly string[]
  alternatives: readonly {
    eventIds: readonly string[]
    totalWeight: number
  }[]
}

export interface TimelineFilter {
  workerIds?: readonly string[]
  phases?: readonly TimelinePhaseValue[]
  kinds?: readonly TimelineEventKindValue[]
  from?: string | number
  to?: string | number
  search?: string
  criticalOnly?: boolean
  failuresOnly?: boolean
  includeNonEffective?: boolean
  includePartial?: boolean
}

export interface TimelineHiddenGap {
  id: string
  beforeRowKey?: string
  afterRowKey?: string
  hiddenRowKeys: readonly string[]
  hiddenEventIds: readonly string[]
  count: number
  startSequence: number
  endSequence: number
  phaseCounts: Readonly<Record<string, number>>
  kindCounts: Readonly<Record<string, number>>
  workerIds: readonly string[]
  boundaryEventIds: readonly string[]
  containsCritical: boolean
  containsFailure: boolean
  containsRecovery: boolean
  causalBridge: boolean
}

export interface TimelineFilteredView {
  rows: readonly TimelineRow[]
  gaps: readonly TimelineHiddenGap[]
  hiddenRowCount: number
  hiddenEventCount: number
  matchedRowCount: number
  visibleEventIds: readonly string[]
  retainedBoundaryEventIds: readonly string[]
  activeFilterCount: number
}

export interface TimelineWindowOptions {
  start?: number
  count?: number
  anchorRowKey?: string
  overscan?: number
  pinRowKeys?: readonly string[]
}

export interface TimelineWindow {
  rows: readonly TimelineRow[]
  firstIndex: number
  lastIndex: number
  total: number
  beforeCount: number
  afterCount: number
  anchorIndex?: number
  pinnedRowKeys: readonly string[]
  revisionKey: string
}

export interface TimelineDiagnostics {
  ready: boolean
  disabled: boolean
  revision: number
  committedSequence: number
  highWatermark: number
  lag: number
  eventCount: number
  effectiveEventCount: number
  duplicateEventCount: number
  staleEventCount: number
  lateEventCount: number
  partialEventCount: number
  missingCauseCount: number
  missingEvidenceCount: number
  cycleCount: number
  workerEpochCount: number
  recoveryChainCount: number
  duplicateRecoveryCount: number
  warnings: readonly string[]
}

export interface WorkerCausalTimelineProjection {
  taskId: string
  runIds: readonly string[]
  projectionRevision: number
  committedAtMs: number
  rows: readonly TimelineRow[]
  events: readonly TimelineEventFacts[]
  graph: TimelineCausalGraph
  workerEpochs: readonly TimelineWorkerEpoch[]
  recoveryChains: readonly TimelineRecoveryChain[]
  backgroundLifecycles: readonly TimelineBackgroundLifecycle[]
  browserSteps: readonly TimelineBrowserStep[]
  criticalPath: TimelineCriticalPath
  rowsByKey: Readonly<Record<string, TimelineRow>>
  rowKeyByEvent: Readonly<Record<string, string>>
  rowKeysByWorker: Readonly<Record<string, readonly string[]>>
  rowKeysByPhase: Readonly<Record<string, readonly string[]>>
  rowKeysByKind: Readonly<Record<string, readonly string[]>>
  diagnostics: TimelineDiagnostics
}

export interface WorkerCausalTimelineOptions {
  disabled?: boolean
  includeNonEffective?: boolean
  includePartial?: boolean
  maximumEvents?: number
  maximumClosureDepth?: number
  maximumAlternativePaths?: number
  coalesceWindowMs?: number
}

export interface TimelineProjectorInput {
  state: CanonicalProjectionState
  taskId: string
  options: Required<WorkerCausalTimelineOptions>
}

export type WorkerCausalTimelineSelector =
  ProjectionSelector<WorkerCausalTimelineProjection>

export interface TimelineProjectionEngineOptions
  extends WorkerCausalTimelineOptions {
  now?: () => number
}

export interface TimelineProjectionEngineAudit {
  closed: boolean
  disabled: boolean
  projectionCount: number
  lastTaskId?: string
  lastRevision?: number
  lastEventCount: number
  lastRowCount: number
  lastDurationMs: number
  lastError?: string
}

export class TimelineProjectionError extends Error {
  readonly code: string
  readonly details: Readonly<Record<string, string | number | boolean>>

  constructor(
    code: string,
    message: string,
    details: Record<string, string | number | boolean> = {},
  ) {
    super(message)
    this.name = "TimelineProjectionError"
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}

export function lifecycleToTimelinePhase(
  lifecycle: EntityLifecycleValue,
): TimelinePhaseValue {
  switch (lifecycle) {
    case "queued":
      return TimelinePhase.QUEUED
    case "admitted":
      return TimelinePhase.ADMITTED
    case "starting":
      return TimelinePhase.STARTING
    case "running":
      return TimelinePhase.RUNNING
    case "waiting_tool":
      return TimelinePhase.WAITING_TOOL
    case "waiting_policy":
      return TimelinePhase.WAITING_POLICY
    case "recovering":
      return TimelinePhase.RECOVERING
    case "completed":
      return TimelinePhase.COMPLETED
    case "failed":
    case "rejected":
    case "expired":
      return TimelinePhase.FAILED
    case "cancelled":
    case "deleted":
      return TimelinePhase.CANCELLED
    case "paused":
      return TimelinePhase.WAITING_POLICY
    default:
      return TimelinePhase.UNKNOWN
  }
}
