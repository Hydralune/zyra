import type {
  GraphEdgeRecord,
  GraphNodeRecord,
  LayoutEdge,
  LayoutNode,
  LayoutSnapshot,
  Point,
  Rect,
  Size,
  TopologyGraphModel,
} from "./contracts.ts"
import {
  add,
  centerOf,
  deterministicJitter,
  distance,
  expandRect,
  interpolatePoint,
  midpoint,
  point,
  polylineBounds,
  polylineLength,
  rect,
  rectFromCenter,
  samePoint,
  snapPoint,
  stableHash,
  subtract,
  unionRects,
} from "./geometry.ts"

export interface StableLayoutOptions {
  direction?: "left-to-right" | "top-to-bottom"
  rankSpacing?: number
  nodeSpacing?: number
  componentSpacing?: number
  nodeWidth?: number
  nodeHeight?: number
  maximumNodeWidth?: number
  titleCharacterWidth?: number
  padding?: number
  grid?: number
  stability?: number
  relaxationPasses?: number
  crossingPasses?: number
  collisionPasses?: number
}

interface NormalizedOptions {
  direction: "left-to-right" | "top-to-bottom"
  rankSpacing: number
  nodeSpacing: number
  componentSpacing: number
  nodeWidth: number
  nodeHeight: number
  maximumNodeWidth: number
  titleCharacterWidth: number
  padding: number
  grid: number
  stability: number
  relaxationPasses: number
  crossingPasses: number
  collisionPasses: number
}

interface RankNode {
  id: string
  rank: number
  order: number
  component: number
  size: Size
  position: Point
  previous?: LayoutNode
  pinned: boolean
}

interface Component {
  id: number
  nodeIds: string[]
  edgeIds: string[]
  minimumSequence: number
}

function bounded(value: number | undefined, fallback: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) return fallback
  return Math.min(maximum, Math.max(minimum, Number(value)))
}

function integer(value: number | undefined, fallback: number, minimum: number, maximum: number): number {
  return Math.floor(bounded(value, fallback, minimum, maximum))
}

function normalizeOptions(options: StableLayoutOptions): NormalizedOptions {
  return Object.freeze({
    direction: options.direction === "top-to-bottom" ? "top-to-bottom" : "left-to-right",
    rankSpacing: bounded(options.rankSpacing, 210, 80, 800),
    nodeSpacing: bounded(options.nodeSpacing, 54, 12, 400),
    componentSpacing: bounded(options.componentSpacing, 160, 40, 1_200),
    nodeWidth: bounded(options.nodeWidth, 168, 80, 500),
    nodeHeight: bounded(options.nodeHeight, 72, 36, 240),
    maximumNodeWidth: bounded(options.maximumNodeWidth, 280, 100, 800),
    titleCharacterWidth: bounded(options.titleCharacterWidth, 7.2, 3, 24),
    padding: bounded(options.padding, 80, 0, 800),
    grid: bounded(options.grid, 2, 0.25, 100),
    stability: bounded(options.stability, 0.88, 0, 1),
    relaxationPasses: integer(options.relaxationPasses, 6, 0, 40),
    crossingPasses: integer(options.crossingPasses, 8, 0, 40),
    collisionPasses: integer(options.collisionPasses, 12, 0, 80),
  })
}

function nodeSize(node: GraphNodeRecord, options: NormalizedOptions): Size {
  const titleRows = Math.max(1, Math.min(3, Math.ceil(node.title.length / 24)))
  const badgeRows =
    (node.capabilities.length > 0 ? 1 : 0) +
    (node.policyViolation || node.openWorldReplaced ? 1 : 0)
  const width = Math.min(
    options.maximumNodeWidth,
    Math.max(options.nodeWidth, 48 + node.title.length * options.titleCharacterWidth),
  )
  return Object.freeze({
    width,
    height: options.nodeHeight + (titleRows - 1) * 18 + badgeRows * 14,
  })
}

function eligibleEdges(model: TopologyGraphModel): GraphEdgeRecord[] {
  const ids = new Set(model.nodes.map((node) => node.id))
  return model.edges
    .filter(
      (edge) =>
        !edge.removed &&
        edge.effective &&
        ids.has(edge.sourceId) &&
        ids.has(edge.targetId) &&
        edge.sourceId !== edge.targetId,
    )
    .sort(compareEdge)
}

function compareEdge(left: GraphEdgeRecord, right: GraphEdgeRecord): number {
  return (
    left.sourceId.localeCompare(right.sourceId) ||
    left.targetId.localeCompare(right.targetId) ||
    left.kind.localeCompare(right.kind) ||
    left.id.localeCompare(right.id)
  )
}

