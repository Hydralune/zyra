import {
  defaultTraceFoldState,
  expandTraceAncestors,
  foldCausalTrace,
  normalizeTraceFoldState,
  toggleTraceFold,
} from "../analysis/fold.ts"
import {
  TraceSearchIndex,
  defaultTraceFilter,
  filterCausalTrace,
} from "../analysis/query.ts"
import {
  TraceProjectionError,
  type CausalTraceProjection,
  type TraceFilter,
  type TraceFoldState,
  type TraceNavigationReceipt,
  type TraceNavigationTarget,
  type TraceReportReference,
  type TraceVirtualWindow,
  type TraceWorkbenchSnapshot,
} from "../contracts.ts"
import {
  TraceNavigationRuntime,
  navigationTargetsForTraceNode,
  resolveTraceFocusRequest,
  subscribeCrossViewTraceShortcuts,
  subscribeTraceFocusRequests,
  type TraceFocusRequest,
  type TraceNavigationRuntimeOptions,
} from "../navigation/runtime.ts"
import { TraceReportReferenceStore } from "../report/pins.ts"
import { TraceVirtualizer } from "../scale/virtualizer.ts"
import {
  TraceReconciliationRuntime,
  type TraceReconciliationResult,
} from "../index/reconciliation.ts"

export interface TraceWorkbenchControllerOptions {
  disabled?: boolean
  navigation?: TraceNavigationRuntime
  navigationOptions?: TraceNavigationRuntimeOptions
  pins?: TraceReportReferenceStore
  reconciliation?: TraceReconciliationRuntime
  viewportHeight?: number
  estimatedRowHeight?: number
  overscanPx?: number
  maximumExpandedNodes?: number
  maximumNavigationHistory?: number
}

export interface TraceWorkbenchControllerAudit {
  taskId: string
  closed: boolean
  disabled: boolean
  projectionRevision: number
  selectedNodeKey?: string
  expandedNodeCount: number
  filteredNodeCount: number
  foldedNodeCount: number
  virtualItemCount: number
  pinCount: number
  navigationCount: number
  reconciliationChangeCount: number
  updateCount: number
  listenerCount: number
  closeStopsTask: false
  search: ReturnType<TraceSearchIndex["audit"]>
  virtualizer: ReturnType<TraceVirtualizer["audit"]>
  navigation: ReturnType<TraceNavigationRuntime["audit"]>
  pins: ReturnType<TraceReportReferenceStore["audit"]>
  reconciliation: ReturnType<TraceReconciliationRuntime["audit"]>
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

function sameFilter(left: TraceFilter, right: TraceFilter): boolean {
  return JSON.stringify(left) === JSON.stringify(right)
}

function sameFold(left: TraceFoldState, right: TraceFoldState): boolean {
  return (
    left.mode === right.mode &&
    left.preserveCritical === right.preserveCritical &&
    left.preserveFailures === right.preserveFailures &&
    left.maximumVisibleChildren === right.maximumVisibleChildren &&
    [...left.collapsedKeys].sort().join("|") === [...right.collapsedKeys].sort().join("|")
  )
}

function emptyVirtualWindow(): TraceVirtualWindow {
  return Object.freeze({
    items: Object.freeze([]),
    firstIndex: 0,
    lastIndex: -1,
    beforeHeight: 0,
    afterHeight: 0,
    totalHeight: 0,
    total: 0,
    revisionKey: "empty",
  })
}

export class TraceWorkbenchController {
  readonly #taskId: string
  readonly #disabled: boolean
  readonly #navigation: TraceNavigationRuntime
  readonly #pins: TraceReportReferenceStore
  readonly #reconciliation: TraceReconciliationRuntime
  readonly #maximumExpandedNodes: number
  readonly #listeners = new Set<() => void>()
  readonly #unsubscribeNavigation: () => void
  readonly #unsubscribePins: () => void
  readonly #unsubscribeFocus: () => void
  readonly #unsubscribeShortcut: () => void
  #projection: CausalTraceProjection
  #search: TraceSearchIndex
  #virtualizer: TraceVirtualizer
  #filter: TraceFilter = defaultTraceFilter()
  #fold: TraceFoldState = defaultTraceFoldState()
  #selectedNodeKey?: string
  #expandedNodeKeys = new Set<string>()
  #scrollTop = 0
  #viewportHeight: number
  #overscanPx: number
  #anchorKey?: string
  #snapshot: TraceWorkbenchSnapshot
  #reconciliationResult?: TraceReconciliationResult
  #closed = false
  #updateCount = 0

