import type {
  BranchConflictView,
  BranchView,
  CheckpointView,
  PlacementLocation,
  PlacementView,
  RequirementChangeView,
  RouteDecisionView,
  TopologyEdgeView,
  TopologyEvidence,
  TopologyNodeView,
  TopologyProjectionView,
} from "../projection/index.ts"

export type GraphEntityKind =
  | "node"
  | "edge"
  | "cluster"
  | "route"
  | "placement"
  | "checkpoint"
  | "branch"
  | "change"

export type TopologyLayerId =
  | "structure"
  | "route-density"
  | "route-churn"
  | "broadcast"
  | "placement"
  | "provider-model"
  | "privacy"
  | "policy-violation"

export type TopologyStatusFilter =
  | "all"
  | "active"
  | "waiting"
  | "recovering"
  | "failed"
  | "terminal"
  | "changed"
  | "policy-risk"

export type TopologyControlAction =
  | "local-update"
  | "time-travel"
  | "resume-checkpoint"
  | "requirement-change"

export type ControlReceiptPhase =
  | "validating"
  | "submitting"
  | "pending"
  | "committed"
  | "denied"
  | "failed"
  | "cancelled"

export interface Point {
  x: number
  y: number
}

export interface Size {
  width: number
  height: number
}

export interface Rect extends Point, Size {}

export interface Insets {
  top: number
  right: number
  bottom: number
  left: number
}

export interface Transform {
  x: number
  y: number
  scale: number
}

export interface GraphPort {
  id: string
  point: Point
  side: "top" | "right" | "bottom" | "left"
}

export interface GraphNodeRecord {
  id: string
  title: string
  subtitle: string
  state: string
  role: string
  namespace: string
  depth: number
  sequence: number
  revision: number
  graphRevision: number
  commitRevision: number
  location: PlacementLocation
  workerCount: number
  dependencyCount: number
  childCount: number
  routeCount: number
  artifactCount: number
  capabilities: readonly string[]
  dependencyIds: readonly string[]
  childNodeIds: readonly string[]
  workerIds: readonly string[]
  routeIds: readonly string[]
  placementIds: readonly string[]
  artifactIds: readonly string[]
  affectedByChangeIds: readonly string[]
  missingDependencyIds: readonly string[]
  terminal: boolean
  pending: boolean
  removed: boolean
  effective: boolean
  openWorldCreated: boolean
  openWorldRemoved: boolean
  openWorldReplaced: boolean
  policyViolation: boolean
  privacyClass: string
  providerId?: string
  modelId?: string
  backendId?: string
  selectedRouteId?: string
  selectedPlacementId?: string
  evidence: TopologyEvidence
  source: TopologyNodeView
}

export interface GraphEdgeRecord {
  id: string
  sourceId: string
  targetId: string
  kind: string
  relation: string
  sequence: number
  revision: number
  graphRevision: number
  commitRevision: number
  weight: number
  missing: boolean
  inferred: boolean
  runtimeMutation: boolean
  effective: boolean
  pending: boolean
  removed: boolean
  routeIds: readonly string[]
  evidence: TopologyEvidence
  source: TopologyEdgeView
}

export interface GraphRouteRecord {
  id: string
  nodeId?: string
  selectedWorkerId?: string
  providerId?: string
  modelId?: string
  backendId?: string
  location: PlacementLocation
  state: string
  sequence: number
  revision: number
  changed: boolean
  accepted: boolean
  fixedCandidateSelection: boolean
  topologyMutation: boolean
  candidateCount: number
  acceptedCandidateCount: number
  rejectedCandidateCount: number
  reasons: readonly string[]
  policyId?: string
  evidence: TopologyEvidence
  source: RouteDecisionView
}

export interface GraphPlacementRecord {
  id: string
  nodeId?: string
  routeId?: string
  workerId?: string
  providerId?: string
  modelId?: string
  backendId?: string
  location: PlacementLocation
  privacyClass: string
  state: string
  sequence: number
  revision: number
  changed: boolean
  slaSatisfied: boolean
  violations: readonly string[]
  evidence: TopologyEvidence
  source: PlacementView
}

export interface GraphCheckpointRecord {
  id: string
  parentId?: string
  ancestry: readonly string[]
  state: string
  phase: string
  sequence: number
  revision: number
  graphRevision: number
  commitRevision: number
  pendingWrites: number
  committedWrites: number
  interruptCount: number
  resumeCount: number
  nextTaskCount: number
  lineageValid: boolean
  visibilityValid: boolean
  evidence: TopologyEvidence
  source: CheckpointView
}

export interface GraphBranchRecord {
  id: string
  checkpointId?: string
  state: string
  outcome: string
  strategy: string
  sequence: number
  revision: number
  baseRevision: number
  commitRevision: number
  conflictCount: number
  visibleInCanonicalState: boolean
  evidence: TopologyEvidence
  source: BranchView
}