function connectedComponents(
  nodes: readonly GraphNodeRecord[],
  edges: readonly GraphEdgeRecord[],
): Component[] {
  const adjacency = new Map<string, Set<string>>()
  const edgeByNode = new Map<string, string[]>()
  const nodeById = new Map(nodes.map((node) => [node.id, node]))
  for (const node of nodes) adjacency.set(node.id, new Set())
  for (const edge of edges) {
    adjacency.get(edge.sourceId)?.add(edge.targetId)
    adjacency.get(edge.targetId)?.add(edge.sourceId)
    for (const id of [edge.sourceId, edge.targetId]) {
      const values = edgeByNode.get(id) ?? []
      values.push(edge.id)
      edgeByNode.set(id, values)
    }
  }
  const visited = new Set<string>()
  const result: Component[] = []
  const starts = [...nodes].sort(
    (left, right) => left.sequence - right.sequence || left.id.localeCompare(right.id),
  )
  for (const start of starts) {
    if (visited.has(start.id)) continue
    const queue = [start.id]
    visited.add(start.id)
    const nodeIds: string[] = []
    const edgeIds = new Set<string>()
    let minimumSequence = start.sequence
    while (queue.length > 0) {
      const id = queue.shift()!
      nodeIds.push(id)
      minimumSequence = Math.min(minimumSequence, nodeById.get(id)?.sequence ?? minimumSequence)
      for (const edgeId of edgeByNode.get(id) ?? []) edgeIds.add(edgeId)
      const neighbors = [...(adjacency.get(id) ?? [])].sort()
      for (const neighbor of neighbors) {
        if (visited.has(neighbor)) continue
        visited.add(neighbor)
        queue.push(neighbor)
      }
    }
    result.push({
      id: result.length,
      nodeIds: nodeIds.sort(),
      edgeIds: [...edgeIds].sort(),
      minimumSequence,
    })
  }
  result.sort(
    (left, right) =>
      right.nodeIds.length - left.nodeIds.length ||
      left.minimumSequence - right.minimumSequence ||
      left.nodeIds[0]!.localeCompare(right.nodeIds[0]!),
  )
  return result.map((component, index) => ({ ...component, id: index }))
}

function stronglyConnectedComponents(
  nodeIds: readonly string[],
  edges: readonly GraphEdgeRecord[],
): readonly (readonly string[])[] {
  const outgoing = new Map<string, string[]>()
  for (const id of nodeIds) outgoing.set(id, [])
  for (const edge of edges) {
    if (!outgoing.has(edge.sourceId) || !outgoing.has(edge.targetId)) continue
    outgoing.get(edge.sourceId)!.push(edge.targetId)
  }
  for (const values of outgoing.values()) values.sort()
  let index = 0
  const indices = new Map<string, number>()
  const low = new Map<string, number>()
  const stack: string[] = []
  const onStack = new Set<string>()
  const result: string[][] = []
  const visit = (id: string) => {
    indices.set(id, index)
    low.set(id, index)
    index += 1
    stack.push(id)
    onStack.add(id)
    for (const target of outgoing.get(id) ?? []) {
      if (!indices.has(target)) {
        visit(target)
        low.set(id, Math.min(low.get(id)!, low.get(target)!))
      } else if (onStack.has(target)) {
        low.set(id, Math.min(low.get(id)!, indices.get(target)!))
      }
    }
    if (low.get(id) !== indices.get(id)) return
    const component: string[] = []
    while (stack.length > 0) {
      const value = stack.pop()!
      onStack.delete(value)
      component.push(value)
      if (value === id) break
    }
    result.push(component.sort())
  }
  for (const id of [...nodeIds].sort()) {
    if (!indices.has(id)) visit(id)
  }
  return Object.freeze(
    result
      .sort((left, right) => left[0]!.localeCompare(right[0]!))
      .map((values) => Object.freeze(values)),
  )
}

