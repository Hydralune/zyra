import type {
  BrowserStep,
  BrowserViewerWindow,
} from "../contracts.ts"

interface HeightEntry {
  stepId: string
  height: number
  measured: boolean
  revision: number
}

export interface BrowserVirtualizerOptions {
  estimatedHeight?: number
  minimumHeight?: number
  maximumHeight?: number
  overscanPixels?: number
  maximumRetainedMeasurements?: number
}

export class BrowserHistoryVirtualizer {
  readonly #estimatedHeight: number
  readonly #minimumHeight: number
  readonly #maximumHeight: number
  readonly #overscanPixels: number
  readonly #maximumRetainedMeasurements: number
  #steps: readonly BrowserStep[] = Object.freeze([])
  #heights = new Map<string, HeightEntry>()
  #prefix: number[] = [0]
  #revision = 0
  #lastWindow?: BrowserViewerWindow

  constructor(options: BrowserVirtualizerOptions = {}) {
    this.#estimatedHeight = bounded(
      options.estimatedHeight,
      72,
      24,
      512,
    )
    this.#minimumHeight = bounded(
      options.minimumHeight,
      32,
      16,
      this.#estimatedHeight,
    )
    this.#maximumHeight = bounded(
      options.maximumHeight,
      768,
      this.#estimatedHeight,
      4_096,
    )
    this.#overscanPixels = bounded(
      options.overscanPixels,
      640,
      0,
      16_384,
    )
    this.#maximumRetainedMeasurements = bounded(
      options.maximumRetainedMeasurements,
      20_000,
      100,
      100_000,
    )
  }

  get revision(): number {
    return this.#revision
  }

  get count(): number {
    return this.#steps.length
  }

  get totalHeight(): number {
    return this.#prefix.at(-1) ?? 0
  }

  setSteps(steps: readonly BrowserStep[]): void {
    const deduped: BrowserStep[] = []
    const seen = new Set<string>()
    for (const step of steps) {
      if (seen.has(step.stepId)) continue
      seen.add(step.stepId)
      deduped.push(step)
    }
    const changed =
      deduped.length !== this.#steps.length
      || deduped.some(
        (step, index) =>
          step.stepId !== this.#steps[index]?.stepId
          || step.endSequence !== this.#steps[index]?.endSequence,
      )
    if (!changed) return
    this.#steps = Object.freeze(deduped)
    this.#revision += 1
    this.#pruneMeasurements(seen)
    this.#rebuildPrefix()
    this.#lastWindow = undefined
  }

  measure(stepId: string, height: number): boolean {
    if (!this.#steps.some((step) => step.stepId === stepId)) return false
    const normalized = bounded(
      height,
      this.#estimatedHeight,
      this.#minimumHeight,
      this.#maximumHeight,
    )
    const existing = this.#heights.get(stepId)
    if (existing && Math.abs(existing.height - normalized) < 0.5) {
      return false
    }
    this.#revision += 1
    this.#heights.set(stepId, {
      stepId,
      height: normalized,
      measured: true,
      revision: this.#revision,
    })
    this.#rebuildPrefix()
    this.#lastWindow = undefined
    return true
  }

  invalidate(stepId?: string): void {
    if (stepId) {
      if (!this.#heights.delete(stepId)) return
    } else {
      if (!this.#heights.size) return
      this.#heights.clear()
    }
    this.#revision += 1
    this.#rebuildPrefix()
    this.#lastWindow = undefined
  }

  window(
    scrollTop: number,
    viewportHeight: number,
  ): BrowserViewerWindow {
    const safeTop = Math.max(0, Number.isFinite(scrollTop) ? scrollTop : 0)
    const safeViewport = Math.max(
      1,
      Number.isFinite(viewportHeight) ? viewportHeight : 1,
    )
    if (!this.#steps.length) {
      const empty = Object.freeze({
        start: 0,
        end: 0,
        overscanStart: 0,
        overscanEnd: 0,
        beforeHeight: 0,
        afterHeight: 0,
        totalHeight: 0,
        itemHeight: this.#estimatedHeight,
        stepIds: Object.freeze([]),
      })
      this.#lastWindow = empty
      return empty
    }
    const viewportStart = Math.min(safeTop, this.totalHeight)
    const viewportEnd = Math.min(
      this.totalHeight,
      viewportStart + safeViewport,
    )
    const overscanStartPixel = Math.max(
      0,
      viewportStart - this.#overscanPixels,
    )
    const overscanEndPixel = Math.min(
      this.totalHeight,
      viewportEnd + this.#overscanPixels,
    )
    const start = this.#indexAt(viewportStart)
    const end = Math.min(
      this.#steps.length,
      this.#indexAt(viewportEnd) + 1,
    )
    const overscanStart = this.#indexAt(overscanStartPixel)
    const overscanEnd = Math.min(
      this.#steps.length,
      this.#indexAt(overscanEndPixel) + 1,
    )
    const beforeHeight = this.#prefix[overscanStart] ?? 0
    const afterHeight =
      this.totalHeight - (this.#prefix[overscanEnd] ?? this.totalHeight)
    const average =
      overscanEnd > overscanStart
        ? (
          (this.#prefix[overscanEnd] ?? 0)
          - (this.#prefix[overscanStart] ?? 0)
        ) / (overscanEnd - overscanStart)
        : this.#estimatedHeight
    const result = Object.freeze({
      start,
      end,
      overscanStart,
      overscanEnd,
      beforeHeight,
      afterHeight,
      totalHeight: this.totalHeight,
      itemHeight: average,
      stepIds: Object.freeze(
        this.#steps
          .slice(overscanStart, overscanEnd)
          .map((step) => step.stepId),
      ),
    })
    this.#lastWindow = result
    return result
  }

  ensureVisible(
    stepId: string,
    scrollTop: number,
    viewportHeight: number,
    alignment: "nearest" | "start" | "center" | "end" = "nearest",
  ): number {
    const index = this.#steps.findIndex((step) => step.stepId === stepId)
    if (index < 0) return Math.max(0, scrollTop)
    const itemStart = this.#prefix[index] ?? 0
    const itemEnd = this.#prefix[index + 1] ?? itemStart
    const viewportStart = Math.max(0, scrollTop)
    const viewportEnd = viewportStart + Math.max(1, viewportHeight)
    if (
      alignment === "nearest"
      && itemStart >= viewportStart
      && itemEnd <= viewportEnd
    ) return viewportStart
    let next: number
    if (alignment === "start") {
      next = itemStart
    } else if (alignment === "center") {
      next = itemStart - (viewportHeight - (itemEnd - itemStart)) / 2
    } else if (alignment === "end") {
      next = itemEnd - viewportHeight
    } else {
      next =
        itemStart < viewportStart
          ? itemStart
          : itemEnd > viewportEnd
            ? itemEnd - viewportHeight
            : viewportStart
    }
    return Math.max(0, Math.min(next, Math.max(0, this.totalHeight - viewportHeight)))
  }

  stepAtOffset(offset: number): BrowserStep | undefined {
    if (!this.#steps.length) return undefined
    return this.#steps[this.#indexAt(Math.max(0, offset))]
  }

  offsetFor(stepId: string): number | undefined {
    const index = this.#steps.findIndex((step) => step.stepId === stepId)
    return index < 0 ? undefined : this.#prefix[index]
  }

  heightFor(stepId: string): number {
    return this.#heights.get(stepId)?.height ?? this.#estimatedHeight
  }

  visibleSteps(window = this.#lastWindow): readonly BrowserStep[] {
    if (!window) return Object.freeze([])
    return Object.freeze(
      this.#steps.slice(window.overscanStart, window.overscanEnd),
    )
  }

  snapshot(): {
    revision: number
    steps: number
    measured: number
    totalHeight: number
    estimatedHeight: number
    lastWindow?: BrowserViewerWindow
  } {
    return Object.freeze({
      revision: this.#revision,
      steps: this.#steps.length,
      measured: this.#heights.size,
      totalHeight: this.totalHeight,
      estimatedHeight: this.#estimatedHeight,
      lastWindow: this.#lastWindow,
    })
  }

  #indexAt(offset: number): number {
    if (!this.#steps.length) return 0
    const target = Math.max(0, Math.min(offset, this.totalHeight))
    let low = 0
    let high = this.#steps.length
    while (low < high) {
      const middle = Math.floor((low + high) / 2)
      const start = this.#prefix[middle] ?? 0
      const end = this.#prefix[middle + 1] ?? start
      if (target < start) {
        high = middle
      } else if (target >= end && middle < this.#steps.length - 1) {
        low = middle + 1
      } else {
        return middle
      }
    }
    return Math.min(this.#steps.length - 1, low)
  }

  #rebuildPrefix(): void {
    const prefix = new Array<number>(this.#steps.length + 1)
    prefix[0] = 0
    for (let index = 0; index < this.#steps.length; index += 1) {
      const step = this.#steps[index]!
      const height = this.#heights.get(step.stepId)?.height ?? this.#estimatedHeight
      prefix[index + 1] = prefix[index]! + height
    }
    this.#prefix = prefix
  }

  #pruneMeasurements(active: ReadonlySet<string>): void {
    for (const stepId of this.#heights.keys()) {
      if (!active.has(stepId)) this.#heights.delete(stepId)
    }
    if (this.#heights.size <= this.#maximumRetainedMeasurements) return
    const retained = [...this.#heights.values()]
      .sort((left, right) => right.revision - left.revision)
      .slice(0, this.#maximumRetainedMeasurements)
    this.#heights = new Map(retained.map((entry) => [entry.stepId, entry]))
  }
}

function bounded(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const candidate = Number(value)
  if (!Number.isFinite(candidate)) return fallback
  return Math.min(maximum, Math.max(minimum, candidate))
}
