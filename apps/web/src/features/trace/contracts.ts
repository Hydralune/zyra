import type {
  CanonicalProjectionState,
  CausalEventProjection,
  ProjectionSelector,
} from "../../state/contracts.ts"

export const TRACE_PROJECTION_SCHEMA = "zyra.causal-trace-projection/v1" as const

export const TraceNodeKind = {
  EVENT: "event",
  SPAN: "span",
  TASK: "task",
  RUN: "run",
  SESSION: "session",
  NODE: "node",
  WORKER: "worker",
  TOOL: "tool",
  PERMISSION: "permission",
  ARTIFACT: "artifact",
  MUTATION: "mutation",
  CHECKPOINT: "checkpoint",
  COMMAND: "command",
  ROUTE: "route",
  PLACEMENT: "placement",
  PROVIDER: "provider",
  FAILURE: "failure",
  RECOVERY: "recovery",
  MCP: "mcp",
  SKILL: "skill",
  SUBAGENT: "subagent",
  TERMINAL: "terminal",
  BROWSER: "browser",
  BACKGROUND: "background",
  ISSUE: "issue",
} as const

export type TraceNodeKindValue =
  (typeof TraceNodeKind)[keyof typeof TraceNodeKind]

export const TraceSemanticKind = {
  TASK: "task",
  WORKER: "worker",
  TOOL: "tool",
  PERMISSION: "permission",
  COMPACT: "compact",
  CHECKPOINT: "checkpoint",
  RESTORE: "restore",
  PLACEMENT: "placement",
  PROVIDER: "provider",
  PROVIDER_RETRY: "provider-retry",
  FAULT: "fault",
  RECOVERY: "recovery",
  MCP: "mcp",
  MCP_RECONNECT: "mcp-reconnect",
  SKILL: "skill",
  SUBAGENT: "subagent",
  SUBAGENT_YIELD: "subagent-yield",
  TERMINAL: "terminal",
  PTY: "pty",
  BROWSER: "browser",
  ARTIFACT: "artifact",
  MUTATION: "mutation",
  TOPOLOGY: "topology",
  COMMAND: "command",
  BACKGROUND: "background",
  OTHER: "other",
} as const

export type TraceSemanticKindValue =
  (typeof TraceSemanticKind)[keyof typeof TraceSemanticKind]

export const TraceEdgeKind = {
  CAUSATION: "causation",
  CORRELATION: "correlation",
  SPAN_PARENT: "span-parent",
  SPAN_EVENT: "span-event",
  TASK_MEMBER: "task-member",
  RUN_MEMBER: "run-member",
  SESSION_MEMBER: "session-member",
  NODE_MEMBER: "node-member",
  WORKER_MEMBER: "worker-member",
  TOOL_CALL: "tool-call",
  PERMISSION_GUARD: "permission-guard",
  ARTIFACT_PRODUCED: "artifact-produced",
  MUTATION_APPLIED: "mutation-applied",
  CHECKPOINT_STATE: "checkpoint-state",
  COMMAND_CONTROL: "command-control",
  ROUTE_SELECTED: "route-selected",
  PLACEMENT_SELECTED: "placement-selected",
  PROVIDER_DISPATCH: "provider-dispatch",
  FAILURE_TRIGGER: "failure-trigger",
  RECOVERY_ATTEMPT: "recovery-attempt",
  REPLACEMENT: "replacement",
  MCP_CALL: "mcp-call",
  SKILL_EXECUTION: "skill-execution",
  SUBAGENT_PARENT: "subagent-parent",
  SUBAGENT_YIELD: "subagent-yield",
  TERMINAL_IO: "terminal-io",
  BROWSER_ACTION: "browser-action",
  BACKGROUND_LIFECYCLE: "background-lifecycle",
  SEQUENCE: "sequence",
} as const

export type TraceEdgeKindValue =
  (typeof TraceEdgeKind)[keyof typeof TraceEdgeKind]

export const TraceCompleteness = {
  COMPLETE: "complete",
  PARTIAL: "partial",
  LATE: "late",
  ORPHAN: "orphan",
  QUARANTINED: "quarantined",
} as const

