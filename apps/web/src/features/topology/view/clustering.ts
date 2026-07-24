import type {
  ClusterRecord,
  ClusterSnapshot,
  GraphNodeRecord,
  LayoutSnapshot,
  Point,
  Rect,
  TopologyGraphModel,
} from "./contracts.ts"
import {
  centerOf,
  distance,
  expandRect,
  point,
  rect,
  rectArea,
  stableHash,
  unionRects,
} from "./geometry.ts"

export interface ClusterOptions {
  minimumClusterSize?: number
  maximumClusterSize?: number
  targetClusterSize?: number
  maximumLevels?: number
  padding?: number
  namespaceFirst?: boolean
}

interface NormalizedOptions {
  minimumClusterSize: number
  maximumClusterSize: number
  targetClusterSize: number
  maximumLevels: number
  padding: number
  namespaceFirst: boolean
}

interface MutableCluster {
  id: string
  level: number
  parentId?: string
  nodeIds: string[]
  childClusterIds: string[]
  bounds: Rect
}

function integer(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  return Number.isFinite(value)
    ? Math.floor(Math.min(maximum, Math.max(minimum, Number(value))))
    : fallback
}

function normalizeOptions(options: ClusterOptions): NormalizedOptions {
  const minimumClusterSize = integer(options.minimumClusterSize, 4, 2, 1_000)
  const maximumClusterSize = integer(
    options.maximumClusterSize,
    64,
    minimumClusterSize,
    10_000,
  )
  return Object.freeze({
    minimumClusterSize,
    maximumClusterSize,
    targetClusterSize: integer(
      options.targetClusterSize,
      24,
      minimumClusterSize,
      maximumClusterSize,
    ),
    maximumLevels: integer(options.maximumLevels, 6, 1, 16),
    padding: integer(options.padding, 22, 0, 500),
    namespaceFirst: options.namespaceFirst !== false,
  })
}

function nodeBounds(layout: LayoutSnapshot, ids: readonly string[]): Rect {
  return unionRects(
    ids
      .map((id) => layout.nodes.get(id)?.bounds)
      .filter((value): value is Rect => Boolean(value)),
  )
}

function splitByNamespace(
  nodes: readonly GraphNodeRecord[],
  minimum: number,
): readonly (readonly GraphNodeRecord[])[] {
  const groups = new Map<string, GraphNodeRecord[]>()
  for (const node of nodes) {
    const key = node.namespace || "root"
    const values = groups.get(key) ?? []
    values.push(node)
    groups.set(key, values)
  }
  const small: GraphNodeRecord[] = []
  const result: GraphNodeRecord[][] = []
  for (const [key, values] of [...groups.entries()].sort(([left], [right]) =>
    left.localeCompare(right),
  )) {
    values.sort(compareNode)
    if (values.length >= minimum) result.push(values)
    else small.push(...values)
    void key
  }
  if (small.length > 0) result.push(small.sort(compareNode))
  return Object.freeze(result.map((values) => Object.freeze(values)))
}

function compareNode(left: GraphNodeRecord, right: GraphNodeRecord): number {
  return (
    left.depth - right.depth ||
    left.sequence - right.sequence ||
    left.id.localeCompare(right.id)
  )
}

function splitSpatially(
  nodes: readonly GraphNodeRecord[],
  layout: LayoutSnapshot,
  targetSize: number,
  maximumSize: number,
): readonly (readonly GraphNodeRecord[])[] {
  if (nodes.length <= maximumSize) return Object.freeze([Object.freeze([...nodes])])
  const values = nodes
    .map((node) => ({ node, layout: layout.nodes.get(node.id) }))
    .filter(
      (entry): entry is { node: GraphNodeRecord; layout: NonNullable<typeof entry.layout> } =>
        Boolean(entry.layout),
    )
  const result: GraphNodeRecord[][] = []
  const queue = [values]
  while (queue.length > 0) {
    const group = queue.shift()!
    if (group.length <= maximumSize) {
      result.push(group.map((entry) => entry.node).sort(compareNode))
      continue
    }
    const bounds = unionRects(group.map((entry) => entry.layout.bounds))
    const horizontal = bounds.width >= bounds.height
    group.sort((left, right) => {
      const a = horizontal ? left.layout.center.x : left.layout.center.y
      const b = horizontal ? right.layout.center.x : right.layout.center.y
      return a - b || left.node.id.localeCompare(right.node.id)
    })
    let split = Math.round(group.length / 2)
    if (group.length > maximumSize * 2) split = targetSize
    split = Math.max(1, Math.min(group.length - 1, split))
    queue.push(group.slice(0, split), group.slice(split))
  }
  return Object.freeze(result.map((group) => Object.freeze(group)))
}

