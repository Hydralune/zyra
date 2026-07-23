import type { JsonObject, JsonValue } from "../../../events/ingress/index.ts"
import type {
  CanonicalProjectionState,
  ProjectionSelector,
} from "../../../state/contracts.ts"

export const TOPOLOGY_PROJECTION_SCHEMA = "zyra.topology-projection/v1" as const

export type TopologyEntityKind =
  | "node"
  | "edge"
  | "route"
  | "candidate"
  | "placement"
  | "checkpoint"
  | "branch"
  | "conflict"
  | "requirement_change"
  | "namespace"

export type TopologyMutationKind =
  | "add_node"
  | "remove_node"
  | "replace_node"
  | "set_node_role"
  | "set_node_capabilities"
  | "set_node_dependencies"
  | "add_edge"
  | "remove_edge"
  | "replace_edge"
  | "set_graph_metadata"
  | "remove_graph_metadata"
  | "route_selected"
  | "route_changed"
  | "placement_changed"
  | "requirement_changed"
  | "checkpoint_committed"
  | "branch_committed"
  | "branch_rebased"
  | "branch_rejected"
  | "unknown"

export type TopologyEntityState =
  | "planned"
  | "ready"
  | "queued"
  | "leased"
  | "running"
  | "blocked"
  | "waiting"
  | "interrupted"
  | "resuming"
  | "recovering"
  | "needs_revision"
  | "succeeded"
  | "completed"
  | "failed"
  | "cancelled"
  | "superseded"
  | "removed"
  | "conflicted"
  | "rejected"
  | "unknown"

export type EdgeKind =
  | "dependency"
  | "explicit"
  | "parent"
  | "namespace"
  | "route"
  | "placement"
  | "checkpoint"
  | "branch"

export type PlacementLocation = "device" | "local" | "edge" | "cloud" | "unknown"

export type BranchOutcome =
  | "pending"
  | "committed"
  | "rebased"
  | "replayed"
  | "conflicted"
  | "replan_required"
  | "rejected"
  | "discarded"
  | "unknown"

export interface TopologyEvidence {
  eventIds: readonly string[]
  mutationIds: readonly string[]
  correlationIds: readonly string[]
  causationIds: readonly string[]
  spanIds: readonly string[]
  parentSpanIds: readonly string[]
  checkpointIds: readonly string[]
  controlCommandIds: readonly string[]
  graphIds: readonly string[]
  graphRevisions: readonly number[]
  entityRefs: readonly string[]
  firstSequence?: number
  lastSequence?: number
}

export interface TopologyEntityBase {
  id: string
  taskId: string
  runId: string
  kind: TopologyEntityKind
  sequence: number
  revision: number
  graphRevision: number
  commitRevision: number
  state: TopologyEntityState
  title: string
  summary: string
  namespace: string
  subgraphId?: string
  branchId?: string
  checkpointId?: string
  effective: boolean
  terminal: boolean
  pending: boolean
  removed: boolean
  evidence: TopologyEvidence
  attributes: Readonly<JsonObject>
}

export interface TopologyNodeView extends TopologyEntityBase {
  kind: "node"
  role: string
  capabilities: readonly string[]
  dependencies: readonly string[]
  childNodeIds: readonly string[]
  parentNodeId?: string
  workerIds: readonly string[]
  routeIds: readonly string[]
  placementIds: readonly string[]
  artifactIds: readonly string[]
  logicalTaskId?: string
  physicalAttemptRef?: string
  workerLeaseRef?: string
  workspaceRef?: string
  backendRouteRef?: string
  supersededBy?: string
  replacementNodeId?: string
  affectedByChangeIds: readonly string[]
  missingDependencyIds: readonly string[]
  openWorldCreated: boolean
  openWorldRemoved: boolean
  openWorldReplaced: boolean
}