export type TraceCompletenessValue =
  (typeof TraceCompleteness)[keyof typeof TraceCompleteness]

export interface TraceIdentity {
  kind: TraceNodeKindValue
  id: string
  key: string
}

export interface TraceTypedRefs {
  taskId: string
  runId: string
  sessionId?: string
  nodeId?: string
  workerId?: string
  spanId?: string
  parentSpanId?: string
  toolCallId?: string
  permissionId?: string
  artifactIds: readonly string[]
  mutationId?: string
  checkpointId?: string
  commandId?: string
  routeId?: string
  placementId?: string
  providerId?: string
  failureId?: string
  recoveryId?: string
  mcpCallId?: string
  mcpServerId?: string
  skillId?: string
  subagentId?: string
  parentToolCallId?: string
  yieldId?: string
  terminalSessionId?: string
  terminalFrameId?: string
  browserSessionId?: string
  browserStepId?: string
  browserActionId?: string
  backgroundJobId?: string
}

export interface TraceEventAdmission {
  event: CausalEventProjection
  refs: TraceTypedRefs
  semantics: readonly TraceSemanticKindValue[]
  completeness: TraceCompletenessValue
  partialReasons: readonly string[]
  identityConflicts: readonly string[]
  safeAttributes: Readonly<Record<string, string | number | boolean>>
}

export interface TraceNode {
  key: string
  identity: TraceIdentity
  taskId: string
  runId: string
  eventIds: readonly string[]
  primaryEventId?: string
  parentKey?: string
  childKeys: readonly string[]
  incomingEdgeIds: readonly string[]
  outgoingEdgeIds: readonly string[]
  sequence: number
  endSequence: number
  startedAt?: string
  endedAt?: string
  durationMs: number
  title: string
  summary: string
  semantics: readonly TraceSemanticKindValue[]
  completeness: TraceCompletenessValue
  terminal: boolean
  effective: boolean
  critical: boolean
  retryCount: number
  contributionScore: number
  latencyScore: number
  costScore: number
  refs: TraceTypedRefs
  tags: readonly string[]
  diagnostics: readonly string[]
  safeAttributes: Readonly<Record<string, string | number | boolean>>
}

export interface TraceEdge {
  id: string
  kind: TraceEdgeKindValue
  sourceKey: string
  targetKey: string
  sourceEventId?: string
  targetEventId?: string
  explicit: boolean
  weight: number
  critical: boolean
  late: boolean
  missingSource: boolean
  missingTarget: boolean
  label: string
  evidenceEventIds: readonly string[]
  metadata: Readonly<Record<string, string | number | boolean>>
}

export interface TraceQuarantineRecord {
  id: string
  eventId?: string
  nodeKey?: string
  edgeId?: string
  code: string
  message: string
  expectedTaskId?: string
  observedTaskId?: string
  identityKind?: TraceNodeKindValue
  identityId?: string
  sequence: number
  recoverable: boolean
}

export interface TraceHierarchy {
  rootKeys: readonly string[]
  childrenByKey: Readonly<Record<string, readonly string[]>>
  parentByKey: Readonly<Record<string, string>>
  depthByKey: Readonly<Record<string, number>>
  ancestorsByKey: Readonly<Record<string, readonly string[]>>
  descendantsByKey: Readonly<Record<string, readonly string[]>>
}

export interface TraceIndexes {
  nodeKeysByEvent: Readonly<Record<string, readonly string[]>>
  nodeKeysBySpan: Readonly<Record<string, readonly string[]>>
  nodeKeysByWorker: Readonly<Record<string, readonly string[]>>
  nodeKeysByToolCall: Readonly<Record<string, readonly string[]>>
  nodeKeysByArtifact: Readonly<Record<string, readonly string[]>>
  nodeKeysByMutation: Readonly<Record<string, readonly string[]>>
  nodeKeysByCheckpoint: Readonly<Record<string, readonly string[]>>
  nodeKeysByFailure: Readonly<Record<string, readonly string[]>>
  nodeKeysByRecovery: Readonly<Record<string, readonly string[]>>
  nodeKeysBySemantic: Readonly<Record<string, readonly string[]>>
  edgeIdsByEvent: Readonly<Record<string, readonly string[]>>
  eventIdByMutation: Readonly<Record<string, string>>
  producerEventIdsByArtifact: Readonly<Record<string, readonly string[]>>
}

