import type {
  LayoutSnapshot,
  Point,
  Rect,
  SpatialItem,
  SpatialQuery,
  SpatialQueryResult,
} from "./contracts.ts"
import {
  clamp,
  containsPoint,
  distance,
  distanceToSegment,
  expandRect,
  gridCell,
  intersectsRect,
  point,
  rect,
  rectArea,
  rectCells,
} from "./geometry.ts"

export interface SpatialIndexOptions {
  cellSize?: number
  minimumCellSize?: number
  maximumCellSize?: number
  targetItemsPerCell?: number
  maximumCellsPerItem?: number
  maximumQueryCells?: number
}
interface NormalizedOptions {
  cellSize?: number
  minimumCellSize: number
  maximumCellSize: number
  targetItemsPerCell: number
  maximumCellsPerItem: number
  maximumQueryCells: number
}

export interface HitTestOptions {
  kinds?: readonly SpatialItem["kind"][]
  radius?: number
  maximum?: number
}

export interface SpatialIndexSnapshot {
  revision: number
  itemCount: number
  cellCount: number
  overflowCount: number
  cellSize: number
  bounds: Rect
  averageItemsPerCell: number
  maximumItemsPerCell: number
}

function finiteBounded(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  return Number.isFinite(value)
    ? Math.min(maximum, Math.max(minimum, Number(value)))
    : fallback
}

function normalizeOptions(options: SpatialIndexOptions): NormalizedOptions {
  const minimumCellSize = finiteBounded(options.minimumCellSize, 80, 8, 10_000)
  const maximumCellSize = finiteBounded(
    options.maximumCellSize,
    1_200,
    minimumCellSize,
    100_000,
  )
  return Object.freeze({
    cellSize: Number.isFinite(options.cellSize)
      ? finiteBounded(options.cellSize, 240, minimumCellSize, maximumCellSize)
      : undefined,
    minimumCellSize,
    maximumCellSize,
    targetItemsPerCell: finiteBounded(options.targetItemsPerCell, 24, 1, 2_000),
    maximumCellsPerItem: Math.floor(
      finiteBounded(options.maximumCellsPerItem, 128, 1, 100_000),
    ),
    maximumQueryCells: Math.floor(
      finiteBounded(options.maximumQueryCells, 20_000, 10, 1_000_000),
    ),
  })
}

function itemKey(item: SpatialItem): string {
  return `${item.kind}:${item.id}`
}

function estimateCellSize(
  items: readonly SpatialItem[],
  bounds: Rect,
  options: NormalizedOptions,
): number {
  if (options.cellSize !== undefined) return options.cellSize
  if (items.length === 0 || rectArea(bounds) === 0) return options.minimumCellSize
  const desiredCells = Math.max(1, items.length / options.targetItemsPerCell)
  const size = Math.sqrt(rectArea(bounds) / desiredCells)
  return clamp(size, options.minimumCellSize, options.maximumCellSize)
}

function compareItem(left: SpatialItem, right: SpatialItem): number {
  return (
    right.zIndex - left.zIndex ||
    kindPriority(left.kind) - kindPriority(right.kind) ||
    left.id.localeCompare(right.id)
  )
}

function kindPriority(kind: SpatialItem["kind"]): number {
  if (kind === "node") return 0
  if (kind === "cluster") return 1
  return 2
}

export class TopologySpatialIndex {
  readonly options: NormalizedOptions
  #cells = new Map<string, Set<string>>()
  #items = new Map<string, SpatialItem>()
  #cellsByItem = new Map<string, readonly string[]>()
  #overflow = new Set<string>()
  #revision = 0
  #cellSize: number
  #bounds: Rect = rect()

  constructor(options: SpatialIndexOptions = {}) {
    this.options = normalizeOptions(options)
    this.#cellSize = this.options.cellSize ?? this.options.minimumCellSize
  }

  get snapshot(): SpatialIndexSnapshot {
    let total = 0
    let maximum = 0
    for (const values of this.#cells.values()) {
      total += values.size
      maximum = Math.max(maximum, values.size)
    }
    return Object.freeze({
      revision: this.#revision,
      itemCount: this.#items.size,
      cellCount: this.#cells.size,
      overflowCount: this.#overflow.size,
      cellSize: this.#cellSize,
      bounds: this.#bounds,
      averageItemsPerCell: this.#cells.size === 0 ? 0 : total / this.#cells.size,
      maximumItemsPerCell: maximum,
    })
  }

  get itemCount(): number {
    return this.#items.size
  }

  get cellSize(): number {
    return this.#cellSize
  }

  rebuild(items: readonly SpatialItem[], bounds: Rect): SpatialIndexSnapshot {
    this.#cells.clear()
    this.#items.clear()
    this.#cellsByItem.clear()
    this.#overflow.clear()
    this.#bounds = bounds
    this.#cellSize = estimateCellSize(items, bounds, this.options)
    for (const item of [...items].sort(compareItem)) this.#insert(item)
    this.#revision += 1
    return this.snapshot
  }