export interface TopologyEdgeView extends TopologyEntityBase {
  kind: "edge"
  edgeKind: EdgeKind
  sourceId: string
  targetId: string
  relation: string
  weight?: number
  requiredCapabilities: readonly string[]
  condition: Readonly<JsonObject>
  sourceMissing: boolean
  targetMissing: boolean
  inferred: boolean
  runtimeMutation: boolean
  replacementEdgeId?: string
}

export interface TopologyMutationView {
  id: string
  taskId: string
  graphId?: string
  branchId?: string
  deltaId?: string
  entityId: string
  kind: TopologyMutationKind
  sequence: number
  baseRevision: number
  committedRevision: number
  expectedEntityRevision?: number
  effective: boolean
  openWorld: boolean
  beforeDigest?: string
  afterDigest?: string
  readSet: readonly string[]
  writeSet: readonly string[]
  value: Readonly<JsonObject>
  evidence: TopologyEvidence
}

export interface ResourceVectorView {
  cpuCores: number
  memoryMb: number
  gpuUnits: number
  diskMb: number
  networkMbps: number
  processSlots: number
  browserSlots: number
  custom: Readonly<Record<string, number>>
}

export interface ResourceFitView {
  requested: ResourceVectorView
  capacity: ResourceVectorView
  allocated: ResourceVectorView
  available: ResourceVectorView
  fits: boolean
  missing: readonly string[]
  utilization: Readonly<Record<string, number>>
}

export interface RouteCandidateView {
  id: string
  routeId: string
  workerId: string
  workerName: string
  manifestId?: string
  backendId?: string
  providerId?: string
  modelId?: string
  location: PlacementLocation
  rank: number
  score: number
  accepted: boolean
  selected: boolean
  health: string
  reasons: readonly string[]
  capabilities: readonly string[]
  requiredCapabilities: readonly string[]
  missingCapabilities: readonly string[]
  resourceFit?: ResourceFitView
  privacyClass?: string
  costEstimate?: number
  latencyEstimateMs?: number
  evidence: TopologyEvidence
  attributes: Readonly<JsonObject>
}

export interface RouteDecisionView extends TopologyEntityBase {
  kind: "route"
  routeId: string
  routeType: string
  nodeId?: string
  selectedWorkerId?: string
  selectedCandidateId?: string
  selectedProviderId?: string
  selectedModelId?: string
  selectedBackendId?: string
  selectedLocation: PlacementLocation
  previousRouteId?: string
  topK: number
  candidateCount: number
  candidates: readonly RouteCandidateView[]
  requiredCapabilities: readonly string[]
  requiredTools: readonly string[]
  reasons: readonly string[]
  accepted: boolean
  routeHealth: string
  routeChanged: boolean
  fixedCandidateSelection: boolean
  topologyMutation: boolean
  policyId?: string
  decisionId?: string
  resourceDecisionId?: string
  planDigest?: string
}

export interface ModelSplitSegment {
  id: string
  role: string
  providerId?: string
  modelId?: string
  backendId?: string
  location: PlacementLocation
  handoff?: string
  primary: boolean
  attributes: Readonly<JsonObject>
}

export interface SlaAssessment {
  budgetLimit?: number
  costEstimate?: number
  latencyLimitMs?: number
  latencyEstimateMs?: number
  costSatisfied?: boolean
  latencySatisfied?: boolean
  privacySatisfied?: boolean
  resourceSatisfied?: boolean
  satisfied: boolean
  violations: readonly string[]
}

export interface PlacementView extends TopologyEntityBase {
  kind: "placement"
  placementId: string
  routeId?: string
  nodeId?: string
  workerId?: string
  backendId?: string
  providerId?: string
  modelId?: string
  location: PlacementLocation
  privacyClass: string
  modelSplitStrategy: string
  modelSplit: readonly ModelSplitSegment[]
  resourceFit?: ResourceFitView
  costEstimate?: number
  latencyEstimateMs?: number
  sla: SlaAssessment
  previousPlacementId?: string
  changed: boolean
}

export interface CheckpointWriteView {
  id: string
  checkpointId: string
  taskKey: string
  channel: string
  state: "pending" | "committed" | "discarded" | "unknown"
  sequence: number
  writerId?: string
  idempotencyKey?: string
  valueDigest?: string
  branchId?: string
  evidence: TopologyEvidence
}

