import type {
  BranchView,
  PlacementView,
  RouteDecisionView,
  TopologyEvidence,
  TopologyNodeView,
  TopologyProjectionView,
} from "../projection/index.ts"
import type {
  GraphBranchRecord,
  GraphChangeRecord,
  GraphCheckpointRecord,
  GraphEdgeRecord,
  GraphNodeRecord,
  GraphPlacementRecord,
  GraphRouteRecord,
  TopologyEntityDetails,
  TopologyGraphModel,
} from "./contracts.ts"

const EMPTY_STRINGS = Object.freeze([]) as readonly string[]

function freezeArray<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}
function sortedUnique(values: Iterable<string>): readonly string[] {
  return freezeArray([...new Set([...values].filter(Boolean))].sort())
}

function indexMany(
  entries: Iterable<readonly [string | undefined, string]>,
): ReadonlyMap<string, readonly string[]> {
  const result = new Map<string, string[]>()
  for (const [key, value] of entries) {
    if (!key || !value) continue
    const existing = result.get(key)
    if (existing) existing.push(value)
    else result.set(key, [value])
  }
  const frozen = new Map<string, readonly string[]>()
  for (const [key, values] of result) frozen.set(key, sortedUnique(values))
  return frozen
}

function latestRouteForNode(
  nodeId: string,
  routes: readonly RouteDecisionView[],
): RouteDecisionView | undefined {
  let selected: RouteDecisionView | undefined
  for (const route of routes) {
    if (route.nodeId !== nodeId) continue
    if (
      !selected ||
      route.sequence > selected.sequence ||
      (route.sequence === selected.sequence && route.revision > selected.revision) ||
      (route.sequence === selected.sequence &&
        route.revision === selected.revision &&
        route.id.localeCompare(selected.id) > 0)
    ) {
      selected = route
    }
  }
  return selected
}

function latestPlacementForNode(
  nodeId: string,
  placements: readonly PlacementView[],
): PlacementView | undefined {
  let selected: PlacementView | undefined
  for (const placement of placements) {
    if (placement.nodeId !== nodeId) continue
    if (
      !selected ||
      placement.sequence > selected.sequence ||
      (placement.sequence === selected.sequence &&
        placement.revision > selected.revision) ||
      (placement.sequence === selected.sequence &&
        placement.revision === selected.revision &&
        placement.id.localeCompare(selected.id) > 0)
    ) {
      selected = placement
    }
  }
  return selected
}

function nodePolicyViolation(
  node: TopologyNodeView,
  route: RouteDecisionView | undefined,
  placement: PlacementView | undefined,
): boolean {
  if (node.state === "rejected" || node.state === "conflicted") return true
  if (node.missingDependencyIds.length > 0) return true
  if (route && (!route.accepted || route.routeHealth === "failed")) return true
  if (placement && !placement.sla.satisfied) return true
  return false
}

function nodeRecord(
  source: TopologyNodeView,
  routes: readonly RouteDecisionView[],
  placements: readonly PlacementView[],
  depth: number,
): GraphNodeRecord {
  const route = latestRouteForNode(source.id, routes)
  const placement = latestPlacementForNode(source.id, placements)
  return Object.freeze({
    id: source.id,
    title: source.title || source.id,
    subtitle: source.summary || source.role || source.namespace,
    state: source.state,
    role: source.role || "unassigned",
    namespace: source.namespace || "root",
    depth,
    sequence: source.sequence,
    revision: source.revision,
    graphRevision: source.graphRevision,
    commitRevision: source.commitRevision,
    location: placement?.location ?? route?.selectedLocation ?? "unknown",
    workerCount: source.workerIds.length,
    dependencyCount: source.dependencies.length,
    childCount: source.childNodeIds.length,
    routeCount: source.routeIds.length,
    artifactCount: source.artifactIds.length,
    capabilities: freezeArray(source.capabilities),
    dependencyIds: freezeArray(source.dependencies),
    childNodeIds: freezeArray(source.childNodeIds),
    workerIds: freezeArray(source.workerIds),
    routeIds: freezeArray(source.routeIds),
    placementIds: freezeArray(source.placementIds),
    artifactIds: freezeArray(source.artifactIds),
    affectedByChangeIds: freezeArray(source.affectedByChangeIds),
    missingDependencyIds: freezeArray(source.missingDependencyIds),
    terminal: source.terminal,
    pending: source.pending,
    removed: source.removed,
    effective: source.effective,
    openWorldCreated: source.openWorldCreated,
    openWorldRemoved: source.openWorldRemoved,
    openWorldReplaced: source.openWorldReplaced,
    policyViolation: nodePolicyViolation(source, route, placement),
    privacyClass: placement?.privacyClass ?? "unclassified",
    providerId: placement?.providerId ?? route?.selectedProviderId,
    modelId: placement?.modelId ?? route?.selectedModelId,
    backendId: placement?.backendId ?? route?.selectedBackendId,
    selectedRouteId: route?.id,
    selectedPlacementId: placement?.id,
    evidence: source.evidence,
    source,
  })
}

