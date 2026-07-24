import {
  TraceCompleteness,
  TraceEdgeKind,
  TraceNodeKind,
  TraceSemanticKind,
  type TraceBottleneck,
  type TraceCriticalPath,
  type TraceEdge,
  type TraceNode,
  type TracePathAlternative,
} from "../contracts.ts"

const PATH_EDGE_KINDS = new Set<TraceEdge["kind"]>([
  TraceEdgeKind.CAUSATION,
  TraceEdgeKind.CORRELATION,
  TraceEdgeKind.REPLACEMENT,
  TraceEdgeKind.SEQUENCE,
  TraceEdgeKind.SUBAGENT_YIELD,
])

interface EventGraph {
  keys: readonly string[]
  outgoing: ReadonlyMap<string, readonly TraceEdge[]>
  incoming: ReadonlyMap<string, readonly TraceEdge[]>
  nodeByKey: ReadonlyMap<string, TraceNode>
}

interface StrongComponent {
  id: number
  keys: readonly string[]
  cyclic: boolean
  weight: number
  firstSequence: number
  lastSequence: number
}

interface CondensedEdge {
  id: string
  source: number
  target: number
  weight: number
  original: TraceEdge
}

interface LongestPathResult {
  componentIds: readonly number[]
  edgeIds: readonly string[]
  totalWeight: number
}

function eventGraph(
  nodes: readonly TraceNode[],
  edges: readonly TraceEdge[],
): EventGraph {
  const nodeByKey = new Map(
    nodes
      .filter((node) => node.identity.kind === TraceNodeKind.EVENT)
      .map((node) => [node.key, node] as const),
  )
  const outgoing = new Map<string, TraceEdge[]>()
  const incoming = new Map<string, TraceEdge[]>()
  for (const key of nodeByKey.keys()) {
    outgoing.set(key, [])
    incoming.set(key, [])
  }
  for (const edge of edges) {
    if (!PATH_EDGE_KINDS.has(edge.kind)) continue
    if (!nodeByKey.has(edge.sourceKey) || !nodeByKey.has(edge.targetKey)) continue
    outgoing.get(edge.sourceKey)?.push(edge)
    incoming.get(edge.targetKey)?.push(edge)
  }
  const compare = (left: TraceEdge, right: TraceEdge) => {
    const leftNode = nodeByKey.get(left.targetKey)
    const rightNode = nodeByKey.get(right.targetKey)
    return (
      (leftNode?.sequence ?? 0) - (rightNode?.sequence ?? 0) ||
      left.targetKey.localeCompare(right.targetKey) ||
      left.id.localeCompare(right.id)
    )
  }
  for (const values of outgoing.values()) values.sort(compare)
  for (const values of incoming.values()) values.sort((a, b) => compare(b, a))
  const keys = [...nodeByKey.keys()].sort((left, right) =>
    (nodeByKey.get(left)?.sequence ?? 0) - (nodeByKey.get(right)?.sequence ?? 0) ||
    left.localeCompare(right),
  )
  return { keys, outgoing, incoming, nodeByKey }
}

function nodeWeight(node: TraceNode): number {
  const durationWeight = Math.log2(2 + Math.max(0, node.durationMs))
  let semanticWeight = Math.max(1, node.contributionScore)
  if (node.semantics.includes(TraceSemanticKind.FAULT)) semanticWeight += 8
  if (node.semantics.includes(TraceSemanticKind.RECOVERY)) semanticWeight += 6
  if (node.semantics.includes(TraceSemanticKind.PERMISSION)) semanticWeight += 4
  if (node.semantics.includes(TraceSemanticKind.PROVIDER_RETRY)) semanticWeight += 5
  if (node.completeness !== TraceCompleteness.COMPLETE) semanticWeight += 2
  if (!node.effective) semanticWeight *= 0.5
  return durationWeight + semanticWeight
}