export interface CheckpointView extends TopologyEntityBase {
  kind: "checkpoint"
  checkpointId: string
  parentCheckpointId?: string
  ancestry: readonly string[]
  phase: string
  iteration: number
  workflowSignature?: string
  graphSignature?: string
  topologySignature?: string
  contentDigest?: string
  pendingWrites: readonly CheckpointWriteView[]
  committedWrites: readonly CheckpointWriteView[]
  pendingRequestIds: readonly string[]
  inflightMessageIds: readonly string[]
  completedStepIds: readonly string[]
  processedResponseIds: readonly string[]
  sideEffectFenceKeys: readonly string[]
  interruptIds: readonly string[]
  resumeIds: readonly string[]
  nextTaskIds: readonly string[]
  lineageValid: boolean
  visibilityValid: boolean
}

export interface BranchConflictView {
  id: string
  kind: string
  key: string
  branchId: string
  conflictingBranchId?: string
  baseRevision: number
  currentRevision: number
  reason: string
  recoverable: boolean
  evidence: TopologyEvidence
}

export interface BranchView extends TopologyEntityBase {
  kind: "branch"
  branchId: string
  deltaId?: string
  checkpointId?: string
  owner?: string
  baseRevision: number
  outcome: BranchOutcome
  strategy: string
  readSet: readonly string[]
  writeSet: readonly string[]
  entryIds: readonly string[]
  mutationIds: readonly string[]
  conflicts: readonly BranchConflictView[]
  rebasedFromRevision?: number
  deterministicOrderKey: string
  visibleInCanonicalState: boolean
}

export interface RequirementChangeView extends TopologyEntityBase {
  kind: "requirement_change"
  changeId: string
  sourceEventId: string
  text: string
  affectedNodeIds: readonly string[]
  supersededNodeIds: readonly string[]
  needsRevisionNodeIds: readonly string[]
  replanNodeId?: string
  decisionId?: string
  resourceDecisionId?: string
  routeIds: readonly string[]
  localReplan: boolean
  faultClassified: boolean
  causalClosureEventIds: readonly string[]
}

export interface NamespaceView extends TopologyEntityBase {
  kind: "namespace"
  namespaceId: string
  parentNamespaceId?: string
  nodeIds: readonly string[]
  childNamespaceIds: readonly string[]
  depth: number
}

export interface GraphComponentView {
  id: string
  nodeIds: readonly string[]
  edgeIds: readonly string[]
  rootNodeIds: readonly string[]
  leafNodeIds: readonly string[]
  cyclic: boolean
}

export interface CriticalPathView {
  nodeIds: readonly string[]
  edgeIds: readonly string[]
  weight: number
  complete: boolean
}

export interface TopologyGraphAnalysis {
  roots: readonly string[]
  leaves: readonly string[]
  isolated: readonly string[]
  cycles: readonly (readonly string[])[]
  components: readonly GraphComponentView[]
  topologicalOrder: readonly string[]
  criticalPath: CriticalPathView
  reachableByNode: Readonly<Record<string, readonly string[]>>
  ancestorsByNode: Readonly<Record<string, readonly string[]>>
  depthByNode: Readonly<Record<string, number>>
  missingNodeIds: readonly string[]
  duplicateEdgeIds: readonly string[]
}

export interface TopologyRouteMetrics {
  decisionCount: number
  selectedRouteCount: number
  candidateCount: number
  acceptedCandidateCount: number
  routeChangeCount: number
  topologyMutationCount: number
  openWorldMutationCount: number
  fixedCandidateSelectionCount: number
  placementChangeCount: number
  devicePlacementCount: number
  edgePlacementCount: number
  cloudPlacementCount: number
  rejectedBranchCount: number
  rebasedBranchCount: number
  pendingWriteCount: number
  committedWriteCount: number
  requirementChangeCount: number
  supersededNodeCount: number
  effectiveStepCount: number
  effectiveRouteTransitionCount: number
  routeDensity: number
  graphDensity: number
}

