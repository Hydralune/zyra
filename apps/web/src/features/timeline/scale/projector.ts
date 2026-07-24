import type {
  TimelineRow,
  WorkerCausalTimelineProjection,
} from "../projection/index.ts"
import {
  type ScaledTimelineInput,
  type ScaledTimelineOptions,
  type ScaledTimelineProjection,
  type TimelineSearchQuery,
} from "./contracts.ts"
import {
  foldTimelineProjection,
  toggleTimelineFold,
} from "./causal-fold.ts"
import { projectTimelineGoalDrift } from "./goal-drift.ts"
import { projectTimelineOverlays } from "./overlays.ts"
import { TimelineSearchIndex } from "./search-index.ts"
import { TimelineVirtualizer } from "./virtualizer.ts"

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function filterRows(
  rows: readonly TimelineRow[],
  matchKeys: ReadonlySet<string> | undefined,
  criticalOnly: boolean,
  selectedRowKey?: string,
): readonly TimelineRow[] {
  if (!matchKeys && !criticalOnly) return rows
  return freeze(
    rows.filter(
      (row) =>
        row.key === selectedRowKey ||
        ((!matchKeys || matchKeys.has(row.key)) &&
          (!criticalOnly || row.critical)),
    ),
  )
}

function optionsRevision(options: ScaledTimelineOptions): string {
  return JSON.stringify({
    search: options.search,
    scrollOffset: Math.round(options.scrollOffset ?? 0),
    viewportHeight: Math.round(options.viewportHeight ?? 0),
    selectedRowKey: options.selectedRowKey,
    criticalOnly: options.criticalOnly,
    folds: options.fold?.expandedFoldIds
      ? [...options.fold.expandedFoldIds].sort()
      : [],
  })
}

export class ScaledTimelineRuntime {
  readonly virtualizer: TimelineVirtualizer
  readonly searchIndex = new TimelineSearchIndex()
  #expandedFoldIds = new Set<string>()
  #projection?: WorkerCausalTimelineProjection
  #options: ScaledTimelineOptions = Object.freeze({})
  #last?: ScaledTimelineProjection
  #closed = false

