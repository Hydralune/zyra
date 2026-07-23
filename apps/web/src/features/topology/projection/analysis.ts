import type {
  CriticalPathView,
  GraphComponentView,
  TopologyEdgeView,
  TopologyGraphAnalysis,
  TopologyNodeView,
  TopologyProjectionView,
  TopologyRouteMetrics,
} from "./contracts.ts"
import { uniqueStrings } from "./record-reader.ts"

interface GraphIndex {
  nodeIds: readonly string[]
  nodeById: ReadonlyMap<string, TopologyNodeView>
  outgoing: ReadonlyMap<string, readonly TopologyEdgeView[]>
  incoming: ReadonlyMap<string, readonly TopologyEdgeView[]>
  neighbors: ReadonlyMap<string, readonly string[]>
  missingNodeIds: readonly string[]
  duplicateEdgeIds: readonly string[]
}

export function analyzeTopologyGraph(
  nodes: readonly TopologyNodeView[],
  edges: readonly TopologyEdgeView[],
): TopologyGraphAnalysis {
  const index = buildGraphIndex(nodes, edges)
  const roots = index.nodeIds.filter(
    (id) => (index.incoming.get(id) ?? []).length === 0,
  )
  const leaves = index.nodeIds.filter(
    (id) => (index.outgoing.get(id) ?? []).length === 0,
  )
  const isolated = index.nodeIds.filter(
    (id) =>
      (index.incoming.get(id) ?? []).length === 0 &&
      (index.outgoing.get(id) ?? []).length === 0,
  )
  const stronglyConnected = stronglyConnectedComponents(index)
  const cycles = normalizeCycles(
    stronglyConnected
      .filter(
        (component) =>
          component.length > 1 ||
          hasSelfLoop(component[0]!, index.outgoing.get(component[0]!) ?? []),
      )
      .map((component) => cycleForComponent(component, index)),
  )
  const components = weakComponents(index, cycles)
  const topologicalOrder = deterministicTopologicalOrder(index)
  const depthByNode = computeDepths(index, topologicalOrder)
  const reachableByNode: Record<string, readonly string[]> = {}
  const ancestorsByNode: Record<string, readonly string[]> = {}
  for (const nodeId of index.nodeIds) {
    reachableByNode[nodeId] = Object.freeze(reachable(nodeId, index.outgoing))
    ancestorsByNode[nodeId] = Object.freeze(reachable(nodeId, index.incoming))
  }
  return Object.freeze({
    roots: Object.freeze(roots),
    leaves: Object.freeze(leaves),
    isolated: Object.freeze(isolated),
    cycles: Object.freeze(cycles.map((cycle) => Object.freeze(cycle))),
    components: Object.freeze(components),
    topologicalOrder: Object.freeze(topologicalOrder),
    criticalPath: Object.freeze(
      criticalPath(index, topologicalOrder, cycles.length === 0),
    ),
    reachableByNode: Object.freeze(reachableByNode),
    ancestorsByNode: Object.freeze(ancestorsByNode),
    depthByNode: Object.freeze(depthByNode),
    missingNodeIds: Object.freeze(index.missingNodeIds),
    duplicateEdgeIds: Object.freeze(index.duplicateEdgeIds),
  })
}