function edgeRecord(
  source: TopologyProjectionView["edges"][number],
): GraphEdgeRecord {
  return Object.freeze({
    id: source.id,
    sourceId: source.sourceId,
    targetId: source.targetId,
    kind: source.edgeKind,
    relation: source.relation,
    sequence: source.sequence,
    revision: source.revision,
    graphRevision: source.graphRevision,
    commitRevision: source.commitRevision,
    weight: Number.isFinite(source.weight) ? Number(source.weight) : 1,
    missing: source.sourceMissing || source.targetMissing,
    inferred: source.inferred,
    runtimeMutation: source.runtimeMutation,
    effective: source.effective,
    pending: source.pending,
    removed: source.removed,
    routeIds: sortedUnique(
      source.evidence.entityRefs
        .filter((value) => value.startsWith("route:"))
        .map((value) => value.slice("route:".length)),
    ),
    evidence: source.evidence,
    source,
  })
}

function routeRecord(source: RouteDecisionView): GraphRouteRecord {
  const acceptedCandidateCount = source.candidates.filter(
    (candidate) => candidate.accepted,
  ).length
  return Object.freeze({
    id: source.id,
    nodeId: source.nodeId,
    selectedWorkerId: source.selectedWorkerId,
    providerId: source.selectedProviderId,
    modelId: source.selectedModelId,
    backendId: source.selectedBackendId,
    location: source.selectedLocation,
    state: source.state,
    sequence: source.sequence,
    revision: source.revision,
    changed: source.routeChanged,
    accepted: source.accepted,
    fixedCandidateSelection: source.fixedCandidateSelection,
    topologyMutation: source.topologyMutation,
    candidateCount: source.candidateCount,
    acceptedCandidateCount,
    rejectedCandidateCount: Math.max(0, source.candidateCount - acceptedCandidateCount),
    reasons: freezeArray(source.reasons),
    policyId: source.policyId,
    evidence: source.evidence,
    source,
  })
}

function placementRecord(source: PlacementView): GraphPlacementRecord {
  return Object.freeze({
    id: source.id,
    nodeId: source.nodeId,
    routeId: source.routeId,
    workerId: source.workerId,
    providerId: source.providerId,
    modelId: source.modelId,
    backendId: source.backendId,
    location: source.location,
    privacyClass: source.privacyClass,
    state: source.state,
    sequence: source.sequence,
    revision: source.revision,
    changed: source.changed,
    slaSatisfied: source.sla.satisfied,
    violations: freezeArray(source.sla.violations),
    evidence: source.evidence,
    source,
  })
}

function checkpointRecord(
  source: TopologyProjectionView["checkpoints"][number],
): GraphCheckpointRecord {
  return Object.freeze({
    id: source.checkpointId,
    parentId: source.parentCheckpointId,
    ancestry: freezeArray(source.ancestry),
    state: source.state,
    phase: source.phase,
    sequence: source.sequence,
    revision: source.revision,
    graphRevision: source.graphRevision,
    commitRevision: source.commitRevision,
    pendingWrites: source.pendingWrites.length,
    committedWrites: source.committedWrites.length,
    interruptCount: source.interruptIds.length,
    resumeCount: source.resumeIds.length,
    nextTaskCount: source.nextTaskIds.length,
    lineageValid: source.lineageValid,
    visibilityValid: source.visibilityValid,
    evidence: source.evidence,
    source,
  })
}