export interface TopologyProjectionDiagnostics {
  ready: boolean
  disabled: boolean
  snapshotComplete: boolean
  connected: boolean
  projectionRevision: number
  cursorGeneration: number
  committedSequence: number
  highWatermark: number
  lag: number
  duplicateEvents: number
  staleEvents: number
  missingSequences: readonly number[]
  warnings: readonly string[]
  errors: readonly string[]
  rejectedEntityIds: readonly string[]
  missingEvidenceEntityIds: readonly string[]
  ambiguousEntityIds: readonly string[]
  sensitiveFieldDrops: number
}

export interface TopologyProjectionView {
  schema: typeof TOPOLOGY_PROJECTION_SCHEMA
  taskId: string
  runIds: readonly string[]
  projectionRevision: number
  graphRevision: number
  commitRevision: number
  nodes: readonly TopologyNodeView[]
  edges: readonly TopologyEdgeView[]
  mutations: readonly TopologyMutationView[]
  routes: readonly RouteDecisionView[]
  placements: readonly PlacementView[]
  checkpoints: readonly CheckpointView[]
  branches: readonly BranchView[]
  conflicts: readonly BranchConflictView[]
  requirementChanges: readonly RequirementChangeView[]
  namespaces: readonly NamespaceView[]
  analysis: TopologyGraphAnalysis
  metrics: TopologyRouteMetrics
  diagnostics: TopologyProjectionDiagnostics
  evidenceByEntity: Readonly<Record<string, TopologyEvidence>>
}

export interface TopologyProjectionOptions {
  disabled?: boolean
  includeRemoved?: boolean
  includeRejected?: boolean
  includeNonEffective?: boolean
  maximumEntities?: number
  maximumEvidenceEvents?: number
  strict?: boolean
}

export interface NormalizedTopologyProjectionOptions {
  disabled: boolean
  includeRemoved: boolean
  includeRejected: boolean
  includeNonEffective: boolean
  maximumEntities: number
  maximumEvidenceEvents: number
  strict: boolean
}

export interface TopologyProjectionContext {
  state: CanonicalProjectionState
  taskId: string
  options: NormalizedTopologyProjectionOptions
  records: readonly ProjectionRecord[]
  evidence: TopologyEvidenceResolver
  sensitiveFieldDrops: { value: number }
}

export interface ProjectionRecord {
  id: string
  domain: string
  entityId: string
  taskId: string
  runId: string
  sequence: number
  revision: number
  lifecycle: string
  terminal: boolean
  effective: boolean
  removed: boolean
  title: string
  summary: string
  eventId: string
  mutationId?: string
  checkpointId?: string
  correlationId?: string
  causationId?: string
  spanId?: string
  parentSpanId?: string
  nodeId?: string
  workerId?: string
  attributes: Readonly<JsonObject>
  metadata: Readonly<JsonObject>
  raw: unknown
}

export interface TopologyEvidenceResolver {
  forRecord(record: ProjectionRecord, extraEventIds?: readonly string[]): TopologyEvidence
  forEventIds(eventIds: readonly string[]): TopologyEvidence
  merge(...items: readonly (TopologyEvidence | undefined)[]): TopologyEvidence
  relatedEventIds(seedIds: readonly string[], limit?: number): readonly string[]
}

export type TopologySelector = ProjectionSelector<TopologyProjectionView>

export interface TopologyProjectionEngineOptions extends TopologyProjectionOptions {
  onProject?: (view: TopologyProjectionView) => void
}

export class TopologyProjectionError extends Error {
  readonly code: string
  readonly details: Readonly<Record<string, JsonValue>>

  constructor(
    code: string,
    message: string,
    details: Readonly<Record<string, JsonValue>> = {},
  ) {
    super(message)
    this.name = "TopologyProjectionError"
    this.code = code
    this.details = Object.freeze({ ...details })
  }
}