export function calculateTopologyMetrics(
  view: Pick<
    TopologyProjectionView,
    | "nodes"
    | "edges"
    | "mutations"
    | "routes"
    | "placements"
    | "branches"
    | "checkpoints"
    | "requirementChanges"
  >,
): TopologyRouteMetrics {
  const graphNodeCount = view.nodes.filter((node) => !node.removed).length
  const graphEdgeCount = view.edges.filter(
    (edge) => !edge.removed && edge.edgeKind !== "route" && edge.edgeKind !== "placement",
  ).length
  const maximumGraphEdges =
    graphNodeCount <= 1 ? 0 : graphNodeCount * (graphNodeCount - 1)
  const routeCandidates = view.routes.flatMap((route) => route.candidates)
  const effectiveStepKeys = new Set<string>()
  for (const node of view.nodes) {
    if (node.effective && !node.removed) {
      effectiveStepKeys.add(`node:${node.id}:${node.revision}:${node.state}`)
    }
  }
  for (const route of view.routes) {
    if (route.effective && route.accepted) {
      effectiveStepKeys.add(`route:${route.routeId}:${route.revision}`)
    }
  }
  for (const placement of view.placements) {
    if (placement.effective && placement.sla.satisfied) {
      effectiveStepKeys.add(`placement:${placement.placementId}:${placement.revision}`)
    }
  }
  for (const checkpoint of view.checkpoints) {
    if (checkpoint.effective && checkpoint.committedWrites.length > 0) {
      effectiveStepKeys.add(
        `checkpoint:${checkpoint.checkpointId}:${checkpoint.commitRevision}`,
      )
    }
  }
  return Object.freeze({
    decisionCount: view.routes.length,
    selectedRouteCount: view.routes.filter((route) => route.selectedCandidateId).length,
    candidateCount: routeCandidates.length,
    acceptedCandidateCount: routeCandidates.filter((candidate) => candidate.accepted).length,
    routeChangeCount: view.routes.filter((route) => route.routeChanged).length,
    topologyMutationCount: view.mutations.length,
    openWorldMutationCount: view.mutations.filter((mutation) => mutation.openWorld).length,
    fixedCandidateSelectionCount: view.routes.filter(
      (route) => route.fixedCandidateSelection,
    ).length,
    placementChangeCount: view.placements.filter((placement) => placement.changed).length,
    devicePlacementCount: view.placements.filter((placement) =>
      ["device", "local"].includes(placement.location),
    ).length,
    edgePlacementCount: view.placements.filter(
      (placement) => placement.location === "edge",
    ).length,
    cloudPlacementCount: view.placements.filter(
      (placement) => placement.location === "cloud",
    ).length,
    rejectedBranchCount: view.branches.filter((branch) =>
      ["rejected", "conflicted", "discarded"].includes(branch.outcome),
    ).length,
    rebasedBranchCount: view.branches.filter(
      (branch) => branch.outcome === "rebased",
    ).length,
    pendingWriteCount: view.checkpoints.reduce(
      (count, checkpoint) => count + checkpoint.pendingWrites.length,
      0,
    ),
    committedWriteCount: view.checkpoints.reduce(
      (count, checkpoint) => count + checkpoint.committedWrites.length,
      0,
    ),
    requirementChangeCount: view.requirementChanges.length,
    supersededNodeCount: new Set(
      view.requirementChanges.flatMap((change) => change.supersededNodeIds),
    ).size,
    effectiveStepCount: effectiveStepKeys.size,
    effectiveRouteTransitionCount: view.routes.filter(
      (route) => route.effective && route.accepted && route.selectedCandidateId,
    ).length,
    routeDensity:
      graphNodeCount === 0 ? 0 : view.routes.length / graphNodeCount,
    graphDensity:
      maximumGraphEdges === 0 ? 0 : graphEdgeCount / maximumGraphEdges,
  })
}

function buildGraphIndex(
  nodes: readonly TopologyNodeView[],
  edges: readonly TopologyEdgeView[],
): GraphIndex {
  const visibleNodes = nodes.filter((node) => !node.removed)
  const nodeById = new Map(visibleNodes.map((node) => [node.id, node]))
  const nodeIds = [...nodeById.keys()].sort()
  const outgoing = new Map<string, TopologyEdgeView[]>()
  const incoming = new Map<string, TopologyEdgeView[]>()
  const neighbors = new Map<string, Set<string>>()
  const missing = new Set<string>()
  const edgeIds = new Set<string>()
  const duplicateEdgeIds = new Set<string>()
  for (const id of nodeIds) {
    outgoing.set(id, [])
    incoming.set(id, [])
    neighbors.set(id, new Set())
  }
  for (const edge of edges) {
    if (edge.removed) continue
    if (edgeIds.has(edge.id)) duplicateEdgeIds.add(edge.id)
    edgeIds.add(edge.id)
    const sourcePresent = nodeById.has(edge.sourceId)
    const targetPresent = nodeById.has(edge.targetId)
    if (!sourcePresent) missing.add(edge.sourceId)
    if (!targetPresent) missing.add(edge.targetId)
    if (!sourcePresent || !targetPresent) continue
    outgoing.get(edge.sourceId)!.push(edge)
    incoming.get(edge.targetId)!.push(edge)
    neighbors.get(edge.sourceId)!.add(edge.targetId)
    neighbors.get(edge.targetId)!.add(edge.sourceId)
  }
  for (const values of outgoing.values()) values.sort(compareEdges)
  for (const values of incoming.values()) values.sort(compareEdges)
  return {
    nodeIds: Object.freeze(nodeIds),
    nodeById,
    outgoing,
    incoming,
    neighbors: new Map(
      [...neighbors.entries()].map(([id, values]) => [
        id,
        Object.freeze([...values].sort()),
      ]),
    ),
    missingNodeIds: Object.freeze([...missing].sort()),
    duplicateEdgeIds: Object.freeze([...duplicateEdgeIds].sort()),
  }
}