export interface GraphChangeRecord {
  id: string
  text: string
  state: string
  sequence: number
  revision: number
  affectedNodeIds: readonly string[]
  supersededNodeIds: readonly string[]
  needsRevisionNodeIds: readonly string[]
  routeIds: readonly string[]
  replanNodeId?: string
  localReplan: boolean
  faultClassified: boolean
  evidence: TopologyEvidence
  source: RequirementChangeView
}

export interface TopologyGraphModel {
  taskId: string
  projectionRevision: number
  graphRevision: number
  commitRevision: number
  nodes: readonly GraphNodeRecord[]
  edges: readonly GraphEdgeRecord[]
  routes: readonly GraphRouteRecord[]
  placements: readonly GraphPlacementRecord[]
  checkpoints: readonly GraphCheckpointRecord[]
  branches: readonly GraphBranchRecord[]
  conflicts: readonly BranchConflictView[]
  changes: readonly GraphChangeRecord[]
  nodeById: ReadonlyMap<string, GraphNodeRecord>
  edgeById: ReadonlyMap<string, GraphEdgeRecord>
  routeById: ReadonlyMap<string, GraphRouteRecord>
  placementById: ReadonlyMap<string, GraphPlacementRecord>
  checkpointById: ReadonlyMap<string, GraphCheckpointRecord>
  branchById: ReadonlyMap<string, GraphBranchRecord>
  incomingByNode: ReadonlyMap<string, readonly string[]>
  outgoingByNode: ReadonlyMap<string, readonly string[]>
  routesByNode: ReadonlyMap<string, readonly string[]>
  placementsByNode: ReadonlyMap<string, readonly string[]>
  changesByNode: ReadonlyMap<string, readonly string[]>
  diagnostics: TopologyProjectionView["diagnostics"]
  metrics: TopologyProjectionView["metrics"]
  source: TopologyProjectionView
}

export interface LayoutNode {
  id: string
  center: Point
  size: Size
  bounds: Rect
  rank: number
  order: number
  component: number
  pinned: boolean
  stable: boolean
  clusterId?: string
}

export interface LayoutEdge {
  id: string
  sourceId: string
  targetId: string
  points: readonly Point[]
  bounds: Rect
  length: number
}

export interface LayoutSnapshot {
  revision: number
  graphRevision: number
  nodes: ReadonlyMap<string, LayoutNode>
  edges: ReadonlyMap<string, LayoutEdge>
  bounds: Rect
  addedNodeIds: readonly string[]
  movedNodeIds: readonly string[]
  removedNodeIds: readonly string[]
  stableNodeCount: number
  unstableNodeCount: number
  collisionCount: number
}

export interface SpatialItem {
  id: string
  kind: "node" | "edge" | "cluster"
  bounds: Rect
  zIndex: number
}

export interface SpatialQuery {
  viewport: Rect
  overscan: number
  maximum: number
  kinds?: readonly SpatialItem["kind"][]
}

export interface SpatialQueryResult {
  itemIds: readonly string[]
  nodeIds: readonly string[]
  edgeIds: readonly string[]
  clusterIds: readonly string[]
  visitedCells: number
  candidateCount: number
  clipped: boolean
}

export interface ClusterRecord {
  id: string
  level: number
  parentId?: string
  nodeIds: readonly string[]
  childClusterIds: readonly string[]
  bounds: Rect
  center: Point
  stateCounts: Readonly<Record<string, number>>
  locationCounts: Readonly<Record<string, number>>
  policyViolationCount: number
  changedCount: number
  routeCount: number
  density: number
}

export interface ClusterSnapshot {
  revision: number
  levels: readonly number[]
  clusters: readonly ClusterRecord[]
  clusterById: ReadonlyMap<string, ClusterRecord>
  nodeToCluster: ReadonlyMap<string, string>
}

export interface TopologyFilterState {
  query: string
  status: TopologyStatusFilter
  namespaces: readonly string[]
  roles: readonly string[]
  locations: readonly PlacementLocation[]
  providers: readonly string[]
  models: readonly string[]
  privacyClasses: readonly string[]
  capabilities: readonly string[]
  changedOnly: boolean
  openWorldOnly: boolean
  policyViolationsOnly: boolean
  includeRemoved: boolean
  includePending: boolean
  includeInferredEdges: boolean
}

export interface SearchMatch {
  entityId: string
  kind: GraphEntityKind
  score: number
  primary: string
  secondary: string
  matchedFields: readonly string[]
  ranges: readonly { start: number; end: number }[]
}

export interface FilterResult {
  nodeIds: readonly string[]
  edgeIds: readonly string[]
  routeIds: readonly string[]
  placementIds: readonly string[]
  checkpointIds: readonly string[]
  branchIds: readonly string[]
  changeIds: readonly string[]
  hiddenNodeCount: number
  orphanedVisibleEdgeCount: number
  searchMatches: readonly SearchMatch[]
}

export interface LayerDatum {
  entityId: string
  value: number
  normalized: number
  severity: "none" | "info" | "warning" | "critical"
  label: string
  details: readonly string[]
  colorToken: string
}