function stronglyConnected(graph: EventGraph): {
  components: readonly StrongComponent[]
  componentByKey: ReadonlyMap<string, number>
} {
  let nextIndex = 0
  const stack: string[] = []
  const onStack = new Set<string>()
  const indexByKey = new Map<string, number>()
  const lowByKey = new Map<string, number>()
  const raw: string[][] = []

  const visit = (key: string): void => {
    const currentIndex = nextIndex++
    indexByKey.set(key, currentIndex)
    lowByKey.set(key, currentIndex)
    stack.push(key)
    onStack.add(key)
    for (const edge of graph.outgoing.get(key) ?? []) {
      const target = edge.targetKey
      if (!indexByKey.has(target)) {
        visit(target)
        lowByKey.set(key, Math.min(lowByKey.get(key)!, lowByKey.get(target)!))
      } else if (onStack.has(target)) {
        lowByKey.set(key, Math.min(lowByKey.get(key)!, indexByKey.get(target)!))
      }
    }
    if (lowByKey.get(key) !== indexByKey.get(key)) return
    const component: string[] = []
    while (stack.length) {
      const member = stack.pop()!
      onStack.delete(member)
      component.push(member)
      if (member === key) break
    }
    component.sort((left, right) =>
      (graph.nodeByKey.get(left)?.sequence ?? 0) - (graph.nodeByKey.get(right)?.sequence ?? 0) ||
      left.localeCompare(right),
    )
    raw.push(component)
  }

  for (const key of graph.keys) if (!indexByKey.has(key)) visit(key)
  raw.sort((left, right) =>
    (graph.nodeByKey.get(left[0]!)?.sequence ?? 0) -
      (graph.nodeByKey.get(right[0]!)?.sequence ?? 0) ||
    left[0]!.localeCompare(right[0]!),
  )
  const componentByKey = new Map<string, number>()
  const components = raw.map((keys, id) => {
    keys.forEach((key) => componentByKey.set(key, id))
    const selfLoop = keys.length === 1 &&
      (graph.outgoing.get(keys[0]!) ?? []).some((edge) => edge.targetKey === keys[0])
    return Object.freeze({
      id,
      keys: Object.freeze(keys),
      cyclic: keys.length > 1 || selfLoop,
      weight: keys.reduce((total, key) => total + nodeWeight(graph.nodeByKey.get(key)!), 0),
      firstSequence: Math.min(...keys.map((key) => graph.nodeByKey.get(key)!.sequence)),
      lastSequence: Math.max(...keys.map((key) => graph.nodeByKey.get(key)!.endSequence)),
    })
  })
  return { components: Object.freeze(components), componentByKey }
}

function condensedEdges(
  graph: EventGraph,
  componentByKey: ReadonlyMap<string, number>,
): readonly CondensedEdge[] {
  const best = new Map<string, CondensedEdge>()
  for (const values of graph.outgoing.values()) {
    for (const edge of values) {
      const source = componentByKey.get(edge.sourceKey)
      const target = componentByKey.get(edge.targetKey)
      if (source === undefined || target === undefined || source === target) continue
      const key = `${source}:${target}`
      const candidate: CondensedEdge = {
        id: edge.id,
        source,
        target,
        weight: Math.max(0, edge.weight),
        original: edge,
      }
      const existing = best.get(key)
      if (
        !existing ||
        candidate.weight > existing.weight ||
        (candidate.weight === existing.weight && candidate.id < existing.id)
      ) {
        best.set(key, candidate)
      }
    }
  }
  return Object.freeze([...best.values()].sort((left, right) =>
    left.source - right.source || left.target - right.target || left.id.localeCompare(right.id),
  ))
}

function topologicalOrder(
  components: readonly StrongComponent[],
  edges: readonly CondensedEdge[],
): readonly number[] {
  const indegree = new Array<number>(components.length).fill(0)
  const outgoing = new Map<number, CondensedEdge[]>()
  for (const component of components) outgoing.set(component.id, [])
  for (const edge of edges) {
    indegree[edge.target] = (indegree[edge.target] ?? 0) + 1
    outgoing.get(edge.source)?.push(edge)
  }
  const ready = components
    .filter((component) => indegree[component.id] === 0)
    .map((component) => component.id)
  ready.sort((left, right) =>
    components[left]!.firstSequence - components[right]!.firstSequence || left - right,
  )
  const order: number[] = []
  while (ready.length) {
    const id = ready.shift()!
    order.push(id)
    for (const edge of outgoing.get(id) ?? []) {
      indegree[edge.target] -= 1
      if (indegree[edge.target] === 0) {
        ready.push(edge.target)
        ready.sort((left, right) =>
          components[left]!.firstSequence - components[right]!.firstSequence || left - right,
        )
      }
    }
  }
  if (order.length < components.length) {
    for (const component of components) if (!order.includes(component.id)) order.push(component.id)
  }
  return Object.freeze(order)
}

