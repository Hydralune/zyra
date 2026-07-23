import type { CanonicalProjectionState } from "../../../state/contracts.ts"
import { projectionSelector } from "../../../state/selectors.ts"
import { analyzeTopologyGraph, calculateTopologyMetrics } from "./analysis.ts"
import { projectRequirementChanges } from "./changes.ts"
import { projectCheckpoints } from "./checkpoints.ts"
import {
  TOPOLOGY_PROJECTION_SCHEMA,
  TopologyProjectionError,
  type TopologyEvidence,
  type TopologyProjectionEngineOptions,
  type TopologyProjectionOptions,
  type TopologyProjectionView,
  type TopologySelector,
} from "./contracts.ts"
import { buildProjectionDiagnostics } from "./diagnostics.ts"
import { projectEdges } from "./edges.ts"
import { createEvidenceResolver } from "./evidence.ts"
import { projectNodes } from "./nodes.ts"
import { projectPlacements } from "./placements.ts"
import {
  collectProjectionRecords,
  deepFreeze,
  normalizeTopologyOptions,
} from "./record-reader.ts"
import { projectRoutes } from "./routes.ts"

export function buildTopologyProjection(
  state: CanonicalProjectionState,
  taskId: string,
  options: TopologyProjectionOptions = {},
): TopologyProjectionView {
  const normalized = normalizeTopologyOptions(options)
  if (normalized.disabled) {
    throw new TopologyProjectionError(
      "TOPOLOGY_PROJECTION_DISABLED",
      `Topology projection is disabled for task ${taskId}`,
      { task_id: taskId },
    )
  }
  const sensitiveFieldDrops = { value: 0 }
  const records = collectProjectionRecords(state, taskId, normalized)
  const evidence = createEvidenceResolver(
    state,
    taskId,
    normalized.maximumEvidenceEvents,
  )
  const context = {
    state,
    taskId,
    options: normalized,
    records,
    evidence,
    sensitiveFieldDrops,
  }
  const nodeResult = projectNodes(context)
  const edges = projectEdges(context, nodeResult.nodes, nodeResult.mutations)
  const routes = projectRoutes(context, nodeResult.nodes)
  const placements = projectPlacements(context, routes, nodeResult.nodes)
  const checkpointResult = projectCheckpoints(context)
  const changes = projectRequirementChanges(
    context,
    nodeResult.nodes,
    routes,
    nodeResult.mutations,
  )
  const nodes = filterEntities(nodeResult.nodes, normalized)
  const filteredEdges = filterEntities(edges, normalized)
  const filteredRoutes = filterEntities(routes, normalized)
  const filteredPlacements = filterEntities(placements, normalized)
  const checkpoints = filterEntities(checkpointResult.checkpoints, normalized)
  const branches = filterEntities(checkpointResult.branches, normalized)
  const requirementChanges = filterEntities(changes, normalized)
  enforceEntityLimit(
    normalized.maximumEntities,
    nodes.length +
      filteredEdges.length +
      filteredRoutes.length +
      filteredPlacements.length +
      checkpoints.length +
      branches.length +
      requirementChanges.length,
    taskId,
  )
  const analysis = analyzeTopologyGraph(nodes, filteredEdges)
  const evidenceByEntity = buildEvidenceIndex({
    nodes,
    edges: filteredEdges,
    routes: filteredRoutes,
    placements: filteredPlacements,
    checkpoints,
    branches,
    changes: requirementChanges,
    namespaces: nodeResult.namespaces,
    conflicts: checkpointResult.conflicts,
    mutations: nodeResult.mutations,
  })
  const diagnostics = buildProjectionDiagnostics(context, {
    nodes,
    edges: filteredEdges,
    routes: filteredRoutes,
    checkpoints,
    branches,
    changes: requirementChanges,
  })
  if (normalized.strict && diagnostics.errors.length > 0) {
    throw new TopologyProjectionError(
      "TOPOLOGY_PROJECTION_INTEGRITY_FAILED",
      `Topology projection integrity failed for task ${taskId}`,
      {
        task_id: taskId,
        errors: diagnostics.errors.join(","),
      },
    )
  }
  const graphRevision = maximum([
    ...nodes.map((node) => node.graphRevision),
    ...filteredEdges.map((edge) => edge.graphRevision),
    ...nodeResult.mutations.map((mutation) => mutation.committedRevision),
    ...filteredRoutes.map((route) => route.graphRevision),
    ...checkpoints.map((checkpoint) => checkpoint.graphRevision),
  ])
  const commitRevision = maximum([
    ...nodes.map((node) => node.commitRevision),
    ...filteredEdges.map((edge) => edge.commitRevision),
    ...filteredRoutes.map((route) => route.commitRevision),
    ...filteredPlacements.map((placement) => placement.commitRevision),
    ...checkpoints.map((checkpoint) => checkpoint.commitRevision),
    ...branches.map((branch) => branch.commitRevision),
  ])
  const partial = {
    schema: TOPOLOGY_PROJECTION_SCHEMA,
    taskId,
    runIds: Object.freeze(
      [...new Set(records.map((record) => record.runId).filter(Boolean))].sort(),
    ),
    projectionRevision: state.revision,
    graphRevision,
    commitRevision,
    nodes: Object.freeze(nodes),
    edges: Object.freeze(filteredEdges),
    mutations: Object.freeze(nodeResult.mutations),
    routes: Object.freeze(filteredRoutes),
    placements: Object.freeze(filteredPlacements),
    checkpoints: Object.freeze(checkpoints),
    branches: Object.freeze(branches),
    conflicts: Object.freeze(checkpointResult.conflicts),
    requirementChanges: Object.freeze(requirementChanges),
    namespaces: Object.freeze(nodeResult.namespaces),
    analysis,
    diagnostics,
    evidenceByEntity: Object.freeze(evidenceByEntity),
  }
  const metrics = calculateTopologyMetrics(partial)
  return deepFreeze({ ...partial, metrics })
}