function assignRanks(
  component: Component,
  edges: readonly GraphEdgeRecord[],
  model: TopologyGraphModel,
): Map<string, number> {
  const componentEdges = edges.filter((edge) => component.edgeIds.includes(edge.id))
  const sccs = stronglyConnectedComponents(component.nodeIds, componentEdges)
  const groupByNode = new Map<string, number>()
  sccs.forEach((group, index) => {
    for (const id of group) groupByNode.set(id, index)
  })
  const incoming = new Map<number, Set<number>>()
  const outgoing = new Map<number, Set<number>>()
  for (let index = 0; index < sccs.length; index += 1) {
    incoming.set(index, new Set())
    outgoing.set(index, new Set())
  }
  for (const edge of componentEdges) {
    const source = groupByNode.get(edge.sourceId)!
    const target = groupByNode.get(edge.targetId)!
    if (source === target) continue
    outgoing.get(source)!.add(target)
    incoming.get(target)!.add(source)
  }
  const groupKey = (group: number): string => {
    const values = sccs[group]!
    const sequence = Math.min(
      ...values.map((id) => model.nodeById.get(id)?.sequence ?? Number.MAX_SAFE_INTEGER),
    )
    return `${String(sequence).padStart(16, "0")}:${values[0]}`
  }
  const ready = [...incoming.entries()]
    .filter(([, values]) => values.size === 0)
    .map(([id]) => id)
    .sort((left, right) => groupKey(left).localeCompare(groupKey(right)))
  const order: number[] = []
  const remainingIncoming = new Map(
    [...incoming.entries()].map(([id, values]) => [id, new Set(values)]),
  )
  while (ready.length > 0) {
    const current = ready.shift()!
    order.push(current)
    for (const target of [...(outgoing.get(current) ?? [])].sort(
      (left, right) => groupKey(left).localeCompare(groupKey(right)),
    )) {
      const values = remainingIncoming.get(target)!
      values.delete(current)
      if (values.size === 0 && !order.includes(target) && !ready.includes(target)) {
        ready.push(target)
        ready.sort((left, right) => groupKey(left).localeCompare(groupKey(right)))
      }
    }
  }
  for (let group = 0; group < sccs.length; group += 1) {
    if (!order.includes(group)) order.push(group)
  }
  const rankByGroup = new Map<number, number>()
  for (const group of order) {
    const parents = [...(incoming.get(group) ?? [])]
    const rank =
      parents.length === 0
        ? 0
        : Math.max(...parents.map((parent) => rankByGroup.get(parent) ?? 0)) + 1
    rankByGroup.set(group, rank)
  }
  const result = new Map<string, number>()
  for (const [id, group] of groupByNode) result.set(id, rankByGroup.get(group) ?? 0)
  return result
}

function adjacencyMaps(
  nodes: readonly RankNode[],
  edges: readonly GraphEdgeRecord[],
): {
  incoming: Map<string, string[]>
  outgoing: Map<string, string[]>
} {
  const ids = new Set(nodes.map((node) => node.id))
  const incoming = new Map<string, string[]>()
  const outgoing = new Map<string, string[]>()
  for (const node of nodes) {
    incoming.set(node.id, [])
    outgoing.set(node.id, [])
  }
  for (const edge of edges) {
    if (!ids.has(edge.sourceId) || !ids.has(edge.targetId)) continue
    outgoing.get(edge.sourceId)!.push(edge.targetId)
    incoming.get(edge.targetId)!.push(edge.sourceId)
  }
  for (const values of [...incoming.values(), ...outgoing.values()]) values.sort()
  return { incoming, outgoing }
}

function median(values: readonly number[]): number {
  if (values.length === 0) return 0
  const sorted = [...values].sort((left, right) => left - right)
  const middle = Math.floor(sorted.length / 2)
  return sorted.length % 2 === 0
    ? (sorted[middle - 1]! + sorted[middle]!) / 2
    : sorted[middle]!
}

function reduceCrossings(
  nodes: RankNode[],
  edges: readonly GraphEdgeRecord[],
  passes: number,
): void {
  const { incoming, outgoing } = adjacencyMaps(nodes, edges)
  const rankGroups = new Map<number, RankNode[]>()
  for (const node of nodes) {
    const values = rankGroups.get(node.rank) ?? []
    values.push(node)
    rankGroups.set(node.rank, values)
  }
  const ranks = [...rankGroups.keys()].sort((left, right) => left - right)
  for (const values of rankGroups.values()) {
    values.sort((left, right) => left.order - right.order || left.id.localeCompare(right.id))
  }
  const orderById = () =>
    new Map(nodes.map((node) => [node.id, node.order] as const))
  for (let pass = 0; pass < passes; pass += 1) {
    const descending = pass % 2 === 1
    const orderedRanks = descending ? [...ranks].reverse() : ranks
    const positions = orderById()
    for (const rank of orderedRanks) {
      const values = rankGroups.get(rank)!
      const neighborMap = descending ? outgoing : incoming
      values.sort((left, right) => {
        const leftNeighbors = neighborMap.get(left.id) ?? []
        const rightNeighbors = neighborMap.get(right.id) ?? []
        const leftBarycenter =
          leftNeighbors.length > 0
            ? median(leftNeighbors.map((id) => positions.get(id) ?? left.order))
            : left.order
        const rightBarycenter =
          rightNeighbors.length > 0
            ? median(rightNeighbors.map((id) => positions.get(id) ?? right.order))
            : right.order
        return (
          leftBarycenter - rightBarycenter ||
          left.order - right.order ||
          left.id.localeCompare(right.id)
        )
      })
      values.forEach((node, index) => {
        node.order = index
      })
    }
  }
}

