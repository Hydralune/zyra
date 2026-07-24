import type {
  TimelineCriticalPath,
  TimelineEventKindValue,
  TimelinePhaseValue,
  TimelineRow,
  WorkerCausalTimelineProjection,
} from "../projection/index.ts"

export interface TimelineMeasuredRow {
  key: string
  index: number
  offset: number
  size: number
  measured: boolean
  row: TimelineRow
}

export interface TimelineVirtualRange {
  rows: readonly TimelineMeasuredRow[]
  startIndex: number
  endIndex: number
  overscanStartIndex: number
  overscanEndIndex: number
  beforeHeight: number
  visibleHeight: number
  afterHeight: number
  totalHeight: number
  viewportHeight: number
  scrollOffset: number
  anchorKey?: string
  anchorOffset?: number
  revision: number
}

export interface TimelineVirtualizerOptions {
  estimatedRowHeight?: number
  minimumRowHeight?: number
  maximumRowHeight?: number
  overscanPixels?: number
  maximumRenderedRows?: number
  measurementCacheLimit?: number
}

export interface TimelineScrollAnchor {
  rowKey: string
  offsetWithinRow: number
  previousIndex: number
  previousAbsoluteOffset: number
}

export type TimelineFoldKind =
  | "time"
  | "causal"
  | "recovery"
  | "worker-idle"
  | "repeated"

export interface TimelineFold {
  id: string
  kind: TimelineFoldKind
  label: string
  startIndex: number
  endIndex: number
  startSequence: number
  endSequence: number
  startedAt: string
  endedAt: string
  durationMs: number
  rowKeys: readonly string[]
  eventIds: readonly string[]
  workerIds: readonly string[]
  phases: readonly TimelinePhaseValue[]
  kinds: readonly TimelineEventKindValue[]
  boundaryEventIds: readonly string[]
  incomingEventIds: readonly string[]
  outgoingEventIds: readonly string[]
  hiddenCount: number
  effectiveCount: number
  containsCritical: boolean
  containsFailure: boolean
  containsRecovery: boolean
  containsControl: boolean
  expanded: boolean
  safeToCollapse: boolean
  reason: string
}

export interface TimelineFoldOptions {
  timeBucketMs?: number
  minimumTimeFoldRows?: number
  minimumCausalFoldRows?: number
  maximumFoldRows?: number
  preserveCritical?: boolean
  preserveFailures?: boolean
  preserveControls?: boolean
  expandedFoldIds?: ReadonlySet<string>
}

export interface TimelineFoldProjection {
  folds: readonly TimelineFold[]
  visibleRows: readonly TimelineRow[]
  foldedRowKeys: ReadonlySet<string>
  foldByRowKey: ReadonlyMap<string, TimelineFold>
  hiddenRowCount: number
  effectiveRowCount: number
  revision: string
}

export interface TimelineSearchDocument {
  rowKey: string
  index: number
  normalized: string
  tokens: readonly string[]
  prefixes: readonly string[]
  trigrams: readonly string[]
  workerId?: string
  phase: TimelinePhaseValue
  kind: TimelineEventKindValue
  sequence: number
  critical: boolean
  effective: boolean
}

export interface TimelineSearchMatch {
  rowKey: string
  index: number
  score: number
  matchedTokens: readonly string[]
  matchedFields: readonly string[]
  highlights: readonly {
    start: number
    end: number
    text: string
  }[]
}

export interface TimelineSearchQuery {
  text: string
  workerIds?: readonly string[]
  phases?: readonly TimelinePhaseValue[]
  kinds?: readonly TimelineEventKindValue[]
  criticalOnly?: boolean
  effectiveOnly?: boolean
  limit?: number
}

export interface TimelineSearchResult {
  query: TimelineSearchQuery
  matches: readonly TimelineSearchMatch[]
  totalMatches: number
  scannedDocuments: number
  candidateDocuments: number
  elapsedMs: number
}

export type TimelineDriftKind =
  | "initial"
  | "goal-update"
  | "requirement-change"
  | "steer"
  | "replan"
  | "constraint-change"

export interface TimelineGoalEpoch {
  id: string
  kind: TimelineDriftKind
  ordinal: number
  startSequence: number
  endSequence: number
  startEventId: string
  endEventId: string
  startedAt: string
  endedAt: string
  sourceRowKeys: readonly string[]
  sourceEventIds: readonly string[]
  goalDigest?: string
  previousGoalDigest?: string
  instruction?: string
  affectedNodeIds: readonly string[]
  changedTerms: readonly string[]
  retainedTerms: readonly string[]
  driftScore: number
  explicit: boolean
  effective: boolean
}

export interface TimelineGoalDrift {
  epochs: readonly TimelineGoalEpoch[]
  currentEpoch?: TimelineGoalEpoch
  maximumDriftScore: number
  totalGoalChanges: number
  totalRequirementChanges: number
  totalSteers: number
  driftRowKeys: ReadonlySet<string>
}

export type TimelineOverlayKind =
  | "effective-step"
  | "heartbeat"
  | "noop"
  | "replay"
  | "compact"
  | "checkpoint"
  | "placement"
  | "route"
  | "worker-replacement"
  | "control"
  | "permission"
  | "fault"
  | "recovery"
  | "goal-drift"
  | "critical"

export interface TimelineRowOverlay {
  rowKey: string
  kinds: readonly TimelineOverlayKind[]
  effectiveStep: boolean
  effectiveReason: string
  transitionWeight: number
  compactId?: string
  checkpointId?: string
  placementId?: string
  routeId?: string
  previousWorkerId?: string
  replacementWorkerId?: string
  controlCommandId?: string
  permissionId?: string
  failureId?: string
  recoveryId?: string
  driftEpochId?: string
  labels: readonly string[]
  eventIds: readonly string[]
}

export interface TimelineOverlayProjection {
  byRowKey: ReadonlyMap<string, TimelineRowOverlay>
  rowsByKind: ReadonlyMap<TimelineOverlayKind, readonly string[]>
  effectiveStepCount: number
  excludedNoopCount: number
  compactCount: number
  checkpointCount: number
  placementCount: number
  controlCount: number
  recoveryCount: number
}

export interface ScaledTimelineOptions {
  fold?: TimelineFoldOptions
  virtualizer?: TimelineVirtualizerOptions
  search?: TimelineSearchQuery
  scrollOffset?: number
  viewportHeight?: number
  selectedRowKey?: string
  criticalOnly?: boolean
}

export interface ScaledTimelineProjection {
  taskId: string
  sourceRevision: number
  rows: readonly TimelineRow[]
  folds: TimelineFoldProjection
  virtual: TimelineVirtualRange
  overlays: TimelineOverlayProjection
  goalDrift: TimelineGoalDrift
  search?: TimelineSearchResult
  criticalPath: TimelineCriticalPath
  selectedRow?: TimelineRow
  thousandsMode: boolean
  totalRows: number
  renderedRows: number
  effectiveSteps: number
  hiddenByFolds: number
  revision: string
}

export interface ScaledTimelineInput {
  projection: WorkerCausalTimelineProjection
  options?: ScaledTimelineOptions
}