export interface LayerSnapshot {
  id: TopologyLayerId
  revision: number
  data: ReadonlyMap<string, LayerDatum>
  minimum: number
  maximum: number
  average: number
  visibleCount: number
  criticalCount: number
}

export interface TopologySelection {
  kind: GraphEntityKind
  id: string
  source: "pointer" | "keyboard" | "search" | "minimap" | "causation" | "programmatic"
  selectedAt: number
}

export interface NavigationIntent {
  id: string
  kind:
    | "select-node"
    | "select-route"
    | "select-placement"
    | "select-checkpoint"
    | "open-artifact"
    | "open-timeline"
    | "open-causation"
    | "focus-subgraph"
  taskId: string
  entityId: string
  artifactId?: string
  eventId?: string
  causationId?: string
  correlationId?: string
  nodeId?: string
  sequence?: number
  evidence: TopologyEvidence
}

export interface ViewportSnapshot {
  viewport: Rect
  transform: Transform
  graphBounds: Rect
  minimumScale: number
  maximumScale: number
  interacting: boolean
  pointerId?: number
  focusEntityId?: string
  hoverEntityId?: string
  selected?: TopologySelection
  expandedClusterIds: readonly string[]
  lastInput: "pointer" | "wheel" | "keyboard" | "minimap" | "programmatic"
  revision: number
}

export interface RenderNode {
  node: GraphNodeRecord
  layout: LayoutNode
  selected: boolean
  hovered: boolean
  focused: boolean
  dimmed: boolean
  layer?: LayerDatum
}

export interface RenderEdge {
  edge: GraphEdgeRecord
  layout: LayoutEdge
  selected: boolean
  highlighted: boolean
  dimmed: boolean
  layer?: LayerDatum
}

export interface RenderCluster {
  cluster: ClusterRecord
  selected: boolean
  hovered: boolean
  dimmed: boolean
  layer?: LayerDatum
}

export interface RenderPlan {
  revision: number
  nodes: readonly RenderNode[]
  edges: readonly RenderEdge[]
  clusters: readonly RenderCluster[]
  totalNodeCount: number
  totalEdgeCount: number
  virtualizedNodeCount: number
  virtualizedEdgeCount: number
  clipped: boolean
  lodLevel: number
  query: SpatialQueryResult
}

export interface TopologyControlRequest {
  action: TopologyControlAction
  taskId: string
  runId: string
  sessionId?: string
  nodeId?: string
  checkpointId?: string
  expectedRevision?: number
  text?: string
  fields?: Readonly<Record<string, unknown>>
  sealed: boolean
  actorId: string
}

export interface TopologyControlReceipt {
  id: string
  action: TopologyControlAction
  phase: ControlReceiptPhase
  taskId: string
  runId: string
  commandName: string
  requestId: string
  commandId: string
  idempotencyKey: string
  checkpointId?: string
  expectedRevision?: number
  observedRevision?: number
  observedEventIds: readonly string[]
  submittedAt: number
  updatedAt: number
  summary: string
  errorCode?: string
  errorMessage?: string
  denied: boolean
  interventionCounted: boolean
  replayed: boolean
  raw?: Readonly<Record<string, unknown>>
}

export interface TopologyControllerSnapshot {
  model: TopologyGraphModel
  layout: LayoutSnapshot
  clusters: ClusterSnapshot
  filters: TopologyFilterState
  filtered: FilterResult
  layers: ReadonlyMap<TopologyLayerId, LayerSnapshot>
  activeLayer: TopologyLayerId
  viewport: ViewportSnapshot
  renderPlan: RenderPlan
  receipts: readonly TopologyControlReceipt[]
  selectedDetails?: TopologyEntityDetails
  loading: boolean
  reconnecting: boolean
  error?: string
  revision: number
}

export interface TopologyEntityDetails {
  kind: GraphEntityKind
  id: string
  title: string
  subtitle: string
  state: string
  facts: readonly { label: string; value: string; tone?: string }[]
  relatedEntityIds: readonly string[]
  artifactIds: readonly string[]
  eventIds: readonly string[]
  mutationIds: readonly string[]
  checkpointIds: readonly string[]
  actions: readonly NavigationIntent[]
  raw:
    | GraphNodeRecord
    | GraphEdgeRecord
    | GraphRouteRecord
    | GraphPlacementRecord
    | GraphCheckpointRecord
    | GraphBranchRecord
    | GraphChangeRecord
    | ClusterRecord
}

export interface TopologyControlTransport {
  submit(request: TopologyControlRequest, signal?: AbortSignal): Promise<Readonly<Record<string, unknown>>>
}

export interface TopologyControllerOptions {
  initialViewport?: Partial<Rect>
  initialTransform?: Partial<Transform>
  maximumRenderedNodes?: number
  maximumRenderedEdges?: number
  largeGraphThreshold?: number
  clusterThreshold?: number
  now?: () => number
  controlTransport?: TopologyControlTransport
}

export type TopologyListener = () => void