function initialPositions(
  nodes: RankNode[],
  options: NormalizedOptions,
  componentOffset: Point,
): void {
  const groups = new Map<number, RankNode[]>()
  for (const node of nodes) {
    const values = groups.get(node.rank) ?? []
    values.push(node)
    groups.set(node.rank, values)
  }
  for (const values of groups.values()) {
    values.sort((left, right) => left.order - right.order || left.id.localeCompare(right.id))
    let cursor = 0
    for (const node of values) {
      const secondary = cursor + node.size.height / 2
      const primary = node.rank * options.rankSpacing
      node.position =
        options.direction === "left-to-right"
          ? point(componentOffset.x + primary, componentOffset.y + secondary)
          : point(componentOffset.x + secondary, componentOffset.y + primary)
      cursor += node.size.height + options.nodeSpacing
    }
    const total = Math.max(0, cursor - options.nodeSpacing)
    for (const node of values) {
      node.position =
        options.direction === "left-to-right"
          ? point(node.position.x, node.position.y - total / 2)
          : point(node.position.x - total / 2, node.position.y)
    }
  }
}

function neighborCenters(
  id: string,
  map: Map<string, string[]>,
  byId: Map<string, RankNode>,
  direction: NormalizedOptions["direction"],
): number[] {
  return (map.get(id) ?? [])
    .map((neighborId) => byId.get(neighborId))
    .filter((value): value is RankNode => Boolean(value))
    .map((value) => (direction === "left-to-right" ? value.position.y : value.position.x))
}

function relaxPositions(
  nodes: RankNode[],
  edges: readonly GraphEdgeRecord[],
  options: NormalizedOptions,
): void {
  const { incoming, outgoing } = adjacencyMaps(nodes, edges)
  const byId = new Map(nodes.map((node) => [node.id, node]))
  for (let pass = 0; pass < options.relaxationPasses; pass += 1) {
    const forward = pass % 2 === 0
    const ordered = [...nodes].sort((left, right) =>
      forward
        ? left.rank - right.rank || left.order - right.order
        : right.rank - left.rank || left.order - right.order,
    )
    for (const node of ordered) {
      const parents = neighborCenters(node.id, incoming, byId, options.direction)
      const children = neighborCenters(node.id, outgoing, byId, options.direction)
      const targets = [...parents, ...children]
      if (targets.length === 0 || node.pinned) continue
      const desired = median(targets)
      if (options.direction === "left-to-right") {
        node.position = point(node.position.x, node.position.y * 0.55 + desired * 0.45)
      } else {
        node.position = point(node.position.x * 0.55 + desired * 0.45, node.position.y)
      }
    }
    separateRanks(nodes, options)
  }
}

function separateRanks(nodes: RankNode[], options: NormalizedOptions): number {
  const groups = new Map<number, RankNode[]>()
  for (const node of nodes) {
    const values = groups.get(node.rank) ?? []
    values.push(node)
    groups.set(node.rank, values)
  }
  let collisions = 0
  for (const values of groups.values()) {
    values.sort((left, right) => {
      const leftValue =
        options.direction === "left-to-right" ? left.position.y : left.position.x
      const rightValue =
        options.direction === "left-to-right" ? right.position.y : right.position.x
      return leftValue - rightValue || left.order - right.order || left.id.localeCompare(right.id)
    })
    for (let index = 1; index < values.length; index += 1) {
      const previous = values[index - 1]!
      const current = values[index]!
      const previousAxis =
        options.direction === "left-to-right" ? previous.position.y : previous.position.x
      const currentAxis =
        options.direction === "left-to-right" ? current.position.y : current.position.x
      const minimum =
        (previous.size.height + current.size.height) / 2 + options.nodeSpacing
      if (currentAxis - previousAxis >= minimum) continue
      collisions += 1
      const correction = minimum - (currentAxis - previousAxis)
      if (current.pinned && !previous.pinned) {
        previous.position =
          options.direction === "left-to-right"
            ? point(previous.position.x, previous.position.y - correction)
            : point(previous.position.x - correction, previous.position.y)
      } else {
        current.position =
          options.direction === "left-to-right"
            ? point(current.position.x, current.position.y + correction)
            : point(current.position.x + correction, current.position.y)
      }
    }
  }
  return collisions
}

function blendPreviousPositions(
  nodes: RankNode[],
  options: NormalizedOptions,
): void {
  for (const node of nodes) {
    if (!node.previous) {
      node.position = add(node.position, deterministicJitter(node.id, 6))
      continue
    }
    const previous = node.previous.center
    const amount = node.pinned ? 1 : options.stability
    node.position = interpolatePoint(node.position, previous, amount)
  }
}

