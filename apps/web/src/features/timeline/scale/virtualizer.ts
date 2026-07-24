import type { TimelineRow } from "../projection/index.ts"
import type {
  TimelineMeasuredRow,
  TimelineScrollAnchor,
  TimelineVirtualizerOptions,
  TimelineVirtualRange,
} from "./contracts.ts"

function integer(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const candidate = value ?? fallback
  if (!Number.isFinite(candidate)) return fallback
  return Math.max(minimum, Math.min(maximum, Math.floor(candidate)))
}

function finite(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  const candidate = value ?? fallback
  if (!Number.isFinite(candidate)) return fallback
  return Math.max(minimum, Math.min(maximum, candidate))
}

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

class FenwickTree {
  readonly values: Float64Array
  readonly tree: Float64Array

  constructor(values: readonly number[]) {
    this.values = Float64Array.from(values)
    this.tree = new Float64Array(values.length + 1)
    for (let index = 0; index < values.length; index += 1) {
      this.#add(index, values[index])
    }
  }

  get length(): number {
    return this.values.length
  }

  total(): number {
    return this.prefix(this.values.length)
  }

  prefix(count: number): number {
    let index = Math.max(0, Math.min(this.values.length, Math.floor(count)))
    let result = 0
    while (index > 0) {
      result += this.tree[index]
      index -= index & -index
    }
    return result
  }

  value(index: number): number {
    return this.values[index] ?? 0
  }

  update(index: number, value: number): boolean {
    if (index < 0 || index >= this.values.length) return false
    const previous = this.values[index]
    if (previous === value) return false
    this.values[index] = value
    this.#add(index, value - previous)
    return true
  }

  indexAtOffset(offset: number): number {
    if (!this.values.length) return 0
    const target = Math.max(0, Math.min(this.total(), offset))
    let index = 0
    let sum = 0
    let bit = 1
    while (bit << 1 <= this.values.length) bit <<= 1
    while (bit > 0) {
      const next = index + bit
      if (
        next <= this.values.length &&
        sum + this.tree[next] <= target
      ) {
        index = next
        sum += this.tree[next]
      }
      bit >>= 1
    }
    return Math.min(this.values.length - 1, index)
  }

  #add(index: number, delta: number): void {
    let cursor = index + 1
    while (cursor < this.tree.length) {
      this.tree[cursor] += delta
      cursor += cursor & -cursor
    }
  }
}

interface Measurement {
  size: number
  touched: number
}

export class TimelineVirtualizer {
  readonly estimatedRowHeight: number
  readonly minimumRowHeight: number
  readonly maximumRowHeight: number
  readonly overscanPixels: number
  readonly maximumRenderedRows: number
  readonly measurementCacheLimit: number
  #rows: readonly TimelineRow[] = Object.freeze([])
  #indexByKey = new Map<string, number>()
  #measurements = new Map<string, Measurement>()
  #sizes = new FenwickTree([])
  #touch = 0
  #revision = 0
  #lastRange?: TimelineVirtualRange

  constructor(options: TimelineVirtualizerOptions = {}) {
    this.estimatedRowHeight = finite(
      options.estimatedRowHeight,
      72,
      16,
      1000,
    )
    this.minimumRowHeight = finite(
      options.minimumRowHeight,
      28,
      8,
      this.estimatedRowHeight,
    )
    this.maximumRowHeight = finite(
      options.maximumRowHeight,
      640,
      this.estimatedRowHeight,
      4096,
    )
    this.overscanPixels = finite(
      options.overscanPixels,
      800,
      0,
      100_000,
    )
    this.maximumRenderedRows = integer(
      options.maximumRenderedRows,
      240,
      20,
      5000,
    )
    this.measurementCacheLimit = integer(
      options.measurementCacheLimit,
      20_000,
      100,
      1_000_000,
    )
  }

  get revision(): number {
    return this.#revision
  }

  get totalHeight(): number {
    return this.#sizes.total()
  }

  get rowCount(): number {
    return this.#rows.length
  }

  rows(): readonly TimelineRow[] {
    return this.#rows
  }

  setRows(rows: readonly TimelineRow[]): boolean {
    const unchanged =
      rows.length === this.#rows.length &&
      rows.every((row, index) => row.key === this.#rows[index]?.key)
    if (unchanged) {
      this.#rows = rows
      return false
    }
    const previous = this.#indexByKey
    this.#rows = rows
    this.#indexByKey = new Map(
      rows.map((row, index) => [row.key, index] as const),
    )
    const values = rows.map((row) => {
      const measured = this.#measurements.get(row.key)
      if (measured) {
        measured.touched = ++this.#touch
        return measured.size
      }
      const oldIndex = previous.get(row.key)
      if (oldIndex !== undefined) {
        const oldValue = this.#sizes.value(oldIndex)
        if (oldValue > 0) return oldValue
      }
      return this.#estimate(row)
    })
    this.#sizes = new FenwickTree(values)
    this.#revision += 1
    this.#lastRange = undefined
    this.#pruneMeasurements()
    return true
  }