function branchRecord(source: BranchView): GraphBranchRecord {
  return Object.freeze({
    id: source.branchId,
    checkpointId: source.checkpointId,
    state: source.state,
    outcome: source.outcome,
    strategy: source.strategy,
    sequence: source.sequence,
    revision: source.revision,
    baseRevision: source.baseRevision,
    commitRevision: source.commitRevision,
    conflictCount: source.conflicts.length,
    visibleInCanonicalState: source.visibleInCanonicalState,
    evidence: source.evidence,
    source,
  })
}

function changeRecord(
  source: TopologyProjectionView["requirementChanges"][number],
): GraphChangeRecord {
  return Object.freeze({
    id: source.changeId,
    text: source.text,
    state: source.state,
    sequence: source.sequence,
    revision: source.revision,
    affectedNodeIds: freezeArray(source.affectedNodeIds),
    supersededNodeIds: freezeArray(source.supersededNodeIds),
    needsRevisionNodeIds: freezeArray(source.needsRevisionNodeIds),
    routeIds: freezeArray(source.routeIds),
    replanNodeId: source.replanNodeId,
    localReplan: source.localReplan,
    faultClassified: source.faultClassified,
    evidence: source.evidence,
    source,
  })
}

function mapById<T extends { id: string }>(values: readonly T[]): ReadonlyMap<string, T> {
  const result = new Map<string, T>()
  for (const value of values) {
    const existing = result.get(value.id)
    if (!existing) {
      result.set(value.id, value)
      continue
    }
    const a = existing as T & { sequence?: number; revision?: number }
    const b = value as T & { sequence?: number; revision?: number }
    if (
      (b.sequence ?? 0) > (a.sequence ?? 0) ||
      ((b.sequence ?? 0) === (a.sequence ?? 0) &&
        (b.revision ?? 0) >= (a.revision ?? 0))
    ) {
      result.set(value.id, value)
    }
  }
  return result
}

export function buildTopologyGraphModel(
  source: TopologyProjectionView,
): TopologyGraphModel {
  const nodes = freezeArray(
    source.nodes
      .map((node) =>
        nodeRecord(
          node,
          source.routes,
          source.placements,
          source.analysis.depthByNode[node.id] ?? 0,
        ),
      )
      .sort(compareNode),
  )
  const edges = freezeArray(source.edges.map(edgeRecord).sort(compareEdge))
  const routes = freezeArray(source.routes.map(routeRecord).sort(compareRecent))
  const placements = freezeArray(
    source.placements.map(placementRecord).sort(compareRecent),
  )
  const checkpoints = freezeArray(
    source.checkpoints.map(checkpointRecord).sort(compareRecent),
  )
  const branches = freezeArray(source.branches.map(branchRecord).sort(compareRecent))
  const changes = freezeArray(
    source.requirementChanges.map(changeRecord).sort(compareRecent),
  )
  const incomingByNode = indexMany(edges.map((edge) => [edge.targetId, edge.id] as const))
  const outgoingByNode = indexMany(edges.map((edge) => [edge.sourceId, edge.id] as const))
  const routesByNode = indexMany(routes.map((route) => [route.nodeId, route.id] as const))
  const placementsByNode = indexMany(
    placements.map((placement) => [placement.nodeId, placement.id] as const),
  )
  const changesByNode = indexMany(
    changes.flatMap((change) =>
      change.affectedNodeIds.map((nodeId) => [nodeId, change.id] as const),
    ),
  )
  return Object.freeze({
    taskId: source.taskId,
    projectionRevision: source.projectionRevision,
    graphRevision: source.graphRevision,
    commitRevision: source.commitRevision,
    nodes,
    edges,
    routes,
    placements,
    checkpoints,
    branches,
    conflicts: freezeArray(source.conflicts),
    changes,
    nodeById: mapById(nodes),
    edgeById: mapById(edges),
    routeById: mapById(routes),
    placementById: mapById(placements),
    checkpointById: mapById(checkpoints),
    branchById: mapById(branches),
    incomingByNode,
    outgoingByNode,
    routesByNode,
    placementsByNode,
    changesByNode,
    diagnostics: source.diagnostics,
    metrics: source.metrics,
    source,
  })
}