function buildLeafClusters(
  model: TopologyGraphModel,
  layout: LayoutSnapshot,
  options: NormalizedOptions,
): MutableCluster[] {
  const active = model.nodes.filter(
    (node) => !node.removed && node.effective && layout.nodes.has(node.id),
  )
  const namespaceGroups = options.namespaceFirst
    ? splitByNamespace(active, options.minimumClusterSize)
    : [active]
  const result: MutableCluster[] = []
  let index = 0
  for (const namespaceGroup of namespaceGroups) {
    const spatialGroups = splitSpatially(
      namespaceGroup,
      layout,
      options.targetClusterSize,
      options.maximumClusterSize,
    )
    for (const group of spatialGroups) {
      if (group.length < options.minimumClusterSize && result.length > 0) {
        const nearest = nearestCluster(result, nodeBounds(layout, group.map((node) => node.id)))
        if (
          nearest &&
          nearest.nodeIds.length + group.length <= options.maximumClusterSize
        ) {
          nearest.nodeIds.push(...group.map((node) => node.id))
          nearest.nodeIds.sort()
          nearest.bounds = nodeBounds(layout, nearest.nodeIds)
          continue
        }
      }
      const nodeIds = group.map((node) => node.id).sort()
      result.push({
        id: `cluster:0:${String(index).padStart(5, "0")}:${digestIds(nodeIds)}`,
        level: 0,
        nodeIds,
        childClusterIds: [],
        bounds: nodeBounds(layout, nodeIds),
      })
      index += 1
    }
  }
  return result
}

function nearestCluster(
  clusters: readonly MutableCluster[],
  bounds: Rect,
): MutableCluster | undefined {
  const center = centerOf(bounds)
  let selected: MutableCluster | undefined
  let selectedDistance = Number.POSITIVE_INFINITY
  for (const cluster of clusters) {
    const candidateDistance = distance(center, centerOf(cluster.bounds))
    if (
      candidateDistance < selectedDistance ||
      (candidateDistance === selectedDistance &&
        (!selected || cluster.id.localeCompare(selected.id) < 0))
    ) {
      selected = cluster
      selectedDistance = candidateDistance
    }
  }
  return selected
}

function digestIds(ids: readonly string[]): string {
  return stableHash(ids.join("\0")).toString(36).padStart(7, "0")
}

function parentGroups(
  children: readonly MutableCluster[],
  targetSize: number,
): readonly (readonly MutableCluster[])[] {
  if (children.length <= 1) return Object.freeze([Object.freeze([...children])])
  const remaining = [...children].sort(
    (left, right) =>
      centerOf(left.bounds).x - centerOf(right.bounds).x ||
      centerOf(left.bounds).y - centerOf(right.bounds).y ||
      left.id.localeCompare(right.id),
  )
  const result: MutableCluster[][] = []
  const parentTarget = Math.max(2, Math.round(Math.sqrt(targetSize)))
  while (remaining.length > 0) {
    const seed = remaining.shift()!
    const seedCenter = centerOf(seed.bounds)
    remaining.sort(
      (left, right) =>
        distance(seedCenter, centerOf(left.bounds)) -
          distance(seedCenter, centerOf(right.bounds)) ||
        left.id.localeCompare(right.id),
    )
    result.push([seed, ...remaining.splice(0, Math.min(parentTarget - 1, remaining.length))])
  }
  return Object.freeze(result.map((values) => Object.freeze(values)))
}

function buildParents(
  children: readonly MutableCluster[],
  level: number,
  options: NormalizedOptions,
): MutableCluster[] {
  const groups = parentGroups(children, options.targetClusterSize)
  const result: MutableCluster[] = []
  groups.forEach((values, index) => {
    const nodeIds = [...new Set(values.flatMap((value) => value.nodeIds))].sort()
    const id = `cluster:${level}:${String(index).padStart(5, "0")}:${digestIds(
      values.map((value) => value.id),
    )}`
    const parent: MutableCluster = {
      id,
      level,
      nodeIds,
      childClusterIds: values.map((value) => value.id).sort(),
      bounds: unionRects(values.map((value) => value.bounds)),
    }
    for (const child of values) child.parentId = id
    result.push(parent)
  })
  return result
}

