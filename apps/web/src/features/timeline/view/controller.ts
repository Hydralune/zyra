import type {
  TimelineFilter,
  TimelineFilteredView,
  TimelinePhaseValue,
  TimelineEventKindValue,
  TimelineRow,
  TimelineWindow,
  WorkerCausalTimelineProjection,
} from "../projection/contracts.ts"
import {
  filterTimelineRows,
  revealTimelineRow,
  windowTimelineRows,
} from "../projection/index.ts"

export interface TimelineViewState {
  filter: TimelineFilter
  filtered: TimelineFilteredView
  window: TimelineWindow
  selectedRowKey?: string
  selectedEventId?: string
  expandedRowKeys: ReadonlySet<string>
  followLatest: boolean
  windowCount: number
  overscan: number
}

export interface TimelineWorkbenchSnapshot {
  taskId: string
  revision: number
  projection: WorkerCausalTimelineProjection
  view: TimelineViewState
  selectedRow?: TimelineRow
  loading: boolean
  reconnecting: boolean
  error?: string
  closed: boolean
}

type TimelineListener = () => void

type TimelineViewPatch = Omit<
  Partial<TimelineViewState>,
  "selectedRowKey" | "selectedEventId"
> & {
  selectedRowKey?: string | null
  selectedEventId?: string | null
}

function emptyFiltered(): TimelineFilteredView {
  return Object.freeze({
    rows: Object.freeze([]),
    gaps: Object.freeze([]),
    hiddenRowCount: 0,
    hiddenEventCount: 0,
    matchedRowCount: 0,
    visibleEventIds: Object.freeze([]),
    retainedBoundaryEventIds: Object.freeze([]),
    activeFilterCount: 0,
  })
}

function emptyWindow(): TimelineWindow {
  return Object.freeze({
    rows: Object.freeze([]),
    firstIndex: 0,
    lastIndex: -1,
    total: 0,
    beforeCount: 0,
    afterCount: 0,
    pinnedRowKeys: Object.freeze([]),
    revisionKey: "0:empty",
  })
}

function setValue(values: readonly string[] | undefined, value: string): readonly string[] {
  const result = new Set(values ?? [])
  if (result.has(value)) result.delete(value)
  else result.add(value)
  return Object.freeze([...result].sort())
}

function compactFilter(filter: TimelineFilter): TimelineFilter {
  const result: TimelineFilter = {}
  if (filter.workerIds?.length) result.workerIds = Object.freeze([...filter.workerIds])
  if (filter.phases?.length) result.phases = Object.freeze([...filter.phases])
  if (filter.kinds?.length) result.kinds = Object.freeze([...filter.kinds])
  if (filter.from !== undefined) result.from = filter.from
  if (filter.to !== undefined) result.to = filter.to
  if (filter.search?.trim()) result.search = filter.search.trim()
  if (filter.criticalOnly) result.criticalOnly = true
  if (filter.failuresOnly) result.failuresOnly = true
  if (filter.includeNonEffective) result.includeNonEffective = true
  if (filter.includePartial === false) result.includePartial = false
  return Object.freeze(result)
}

function selectedEventForRow(
  row: TimelineRow | undefined,
  requested?: string,
): string | undefined {
  if (!row) return undefined
  if (requested && row.eventIds.includes(requested)) return requested
  return row.primaryEventId
}

export class TimelineWorkbenchController {
  readonly taskId: string
  readonly #listeners = new Set<TimelineListener>()
  readonly #announcements: string[] = []
  #projection: WorkerCausalTimelineProjection
  #snapshot: TimelineWorkbenchSnapshot