function compareNode(left: GraphNodeRecord, right: GraphNodeRecord): number {
  return (
    left.depth - right.depth ||
    left.sequence - right.sequence ||
    left.id.localeCompare(right.id)
  )
}

function compareEdge(left: GraphEdgeRecord, right: GraphEdgeRecord): number {
  return (
    left.sourceId.localeCompare(right.sourceId) ||
    left.targetId.localeCompare(right.targetId) ||
    left.kind.localeCompare(right.kind) ||
    left.id.localeCompare(right.id)
  )
}

function compareRecent(
  left: { sequence: number; revision: number; id: string },
  right: { sequence: number; revision: number; id: string },
): number {
  return (
    right.sequence - left.sequence ||
    right.revision - left.revision ||
    left.id.localeCompare(right.id)
  )
}

function evidenceActions(
  taskId: string,
  entityId: string,
  evidence: TopologyEvidence,
): TopologyEntityDetails["actions"] {
  const result: TopologyEntityDetails["actions"][number][] = []
  for (const artifactId of evidence.entityRefs
    .filter((value) => value.startsWith("artifact:"))
    .map((value) => value.slice("artifact:".length))) {
    result.push(
      Object.freeze({
        id: `artifact:${entityId}:${artifactId}`,
        kind: "open-artifact",
        taskId,
        entityId,
        artifactId,
        evidence,
      }),
    )
  }
  for (const eventId of evidence.eventIds.slice(-16)) {
    result.push(
      Object.freeze({
        id: `timeline:${entityId}:${eventId}`,
        kind: "open-timeline",
        taskId,
        entityId,
        eventId,
        sequence: evidence.lastSequence,
        evidence,
      }),
    )
  }
  for (const causationId of evidence.causationIds.slice(-16)) {
    result.push(
      Object.freeze({
        id: `causation:${entityId}:${causationId}`,
        kind: "open-causation",
        taskId,
        entityId,
        causationId,
        evidence,
      }),
    )
  }
  return freezeArray(result)
}

function evidenceArtifactIds(evidence: TopologyEvidence): readonly string[] {
  return sortedUnique(
    evidence.entityRefs
      .filter((value) => value.startsWith("artifact:"))
      .map((value) => value.slice("artifact:".length)),
  )
}

function baseDetails(
  model: TopologyGraphModel,
  kind: TopologyEntityDetails["kind"],
  raw: TopologyEntityDetails["raw"],
  title: string,
  subtitle: string,
  state: string,
  facts: TopologyEntityDetails["facts"],
  relatedEntityIds: readonly string[],
  evidence: TopologyEvidence,
): TopologyEntityDetails {
  return Object.freeze({
    kind,
    id: raw.id,
    title,
    subtitle,
    state,
    facts: freezeArray(facts),
    relatedEntityIds: sortedUnique(relatedEntityIds),
    artifactIds: evidenceArtifactIds(evidence),
    eventIds: freezeArray(evidence.eventIds),
    mutationIds: freezeArray(evidence.mutationIds),
    checkpointIds: freezeArray(evidence.checkpointIds),
    actions: evidenceActions(model.taskId, raw.id, evidence),
    raw,
  })
}