function restoreIncrementalAnchors(nodes: RankNode[]): void {
  for (const node of nodes) {
    if (!node.previous || node.pinned) continue
    node.position = node.previous.center
  }
}

function resolveGlobalCollisions(
  nodes: RankNode[],
  options: NormalizedOptions,
): number {
  let collisionCount = 0
  for (let pass = 0; pass < options.collisionPasses; pass += 1) {
    let changed = false
    const ordered = [...nodes].sort(
      (left, right) =>
        left.position.y - right.position.y ||
        left.position.x - right.position.x ||
        left.id.localeCompare(right.id),
    )
    for (let leftIndex = 0; leftIndex < ordered.length; leftIndex += 1) {
      const left = ordered[leftIndex]!
      const leftRect = expandRect(rectFromCenter(left.position, left.size), options.nodeSpacing / 4)
      for (let rightIndex = leftIndex + 1; rightIndex < ordered.length; rightIndex += 1) {
        const right = ordered[rightIndex]!
        if (Math.abs(right.position.y - left.position.y) > left.size.height + options.nodeSpacing * 2) break
        const rightRect = expandRect(rectFromCenter(right.position, right.size), options.nodeSpacing / 4)
        const overlapX =
          Math.min(leftRect.x + leftRect.width, rightRect.x + rightRect.width) -
          Math.max(leftRect.x, rightRect.x)
        const overlapY =
          Math.min(leftRect.y + leftRect.height, rightRect.y + rightRect.height) -
          Math.max(leftRect.y, rightRect.y)
        if (overlapX <= 0 || overlapY <= 0) continue
        collisionCount += 1
        changed = true
        const direction = stableHash(`${left.id}:${right.id}`) % 2 === 0 ? 1 : -1
        const leftAnchored = left.pinned || Boolean(left.previous)
        const rightAnchored = right.pinned || Boolean(right.previous)
        if (leftAnchored && rightAnchored) continue
        if (options.direction === "left-to-right" || overlapY <= overlapX) {
          const amount = overlapY / (leftAnchored || rightAnchored ? 1 : 2) + 1
          if (!leftAnchored) left.position = point(left.position.x, left.position.y - amount * direction)
          if (!rightAnchored) right.position = point(right.position.x, right.position.y + amount * direction)
        } else {
          const amount = overlapX / (leftAnchored || rightAnchored ? 1 : 2) + 1
          if (!leftAnchored) left.position = point(left.position.x - amount * direction, left.position.y)
          if (!rightAnchored) right.position = point(right.position.x + amount * direction, right.position.y)
        }
      }
    }
    if (!changed) break
  }
  return collisionCount
}

function componentBounds(nodes: readonly RankNode[]): Rect {
  return unionRects(nodes.map((node) => rectFromCenter(node.position, node.size)))
}

function translateComponent(nodes: RankNode[], delta: Point): void {
  for (const node of nodes) node.position = add(node.position, delta)
}

function arrangeComponents(
  components: readonly RankNode[][],
  options: NormalizedOptions,
): void {
  let cursorX = 0
  let cursorY = 0
  let rowHeight = 0
  const desiredRowWidth = Math.max(
    1_200,
    Math.sqrt(
      components.reduce((total, values) => total + Math.max(1, values.length), 0),
    ) * options.rankSpacing * 2,
  )
  for (const values of components) {
    const bounds = componentBounds(values)
    if (cursorX > 0 && cursorX + bounds.width > desiredRowWidth) {
      cursorX = 0
      cursorY += rowHeight + options.componentSpacing
      rowHeight = 0
    }
    translateComponent(
      values,
      point(cursorX - bounds.x, cursorY - bounds.y),
    )
    cursorX += bounds.width + options.componentSpacing
    rowHeight = Math.max(rowHeight, bounds.height)
  }
}