function longestPath(
  components: readonly StrongComponent[],
  edges: readonly CondensedEdge[],
  excludedEdgeIds: ReadonlySet<string> = new Set(),
): LongestPathResult {
  if (!components.length) return { componentIds: [], edgeIds: [], totalWeight: 0 }
  const order = topologicalOrder(components, edges)
  const incoming = new Map<number, CondensedEdge[]>()
  for (const component of components) incoming.set(component.id, [])
  for (const edge of edges) {
    if (!excludedEdgeIds.has(edge.id)) incoming.get(edge.target)?.push(edge)
  }
  const score = new Array<number>(components.length).fill(Number.NEGATIVE_INFINITY)
  const predecessor = new Array<CondensedEdge | undefined>(components.length)
  for (const id of order) {
    const candidateEdges = incoming.get(id) ?? []
    if (!candidateEdges.length) score[id] = components[id]!.weight
    for (const edge of candidateEdges) {
      const sourceScore = score[edge.source]
      if (!Number.isFinite(sourceScore)) continue
      const candidate = sourceScore + edge.weight + components[id]!.weight
      const current = score[id]
      if (
        candidate > current ||
        (candidate === current && edge.id < (predecessor[id]?.id ?? "~"))
      ) {
        score[id] = candidate
        predecessor[id] = edge
      }
    }
  }
  let tail = order[0] ?? 0
  for (const id of order) {
    if (
      score[id] > score[tail] ||
      (score[id] === score[tail] && components[id]!.lastSequence > components[tail]!.lastSequence)
    ) {
      tail = id
    }
  }
  const componentIds: number[] = []
  const edgeIds: string[] = []
  const seen = new Set<number>()
  let current: number | undefined = tail
  while (current !== undefined && !seen.has(current)) {
    seen.add(current)
    componentIds.push(current)
    const priorEdge: CondensedEdge | undefined = predecessor[current]
    if (!priorEdge) break
    edgeIds.push(priorEdge.id)
    current = priorEdge.source
  }
  componentIds.reverse()
  edgeIds.reverse()
  return Object.freeze({
    componentIds: Object.freeze(componentIds),
    edgeIds: Object.freeze(edgeIds),
    totalWeight: Number.isFinite(score[tail]) ? score[tail] : 0,
  })
}

function expandComponentPath(
  path: LongestPathResult,
  components: readonly StrongComponent[],
  graph: EventGraph,
): readonly string[] {
  const keys: string[] = []
  for (const componentId of path.componentIds) {
    const component = components[componentId]
    if (!component) continue
    const ordered = [...component.keys].sort((left, right) =>
      graph.nodeByKey.get(left)!.sequence - graph.nodeByKey.get(right)!.sequence ||
      left.localeCompare(right),
    )
    keys.push(...ordered)
  }
  return Object.freeze(keys)
}

function pathEventIds(keys: readonly string[], graph: EventGraph): readonly string[] {
  const values: string[] = []
  for (const key of keys) {
    const node = graph.nodeByKey.get(key)
    if (!node) continue
    for (const eventId of node.eventIds) if (!values.includes(eventId)) values.push(eventId)
  }
  return Object.freeze(values)
}