export function topologyEntityDetails(
  model: TopologyGraphModel,
  kind: TopologyEntityDetails["kind"],
  id: string,
  cluster?: TopologyEntityDetails["raw"],
): TopologyEntityDetails | undefined {
  if (kind === "node") {
    const node = model.nodeById.get(id)
    if (!node) return undefined
    const incoming = model.incomingByNode.get(id) ?? EMPTY_STRINGS
    const outgoing = model.outgoingByNode.get(id) ?? EMPTY_STRINGS
    return baseDetails(
      model,
      kind,
      node,
      node.title,
      node.subtitle,
      node.state,
      [
        { label: "Role", value: node.role },
        { label: "Namespace", value: node.namespace },
        { label: "Placement", value: node.location },
        { label: "Provider", value: node.providerId ?? "—" },
        { label: "Model", value: node.modelId ?? "—" },
        { label: "Graph revision", value: String(node.graphRevision) },
        { label: "Commit revision", value: String(node.commitRevision) },
        { label: "Dependencies", value: String(node.dependencyCount) },
        { label: "Workers", value: String(node.workerCount) },
        {
          label: "Policy",
          value: node.policyViolation ? "violation" : "satisfied",
          tone: node.policyViolation ? "danger" : "success",
        },
      ],
      [
        ...incoming,
        ...outgoing,
        ...node.dependencyIds,
        ...node.childNodeIds,
        ...node.routeIds,
        ...node.placementIds,
        ...node.affectedByChangeIds,
      ],
      node.evidence,
    )
  }
  if (kind === "edge") {
    const edge = model.edgeById.get(id)
    if (!edge) return undefined
    return baseDetails(
      model,
      kind,
      edge,
      `${edge.sourceId} → ${edge.targetId}`,
      edge.relation || edge.kind,
      edge.missing ? "missing-endpoint" : edge.runtimeMutation ? "runtime-mutated" : "stable",
      [
        { label: "Kind", value: edge.kind },
        { label: "Relation", value: edge.relation },
        { label: "Weight", value: String(edge.weight) },
        { label: "Runtime mutation", value: edge.runtimeMutation ? "yes" : "no" },
        { label: "Inferred", value: edge.inferred ? "yes" : "no" },
        { label: "Graph revision", value: String(edge.graphRevision) },
      ],
      [edge.sourceId, edge.targetId, ...edge.routeIds],
      edge.evidence,
    )
  }
  if (kind === "route") {
    const route = model.routeById.get(id)
    if (!route) return undefined
    return baseDetails(
      model,
      kind,
      route,
      `Route ${route.id}`,
      route.reasons.join(" · ") || route.location,
      route.state,
      [
        { label: "Node", value: route.nodeId ?? "—" },
        { label: "Worker", value: route.selectedWorkerId ?? "—" },
        { label: "Location", value: route.location },
        { label: "Provider", value: route.providerId ?? "—" },
        { label: "Model", value: route.modelId ?? "—" },
        { label: "Candidates", value: String(route.candidateCount) },
        { label: "Accepted", value: String(route.acceptedCandidateCount) },
        { label: "Topology mutation", value: route.topologyMutation ? "yes" : "no" },
      ],
      [route.nodeId ?? "", route.selectedWorkerId ?? "", route.backendId ?? ""],
      route.evidence,
    )
  }
  if (kind === "placement") {
    const placement = model.placementById.get(id)
    if (!placement) return undefined
    return baseDetails(
      model,
      kind,
      placement,
      `Placement ${placement.id}`,
      `${placement.location} · ${placement.privacyClass}`,
      placement.state,
      [
        { label: "Node", value: placement.nodeId ?? "—" },
        { label: "Route", value: placement.routeId ?? "—" },
        { label: "Worker", value: placement.workerId ?? "—" },
        { label: "Provider", value: placement.providerId ?? "—" },
        { label: "Model", value: placement.modelId ?? "—" },
        { label: "Privacy", value: placement.privacyClass },
        {
          label: "SLA",
          value: placement.slaSatisfied ? "satisfied" : placement.violations.join(", "),
          tone: placement.slaSatisfied ? "success" : "danger",
        },
      ],
      [placement.nodeId ?? "", placement.routeId ?? "", placement.workerId ?? ""],
      placement.evidence,
    )
  }
  if (kind === "checkpoint") {
    const checkpoint = model.checkpointById.get(id)
    if (!checkpoint) return undefined
    return baseDetails(
      model,
      kind,
      checkpoint,
      `Checkpoint ${checkpoint.id}`,
      checkpoint.phase,
      checkpoint.state,
      [
        { label: "Parent", value: checkpoint.parentId ?? "root" },
        { label: "Graph revision", value: String(checkpoint.graphRevision) },
        { label: "Commit revision", value: String(checkpoint.commitRevision) },
        { label: "Pending writes", value: String(checkpoint.pendingWrites) },
        { label: "Committed writes", value: String(checkpoint.committedWrites) },
        { label: "Interrupts", value: String(checkpoint.interruptCount) },
        { label: "Resumes", value: String(checkpoint.resumeCount) },
        { label: "Next tasks", value: String(checkpoint.nextTaskCount) },
        {
          label: "Lineage",
          value: checkpoint.lineageValid ? "valid" : "invalid",
          tone: checkpoint.lineageValid ? "success" : "danger",
        },
        {
          label: "Visibility",
          value: checkpoint.visibilityValid ? "valid" : "invalid",
          tone: checkpoint.visibilityValid ? "success" : "danger",
        },
      ],
      [checkpoint.parentId ?? "", ...checkpoint.ancestry],
      checkpoint.evidence,
    )
  }
  if (kind === "branch") {
    const branch = model.branchById.get(id)
    if (!branch) return undefined
    return baseDetails(
      model,
      kind,
      branch,
      `Branch ${branch.id}`,
      branch.strategy,
      branch.outcome,
      [
        { label: "Checkpoint", value: branch.checkpointId ?? "—" },
        { label: "Base revision", value: String(branch.baseRevision) },
        { label: "Commit revision", value: String(branch.commitRevision) },
        { label: "Conflicts", value: String(branch.conflictCount) },
        {
          label: "Canonical visibility",
          value: branch.visibleInCanonicalState ? "visible" : "isolated",
        },
      ],
      [branch.checkpointId ?? ""],
      branch.evidence,
    )
  }
  if (kind === "change") {
    const change = model.changes.find((candidate) => candidate.id === id)
    if (!change) return undefined
    return baseDetails(
      model,
      kind,
      change,
      `Requirement change ${change.id}`,
      change.text,
      change.state,
      [
        { label: "Affected nodes", value: String(change.affectedNodeIds.length) },
        { label: "Superseded", value: String(change.supersededNodeIds.length) },
        { label: "Needs revision", value: String(change.needsRevisionNodeIds.length) },
        { label: "Replan node", value: change.replanNodeId ?? "—" },
        { label: "Local replan", value: change.localReplan ? "yes" : "no" },
        { label: "Fault classified", value: change.faultClassified ? "yes" : "no" },
      ],
      [
        ...change.affectedNodeIds,
        ...change.supersededNodeIds,
        ...change.needsRevisionNodeIds,
        ...change.routeIds,
      ],
      change.evidence,
    )
  }
  if (kind === "cluster" && cluster && "nodeIds" in cluster) {
    const value = cluster as Extract<TopologyEntityDetails["raw"], { nodeIds: readonly string[] }>
    return Object.freeze({
      kind,
      id: value.id,
      title: `Cluster ${value.id}`,
      subtitle: `${value.nodeIds.length} nodes`,
      state: "aggregate",
      facts: freezeArray([
        { label: "Nodes", value: String(value.nodeIds.length) },
        { label: "Route density", value: String(value.density) },
        { label: "Policy violations", value: String(value.policyViolationCount) },
        { label: "Changed", value: String(value.changedCount) },
      ]),
      relatedEntityIds: freezeArray(value.nodeIds),
      artifactIds: EMPTY_STRINGS,
      eventIds: EMPTY_STRINGS,
      mutationIds: EMPTY_STRINGS,
      checkpointIds: EMPTY_STRINGS,
      actions: EMPTY_STRINGS as never,
      raw: value,
    })
  }
  return undefined
}