  constructor(
    taskId: string,
    projection: CausalTraceProjection,
    options: TraceWorkbenchControllerOptions = {},
  ) {
    if (taskId !== projection.taskId) {
      throw new TypeError(`Trace controller task ${taskId} does not match projection ${projection.taskId}.`)
    }
    this.#taskId = taskId
    this.#disabled = options.disabled === true
    if (this.#disabled) {
      throw new TraceProjectionError(
        "trace_workbench_disabled",
        "Causal trace workbench is disabled; cross-view joins are unavailable.",
        { taskId },
      )
    }
    this.#projection = projection
    this.#search = new TraceSearchIndex(projection.nodes)
    this.#virtualizer = new TraceVirtualizer(projection.nodes, {
      estimatedRowHeight: options.estimatedRowHeight,
    })
    this.#navigation = options.navigation ?? new TraceNavigationRuntime({
      ...options.navigationOptions,
      maximumHistory: options.maximumNavigationHistory,
    })
    this.#pins = options.pins ?? new TraceReportReferenceStore()
    this.#reconciliation = options.reconciliation ?? new TraceReconciliationRuntime()
    this.#maximumExpandedNodes = Math.max(1, Math.min(1_000, options.maximumExpandedNodes ?? 64))
    this.#viewportHeight = boundedNumber(options.viewportHeight, 720, 1, 100_000)
    this.#overscanPx = boundedNumber(options.overscanPx, this.#viewportHeight, 0, 1_000_000)
    this.#reconciliationResult = this.#reconciliation.reconcile(projection)
    this.#pins.rebase(projection)
    this.#snapshot = this.#deriveSnapshot()
    this.#unsubscribeNavigation = this.#navigation.subscribe(() => this.#refresh())
    this.#unsubscribePins = this.#pins.subscribe(() => this.#refresh())
    this.#unsubscribeFocus = subscribeTraceFocusRequests((request) => this.#receiveFocus(request))
    this.#unsubscribeShortcut = subscribeCrossViewTraceShortcuts(
      taskId,
      (request) => this.#receiveFocus(request),
    )
  }

  readonly getSnapshot = (): TraceWorkbenchSnapshot => this.#snapshot

  readonly subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => undefined
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  updateProjection(projection: CausalTraceProjection): void {
    this.#assertOpen()
    if (projection.taskId !== this.#taskId) {
      throw new TypeError(`Cannot update trace controller ${this.#taskId} with ${projection.taskId}.`)
    }
    if (projection === this.#projection) return
    const previousRevision = this.#projection.projectionRevision
    this.#projection = projection
    this.#search.close()
    this.#search = new TraceSearchIndex(projection.nodes)
    this.#virtualizer.setNodes(projection.nodes)
    this.#reconciliationResult = this.#reconciliation.reconcile(projection)
    this.#pins.rebase(projection)
    if (this.#selectedNodeKey && !projection.nodesByKey[this.#selectedNodeKey]) {
      const replacement = this.#reconciliationResult.resolvedNodeKeys
        .map((key) => projection.nodesByKey[key])
        .find(Boolean)
      this.#selectedNodeKey = replacement?.key
    }
    for (const key of [...this.#expandedNodeKeys]) {
      if (!projection.nodesByKey[key]) this.#expandedNodeKeys.delete(key)
    }
    if (projection.projectionRevision < previousRevision) {
      this.#anchorKey = undefined
      this.#scrollTop = 0
    }
    this.#updateCount += 1
    this.#refresh()
  }

  setFilter(filter: TraceFilter): void {
    this.#assertOpen()
    const next = Object.freeze({ ...filter })
    if (sameFilter(this.#filter, next)) return
    this.#filter = next
    this.#scrollTop = 0
    this.#anchorKey = undefined
    this.#refresh()
  }

  patchFilter(patch: Partial<TraceFilter>): void {
    this.setFilter({ ...this.#filter, ...patch })
  }

  resetFilter(): void {
    this.setFilter(defaultTraceFilter())
  }

  setFold(state: TraceFoldState): void {
    this.#assertOpen()
    const next = normalizeTraceFoldState(state)
    if (sameFold(this.#fold, next)) return
    this.#fold = next
    this.#refresh()
  }

  patchFold(patch: Partial<Omit<TraceFoldState, "collapsedKeys">>): void {
    this.setFold({ ...this.#fold, ...patch })
  }

  toggleFold(key: string): void {
    this.setFold(toggleTraceFold(this.#fold, key))
  }

  expandAncestors(nodeKey: string): void {
    this.setFold(expandTraceAncestors(this.#fold, this.#projection, nodeKey))
  }

  select(nodeKey: string | undefined, options: { expand?: boolean; anchor?: boolean } = {}): void {
    this.#assertOpen()
    if (nodeKey && !this.#projection.nodesByKey[nodeKey]) {
      throw new Error(`Unknown trace node ${nodeKey}.`)
    }
    this.#selectedNodeKey = nodeKey
    if (nodeKey && options.expand !== false) this.#rememberExpanded(nodeKey)
    if (nodeKey && options.anchor !== false) {
      this.#anchorKey = nodeKey
      this.#fold = expandTraceAncestors(this.#fold, this.#projection, nodeKey)
    }
    this.#refresh()
  }

  toggleExpanded(nodeKey: string): void {
    this.#assertOpen()
    if (!this.#projection.nodesByKey[nodeKey]) return
    if (this.#expandedNodeKeys.has(nodeKey)) this.#expandedNodeKeys.delete(nodeKey)
    else this.#rememberExpanded(nodeKey)
    this.#refresh()
  }

  setViewport(scrollTop: number, viewportHeight = this.#viewportHeight): void {
    this.#assertOpen()
    const top = boundedNumber(scrollTop, 0, 0, Number.MAX_SAFE_INTEGER)
    const height = boundedNumber(viewportHeight, this.#viewportHeight, 1, 100_000)
    if (top === this.#scrollTop && height === this.#viewportHeight) return
    this.#scrollTop = top
    this.#viewportHeight = height
    this.#refresh()
  }

  measure(nodeKey: string, height: number): boolean {
    this.#assertOpen()
    const previousIndex = this.#virtualizer.indexForKey(nodeKey)
    if (previousIndex === undefined) return false
    const changed = this.#virtualizer.measure(nodeKey, height)
    if (changed) this.#refresh()
    return changed
  }

  reveal(nodeKey: string): number | undefined {
    this.#assertOpen()
    const node = this.#projection.nodesByKey[nodeKey]
    if (!node) return undefined
    this.#fold = expandTraceAncestors(this.#fold, this.#projection, nodeKey)
    this.#selectedNodeKey = nodeKey
    this.#anchorKey = nodeKey
    this.#rememberExpanded(nodeKey)
    this.#refresh()
    return this.#virtualizer.offsetForKey(nodeKey)
  }

  pin(nodeKey: string, note?: string): TraceReportReference {
    this.#assertOpen()
    const reference = this.#pins.pin(this.#projection, nodeKey, note)
    this.#rememberExpanded(nodeKey)
    this.#refresh()
    return reference
  }

  unpin(nodeKey: string): boolean {
    this.#assertOpen()
    return this.#pins.unpinNode(nodeKey)
  }

  togglePin(nodeKey: string, note?: string): boolean {
    this.#assertOpen()
    if (this.#pins.has(nodeKey)) {
      this.#pins.unpinNode(nodeKey)
      return false
    }
    this.#pins.pin(this.#projection, nodeKey, note)
    return true
  }

  navigationTargets(nodeKey: string): readonly TraceNavigationTarget[] {
    const node = this.#projection.nodesByKey[nodeKey]
    return node ? navigationTargetsForTraceNode(node) : Object.freeze([])
  }

  navigate(target: TraceNavigationTarget): TraceNavigationReceipt {
    this.#assertOpen()
    return this.#navigation.navigate(target, this.#projection)
  }

  navigateNode(nodeKey: string, targetId: string): TraceNavigationReceipt | undefined {
    const target = this.navigationTargets(nodeKey).find((candidate) => candidate.id === targetId)
    return target ? this.navigate(target) : undefined
  }

  navigateBack(): TraceNavigationReceipt | undefined {
    this.#assertOpen()
    return this.#navigation.back(this.#projection)
  }

  reconciliation(): TraceReconciliationResult | undefined {
    return this.#reconciliationResult
  }

  exportReport() {
    this.#assertOpen()
    return this.#pins.export(this.#projection)
  }

  close(reason = "Trace viewer closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#unsubscribeNavigation()
    this.#unsubscribePins()
    this.#unsubscribeFocus()
    this.#unsubscribeShortcut()
    this.#listeners.clear()
    this.#search.close()
    this.#virtualizer.close()
    this.#navigation.cancel(reason)
    this.#navigation.close()
    this.#pins.close()
    this.#reconciliation.close()
    this.#expandedNodeKeys.clear()
    this.#selectedNodeKey = undefined
    // Deliberately no task, worker, terminal, browser, permission, or lifecycle API call.
  }

  audit(): TraceWorkbenchControllerAudit {
    return Object.freeze({
      taskId: this.#taskId,
      closed: this.#closed,
      disabled: this.#disabled,
      projectionRevision: this.#projection.projectionRevision,
      selectedNodeKey: this.#selectedNodeKey,
      expandedNodeCount: this.#expandedNodeKeys.size,
      filteredNodeCount: this.#snapshot.filtered.nodes.length,
      foldedNodeCount: this.#snapshot.folded.nodes.length,
      virtualItemCount: this.#snapshot.virtualWindow.items.length,
      pinCount: this.#snapshot.pins.length,
      navigationCount: this.#snapshot.navigationHistory.length,
      reconciliationChangeCount: this.#reconciliationResult?.changes.length ?? 0,
      updateCount: this.#updateCount,
      listenerCount: this.#listeners.size,
      closeStopsTask: false,
      search: this.#search.audit(),
      virtualizer: this.#virtualizer.audit(),
      navigation: this.#navigation.audit(),
      pins: this.#pins.audit(),
      reconciliation: this.#reconciliation.audit(),
    })
  }

  #receiveFocus(request: TraceFocusRequest): void {
    if (this.#closed || request.taskId !== this.#taskId) return
    const target = resolveTraceFocusRequest(this.#projection, request)
    if (!target?.traceNodeKey) return
    this.reveal(target.traceNodeKey)
    this.#navigation.navigate(target, this.#projection)
  }

  #rememberExpanded(nodeKey: string): void {
    this.#expandedNodeKeys.delete(nodeKey)
    this.#expandedNodeKeys.add(nodeKey)
    while (this.#expandedNodeKeys.size > this.#maximumExpandedNodes) {
      const oldest = this.#expandedNodeKeys.values().next().value
      if (!oldest) break
      this.#expandedNodeKeys.delete(oldest)
    }
  }

  #deriveSnapshot(): TraceWorkbenchSnapshot {
    const filtered = filterCausalTrace(this.#projection, this.#filter, this.#search)
    const folded = foldCausalTrace(this.#projection, filtered, this.#fold)
    this.#virtualizer.setNodes(folded.nodes)
    const pinnedKeys = this.#pins.list().map((reference) => reference.nodeKey)
    const virtualWindow = folded.nodes.length
      ? this.#virtualizer.window({
          scrollTop: this.#scrollTop,
          viewportHeight: this.#viewportHeight,
          overscanPx: this.#overscanPx,
          anchorKey: this.#anchorKey,
          pinnedKeys,
        })
      : emptyVirtualWindow()
    return Object.freeze({
      projection: this.#projection,
      filtered,
      folded,
      virtualWindow,
      selectedNodeKey: this.#selectedNodeKey,
      expandedNodeKeys: new Set(this.#expandedNodeKeys),
      filter: this.#filter,
      fold: this.#fold,
      pins: this.#pins.list(),
      navigationHistory: this.#navigation.history,
      activeNavigation: this.#navigation.active,
      closed: this.#closed,
      disabled: this.#disabled,
    })
  }

  #refresh(): void {
    if (this.#closed) return
    this.#snapshot = this.#deriveSnapshot()
    for (const listener of this.#listeners) listener()
  }

  #assertOpen(): void {
    if (this.#closed) throw new Error("Trace workbench controller is closed.")
    if (this.#disabled) throw new Error("Trace workbench controller is disabled.")
  }
}