function orthogonalEdgePoints(
  source: LayoutNode,
  target: LayoutNode,
  edge: GraphEdgeRecord,
  options: NormalizedOptions,
): readonly Point[] {
  const sourceCenter = source.center
  const targetCenter = target.center
  if (options.direction === "left-to-right") {
    const sourcePoint = point(source.bounds.x + source.bounds.width, sourceCenter.y)
    const targetPoint = point(target.bounds.x, targetCenter.y)
    const span = targetPoint.x - sourcePoint.x
    if (span > 24) {
      const bend = sourcePoint.x + span * 0.5
      return Object.freeze([
        sourcePoint,
        point(bend, sourcePoint.y),
        point(bend, targetPoint.y),
        targetPoint,
      ])
    }
    const offset =
      Math.max(source.size.height, target.size.height) / 2 +
      26 +
      (stableHash(edge.id) % 5) * 9
    const direction = sourceCenter.y <= targetCenter.y ? -1 : 1
    return Object.freeze([
      sourcePoint,
      point(sourcePoint.x + 22, sourcePoint.y),
      point(sourcePoint.x + 22, sourcePoint.y + offset * direction),
      point(targetPoint.x - 22, targetPoint.y + offset * direction),
      point(targetPoint.x - 22, targetPoint.y),
      targetPoint,
    ])
  }
  const sourcePoint = point(sourceCenter.x, source.bounds.y + source.bounds.height)
  const targetPoint = point(targetCenter.x, target.bounds.y)
  const span = targetPoint.y - sourcePoint.y
  if (span > 24) {
    const bend = sourcePoint.y + span * 0.5
    return Object.freeze([
      sourcePoint,
      point(sourcePoint.x, bend),
      point(targetPoint.x, bend),
      targetPoint,
    ])
  }
  const offset =
    Math.max(source.size.width, target.size.width) / 2 +
    26 +
    (stableHash(edge.id) % 5) * 9
  const direction = sourceCenter.x <= targetCenter.x ? -1 : 1
  return Object.freeze([
    sourcePoint,
    point(sourcePoint.x, sourcePoint.y + 22),
    point(sourcePoint.x + offset * direction, sourcePoint.y + 22),
    point(targetPoint.x + offset * direction, targetPoint.y - 22),
    point(targetPoint.x, targetPoint.y - 22),
    targetPoint,
  ])
}

function selfEdgePoints(
  node: LayoutNode,
  edge: GraphEdgeRecord,
): readonly Point[] {
  const offset = 22 + (stableHash(edge.id) % 7) * 4
  const right = node.bounds.x + node.bounds.width
  const top = node.bounds.y
  return Object.freeze([
    point(right, node.center.y),
    point(right + offset, node.center.y),
    point(right + offset, top - offset),
    point(node.center.x, top - offset),
    point(node.center.x, top),
  ])
}

function missingEdgePoints(
  source: LayoutNode | undefined,
  target: LayoutNode | undefined,
  edge: GraphEdgeRecord,
): readonly Point[] {
  const anchor = source?.center ?? target?.center ?? point()
  const jitter = deterministicJitter(edge.id, 90)
  const destination = add(anchor, jitter)
  return Object.freeze([anchor, midpoint(anchor, destination), destination])
}

function buildLayoutEdges(
  model: TopologyGraphModel,
  nodes: ReadonlyMap<string, LayoutNode>,
  options: NormalizedOptions,
): ReadonlyMap<string, LayoutEdge> {
  const result = new Map<string, LayoutEdge>()
  for (const edge of model.edges) {
    if (edge.removed || !edge.effective) continue
    const source = nodes.get(edge.sourceId)
    const target = nodes.get(edge.targetId)
    let points: readonly Point[]
    if (!source || !target) points = missingEdgePoints(source, target, edge)
    else if (source.id === target.id) points = selfEdgePoints(source, edge)
    else points = orthogonalEdgePoints(source, target, edge, options)
    result.set(
      edge.id,
      Object.freeze({
        id: edge.id,
        sourceId: edge.sourceId,
        targetId: edge.targetId,
        points,
        bounds: polylineBounds(points, 8),
        length: polylineLength(points),
      }),
    )
  }
  return result
}

function layoutNodeFromRank(value: RankNode, options: NormalizedOptions): LayoutNode {
  const center = snapPoint(value.position, options.grid)
  const stable = Boolean(value.previous && samePoint(center, value.previous.center, 1))
  return Object.freeze({
    id: value.id,
    center,
    size: value.size,
    bounds: rectFromCenter(center, value.size),
    rank: value.rank,
    order: value.order,
    component: value.component,
    pinned: value.pinned,
    stable,
  })
}

export class StableTopologyLayout {
  readonly options: NormalizedOptions
  #snapshot?: LayoutSnapshot
  #pins = new Map<string, Point>()
  #revision = 0

  constructor(options: StableLayoutOptions = {}) {
    this.options = normalizeOptions(options)
  }

  get snapshot(): LayoutSnapshot | undefined {
    return this.#snapshot
  }

  pin(nodeId: string, center?: Point): void {
    const id = String(nodeId || "").trim()
    if (!id) throw new TypeError("Layout pin requires a node id.")
    const value = center ?? this.#snapshot?.nodes.get(id)?.center
    if (!value) throw new TypeError(`Cannot pin unknown topology node ${id}.`)
    this.#pins.set(id, point(value.x, value.y))
  }

  unpin(nodeId: string): boolean {
    return this.#pins.delete(nodeId)
  }