export interface TraceCriticalPath {
  nodeKeys: readonly string[]
  eventIds: readonly string[]
  edgeIds: readonly string[]
  workerIds: readonly string[]
  providerIds: readonly string[]
  totalWeight: number
  durationMs: number
  complete: boolean
  cycleNodeKeys: readonly string[]
  missingNodeKeys: readonly string[]
  bottlenecks: readonly TraceBottleneck[]
  alternatives: readonly TracePathAlternative[]
}

export interface TraceBottleneck {
  id: string
  nodeKey: string
  kind: "latency" | "retry" | "permission" | "provider" | "recovery" | "fan-in"
  score: number
  durationMs: number
  label: string
  evidenceEventIds: readonly string[]
}

export interface TracePathAlternative {
  id: string
  nodeKeys: readonly string[]
  eventIds: readonly string[]
  totalWeight: number
  divergenceKey?: string
  convergenceKey?: string
}

export interface TraceDiagnostics {
  ready: boolean
  disabled: boolean
  projectionRevision: number
  committedSequence: number
  highWatermark: number
  lag: number
  eventCount: number
  nodeCount: number
  edgeCount: number
  partialCount: number
  lateCount: number
  orphanCount: number
  quarantineCount: number
  missingSpanCount: number
  missingArtifactCount: number
  identityConflictCount: number
  cycleCount: number
  warnings: readonly string[]
}

export interface CausalTraceProjection {
  schema: typeof TRACE_PROJECTION_SCHEMA
  taskId: string
  runIds: readonly string[]
  projectionRevision: number
  committedAtMs: number
  nodes: readonly TraceNode[]
  edges: readonly TraceEdge[]
  nodesByKey: Readonly<Record<string, TraceNode>>
  edgesById: Readonly<Record<string, TraceEdge>>
  admissionByEvent: Readonly<Record<string, TraceEventAdmission>>
  hierarchy: TraceHierarchy
  indexes: TraceIndexes
  criticalPath: TraceCriticalPath
  quarantine: readonly TraceQuarantineRecord[]
  diagnostics: TraceDiagnostics
}

export interface TraceProjectionOptions {
  disabled?: boolean
  includeNonEffective?: boolean
  includePartial?: boolean
  maximumEvents?: number
  maximumClosureDepth?: number
  maximumAlternativePaths?: number
  maximumAttributesPerEvent?: number
  lateSequenceTolerance?: number
}

export type CausalTraceSelector = ProjectionSelector<CausalTraceProjection>

export interface TraceFilter {
  search?: string
  semantics?: readonly TraceSemanticKindValue[]
  nodeKinds?: readonly TraceNodeKindValue[]
  workerIds?: readonly string[]
  providerIds?: readonly string[]
  completeness?: readonly TraceCompletenessValue[]
  fromSequence?: number
  toSequence?: number
  criticalOnly?: boolean
  failuresOnly?: boolean
  retriesOnly?: boolean
  includeNonEffective?: boolean
  includeIssueNodes?: boolean
}

export interface TraceSearchMatch {
  nodeKey: string
  score: number
  matchedTerms: readonly string[]
  matchedFields: readonly string[]
  ranges: readonly { field: string; start: number; end: number }[]
}

export interface TraceHiddenGap {
  id: string
  beforeKey?: string
  afterKey?: string
  hiddenNodeKeys: readonly string[]
  hiddenEventIds: readonly string[]
  count: number
  startSequence: number
  endSequence: number
  semantics: Readonly<Record<string, number>>
  containsCritical: boolean
  containsFailure: boolean
  containsRecovery: boolean
  causalBridge: boolean
}

export interface TraceFilteredProjection {
  nodes: readonly TraceNode[]
  matches: readonly TraceSearchMatch[]
  gaps: readonly TraceHiddenGap[]
  visibleNodeKeys: readonly string[]
  visibleEdgeIds: readonly string[]
  hiddenNodeCount: number
  hiddenEventCount: number
  activeFilterCount: number
}