  constructor(options: ScaledTimelineOptions = {}) {
    this.#options = Object.freeze({ ...options })
    this.#expandedFoldIds = new Set(
      options.fold?.expandedFoldIds ?? [],
    )
    this.virtualizer = new TimelineVirtualizer(options.virtualizer)
  }

  get closed(): boolean {
    return this.#closed
  }

  snapshot(): ScaledTimelineProjection | undefined {
    return this.#last
  }

  setProjection(
    projection: WorkerCausalTimelineProjection,
  ): ScaledTimelineProjection {
    this.#assertOpen()
    this.#projection = projection
    this.searchIndex.rebuild(projection.rows, projection.events)
    return this.#project()
  }

  setOptions(
    options: Partial<ScaledTimelineOptions>,
  ): ScaledTimelineProjection | undefined {
    this.#assertOpen()
    if (options.fold?.expandedFoldIds) {
      this.#expandedFoldIds = new Set(options.fold.expandedFoldIds)
    }
    this.#options = Object.freeze({
      ...this.#options,
      ...options,
      fold: {
        ...this.#options.fold,
        ...options.fold,
        expandedFoldIds: this.#expandedFoldIds,
      },
      virtualizer: {
        ...this.#options.virtualizer,
        ...options.virtualizer,
      },
    })
    return this.#projection ? this.#project() : undefined
  }

  setSearch(
    query: TimelineSearchQuery | undefined,
  ): ScaledTimelineProjection | undefined {
    return this.setOptions({ search: query })
  }

  setViewport(
    scrollOffset: number,
    viewportHeight: number,
  ): ScaledTimelineProjection | undefined {
    return this.setOptions({ scrollOffset, viewportHeight })
  }

  selectRow(
    rowKey: string | undefined,
  ): ScaledTimelineProjection | undefined {
    return this.setOptions({ selectedRowKey: rowKey })
  }

  toggleFold(foldId: string): ScaledTimelineProjection | undefined {
    this.#assertOpen()
    this.#expandedFoldIds = new Set(
      toggleTimelineFold(this.#expandedFoldIds, foldId),
    )
    return this.setOptions({
      fold: {
        ...this.#options.fold,
        expandedFoldIds: this.#expandedFoldIds,
      },
    })
  }

  measureRow(
    rowKey: string,
    height: number,
  ): ScaledTimelineProjection | undefined {
    this.#assertOpen()
    if (!this.virtualizer.measure(rowKey, height)) return this.#last
    return this.#projection ? this.#project() : undefined
  }

  scrollOffsetToReveal(
    rowKey: string,
    align: "start" | "center" | "end" | "nearest" = "nearest",
  ): number | undefined {
    return this.virtualizer.scrollOffsetToReveal(
      rowKey,
      this.#options.scrollOffset ?? 0,
      this.#options.viewportHeight ?? 800,
      align,
    )
  }

  close(): void {
    this.#closed = true
    this.#projection = undefined
    this.#last = undefined
  }

  #project(): ScaledTimelineProjection {
    const projection = this.#projection as WorkerCausalTimelineProjection
    const goalDrift = projectTimelineGoalDrift(projection)
    const overlays = projectTimelineOverlays(projection, goalDrift)
    const search =
      this.#options.search &&
      (this.#options.search.text.trim() ||
        this.#options.search.workerIds?.length ||
        this.#options.search.phases?.length ||
        this.#options.search.kinds?.length ||
        this.#options.search.criticalOnly ||
        this.#options.search.effectiveOnly)
        ? this.searchIndex.search(this.#options.search)
        : undefined
    const matchKeys = search
      ? new Set(search.matches.map((match) => match.rowKey))
      : undefined
    const filtered = filterRows(
      projection.rows,
      matchKeys,
      Boolean(this.#options.criticalOnly),
      this.#options.selectedRowKey,
    )
    const folds = foldTimelineProjection(projection, filtered, {
      ...this.#options.fold,
      expandedFoldIds: this.#expandedFoldIds,
    })
    const anchor = this.virtualizer.captureAnchor(
      this.#options.scrollOffset ?? 0,
    )
    this.virtualizer.setRows(folds.visibleRows)
    const restored = this.virtualizer.restoreAnchor(
      anchor,
      this.#options.scrollOffset ?? 0,
    )
    const selectedRow = this.#options.selectedRowKey
      ? projection.rowsByKey[this.#options.selectedRowKey]
      : undefined
    const virtual = this.virtualizer.range(
      restored,
      this.#options.viewportHeight ?? 800,
      {
        pinRowKeys: selectedRow ? [selectedRow.key] : undefined,
        anchorKey: anchor?.rowKey,
      },
    )
    const result: ScaledTimelineProjection = Object.freeze({
      taskId: projection.taskId,
      sourceRevision: projection.projectionRevision,
      rows: folds.visibleRows,
      folds,
      virtual,
      overlays,
      goalDrift,
      search,
      criticalPath: projection.criticalPath,
      selectedRow,
      thousandsMode: projection.rows.length >= 1000,
      totalRows: projection.rows.length,
      renderedRows: virtual.rows.length,
      effectiveSteps: overlays.effectiveStepCount,
      hiddenByFolds: folds.hiddenRowCount,
      revision: [
        projection.projectionRevision,
        folds.revision,
        this.virtualizer.revision,
        this.searchIndex.revision,
        optionsRevision(this.#options),
      ].join("|"),
    })
    this.#last = result
    return result
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new Error("scaled timeline runtime is closed")
    }
  }
}

export function projectScaledTimeline(
  input: ScaledTimelineInput,
): ScaledTimelineProjection {
  const runtime = new ScaledTimelineRuntime(input.options)
  const result = runtime.setProjection(input.projection)
  runtime.close()
  return result
}