function pathAlternative(
  id: string,
  path: LongestPathResult,
  components: readonly StrongComponent[],
  graph: EventGraph,
  primaryKeys: readonly string[],
): TracePathAlternative {
  const nodeKeys = expandComponentPath(path, components, graph)
  let divergenceKey: string | undefined
  let convergenceKey: string | undefined
  const maximum = Math.max(primaryKeys.length, nodeKeys.length)
  for (let index = 0; index < maximum; index += 1) {
    if (primaryKeys[index] !== nodeKeys[index]) {
      divergenceKey = primaryKeys[Math.max(0, index - 1)]
      break
    }
  }
  for (let offset = 1; offset <= maximum; offset += 1) {
    const primary = primaryKeys[primaryKeys.length - offset]
    const alternate = nodeKeys[nodeKeys.length - offset]
    if (primary && primary === alternate) convergenceKey = primary
    else if (convergenceKey) break
  }
  return Object.freeze({
    id,
    nodeKeys,
    eventIds: pathEventIds(nodeKeys, graph),
    totalWeight: path.totalWeight,
    divergenceKey,
    convergenceKey,
  })
}

function alternatives(
  primary: LongestPathResult,
  components: readonly StrongComponent[],
  edges: readonly CondensedEdge[],
  graph: EventGraph,
  maximum: number,
): readonly TracePathAlternative[] {
  const primaryKeys = expandComponentPath(primary, components, graph)
  const candidates = new Map<string, TracePathAlternative>()
  for (const edgeId of primary.edgeIds) {
    const result = longestPath(components, edges, new Set([edgeId]))
    const keys = expandComponentPath(result, components, graph)
    if (!keys.length || keys.join("|") === primaryKeys.join("|")) continue
    const signature = keys.join("|")
    if (!candidates.has(signature)) {
      candidates.set(
        signature,
        pathAlternative(`trace-alternative:${candidates.size + 1}`, result, components, graph, primaryKeys),
      )
    }
    if (candidates.size >= maximum * 3) break
  }
  return Object.freeze([...candidates.values()]
    .sort((left, right) => right.totalWeight - left.totalWeight || left.id.localeCompare(right.id))
    .slice(0, maximum))
}

function bottleneckKind(node: TraceNode, fanIn: number): TraceBottleneck["kind"] {
  if (node.retryCount > 0) return "retry"
  if (node.semantics.includes(TraceSemanticKind.PERMISSION)) return "permission"
  if (node.semantics.includes(TraceSemanticKind.PROVIDER)) return "provider"
  if (node.semantics.includes(TraceSemanticKind.RECOVERY)) return "recovery"
  if (fanIn >= 3) return "fan-in"
  return "latency"
}

function bottleneckLabel(node: TraceNode, kind: TraceBottleneck["kind"]): string {
  switch (kind) {
    case "retry":
      return `${node.retryCount} retry/reconnect transition(s) on ${node.title}`
    case "permission":
      return `Permission gate on ${node.title}`
    case "provider":
      return `Provider dispatch latency on ${node.title}`
    case "recovery":
      return `Recovery work on ${node.title}`
    case "fan-in":
      return `Causal fan-in at ${node.title}`
    default:
      return `${node.durationMs} ms attributed to ${node.title}`
  }
}

function buildBottlenecks(
  keys: readonly string[],
  graph: EventGraph,
): readonly TraceBottleneck[] {
  const values: TraceBottleneck[] = []
  for (const key of keys) {
    const node = graph.nodeByKey.get(key)
    if (!node) continue
    const fanIn = graph.incoming.get(key)?.length ?? 0
    const kind = bottleneckKind(node, fanIn)
    const score =
      Math.log2(2 + node.durationMs) +
      node.retryCount * 8 +
      fanIn * 2 +
      (node.semantics.includes(TraceSemanticKind.FAULT) ? 10 : 0) +
      (node.completeness !== TraceCompleteness.COMPLETE ? 3 : 0)
    if (score < 3) continue
    values.push(Object.freeze({
      id: `bottleneck:${key}`,
      nodeKey: key,
      kind,
      score,
      durationMs: node.durationMs,
      label: bottleneckLabel(node, kind),
      evidenceEventIds: node.eventIds,
    }))
  }
  values.sort((left, right) => right.score - left.score || left.nodeKey.localeCompare(right.nodeKey))
  return Object.freeze(values.slice(0, 12))
}