  rebuildLayout(layout: LayoutSnapshot): SpatialIndexSnapshot {
    const items: SpatialItem[] = []
    for (const node of layout.nodes.values()) {
      items.push(
        Object.freeze({
          id: node.id,
          kind: "node",
          bounds: node.bounds,
          zIndex: 20,
        }),
      )
    }
    for (const edge of layout.edges.values()) {
      items.push(
        Object.freeze({
          id: edge.id,
          kind: "edge",
          bounds: edge.bounds,
          zIndex: 5,
        }),
      )
    }
    return this.rebuild(items, layout.bounds)
  }

  upsert(item: SpatialItem): SpatialIndexSnapshot {
    const key = itemKey(item)
    this.#removeKey(key)
    this.#insert(item)
    this.#revision += 1
    return this.snapshot
  }

  remove(kind: SpatialItem["kind"], id: string): boolean {
    const removed = this.#removeKey(`${kind}:${id}`)
    if (removed) this.#revision += 1
    return removed
  }

  item(kind: SpatialItem["kind"], id: string): SpatialItem | undefined {
    return this.#items.get(`${kind}:${id}`)
  }

  query(input: SpatialQuery): SpatialQueryResult {
    const maximum = Math.max(1, Math.floor(input.maximum))
    const viewport = expandRect(input.viewport, Math.max(0, input.overscan))
    const allowedKinds = input.kinds ? new Set(input.kinds) : undefined
    const cellIds = rectCells(viewport, this.#cellSize, this.options.maximumQueryCells)
    const candidateKeys = new Set<string>()
    for (const cellId of cellIds) {
      for (const key of this.#cells.get(cellId) ?? []) candidateKeys.add(key)
    }
    for (const key of this.#overflow) candidateKeys.add(key)
    const candidates: SpatialItem[] = []
    for (const key of candidateKeys) {
      const item = this.#items.get(key)
      if (!item) continue
      if (allowedKinds && !allowedKinds.has(item.kind)) continue
      if (!intersectsRect(item.bounds, viewport)) continue
      candidates.push(item)
    }
    candidates.sort(compareItem)
    const selected = candidates.slice(0, maximum)
    const nodeIds: string[] = []
    const edgeIds: string[] = []
    const clusterIds: string[] = []
    for (const item of selected) {
      if (item.kind === "node") nodeIds.push(item.id)
      else if (item.kind === "edge") edgeIds.push(item.id)
      else clusterIds.push(item.id)
    }
    return Object.freeze({
      itemIds: Object.freeze(selected.map(itemKey)),
      nodeIds: Object.freeze(nodeIds),
      edgeIds: Object.freeze(edgeIds),
      clusterIds: Object.freeze(clusterIds),
      visitedCells: cellIds.length,
      candidateCount: candidates.length,
      clipped: candidates.length > selected.length,
    })
  }

  queryRect(
    viewport: Rect,
    options: {
      overscan?: number
      maximum?: number
      kinds?: readonly SpatialItem["kind"][]
    } = {},
  ): readonly SpatialItem[] {
    const result = this.query({
      viewport,
      overscan: options.overscan ?? 0,
      maximum: options.maximum ?? 10_000,
      kinds: options.kinds,
    })
    return Object.freeze(
      result.itemIds
        .map((key) => this.#items.get(key))
        .filter((value): value is SpatialItem => Boolean(value)),
    )
  }

  hitTest(target: Point, options: HitTestOptions = {}): readonly SpatialItem[] {
    const radius = Math.max(0, options.radius ?? 8)
    const maximum = Math.max(1, Math.floor(options.maximum ?? 8))
    const kinds = options.kinds ? new Set(options.kinds) : undefined
    const queryBounds = rect(target.x - radius, target.y - radius, radius * 2, radius * 2)
    const candidates = this.queryRect(queryBounds, {
      overscan: radius,
      maximum: Math.max(maximum * 10, 100),
      kinds: options.kinds,
    })
    const ranked = candidates
      .filter((item) => !kinds || kinds.has(item.kind))
      .map((item) => ({
        item,
        distance: this.#distanceToItem(target, item),
      }))
      .filter((entry) => entry.distance <= radius || containsPoint(entry.item.bounds, target))
      .sort(
        (left, right) =>
          left.distance - right.distance || compareItem(left.item, right.item),
      )
      .slice(0, maximum)
      .map((entry) => entry.item)
    return Object.freeze(ranked)
  }

  nearest(
    target: Point,
    options: HitTestOptions & { maximumDistance?: number } = {},
  ): SpatialItem | undefined {
    const maximumDistance = Math.max(0, options.maximumDistance ?? Number.POSITIVE_INFINITY)
    const radius = Number.isFinite(maximumDistance)
      ? maximumDistance
      : Math.max(this.#bounds.width, this.#bounds.height, this.#cellSize)
    const candidates = this.queryRect(
      rect(target.x - radius, target.y - radius, radius * 2, radius * 2),
      {
        maximum: Math.max(1_000, this.#items.size),
        kinds: options.kinds,
      },
    )
    let selected: SpatialItem | undefined
    let selectedDistance = maximumDistance
    for (const item of candidates) {
      const candidateDistance = this.#distanceToItem(target, item)
      if (
        candidateDistance < selectedDistance ||
        (candidateDistance === selectedDistance &&
          (!selected || compareItem(item, selected) < 0))
      ) {
        selected = item
        selectedDistance = candidateDistance
      }
    }
    return selected
  }

  itemsInCell(column: number, row: number): readonly SpatialItem[] {
    const keys = this.#cells.get(`${Math.floor(column)}:${Math.floor(row)}`) ?? []
    return Object.freeze(
      [...keys]
        .map((key) => this.#items.get(key))
        .filter((value): value is SpatialItem => Boolean(value))
        .sort(compareItem),
    )
  }

  occupiedCells(): readonly {
    column: number
    row: number
    itemCount: number
    bounds: Rect
  }[] {
    return Object.freeze(
      [...this.#cells.entries()]
        .map(([key, values]) => {
          const [columnText, rowText] = key.split(":")
          const column = Number(columnText)
          const row = Number(rowText)
          return Object.freeze({
            column,
            row,
            itemCount: values.size,
            bounds: rect(
              column * this.#cellSize,
              row * this.#cellSize,
              this.#cellSize,
              this.#cellSize,
            ),
          })
        })
        .sort(
          (left, right) =>
            left.row - right.row || left.column - right.column,
        ),
    )
  }

  densityAt(target: Point, radius = this.#cellSize): number {
    const area = Math.max(1, Math.PI * radius * radius)
    const candidates = this.queryRect(
      rect(target.x - radius, target.y - radius, radius * 2, radius * 2),
      { maximum: this.#items.size },
    )
    const count = candidates.filter(
      (item) => distance(target, centerOfItem(item)) <= radius,
    ).length
    return count / area
  }

  #insert(item: SpatialItem): void {
    const key = itemKey(item)
    if (this.#items.has(key)) throw new TypeError(`Duplicate spatial item ${key}.`)
    const frozen = Object.freeze({
      id: String(item.id),
      kind: item.kind,
      bounds: item.bounds,
      zIndex: Number.isFinite(item.zIndex) ? item.zIndex : 0,
    })
    this.#items.set(key, frozen)
    const cells = rectCells(
      frozen.bounds,
      this.#cellSize,
      this.options.maximumCellsPerItem + 1,
    )
    if (cells.length > this.options.maximumCellsPerItem) {
      this.#overflow.add(key)
      this.#cellsByItem.set(key, Object.freeze([]))
      return
    }
    this.#cellsByItem.set(key, cells)
    for (const cell of cells) {
      const values = this.#cells.get(cell) ?? new Set<string>()
      values.add(key)
      this.#cells.set(cell, values)
    }
  }

  #removeKey(key: string): boolean {
    if (!this.#items.has(key)) return false
    for (const cell of this.#cellsByItem.get(key) ?? []) {
      const values = this.#cells.get(cell)
      values?.delete(key)
      if (values?.size === 0) this.#cells.delete(cell)
    }
    this.#overflow.delete(key)
    this.#cellsByItem.delete(key)
    this.#items.delete(key)
    return true
  }

  #distanceToItem(target: Point, item: SpatialItem): number {
    if (containsPoint(item.bounds, target)) return 0
    if (item.kind === "edge") {
      const start = point(item.bounds.x, item.bounds.y)
      const end = point(
        item.bounds.x + item.bounds.width,
        item.bounds.y + item.bounds.height,
      )
      return distanceToSegment(target, start, end)
    }
    const nearest = point(
      clamp(target.x, item.bounds.x, item.bounds.x + item.bounds.width),
      clamp(target.y, item.bounds.y, item.bounds.y + item.bounds.height),
    )
    return distance(target, nearest)
  }
}

function centerOfItem(item: SpatialItem): Point {
  return point(
    item.bounds.x + item.bounds.width / 2,
    item.bounds.y + item.bounds.height / 2,
  )
}

export function spatialItemsFromLayout(
  layout: LayoutSnapshot,
  clusters: readonly { id: string; bounds: Rect }[] = [],
): readonly SpatialItem[] {
  const result: SpatialItem[] = []
  for (const edge of layout.edges.values()) {
    result.push(
      Object.freeze({
        id: edge.id,
        kind: "edge",
        bounds: edge.bounds,
        zIndex: 5,
      }),
    )
  }
  for (const cluster of clusters) {
    result.push(
      Object.freeze({
        id: cluster.id,
        kind: "cluster",
        bounds: cluster.bounds,
        zIndex: 10,
      }),
    )
  }
  for (const node of layout.nodes.values()) {
    result.push(
      Object.freeze({
        id: node.id,
        kind: "node",
        bounds: node.bounds,
        zIndex: 20,
      }),
    )
  }
  return Object.freeze(result)
}

export function cellForPoint(
  target: Point,
  index: TopologySpatialIndex,
): { column: number; row: number } {
  return gridCell(target, index.cellSize)
}