function stronglyConnectedComponents(index: GraphIndex): string[][] {
  let cursor = 0
  const indexes = new Map<string, number>()
  const lowLinks = new Map<string, number>()
  const stack: string[] = []
  const onStack = new Set<string>()
  const components: string[][] = []
  const visit = (nodeId: string) => {
    indexes.set(nodeId, cursor)
    lowLinks.set(nodeId, cursor)
    cursor += 1
    stack.push(nodeId)
    onStack.add(nodeId)
    for (const edge of index.outgoing.get(nodeId) ?? []) {
      const target = edge.targetId
      if (!indexes.has(target)) {
        visit(target)
        lowLinks.set(
          nodeId,
          Math.min(lowLinks.get(nodeId)!, lowLinks.get(target)!),
        )
      } else if (onStack.has(target)) {
        lowLinks.set(
          nodeId,
          Math.min(lowLinks.get(nodeId)!, indexes.get(target)!),
        )
      }
    }
    if (lowLinks.get(nodeId) !== indexes.get(nodeId)) return
    const component: string[] = []
    while (stack.length > 0) {
      const value = stack.pop()!
      onStack.delete(value)
      component.push(value)
      if (value === nodeId) break
    }
    component.sort()
    components.push(component)
  }
  for (const nodeId of index.nodeIds) {
    if (!indexes.has(nodeId)) visit(nodeId)
  }
  return components.sort((left, right) =>
    left.join("\0").localeCompare(right.join("\0")),
  )
}

function cycleForComponent(component: readonly string[], index: GraphIndex): string[] {
  if (component.length === 1) return [component[0]!, component[0]!]
  const allowed = new Set(component)
  const start = [...component].sort()[0]!
  const path: string[] = []
  const seen = new Set<string>()
  const visit = (nodeId: string): boolean => {
    path.push(nodeId)
    seen.add(nodeId)
    for (const edge of index.outgoing.get(nodeId) ?? []) {
      if (!allowed.has(edge.targetId)) continue
      if (edge.targetId === start) {
        path.push(start)
        return true
      }
      if (!seen.has(edge.targetId) && visit(edge.targetId)) return true
    }
    path.pop()
    return false
  }
  return visit(start) ? path : [...component, start]
}

function normalizeCycles(cycles: readonly (readonly string[])[]): string[][] {
  const byKey = new Map<string, string[]>()
  for (const cycle of cycles) {
    const values = cycle.slice(0, -1)
    if (values.length === 0) continue
    let best = [...values]
    for (let index = 1; index < values.length; index += 1) {
      const candidate = [...values.slice(index), ...values.slice(0, index)]
      if (candidate.join("\0").localeCompare(best.join("\0")) < 0) best = candidate
    }
    const normalized = [...best, best[0]!]
    byKey.set(normalized.join(">"), normalized)
  }
  return [...byKey.values()].sort((left, right) =>
    left.join("\0").localeCompare(right.join("\0")),
  )
}

function weakComponents(
  index: GraphIndex,
  cycles: readonly (readonly string[])[],
): GraphComponentView[] {
  const remaining = new Set(index.nodeIds)
  const cycleNodes = new Set(cycles.flat())
  const output: GraphComponentView[] = []
  while (remaining.size > 0) {
    const seed = [...remaining].sort()[0]!
    const queue = [seed]
    const nodeIds: string[] = []
    remaining.delete(seed)
    while (queue.length > 0) {
      const current = queue.shift()!
      nodeIds.push(current)
      for (const neighbor of index.neighbors.get(current) ?? []) {
        if (!remaining.has(neighbor)) continue
        remaining.delete(neighbor)
        queue.push(neighbor)
      }
    }
    nodeIds.sort()
    const members = new Set(nodeIds)
    const edges = nodeIds.flatMap((nodeId) =>
      (index.outgoing.get(nodeId) ?? []).filter((edge) =>
        members.has(edge.targetId),
      ),
    )
    output.push(
      Object.freeze({
        id: `component:${nodeIds[0]}`,
        nodeIds: Object.freeze(nodeIds),
        edgeIds: Object.freeze(uniqueStrings(edges.map((edge) => edge.id))),
        rootNodeIds: Object.freeze(
          nodeIds.filter(
            (nodeId) =>
              (index.incoming.get(nodeId) ?? []).filter((edge) =>
                members.has(edge.sourceId),
              ).length === 0,
          ),
        ),
        leafNodeIds: Object.freeze(
          nodeIds.filter(
            (nodeId) =>
              (index.outgoing.get(nodeId) ?? []).filter((edge) =>
                members.has(edge.targetId),
              ).length === 0,
          ),
        ),
        cyclic: nodeIds.some((nodeId) => cycleNodes.has(nodeId)),
      }),
    )
  }
  return output
}