function aggregateCounts(
  nodeIds: readonly string[],
  model: TopologyGraphModel,
): {
  stateCounts: Readonly<Record<string, number>>
  locationCounts: Readonly<Record<string, number>>
  policyViolationCount: number
  changedCount: number
  routeCount: number
} {
  const stateCounts: Record<string, number> = {}
  const locationCounts: Record<string, number> = {}
  let policyViolationCount = 0
  let changedCount = 0
  let routeCount = 0
  for (const id of nodeIds) {
    const node = model.nodeById.get(id)
    if (!node) continue
    stateCounts[node.state] = (stateCounts[node.state] ?? 0) + 1
    locationCounts[node.location] = (locationCounts[node.location] ?? 0) + 1
    if (node.policyViolation) policyViolationCount += 1
    if (
      node.openWorldCreated ||
      node.openWorldRemoved ||
      node.openWorldReplaced ||
      node.affectedByChangeIds.length > 0
    ) {
      changedCount += 1
    }
    routeCount += model.routesByNode.get(id)?.length ?? 0
  }
  return {
    stateCounts: Object.freeze(stateCounts),
    locationCounts: Object.freeze(locationCounts),
    policyViolationCount,
    changedCount,
    routeCount,
  }
}

function freezeCluster(
  value: MutableCluster,
  model: TopologyGraphModel,
  padding: number,
): ClusterRecord {
  const counts = aggregateCounts(value.nodeIds, model)
  const bounds = expandRect(value.bounds, padding + value.level * 6)
  const area = Math.max(1, rectArea(bounds))
  return Object.freeze({
    id: value.id,
    level: value.level,
    parentId: value.parentId,
    nodeIds: Object.freeze([...value.nodeIds]),
    childClusterIds: Object.freeze([...value.childClusterIds]),
    bounds,
    center: centerOf(bounds),
    stateCounts: counts.stateCounts,
    locationCounts: counts.locationCounts,
    policyViolationCount: counts.policyViolationCount,
    changedCount: counts.changedCount,
    routeCount: counts.routeCount,
    density: value.nodeIds.length / area,
  })
}