export interface TraceFoldState {
  collapsedKeys: ReadonlySet<string>
  mode: "manual" | "span" | "worker" | "semantic"
  preserveCritical: boolean
  preserveFailures: boolean
  maximumVisibleChildren: number
}

export interface TraceFoldGroup {
  key: string
  parentKey?: string
  memberKeys: readonly string[]
  visibleMemberKeys: readonly string[]
  hiddenMemberKeys: readonly string[]
  boundaryEdgeIds: readonly string[]
  collapsed: boolean
  summary: string
  criticalCount: number
  failureCount: number
  partialCount: number
}

export interface TraceFoldedProjection {
  nodes: readonly TraceNode[]
  groups: readonly TraceFoldGroup[]
  visibleNodeKeys: readonly string[]
  hiddenNodeKeys: readonly string[]
  visibleEdgeIds: readonly string[]
  boundaryEdgeIds: readonly string[]
}

export interface TraceVirtualWindowOptions {
  scrollTop: number
  viewportHeight: number
  overscanPx?: number
  estimatedRowHeight?: number
  anchorKey?: string
  pinnedKeys?: readonly string[]
}

export interface TraceVirtualItem {
  key: string
  index: number
  top: number
  height: number
  measured: boolean
  pinned: boolean
}

export interface TraceVirtualWindow {
  items: readonly TraceVirtualItem[]
  firstIndex: number
  lastIndex: number
  beforeHeight: number
  afterHeight: number
  totalHeight: number
  total: number
  anchorIndex?: number
  revisionKey: string
}

export const TraceViewKind = {
  TRACE: "trace",
  TIMELINE: "timeline",
  TOPOLOGY: "topology",
  TERMINAL: "terminal",
  BROWSER: "browser",
  ARTIFACT: "artifact",
  DIFF: "diff",
} as const

export type TraceViewKindValue =
  (typeof TraceViewKind)[keyof typeof TraceViewKind]

export interface TraceNavigationTarget {
  id: string
  view: TraceViewKindValue
  taskId: string
  label: string
  traceNodeKey?: string
  eventId?: string
  spanId?: string
  workerId?: string
  nodeId?: string
  toolCallId?: string
  artifactId?: string
  mutationId?: string
  terminalSessionId?: string
  terminalFrameId?: string
  browserSessionId?: string
  browserStepId?: string
  browserActionId?: string
  routeId?: string
  placementId?: string
  selector?: string
}

export interface TraceNavigationResolution {
  target: TraceNavigationTarget
  available: boolean
  selector?: string
  reason?: string
  fallbackTargets: readonly TraceNavigationTarget[]
}

export interface TraceNavigationReceipt {
  id: string
  target: TraceNavigationTarget
  phase: "resolved" | "focused" | "unavailable" | "cancelled" | "failed"
  attemptedAt: number
  settledAt: number
  selector?: string
  reason?: string
}

export interface TraceReportReference {
  id: string
  taskId: string
  runId: string
  nodeKey: string
  eventIds: readonly string[]
  artifactIds: readonly string[]
  mutationIds: readonly string[]
  spanId?: string
  workerId?: string
  providerId?: string
  label: string
  note?: string
  createdAt: string
  revision: number
  stale: boolean
}

export interface TraceWorkbenchSnapshot {
  projection: CausalTraceProjection
  filtered: TraceFilteredProjection
  folded: TraceFoldedProjection
  virtualWindow: TraceVirtualWindow
  selectedNodeKey?: string
  expandedNodeKeys: ReadonlySet<string>
  filter: TraceFilter
  fold: TraceFoldState
  pins: readonly TraceReportReference[]
  navigationHistory: readonly TraceNavigationReceipt[]
  activeNavigation?: TraceNavigationReceipt
  closed: boolean
  disabled: boolean
}

export interface TraceProjectionInput {
  state: CanonicalProjectionState
  taskId: string
  options: Required<TraceProjectionOptions>
}

export class TraceProjectionError extends Error {
  readonly code: string
  readonly details: Readonly<Record<string, string | number | boolean>>

  constructor(
    code: string,
    message: string,
    details: Record<string, string | number | boolean> = {},
  ) {
    super(message)
    this.name = "TraceProjectionError"
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}
