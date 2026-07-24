import type {
  TraceNode,
  TraceVirtualItem,
  TraceVirtualWindow,
  TraceVirtualWindowOptions,
} from "../contracts.ts"

class FenwickTree {
  readonly #values: number[]
  readonly #tree: number[]

  constructor(size: number, initial: number) {
    this.#values = new Array<number>(size).fill(initial)
    this.#tree = new Array<number>(size + 1).fill(0)
    for (let index = 0; index < size; index += 1) this.#add(index, initial)
  }

  get size(): number {
    return this.#values.length
  }

  value(index: number): number {
    return this.#values[index] ?? 0
  }

  set(index: number, value: number): boolean {
    if (index < 0 || index >= this.#values.length) return false
    const bounded = Math.max(1, Math.min(4_096, value))
    const previous = this.#values[index] ?? 0
    if (previous === bounded) return false
    this.#values[index] = bounded
    this.#add(index, bounded - previous)
    return true
  }

  prefix(exclusive: number): number {
    let cursor = Math.max(0, Math.min(this.#values.length, exclusive))
    let total = 0
    while (cursor > 0) {
      total += this.#tree[cursor] ?? 0
      cursor -= cursor & -cursor
    }
    return total
  }

  total(): number {
    return this.prefix(this.#values.length)
  }

  lowerBound(offset: number): number {
    if (!this.#values.length) return 0
    const target = Math.max(0, Math.min(this.total(), offset))
    let index = 0
    let sum = 0
    let bit = 1
    while (bit * 2 <= this.#values.length) bit *= 2
    while (bit > 0) {
      const next = index + bit
      if (next <= this.#values.length && sum + (this.#tree[next] ?? 0) <= target) {
        index = next
        sum += this.#tree[next] ?? 0
      }
      bit = Math.floor(bit / 2)
    }
    return Math.max(0, Math.min(this.#values.length - 1, index))
  }

  #add(index: number, delta: number): void {
    let cursor = index + 1
    while (cursor < this.#tree.length) {
      this.#tree[cursor] = (this.#tree[cursor] ?? 0) + delta
      cursor += cursor & -cursor
    }
  }
}

export interface TraceVirtualizerAudit {
  closed: boolean
  rowCount: number
  measuredCount: number
  totalHeight: number
  revision: number
  windowCount: number
  measurementCount: number
  ignoredMeasurementCount: number
  lastFirstIndex: number
  lastLastIndex: number
  lastPinnedCount: number
}

function boundedNumber(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  if (!Number.isFinite(value)) return fallback
  return Math.max(minimum, Math.min(maximum, Number(value)))
}

function nodeRevisionKey(nodes: readonly TraceNode[]): string {
  const first = nodes[0]
  const last = nodes.at(-1)
  return [
    nodes.length,
    first?.key ?? "",
    first?.sequence ?? 0,
    last?.key ?? "",
    last?.endSequence ?? 0,
  ].join(":")
}

function sortedUnique(values: readonly number[]): number[] {
  return [...new Set(values)].sort((left, right) => left - right)
}

export class TraceVirtualizer {
  #nodes: readonly TraceNode[]
  #indexByKey = new Map<string, number>()
  #heights: FenwickTree
  #estimatedHeight: number
  #measured = new Set<string>()
  #revision = 0
  #closed = false
  #windowCount = 0
  #measurementCount = 0
  #ignoredMeasurementCount = 0
  #lastFirstIndex = 0
  #lastLastIndex = -1
  #lastPinnedCount = 0

  constructor(
    nodes: readonly TraceNode[],
    options: { estimatedRowHeight?: number } = {},
  ) {
    this.#estimatedHeight = boundedNumber(options.estimatedRowHeight, 96, 24, 512)
    this.#nodes = Object.freeze([...nodes])
    this.#heights = new FenwickTree(nodes.length, this.#estimatedHeight)
    this.#rebuildIndex()
  }

  setNodes(nodes: readonly TraceNode[]): void {
    if (this.#closed) throw new Error("Trace virtualizer is closed.")
    if (nodeRevisionKey(nodes) === nodeRevisionKey(this.#nodes)) return
    const previousHeights = new Map<string, number>()
    for (const [key, index] of this.#indexByKey) {
      if (this.#measured.has(key)) previousHeights.set(key, this.#heights.value(index))
    }
    this.#nodes = Object.freeze([...nodes])
    this.#heights = new FenwickTree(nodes.length, this.#estimatedHeight)
    this.#measured.clear()
    this.#rebuildIndex()
    for (const [key, height] of previousHeights) {
      const index = this.#indexByKey.get(key)
      if (index === undefined) continue
      this.#heights.set(index, height)
      this.#measured.add(key)
    }
    this.#revision += 1
  }

  measure(key: string, height: number): boolean {
    if (this.#closed) return false
    const index = this.#indexByKey.get(key)
    if (index === undefined || !Number.isFinite(height) || height <= 0) {
      this.#ignoredMeasurementCount += 1
      return false
    }
    const changed = this.#heights.set(index, height)
    this.#measured.add(key)
    if (changed) {
      this.#measurementCount += 1
      this.#revision += 1
    }
    return changed
  }

  measureBatch(values: Readonly<Record<string, number>>): number {
    let changed = 0
    for (const [key, height] of Object.entries(values)) if (this.measure(key, height)) changed += 1
    return changed
  }

  offsetForKey(key: string): number | undefined {
    const index = this.#indexByKey.get(key)
    return index === undefined ? undefined : this.#heights.prefix(index)
  }

  indexForKey(key: string): number | undefined {
    return this.#indexByKey.get(key)
  }

  keyAtOffset(offset: number): string | undefined {
    if (!this.#nodes.length) return undefined
    return this.#nodes[this.#heights.lowerBound(offset)]?.key
  }

  window(options: TraceVirtualWindowOptions): TraceVirtualWindow {
    if (this.#closed) throw new Error("Trace virtualizer is closed.")
    const total = this.#nodes.length
    if (!total) return this.#emptyWindow()
    const scrollTop = boundedNumber(options.scrollTop, 0, 0, Number.MAX_SAFE_INTEGER)
    const viewportHeight = boundedNumber(options.viewportHeight, 600, 1, 100_000)
    const overscan = boundedNumber(options.overscanPx, viewportHeight, 0, 1_000_000)
    const startOffset = Math.max(0, scrollTop - overscan)
    const endOffset = Math.min(this.#heights.total(), scrollTop + viewportHeight + overscan)
    let firstIndex = this.#heights.lowerBound(startOffset)
    let lastIndex = this.#heights.lowerBound(endOffset)
    const anchorIndex = options.anchorKey ? this.#indexByKey.get(options.anchorKey) : undefined
    const pinnedIndexes = (options.pinnedKeys ?? [])
      .map((key) => this.#indexByKey.get(key))
      .filter((index): index is number => index !== undefined)
    if (anchorIndex !== undefined) {
      firstIndex = Math.min(firstIndex, anchorIndex)
      lastIndex = Math.max(lastIndex, anchorIndex)
    }
    const indexes = sortedUnique([
      ...Array.from({ length: Math.max(0, lastIndex - firstIndex + 1) }, (_, offset) => firstIndex + offset),
      ...pinnedIndexes,
    ].filter((index) => index >= 0 && index < total))
    const pinnedSet = new Set(pinnedIndexes)
    const items = Object.freeze(indexes.map((index) => {
      const node = this.#nodes[index]!
      return Object.freeze({
        key: node.key,
        index,
        top: this.#heights.prefix(index),
        height: this.#heights.value(index),
        measured: this.#measured.has(node.key),
        pinned: pinnedSet.has(index),
      }) satisfies TraceVirtualItem
    }))
    this.#windowCount += 1
    this.#lastFirstIndex = firstIndex
    this.#lastLastIndex = lastIndex
    this.#lastPinnedCount = pinnedIndexes.length
    return Object.freeze({
      items,
      firstIndex,
      lastIndex,
      beforeHeight: this.#heights.prefix(firstIndex),
      afterHeight: Math.max(0, this.#heights.total() - this.#heights.prefix(lastIndex + 1)),
      totalHeight: this.#heights.total(),
      total,
      anchorIndex,
      revisionKey: `${nodeRevisionKey(this.#nodes)}:${this.#revision}:${firstIndex}:${lastIndex}:${pinnedIndexes.join(",")}`,
    })
  }

  visibleKeys(options: TraceVirtualWindowOptions): readonly string[] {
    return Object.freeze(this.window(options).items.map((item) => item.key))
  }

  scrollAdjustmentForMeasurement(
    key: string,
    previousHeight: number,
    nextHeight: number,
    anchorKey?: string,
  ): number {
    const index = this.#indexByKey.get(key)
    if (index === undefined) return 0
    const anchorIndex = anchorKey ? this.#indexByKey.get(anchorKey) : undefined
    if (anchorIndex === undefined || index >= anchorIndex) return 0
    const previous = boundedNumber(previousHeight, this.#estimatedHeight, 1, 4_096)
    const next = boundedNumber(nextHeight, previous, 1, 4_096)
    return next - previous
  }

  estimateHeightFromNode(node: TraceNode): number {
    let height = this.#estimatedHeight
    height += Math.min(96, Math.ceil(node.summary.length / 72) * 18)
    height += Math.min(72, node.tags.length * 4)
    height += Math.min(96, node.diagnostics.length * 16)
    if (node.childKeys.length) height += 12
    if (node.identity.kind === "issue") height += 24
    return Math.max(24, Math.min(512, height))
  }

  estimateUnmeasured(): number {
    let changed = 0
    for (const node of this.#nodes) {
      if (this.#measured.has(node.key)) continue
      const index = this.#indexByKey.get(node.key)
      if (index === undefined) continue
      if (this.#heights.set(index, this.estimateHeightFromNode(node))) changed += 1
    }
    if (changed) this.#revision += 1
    return changed
  }

  resetMeasurements(keys?: readonly string[]): void {
    if (!keys) {
      this.#heights = new FenwickTree(this.#nodes.length, this.#estimatedHeight)
      this.#measured.clear()
      this.#revision += 1
      return
    }
    let changed = false
    for (const key of keys) {
      const index = this.#indexByKey.get(key)
      if (index === undefined) continue
      changed = this.#heights.set(index, this.#estimatedHeight) || changed
      this.#measured.delete(key)
    }
    if (changed) this.#revision += 1
  }

  close(): void {
    this.#closed = true
    this.#nodes = Object.freeze([])
    this.#indexByKey.clear()
    this.#measured.clear()
    this.#heights = new FenwickTree(0, this.#estimatedHeight)
  }

  audit(): TraceVirtualizerAudit {
    return Object.freeze({
      closed: this.#closed,
      rowCount: this.#nodes.length,
      measuredCount: this.#measured.size,
      totalHeight: this.#heights.total(),
      revision: this.#revision,
      windowCount: this.#windowCount,
      measurementCount: this.#measurementCount,
      ignoredMeasurementCount: this.#ignoredMeasurementCount,
      lastFirstIndex: this.#lastFirstIndex,
      lastLastIndex: this.#lastLastIndex,
      lastPinnedCount: this.#lastPinnedCount,
    })
  }

  #rebuildIndex(): void {
    this.#indexByKey = new Map()
    this.#nodes.forEach((node, index) => this.#indexByKey.set(node.key, index))
  }

  #emptyWindow(): TraceVirtualWindow {
    return Object.freeze({
      items: Object.freeze([]),
      firstIndex: 0,
      lastIndex: -1,
      beforeHeight: 0,
      afterHeight: 0,
      totalHeight: 0,
      total: 0,
      revisionKey: `empty:${this.#revision}`,
    })
  }
}

export function virtualizeTraceNodes(
  nodes: readonly TraceNode[],
  options: TraceVirtualWindowOptions,
): TraceVirtualWindow {
  const virtualizer = new TraceVirtualizer(nodes, {
    estimatedRowHeight: options.estimatedRowHeight,
  })
  virtualizer.estimateUnmeasured()
  const result = virtualizer.window(options)
  virtualizer.close()
  return result
}