  clearPins(): void {
    this.#pins.clear()
  }

  pinnedNodeIds(): readonly string[] {
    return Object.freeze([...this.#pins.keys()].sort())
  }

  restore(snapshot: LayoutSnapshot): void {
    this.#snapshot = snapshot
    this.#revision = Math.max(this.#revision, snapshot.revision)
    this.#pins.clear()
    for (const node of snapshot.nodes.values()) {
      if (node.pinned) this.#pins.set(node.id, node.center)
    }
  }

  compute(model: TopologyGraphModel): LayoutSnapshot {
    const previous = this.#snapshot
    const edges = eligibleEdges(model)
    const activeNodes = model.nodes
      .filter((node) => !node.removed && node.effective)
      .sort(
        (left, right) =>
          left.sequence - right.sequence || left.id.localeCompare(right.id),
      )
    const components = connectedComponents(activeNodes, edges)
    const rankComponents: RankNode[][] = []
    let collisionCount = 0
    for (const component of components) {
      const ranks = assignRanks(component, edges, model)
      const componentNodes = component.nodeIds
        .map((id) => model.nodeById.get(id))
        .filter((value): value is GraphNodeRecord => Boolean(value))
      const grouped = new Map<number, GraphNodeRecord[]>()
      for (const node of componentNodes) {
        const rank = ranks.get(node.id) ?? node.depth
        const values = grouped.get(rank) ?? []
        values.push(node)
        grouped.set(rank, values)
      }
      const rankNodes: RankNode[] = []
      for (const [rank, values] of [...grouped.entries()].sort(
        ([left], [right]) => left - right,
      )) {
        values.sort((left, right) => {
          const leftPrevious = previous?.nodes.get(left.id)
          const rightPrevious = previous?.nodes.get(right.id)
          if (leftPrevious && rightPrevious) {
            return (
              leftPrevious.order - rightPrevious.order ||
              left.id.localeCompare(right.id)
            )
          }
          if (leftPrevious) return -1
          if (rightPrevious) return 1
          return left.sequence - right.sequence || left.id.localeCompare(right.id)
        })
        values.forEach((node, order) => {
          rankNodes.push({
            id: node.id,
            rank,
            order,
            component: component.id,
            size: nodeSize(node, this.options),
            position: point(),
            previous: previous?.nodes.get(node.id),
            pinned: this.#pins.has(node.id),
          })
        })
      }
      const componentEdges = edges.filter((edge) => component.edgeIds.includes(edge.id))
      reduceCrossings(rankNodes, componentEdges, this.options.crossingPasses)
      initialPositions(rankNodes, this.options, point())
      blendPreviousPositions(rankNodes, this.options)
      for (const node of rankNodes) {
        const pinned = this.#pins.get(node.id)
        if (pinned) node.position = pinned
      }
      relaxPositions(rankNodes, componentEdges, this.options)
      collisionCount += separateRanks(rankNodes, this.options)
      collisionCount += resolveGlobalCollisions(rankNodes, this.options)
      rankComponents.push(rankNodes)
    }
    arrangeComponents(rankComponents, this.options)
    const allRankNodes = rankComponents.flat()
    for (const node of allRankNodes) {
      const pinned = this.#pins.get(node.id)
      if (pinned) node.position = pinned
    }
    restoreIncrementalAnchors(allRankNodes)
    collisionCount += resolveGlobalCollisions(allRankNodes, this.options)
    const nodeMap = new Map<string, LayoutNode>()
    for (const value of allRankNodes) {
      nodeMap.set(value.id, layoutNodeFromRank(value, this.options))
    }
    const edgeMap = buildLayoutEdges(model, nodeMap, this.options)
    const bounds =
      nodeMap.size === 0
        ? rect()
        : expandRect(
            unionRects([
              ...[...nodeMap.values()].map((node) => node.bounds),
              ...[...edgeMap.values()].map((edge) => edge.bounds),
            ]),
            this.options.padding,
          )
    const previousIds = new Set(previous?.nodes.keys() ?? [])
    const currentIds = new Set(nodeMap.keys())
    const addedNodeIds = [...currentIds].filter((id) => !previousIds.has(id)).sort()
    const removedNodeIds = [...previousIds].filter((id) => !currentIds.has(id)).sort()
    const movedNodeIds = [...currentIds]
      .filter((id) => {
        const before = previous?.nodes.get(id)
        const after = nodeMap.get(id)
        return Boolean(before && after && !samePoint(before.center, after.center, 1))
      })
      .sort()
    const stableNodeCount = [...nodeMap.values()].filter((node) => node.stable).length
    this.#revision += 1
    const snapshot: LayoutSnapshot = Object.freeze({
      revision: this.#revision,
      graphRevision: model.graphRevision,
      nodes: nodeMap,
      edges: edgeMap,
      bounds,
      addedNodeIds: Object.freeze(addedNodeIds),
      movedNodeIds: Object.freeze(movedNodeIds),
      removedNodeIds: Object.freeze(removedNodeIds),
      stableNodeCount,
      unstableNodeCount: nodeMap.size - stableNodeCount,
      collisionCount,
    })
    this.#snapshot = snapshot
    for (const id of [...this.#pins.keys()]) {
      if (!nodeMap.has(id)) this.#pins.delete(id)
    }
    return snapshot
  }

  movePinnedNode(nodeId: string, nextCenter: Point): LayoutSnapshot | undefined {
    if (!this.#snapshot?.nodes.has(nodeId)) return this.#snapshot
    this.#pins.set(nodeId, point(nextCenter.x, nextCenter.y))
    const nodes = new Map(this.#snapshot.nodes)
    const existing = nodes.get(nodeId)!
    const center = snapPoint(nextCenter, this.options.grid)
    nodes.set(
      nodeId,
      Object.freeze({
        ...existing,
        center,
        bounds: rectFromCenter(center, existing.size),
        pinned: true,
        stable: false,
      }),
    )
    const translatedEdges = new Map(this.#snapshot.edges)
    for (const edge of translatedEdges.values()) {
      if (edge.sourceId !== nodeId && edge.targetId !== nodeId) continue
      const source = nodes.get(edge.sourceId)
      const target = nodes.get(edge.targetId)
      if (!source || !target) continue
      const modelEdge: GraphEdgeRecord = {
        id: edge.id,
        sourceId: edge.sourceId,
        targetId: edge.targetId,
        kind: "explicit",
        relation: "interactive",
        sequence: 0,
        revision: 0,
        graphRevision: this.#snapshot.graphRevision,
        commitRevision: 0,
        weight: 1,
        missing: false,
        inferred: false,
        runtimeMutation: false,
        effective: true,
        pending: false,
        removed: false,
        routeIds: Object.freeze([]),
        evidence: {
          eventIds: Object.freeze([]),
          mutationIds: Object.freeze([]),
          correlationIds: Object.freeze([]),
          causationIds: Object.freeze([]),
          spanIds: Object.freeze([]),
          parentSpanIds: Object.freeze([]),
          checkpointIds: Object.freeze([]),
          controlCommandIds: Object.freeze([]),
          graphIds: Object.freeze([]),
          graphRevisions: Object.freeze([]),
          entityRefs: Object.freeze([]),
        },
        source: undefined as never,
      }
      const points = orthogonalEdgePoints(source, target, modelEdge, this.options)
      translatedEdges.set(
        edge.id,
        Object.freeze({
          ...edge,
          points,
          bounds: polylineBounds(points, 8),
          length: polylineLength(points),
        }),
      )
    }
    const bounds = expandRect(
      unionRects([
        ...[...nodes.values()].map((node) => node.bounds),
        ...[...translatedEdges.values()].map((edge) => edge.bounds),
      ]),
      this.options.padding,
    )
    this.#revision += 1
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      revision: this.#revision,
      nodes,
      edges: translatedEdges,
      bounds,
      movedNodeIds: Object.freeze(
        sortedIds([...this.#snapshot.movedNodeIds, nodeId]),
      ),
      stableNodeCount: [...nodes.values()].filter((node) => node.stable).length,
      unstableNodeCount: [...nodes.values()].filter((node) => !node.stable).length,
    })
    return this.#snapshot
  }

  nearestNode(target: Point, maximumDistance = Number.POSITIVE_INFINITY): LayoutNode | undefined {
    let selected: LayoutNode | undefined
    let selectedDistance = Math.max(0, maximumDistance)
    for (const node of this.#snapshot?.nodes.values() ?? []) {
      const candidateDistance = distance(node.center, target)
      if (
        candidateDistance < selectedDistance ||
        (candidateDistance === selectedDistance &&
          (!selected || node.id.localeCompare(selected.id) < 0))
      ) {
        selected = node
        selectedDistance = candidateDistance
      }
    }
    return selected
  }

  centerOfNode(nodeId: string): Point | undefined {
    const value = this.#snapshot?.nodes.get(nodeId)
    return value ? point(value.center.x, value.center.y) : undefined
  }

  componentCenter(component: number): Point | undefined {
    const values = [...(this.#snapshot?.nodes.values() ?? [])].filter(
      (node) => node.component === component,
    )
    return values.length === 0 ? undefined : centerOf(unionRects(values.map((node) => node.bounds)))
  }
}

function sortedIds(values: readonly string[]): string[] {
  return [...new Set(values)].sort()
}