  measure(rowKey: string, size: number): boolean {
    const index = this.#indexByKey.get(rowKey)
    if (index === undefined || !Number.isFinite(size)) return false
    const bounded = Math.max(
      this.minimumRowHeight,
      Math.min(this.maximumRowHeight, size),
    )
    const changed = this.#sizes.update(index, bounded)
    this.#measurements.set(rowKey, {
      size: bounded,
      touched: ++this.#touch,
    })
    if (changed) {
      this.#revision += 1
      this.#lastRange = undefined
    }
    this.#pruneMeasurements()
    return changed
  }

  measureMany(
    measurements: Readonly<Record<string, number>> | ReadonlyMap<string, number>,
  ): number {
    let changed = 0
    const entries =
      measurements instanceof Map
        ? measurements.entries()
        : Object.entries(measurements)
    for (const [key, size] of entries) {
      if (this.measure(key, size)) changed += 1
    }
    return changed
  }

  offsetForIndex(index: number): number {
    return this.#sizes.prefix(
      Math.max(0, Math.min(this.#rows.length, Math.floor(index))),
    )
  }

  offsetForKey(rowKey: string): number | undefined {
    const index = this.#indexByKey.get(rowKey)
    return index === undefined ? undefined : this.offsetForIndex(index)
  }

  indexForKey(rowKey: string): number | undefined {
    return this.#indexByKey.get(rowKey)
  }

  rowAtOffset(offset: number): TimelineRow | undefined {
    return this.#rows[this.#sizes.indexAtOffset(offset)]
  }

  captureAnchor(scrollOffset: number): TimelineScrollAnchor | undefined {
    if (!this.#rows.length) return undefined
    const index = this.#sizes.indexAtOffset(scrollOffset)
    const row = this.#rows[index]
    const absolute = this.offsetForIndex(index)
    return Object.freeze({
      rowKey: row.key,
      offsetWithinRow: Math.max(
        0,
        Math.min(this.#sizes.value(index), scrollOffset - absolute),
      ),
      previousIndex: index,
      previousAbsoluteOffset: absolute,
    })
  }

  restoreAnchor(
    anchor: TimelineScrollAnchor | undefined,
    fallbackOffset = 0,
  ): number {
    if (!anchor) return Math.max(0, fallbackOffset)
    const index = this.#indexByKey.get(anchor.rowKey)
    if (index === undefined) {
      const fallbackIndex = Math.max(
        0,
        Math.min(this.#rows.length - 1, anchor.previousIndex),
      )
      return this.offsetForIndex(fallbackIndex)
    }
    return (
      this.offsetForIndex(index) +
      Math.min(this.#sizes.value(index), anchor.offsetWithinRow)
    )
  }

  range(
    scrollOffset: number,
    viewportHeight: number,
    options: {
      pinRowKeys?: readonly string[]
      anchorKey?: string
    } = {},
  ): TimelineVirtualRange {
    const offset = Math.max(0, Math.min(this.totalHeight, scrollOffset))
    const height = Math.max(0, viewportHeight)
    if (!this.#rows.length) {
      return Object.freeze({
        rows: Object.freeze([]),
        startIndex: 0,
        endIndex: -1,
        overscanStartIndex: 0,
        overscanEndIndex: -1,
        beforeHeight: 0,
        visibleHeight: 0,
        afterHeight: 0,
        totalHeight: 0,
        viewportHeight: height,
        scrollOffset: offset,
        revision: this.#revision,
      })
    }
    const startIndex = this.#sizes.indexAtOffset(offset)
    const endIndex = this.#sizes.indexAtOffset(offset + height)
    let overscanStartIndex = this.#sizes.indexAtOffset(
      Math.max(0, offset - this.overscanPixels),
    )
    let overscanEndIndex = this.#sizes.indexAtOffset(
      Math.min(this.totalHeight, offset + height + this.overscanPixels),
    )
    const pinned = new Set<number>()
    for (const key of options.pinRowKeys ?? []) {
      const index = this.#indexByKey.get(key)
      if (index !== undefined) pinned.add(index)
    }
    if (options.anchorKey) {
      const index = this.#indexByKey.get(options.anchorKey)
      if (index !== undefined) pinned.add(index)
    }
    if (
      overscanEndIndex - overscanStartIndex + 1 >
      this.maximumRenderedRows
    ) {
      const visibleCount = Math.max(1, endIndex - startIndex + 1)
      const remaining = Math.max(
        0,
        this.maximumRenderedRows - visibleCount,
      )
      const before = Math.floor(remaining / 2)
      const after = remaining - before
      overscanStartIndex = Math.max(0, startIndex - before)
      overscanEndIndex = Math.min(
        this.#rows.length - 1,
        endIndex + after,
      )
    }
    const measured: TimelineMeasuredRow[] = []
    const indexes = new Set<number>()
    for (
      let index = overscanStartIndex;
      index <= overscanEndIndex;
      index += 1
    ) {
      indexes.add(index)
    }
    for (const index of pinned) indexes.add(index)
    for (const index of [...indexes].sort((a, b) => a - b)) {
      const row = this.#rows[index]
      if (!row) continue
      const measurement = this.#measurements.get(row.key)
      measured.push(
        Object.freeze({
          key: row.key,
          index,
          offset: this.offsetForIndex(index),
          size: this.#sizes.value(index),
          measured: Boolean(measurement),
          row,
        }),
      )
    }
    const beforeHeight = this.offsetForIndex(overscanStartIndex)
    const afterStart = this.offsetForIndex(overscanEndIndex + 1)
    const anchorIndex = options.anchorKey
      ? this.#indexByKey.get(options.anchorKey)
      : undefined
    const result: TimelineVirtualRange = Object.freeze({
      rows: freeze(measured),
      startIndex,
      endIndex,
      overscanStartIndex,
      overscanEndIndex,
      beforeHeight,
      visibleHeight: Math.max(0, afterStart - beforeHeight),
      afterHeight: Math.max(0, this.totalHeight - afterStart),
      totalHeight: this.totalHeight,
      viewportHeight: height,
      scrollOffset: offset,
      anchorKey: options.anchorKey,
      anchorOffset:
        anchorIndex === undefined
          ? undefined
          : this.offsetForIndex(anchorIndex),
      revision: this.#revision,
    })
    this.#lastRange = result
    return result
  }

  lastRange(): TimelineVirtualRange | undefined {
    return this.#lastRange
  }

  scrollOffsetToReveal(
    rowKey: string,
    currentOffset: number,
    viewportHeight: number,
    align: "start" | "center" | "end" | "nearest" = "nearest",
  ): number | undefined {
    const index = this.#indexByKey.get(rowKey)
    if (index === undefined) return undefined
    const start = this.offsetForIndex(index)
    const size = this.#sizes.value(index)
    const end = start + size
    const viewportEnd = currentOffset + viewportHeight
    if (
      align === "nearest" &&
      start >= currentOffset &&
      end <= viewportEnd
    ) {
      return currentOffset
    }
    if (align === "start") return start
    if (align === "center") {
      return Math.max(0, start - (viewportHeight - size) / 2)
    }
    if (align === "end") {
      return Math.max(0, end - viewportHeight)
    }
    if (start < currentOffset) return start
    return Math.max(0, end - viewportHeight)
  }

  #estimate(row: TimelineRow): number {
    let value = this.estimatedRowHeight
    if (row.summary.length > 160) {
      value += Math.min(160, Math.ceil(row.summary.length / 80) * 18)
    }
    if (row.evidence.length > 3) value += 12
    if (row.partial) value += 8
    if (row.critical) value += 4
    return Math.max(
      this.minimumRowHeight,
      Math.min(this.maximumRowHeight, value),
    )
  }

  #pruneMeasurements(): void {
    let excess = this.#measurements.size - this.measurementCacheLimit
    if (excess <= 0) return
    const active = this.#indexByKey
    const removable = [...this.#measurements.entries()]
      .filter(([key]) => !active.has(key))
      .sort((left, right) => left[1].touched - right[1].touched)
    for (const [key] of removable.slice(0, excess)) {
      this.#measurements.delete(key)
    }
    excess = this.#measurements.size - this.measurementCacheLimit
    if (excess <= 0) return
    // The Fenwick tree retains the current measured size. Dropping the oldest
    // active cache entry only means a future row-set rebuild will re-estimate
    // it; this keeps long-lived viewers bounded even after every row has been
    // measured at least once.
    const oldestActive = [...this.#measurements.entries()].sort(
      (left, right) => left[1].touched - right[1].touched,
    )
    for (const [key] of oldestActive.slice(0, excess)) {
      this.#measurements.delete(key)
    }
  }
}

export function virtualizeTimelineRows(
  rows: readonly TimelineRow[],
  options: TimelineVirtualizerOptions & {
    scrollOffset?: number
    viewportHeight?: number
    pinRowKeys?: readonly string[]
    anchorKey?: string
    measurements?: Readonly<Record<string, number>>
  } = {},
): TimelineVirtualRange {
  const virtualizer = new TimelineVirtualizer(options)
  virtualizer.setRows(rows)
  if (options.measurements) virtualizer.measureMany(options.measurements)
  return virtualizer.range(
    options.scrollOffset ?? 0,
    options.viewportHeight ?? 800,
    {
      pinRowKeys: options.pinRowKeys,
      anchorKey: options.anchorKey,
    },
  )
}