export function selectTopologyProjection(
  taskId: string,
  options: TopologyProjectionOptions = {},
): TopologySelector {
  const normalized = normalizeTopologyOptions(options)
  return projectionSelector(
    `feature.topology.${taskId}.${JSON.stringify(normalized)}`,
    [
      `task:${taskId}`,
      `causal:task:${taskId}`,
      `cursor:${taskId}`,
      "domain:node",
      "domain:worker",
      "domain:scheduler",
      "domain:recovery",
      "domain:task",
      "domain:event",
    ],
    (state) => buildTopologyProjection(state, taskId, normalized),
    sameTopologyProjection,
  )
}

export class TopologyProjectionEngine {
  readonly options: TopologyProjectionEngineOptions

  constructor(options: TopologyProjectionEngineOptions = {}) {
    this.options = Object.freeze({ ...options })
  }

  project(
    state: CanonicalProjectionState,
    taskId: string,
    options: TopologyProjectionOptions = {},
  ): TopologyProjectionView {
    const view = buildTopologyProjection(state, taskId, {
      ...this.options,
      ...options,
    })
    this.options.onProject?.(view)
    return view
  }

  selector(
    taskId: string,
    options: TopologyProjectionOptions = {},
  ): TopologySelector {
    return selectTopologyProjection(taskId, {
      ...this.options,
      ...options,
    })
  }
}

function filterEntities<
  T extends {
    removed: boolean
    effective: boolean
    state: string
  },
>(
  values: readonly T[],
  options: ReturnType<typeof normalizeTopologyOptions>,
): T[] {
  return values.filter((value) => {
    if (!options.includeRemoved && value.removed) return false
    if (!options.includeNonEffective && !value.effective) return false
    if (
      !options.includeRejected &&
      ["rejected", "conflicted", "cancelled"].includes(value.state)
    ) {
      return false
    }
    return true
  })
}

function enforceEntityLimit(limit: number, count: number, taskId: string): void {
  if (count <= limit) return
  throw new TopologyProjectionError(
    "TOPOLOGY_PROJECTION_ENTITY_LIMIT",
    `Topology projection entity count ${count} exceeds limit ${limit}`,
    { task_id: taskId, entity_count: count, maximum_entities: limit },
  )
}

function buildEvidenceIndex(input: {
  nodes: TopologyProjectionView["nodes"]
  edges: TopologyProjectionView["edges"]
  routes: TopologyProjectionView["routes"]
  placements: TopologyProjectionView["placements"]
  checkpoints: TopologyProjectionView["checkpoints"]
  branches: TopologyProjectionView["branches"]
  changes: TopologyProjectionView["requirementChanges"]
  namespaces: TopologyProjectionView["namespaces"]
  conflicts: TopologyProjectionView["conflicts"]
  mutations: TopologyProjectionView["mutations"]
}): Record<string, TopologyEvidence> {
  const output: Record<string, TopologyEvidence> = {}
  for (const entity of [
    ...input.nodes,
    ...input.edges,
    ...input.routes,
    ...input.placements,
    ...input.checkpoints,
    ...input.branches,
    ...input.changes,
    ...input.namespaces,
  ]) {
    output[`${entity.kind}:${entity.id}`] = entity.evidence
  }
  for (const conflict of input.conflicts) {
    output[`conflict:${conflict.id}`] = conflict.evidence
  }
  for (const mutation of input.mutations) {
    output[`mutation:${mutation.id}`] = mutation.evidence
  }
  for (const route of input.routes) {
    for (const candidate of route.candidates) {
      output[`candidate:${candidate.id}`] = candidate.evidence
    }
  }
  return output
}

function maximum(values: readonly number[]): number {
  return values.length === 0 ? 0 : Math.max(...values)
}

function sameTopologyProjection(
  left: TopologyProjectionView,
  right: TopologyProjectionView,
): boolean {
  return (
    left.taskId === right.taskId &&
    left.projectionRevision === right.projectionRevision &&
    left.graphRevision === right.graphRevision &&
    left.commitRevision === right.commitRevision &&
    left.diagnostics.ready === right.diagnostics.ready
  )
}