function deterministicTopologicalOrder(index: GraphIndex): string[] {
  const indegree = new Map(
    index.nodeIds.map((nodeId) => [
      nodeId,
      (index.incoming.get(nodeId) ?? []).length,
    ]),
  )
  const ready = index.nodeIds.filter((nodeId) => indegree.get(nodeId) === 0)
  const output: string[] = []
  while (ready.length > 0) {
    ready.sort()
    const nodeId = ready.shift()!
    output.push(nodeId)
    for (const edge of index.outgoing.get(nodeId) ?? []) {
      const next = (indegree.get(edge.targetId) ?? 0) - 1
      indegree.set(edge.targetId, next)
      if (next === 0) ready.push(edge.targetId)
    }
  }
  for (const nodeId of index.nodeIds) {
    if (!output.includes(nodeId)) output.push(nodeId)
  }
  return output
}

function computeDepths(
  index: GraphIndex,
  order: readonly string[],
): Record<string, number> {
  const depths: Record<string, number> = {}
  for (const nodeId of order) {
    const parents = index.incoming.get(nodeId) ?? []
    depths[nodeId] =
      parents.length === 0
        ? 0
        : Math.max(...parents.map((edge) => (depths[edge.sourceId] ?? 0) + 1))
  }
  return depths
}

function criticalPath(
  index: GraphIndex,
  order: readonly string[],
  acyclic: boolean,
): CriticalPathView {
  const distance = new Map<string, number>()
  const previous = new Map<string, { nodeId: string; edgeId: string }>()
  for (const nodeId of order) {
    distance.set(nodeId, distance.get(nodeId) ?? 0)
    for (const edge of index.outgoing.get(nodeId) ?? []) {
      const weight = edge.weight ?? 1
      const candidate = (distance.get(nodeId) ?? 0) + weight
      const current = distance.get(edge.targetId) ?? Number.NEGATIVE_INFINITY
      if (candidate > current) {
        distance.set(edge.targetId, candidate)
        previous.set(edge.targetId, { nodeId, edgeId: edge.id })
      }
    }
  }
  const end =
    [...distance.entries()].sort(
      (left, right) =>
        right[1] - left[1] || left[0].localeCompare(right[0]),
    )[0]?.[0] ?? index.nodeIds[0]
  if (!end) {
    return { nodeIds: Object.freeze([]), edgeIds: Object.freeze([]), weight: 0, complete: true }
  }
  const nodeIds = [end]
  const edgeIds: string[] = []
  let cursor = end
  const seen = new Set([cursor])
  while (previous.has(cursor)) {
    const item = previous.get(cursor)!
    if (seen.has(item.nodeId)) break
    edgeIds.unshift(item.edgeId)
    nodeIds.unshift(item.nodeId)
    cursor = item.nodeId
    seen.add(cursor)
  }
  return {
    nodeIds: Object.freeze(nodeIds),
    edgeIds: Object.freeze(edgeIds),
    weight: distance.get(end) ?? 0,
    complete: acyclic,
  }
}

function reachable(
  start: string,
  adjacency: ReadonlyMap<string, readonly TopologyEdgeView[]>,
): string[] {
  const seen = new Set<string>()
  const queue = [...(adjacency.get(start) ?? [])].map((edge) =>
    edge.sourceId === start ? edge.targetId : edge.sourceId,
  )
  while (queue.length > 0) {
    const nodeId = queue.shift()!
    if (seen.has(nodeId) || nodeId === start) continue
    seen.add(nodeId)
    for (const edge of adjacency.get(nodeId) ?? []) {
      queue.push(edge.sourceId === nodeId ? edge.targetId : edge.sourceId)
    }
  }
  return [...seen].sort()
}

function hasSelfLoop(
  nodeId: string,
  edges: readonly TopologyEdgeView[],
): boolean {
  return edges.some((edge) => edge.targetId === nodeId)
}

function compareEdges(left: TopologyEdgeView, right: TopologyEdgeView): number {
  return (
    left.sequence - right.sequence ||
    left.sourceId.localeCompare(right.sourceId) ||
    left.targetId.localeCompare(right.targetId) ||
    left.id.localeCompare(right.id)
  )
}