  constructor(
    taskId: string,
    projection: WorkerCausalTimelineProjection,
    options: {
      windowCount?: number
      overscan?: number
      followLatest?: boolean
    } = {},
  ) {
    this.taskId = taskId
    this.#projection = projection
    const windowCount = Math.max(20, Math.min(1_000, options.windowCount ?? 120))
    const overscan = Math.max(0, Math.min(500, options.overscan ?? 30))
    const filtered = filterTimelineRows(
      projection.rows,
      projection.graph,
      {},
    )
    const start = options.followLatest === false
      ? 0
      : Math.max(0, filtered.rows.length - windowCount)
    const window = windowTimelineRows(filtered.rows, {
      start,
      count: windowCount,
      overscan,
    })
    this.#snapshot = Object.freeze({
      taskId,
      revision: 1,
      projection,
      view: Object.freeze({
        filter: Object.freeze({}),
        filtered,
        window,
        expandedRowKeys: new Set<string>(),
        followLatest: options.followLatest !== false,
        windowCount,
        overscan,
      }),
      loading: !projection.diagnostics.ready && projection.rows.length === 0,
      reconnecting: projection.diagnostics.lag > 0,
      closed: false,
    })
  }

  readonly subscribe = (listener: TimelineListener): (() => void) => {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  readonly getSnapshot = (): TimelineWorkbenchSnapshot => this.#snapshot

  #emit(snapshot: TimelineWorkbenchSnapshot): void {
    this.#snapshot = Object.freeze(snapshot)
    for (const listener of this.#listeners) listener()
  }

  #rebuild(
    patch: TimelineViewPatch,
    announcement?: string,
  ): void {
    if (this.#snapshot.closed) return
    const previous = this.#snapshot.view
    const filter = compactFilter(patch.filter ?? previous.filter)
    const filtered = filterTimelineRows(
      this.#projection.rows,
      this.#projection.graph,
      filter,
    )
    const windowCount = patch.windowCount ?? previous.windowCount
    const overscan = patch.overscan ?? previous.overscan
    const selectedRowKey =
      patch.selectedRowKey === null
        ? undefined
        : patch.selectedRowKey ?? previous.selectedRowKey
    const selectedEventId =
      patch.selectedEventId === null
        ? undefined
        : patch.selectedEventId ?? previous.selectedEventId
    const selectedVisible =
      selectedRowKey &&
      filtered.rows.some((row) => row.key === selectedRowKey)
    const followLatest = patch.followLatest ?? previous.followLatest
    const requestedWindow = selectedVisible
      ? revealTimelineRow(
          filtered.rows,
          selectedRowKey,
          windowCount,
          overscan,
        )
      : windowTimelineRows(filtered.rows, {
          start: followLatest
            ? Math.max(0, filtered.rows.length - windowCount)
            : Math.min(
                previous.window.firstIndex,
                Math.max(0, filtered.rows.length - 1),
              ),
          count: windowCount,
          overscan,
          pinRowKeys: selectedRowKey ? [selectedRowKey] : [],
        })
    const expandedRowKeys =
      patch.expandedRowKeys ?? previous.expandedRowKeys
    const selectedRow = selectedRowKey
      ? this.#projection.rowsByKey[selectedRowKey]
      : undefined
    this.#emit({
      ...this.#snapshot,
      revision: this.#snapshot.revision + 1,
      projection: this.#projection,
      view: Object.freeze({
        filter,
        filtered,
        window: requestedWindow,
        selectedRowKey: selectedRow?.key,
        selectedEventId: selectedEventForRow(
          selectedRow,
          selectedEventId,
        ),
        expandedRowKeys,
        followLatest,
        windowCount,
        overscan,
      }),
      selectedRow,
      loading:
        !this.#projection.diagnostics.ready &&
        this.#projection.rows.length === 0,
      reconnecting: this.#projection.diagnostics.lag > 0,
      error: undefined,
    })
    if (announcement) this.#announce(announcement)
  }

  project(projection: WorkerCausalTimelineProjection): void {
    if (this.#snapshot.closed) return
    if (projection.taskId !== this.taskId) {
      this.setError(
        `Timeline projection task ${projection.taskId} does not match controller task ${this.taskId}.`,
      )
      return
    }
    if (
      projection === this.#projection ||
      (projection.projectionRevision ===
        this.#projection.projectionRevision &&
        projection.rows.length === this.#projection.rows.length &&
        projection.rows.at(-1)?.key === this.#projection.rows.at(-1)?.key &&
        projection.rows.at(-1)?.endSequence ===
          this.#projection.rows.at(-1)?.endSequence)
    ) {
      return
    }
    const previousRows = this.#projection.rows.length
    const previousSequence =
      this.#projection.diagnostics.committedSequence
    this.#projection = projection
    const added = Math.max(0, projection.rows.length - previousRows)
    const advanced = Math.max(
      0,
      projection.diagnostics.committedSequence - previousSequence,
    )
    this.#rebuild(
      {},
      added || advanced
        ? `Timeline advanced by ${advanced} canonical event${advanced === 1 ? "" : "s"} and ${added} row${added === 1 ? "" : "s"}.`
        : "Timeline projection reconciled.",
    )
  }

  setFilter(filter: TimelineFilter): void {
    this.#rebuild(
      { filter },
      "Timeline filters updated.",
    )
  }

  setSearch(search: string): void {
    this.setFilter({ ...this.#snapshot.view.filter, search })
  }

  toggleWorker(workerId: string): void {
    this.setFilter({
      ...this.#snapshot.view.filter,
      workerIds: setValue(
        this.#snapshot.view.filter.workerIds,
        workerId,
      ),
    })
  }

  togglePhase(phase: TimelinePhaseValue): void {
    this.setFilter({
      ...this.#snapshot.view.filter,
      phases: setValue(
        this.#snapshot.view.filter.phases,
        phase,
      ) as readonly TimelinePhaseValue[],
    })
  }

  toggleKind(kind: TimelineEventKindValue): void {
    this.setFilter({
      ...this.#snapshot.view.filter,
      kinds: setValue(
        this.#snapshot.view.filter.kinds,
        kind,
      ) as readonly TimelineEventKindValue[],
    })
  }

  toggleCritical(): void {
    this.setFilter({
      ...this.#snapshot.view.filter,
      criticalOnly: !this.#snapshot.view.filter.criticalOnly,
    })
  }

  toggleFailures(): void {
    this.setFilter({
      ...this.#snapshot.view.filter,
      failuresOnly: !this.#snapshot.view.filter.failuresOnly,
    })
  }

  togglePartial(): void {
    this.setFilter({
      ...this.#snapshot.view.filter,
      includePartial:
        this.#snapshot.view.filter.includePartial === false,
    })
  }

  resetFilters(): void {
    this.#rebuild(
      { filter: Object.freeze({}) },
      "Timeline filters cleared.",
    )
  }

  selectRow(rowKey: string, eventId?: string): boolean {
    const row = this.#projection.rowsByKey[rowKey]
    if (!row) return false
    this.#rebuild(
      {
        selectedRowKey: row.key,
        selectedEventId: selectedEventForRow(row, eventId),
        followLatest: false,
      },
      `${row.title}, ${row.phase}, sequence ${row.sequence}.`,
    )
    return true
  }

  selectEvent(eventId: string): boolean {
    const rowKey = this.#projection.rowKeyByEvent[eventId]
    if (!rowKey) return false
    return this.selectRow(rowKey, eventId)
  }

  clearSelection(): void {
    if (!this.#snapshot.view.selectedRowKey) return
    this.#rebuild(
      {
        selectedRowKey: null,
        selectedEventId: null,
      },
      "Timeline selection cleared.",
    )
  }

  toggleExpanded(rowKey: string): void {
    const expanded = new Set(this.#snapshot.view.expandedRowKeys)
    if (expanded.has(rowKey)) expanded.delete(rowKey)
    else expanded.add(rowKey)
    this.#rebuild({
      expandedRowKeys: expanded,
    })
  }

  showEarlier(): void {
    const start = Math.max(
      0,
      this.#snapshot.view.window.firstIndex -
        this.#snapshot.view.windowCount,
    )
    const window = windowTimelineRows(this.#snapshot.view.filtered.rows, {
      start,
      count: this.#snapshot.view.windowCount,
      overscan: this.#snapshot.view.overscan,
      pinRowKeys: this.#snapshot.view.selectedRowKey
        ? [this.#snapshot.view.selectedRowKey]
        : [],
    })
    this.#emit({
      ...this.#snapshot,
      revision: this.#snapshot.revision + 1,
      view: Object.freeze({
        ...this.#snapshot.view,
        window,
        followLatest: false,
      }),
    })
    this.#announce(
      `Showing timeline rows ${window.firstIndex + 1} through ${window.lastIndex + 1}.`,
    )
  }

  showLater(): void {
    const rows = this.#snapshot.view.filtered.rows
    const start = Math.min(
      Math.max(0, rows.length - 1),
      this.#snapshot.view.window.lastIndex + 1,
    )
    const window = windowTimelineRows(rows, {
      start,
      count: this.#snapshot.view.windowCount,
      overscan: this.#snapshot.view.overscan,
      pinRowKeys: this.#snapshot.view.selectedRowKey
        ? [this.#snapshot.view.selectedRowKey]
        : [],
    })
    const follows = window.afterCount === 0
    this.#emit({
      ...this.#snapshot,
      revision: this.#snapshot.revision + 1,
      view: Object.freeze({
        ...this.#snapshot.view,
        window,
        followLatest: follows,
      }),
    })
    this.#announce(
      follows
        ? "Following the latest canonical timeline row."
        : `Showing timeline rows ${window.firstIndex + 1} through ${window.lastIndex + 1}.`,
    )
  }

  followLatest(): void {
    this.#rebuild(
      {
        followLatest: true,
        selectedRowKey: null,
        selectedEventId: null,
      },
      "Following the latest canonical timeline row.",
    )
  }

  setWindowCount(count: number): void {
    const windowCount = Math.max(20, Math.min(1_000, Math.trunc(count)))
    this.#rebuild({ windowCount })
  }

  setError(message: string): void {
    if (this.#snapshot.closed) return
    this.#emit({
      ...this.#snapshot,
      revision: this.#snapshot.revision + 1,
      loading: false,
      error: message,
    })
    this.#announce(message)
  }

  #announce(message: string): void {
    this.#announcements.push(message)
    if (this.#announcements.length > 100) this.#announcements.shift()
  }

  takeAnnouncements(): readonly string[] {
    return Object.freeze(this.#announcements.splice(0))
  }

  announcements(): readonly string[] {
    return Object.freeze([...this.#announcements])
  }

  close(reason = "Timeline controller closed."): void {
    if (this.#snapshot.closed) return
    this.#listeners.clear()
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      revision: this.#snapshot.revision + 1,
      closed: true,
    })
    this.#announce(reason)
  }
}