export class TopologyClusterEngine {
  readonly options: NormalizedOptions
  #revision = 0
  #snapshot: ClusterSnapshot = Object.freeze({
    revision: 0,
    levels: Object.freeze([]),
    clusters: Object.freeze([]),
    clusterById: new Map(),
    nodeToCluster: new Map(),
  })

  constructor(options: ClusterOptions = {}) {
    this.options = normalizeOptions(options)
  }

  get snapshot(): ClusterSnapshot {
    return this.#snapshot
  }

  compute(model: TopologyGraphModel, layout: LayoutSnapshot): ClusterSnapshot {
    const leaf = buildLeafClusters(model, layout, this.options)
    const levels: MutableCluster[][] = []
    if (leaf.length > 0) levels.push(leaf)
    let current = leaf
    for (
      let level = 1;
      level < this.options.maximumLevels && current.length > 1;
      level += 1
    ) {
      current = buildParents(current, level, this.options)
      levels.push(current)
    }
    const clusters = levels
      .flat()
      .map((value) => freezeCluster(value, model, this.options.padding))
      .sort(
        (left, right) =>
          left.level - right.level || left.id.localeCompare(right.id),
      )
    const clusterById = new Map(clusters.map((value) => [value.id, value]))
    const nodeToCluster = new Map<string, string>()
    for (const cluster of clusters.filter((value) => value.level === 0)) {
      for (const id of cluster.nodeIds) nodeToCluster.set(id, cluster.id)
    }
    this.#revision += 1
    this.#snapshot = Object.freeze({
      revision: this.#revision,
      levels: Object.freeze(levels.map((_, index) => index)),
      clusters: Object.freeze(clusters),
      clusterById,
      nodeToCluster,
    })
    return this.#snapshot
  }

  levelForScale(scale: number, nodeCount: number): number {
    if (this.#snapshot.levels.length === 0 || nodeCount < this.options.minimumClusterSize) {
      return -1
    }
    const normalized = Math.max(0.001, Number.isFinite(scale) ? scale : 1)
    if (normalized >= 0.8) return -1
    const pressure = Math.max(0, Math.log2(Math.max(1, nodeCount / 250)))
    const zoomPressure = Math.max(0, Math.log2(0.8 / normalized))
    return Math.min(
      this.#snapshot.levels.at(-1) ?? 0,
      Math.max(0, Math.floor((pressure + zoomPressure) / 1.5)),
    )
  }

  visibleClusters(
    level: number,
    expandedIds: ReadonlySet<string>,
    viewport?: Rect,
  ): readonly ClusterRecord[] {
    if (level < 0) return Object.freeze([])
    const result: ClusterRecord[] = []
    const visit = (cluster: ClusterRecord): void => {
      if (viewport && !rectIntersects(cluster.bounds, viewport)) return
      if (expandedIds.has(cluster.id) && cluster.childClusterIds.length > 0) {
        for (const childId of cluster.childClusterIds) {
          const child = this.#snapshot.clusterById.get(childId)
          if (child) visit(child)
        }
        return
      }
      result.push(cluster)
    }
    const roots = this.#snapshot.clusters
      .filter((cluster) => cluster.level === level)
      .sort((left, right) => left.id.localeCompare(right.id))
    for (const cluster of roots) visit(cluster)
    return Object.freeze(result)
  }

  visibleNodeIds(
    level: number,
    expandedIds: ReadonlySet<string>,
  ): ReadonlySet<string> {
    if (level < 0) {
      return new Set(
        this.#snapshot.clusters
          .filter((cluster) => cluster.level === 0)
          .flatMap((cluster) => cluster.nodeIds),
      )
    }
    const hidden = new Set<string>()
    for (const cluster of this.visibleClusters(level, expandedIds)) {
      if (!expandedIds.has(cluster.id)) {
        for (const id of cluster.nodeIds) hidden.add(id)
      }
    }
    const all = new Set(
      this.#snapshot.clusters
        .filter((cluster) => cluster.level === 0)
        .flatMap((cluster) => cluster.nodeIds),
    )
    for (const id of hidden) all.delete(id)
    return all
  }

  clusterPath(nodeId: string): readonly ClusterRecord[] {
    const result: ClusterRecord[] = []
    let id = this.#snapshot.nodeToCluster.get(nodeId)
    const visited = new Set<string>()
    while (id) {
      if (visited.has(id)) break
      visited.add(id)
      const cluster = this.#snapshot.clusterById.get(id)
      if (!cluster) break
      result.push(cluster)
      id = cluster.parentId
    }
    return Object.freeze(result)
  }

  expandPath(nodeId: string): ReadonlySet<string> {
    return new Set(this.clusterPath(nodeId).map((cluster) => cluster.id))
  }

  nearestCluster(target: Point, level: number): ClusterRecord | undefined {
    let selected: ClusterRecord | undefined
    let selectedDistance = Number.POSITIVE_INFINITY
    for (const cluster of this.#snapshot.clusters) {
      if (cluster.level !== level) continue
      const candidateDistance = distance(target, cluster.center)
      if (
        candidateDistance < selectedDistance ||
        (candidateDistance === selectedDistance &&
          (!selected || cluster.id.localeCompare(selected.id) < 0))
      ) {
        selected = cluster
        selectedDistance = candidateDistance
      }
    }
    return selected
  }
}

function rectIntersects(left: Rect, right: Rect): boolean {
  return !(
    left.x + left.width < right.x ||
    right.x + right.width < left.x ||
    left.y + left.height < right.y ||
    right.y + right.height < left.y
  )
}

export function clusterMinimapRecords(
  snapshot: ClusterSnapshot,
  level = snapshot.levels.at(-1) ?? 0,
): readonly {
  id: string
  bounds: Rect
  center: Point
  count: number
  severity: number
}[] {
  return Object.freeze(
    snapshot.clusters
      .filter((cluster) => cluster.level === level)
      .map((cluster) =>
        Object.freeze({
          id: cluster.id,
          bounds: cluster.bounds,
          center: cluster.center,
          count: cluster.nodeIds.length,
          severity:
            cluster.nodeIds.length === 0
              ? 0
              : cluster.policyViolationCount / cluster.nodeIds.length,
        }),
      )
      .sort((left, right) => left.id.localeCompare(right.id)),
  )
}

export function emptyClusterBounds(): Rect {
  return rect()
}