function pathDuration(keys: readonly string[], graph: EventGraph): number {
  let start = Number.POSITIVE_INFINITY
  let end = Number.NEGATIVE_INFINITY
  let summed = 0
  for (const key of keys) {
    const node = graph.nodeByKey.get(key)
    if (!node) continue
    const started = node.startedAt ? Date.parse(node.startedAt) : NaN
    const ended = node.endedAt ? Date.parse(node.endedAt) : NaN
    if (Number.isFinite(started)) start = Math.min(start, started)
    if (Number.isFinite(ended)) end = Math.max(end, ended)
    summed += node.durationMs
  }
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, end - start) : summed
}

export function buildTraceCriticalPath(
  nodes: readonly TraceNode[],
  edges: readonly TraceEdge[],
  maximumAlternatives = 4,
): TraceCriticalPath {
  const graph = eventGraph(nodes, edges)
  if (!graph.keys.length) {
    return Object.freeze({
      nodeKeys: Object.freeze([]),
      eventIds: Object.freeze([]),
      edgeIds: Object.freeze([]),
      workerIds: Object.freeze([]),
      providerIds: Object.freeze([]),
      totalWeight: 0,
      durationMs: 0,
      complete: false,
      cycleNodeKeys: Object.freeze([]),
      missingNodeKeys: Object.freeze([]),
      bottlenecks: Object.freeze([]),
      alternatives: Object.freeze([]),
    })
  }
  const connected = stronglyConnected(graph)
  const condensed = condensedEdges(graph, connected.componentByKey)
  const primary = longestPath(connected.components, condensed)
  const nodeKeys = expandComponentPath(primary, connected.components, graph)
  const eventIds = pathEventIds(nodeKeys, graph)
  const edgeIds = Object.freeze([...primary.edgeIds])
  const workerIds = Object.freeze([...new Set(nodeKeys
    .map((key) => graph.nodeByKey.get(key)?.refs.workerId)
    .filter((value): value is string => Boolean(value)))])
  const providerIds = Object.freeze([...new Set(nodeKeys
    .map((key) => graph.nodeByKey.get(key)?.refs.providerId)
    .filter((value): value is string => Boolean(value)))])
  const cycleNodeKeys = Object.freeze(connected.components
    .filter((component) => component.cyclic)
    .flatMap((component) => component.keys))
  const pathSet = new Set(nodeKeys)
  const missingNodeKeys = Object.freeze(edges
    .filter((edge) =>
      (pathSet.has(edge.sourceKey) || pathSet.has(edge.targetKey)) &&
      (edge.missingSource || edge.missingTarget),
    )
    .flatMap((edge) => [
      ...(edge.missingSource ? [edge.sourceKey] : []),
      ...(edge.missingTarget ? [edge.targetKey] : []),
    ]))
  return Object.freeze({
    nodeKeys,
    eventIds,
    edgeIds,
    workerIds,
    providerIds,
    totalWeight: primary.totalWeight,
    durationMs: pathDuration(nodeKeys, graph),
    complete: cycleNodeKeys.length === 0 && missingNodeKeys.length === 0 && nodeKeys.length > 0,
    cycleNodeKeys,
    missingNodeKeys: Object.freeze([...new Set(missingNodeKeys)]),
    bottlenecks: buildBottlenecks(nodeKeys, graph),
    alternatives: alternatives(
      primary,
      connected.components,
      condensed,
      graph,
      Math.max(0, Math.min(32, maximumAlternatives)),
    ),
  })
}

export function markTraceCriticalPath(
  nodes: readonly TraceNode[],
  edges: readonly TraceEdge[],
  path: TraceCriticalPath,
): { nodes: readonly TraceNode[]; edges: readonly TraceEdge[] } {
  const nodeKeys = new Set(path.nodeKeys)
  const edgeIds = new Set(path.edgeIds)
  return Object.freeze({
    nodes: Object.freeze(nodes.map((node) =>
      node.critical === nodeKeys.has(node.key)
        ? node
        : Object.freeze({ ...node, critical: nodeKeys.has(node.key) }),
    )),
    edges: Object.freeze(edges.map((edge) =>
      edge.critical === edgeIds.has(edge.id)
        ? edge
        : Object.freeze({ ...edge, critical: edgeIds.has(edge.id) }),
    )),
  })
}
