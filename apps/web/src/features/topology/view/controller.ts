import type { TopologyProjectionView } from "../projection/index.ts"
import type {
  NavigationIntent,
  Point,
  Rect,
  TopologyControlRequest,
  TopologyControllerOptions,
  TopologyControllerSnapshot,
  TopologyEntityDetails,
  TopologyFilterState,
  TopologyLayerId,
  TopologyListener,
  TopologySelection,
} from "./contracts.ts"
import { TopologyClusterEngine } from "./clustering.ts"
import {
  TopologyControlRuntime,
  type TopologyControlRuntimeOptions,
} from "./controls.ts"
import {
  DEFAULT_TOPOLOGY_FILTERS,
  filterTopologyModel,
  normalizeTopologyFilters,
  TopologySearchIndex,
} from "./filtering.ts"
import { StableTopologyLayout } from "./layout.ts"
import { buildTopologyLayers } from "./layers.ts"
import { buildTopologyGraphModel, topologyEntityDetails } from "./model.ts"
import {
  navigationIntentsForSelection,
  selectionFromNavigation,
} from "./navigation.ts"
import { TopologyViewportController } from "./interaction.ts"
import {
  announceProjectionChange,
  announceSelection,
  announceViewport,
} from "./accessibility.ts"

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function maximum(value: number | undefined, fallback: number): number {
  return Number.isFinite(value) ? Math.max(1, Math.floor(Number(value))) : fallback
}

function emptyProjection(taskId: string): TopologyProjectionView {
  const evidence = Object.freeze({})
  const analysis = Object.freeze({
    roots: freeze([]),
    leaves: freeze([]),
    isolated: freeze([]),
    cycles: freeze([]),
    components: freeze([]),
    topologicalOrder: freeze([]),
    criticalPath: Object.freeze({
      nodeIds: freeze([]),
      edgeIds: freeze([]),
      weight: 0,
      complete: false,
    }),
    reachableByNode: Object.freeze({}),
    ancestorsByNode: Object.freeze({}),
    depthByNode: Object.freeze({}),
    missingNodeIds: freeze([]),
    duplicateEdgeIds: freeze([]),
  })
  const metrics = Object.freeze({
    decisionCount: 0,
    selectedRouteCount: 0,
    candidateCount: 0,
    acceptedCandidateCount: 0,
    routeChangeCount: 0,
    topologyMutationCount: 0,
    openWorldMutationCount: 0,
    fixedCandidateSelectionCount: 0,
    placementChangeCount: 0,
    devicePlacementCount: 0,
    edgePlacementCount: 0,
    cloudPlacementCount: 0,
    rejectedBranchCount: 0,
    rebasedBranchCount: 0,
    pendingWriteCount: 0,
    committedWriteCount: 0,
    requirementChangeCount: 0,
    supersededNodeCount: 0,
    effectiveStepCount: 0,
    effectiveRouteTransitionCount: 0,
    routeDensity: 0,
    graphDensity: 0,
  })
  return Object.freeze({
    schema: "zyra.topology-projection/v1",
    taskId,
    runIds: freeze([]),
    projectionRevision: 0,
    graphRevision: 0,
    commitRevision: 0,
    nodes: freeze([]),
    edges: freeze([]),
    mutations: freeze([]),
    routes: freeze([]),
    placements: freeze([]),
    checkpoints: freeze([]),
    branches: freeze([]),
    conflicts: freeze([]),
    requirementChanges: freeze([]),
    namespaces: freeze([]),
    analysis,
    metrics,
    diagnostics: Object.freeze({
      ready: false,
      disabled: false,
      snapshotComplete: false,
      connected: false,
      projectionRevision: 0,
      cursorGeneration: 0,
      committedSequence: 0,
      highWatermark: 0,
      lag: 0,
      duplicateEvents: 0,
      staleEvents: 0,
      missingSequences: freeze([]),
      warnings: freeze([]),
      errors: freeze([]),
      rejectedEntityIds: freeze([]),
      missingEvidenceEntityIds: freeze([]),
      ambiguousEntityIds: freeze([]),
      sensitiveFieldDrops: 0,
    }),
    evidenceByEntity: evidence,
  })
}

export class TopologyWorkbenchController {
  readonly layout: StableTopologyLayout
  readonly clusters: TopologyClusterEngine
  readonly search = new TopologySearchIndex()
  readonly viewport: TopologyViewportController
  readonly controls?: TopologyControlRuntime
  readonly maximumRenderedNodes: number
  readonly maximumRenderedEdges: number
  readonly now: () => number
  #filters: TopologyFilterState = DEFAULT_TOPOLOGY_FILTERS
  #activeLayer: TopologyLayerId = "structure"
  #snapshot: TopologyControllerSnapshot
  #listeners = new Set<TopologyListener>()
  #viewportUnsubscribe: () => void
  #controlUnsubscribe?: () => void
  #announcements: string[] = []
  #closed = false
  #revision = 0
  #publishingViewport = false

  constructor(
    taskId: string,
    options: TopologyControllerOptions = {},
  ) {
    this.maximumRenderedNodes = maximum(options.maximumRenderedNodes, 800)
    this.maximumRenderedEdges = maximum(options.maximumRenderedEdges, 1_600)
    this.now = options.now ?? Date.now
    this.layout = new StableTopologyLayout()
    this.clusters = new TopologyClusterEngine()
    this.viewport = new TopologyViewportController({
      viewport: options.initialViewport,
      transform: options.initialTransform,
      maximumRenderedNodes: this.maximumRenderedNodes,
      maximumRenderedEdges: this.maximumRenderedEdges,
      clusterThreshold: options.clusterThreshold ?? options.largeGraphThreshold ?? 1_000,
    })
    if (options.controlTransport) {
      const controlOptions: TopologyControlRuntimeOptions = {
        transport: options.controlTransport,
        now: this.now,
      }
      this.controls = new TopologyControlRuntime(controlOptions)
      this.#controlUnsubscribe = this.controls.subscribe(() => this.#refreshReceipts())
    }
    const model = buildTopologyGraphModel(emptyProjection(taskId))
    const layout = this.layout.compute(model)
    const clusters = this.clusters.compute(model, layout)
    const filtered = filterTopologyModel(model, this.#filters, this.search)
    const layers = buildTopologyLayers(model)
    this.viewport.bind(
      {
        model,
        layout,
        clusters,
        clusterEngine: this.clusters,
        filtered,
        activeLayer: layers.get(this.#activeLayer),
      },
      { fit: false },
    )
    this.#snapshot = Object.freeze({
      model,
      layout,
      clusters,
      filters: this.#filters,
      filtered,
      layers,
      activeLayer: this.#activeLayer,
      viewport: this.viewport.getSnapshot(),
      renderPlan: this.viewport.getRenderPlan(),
      receipts: freeze([]),
      loading: true,
      reconnecting: false,
      revision: 0,
    })
    this.#viewportUnsubscribe = this.viewport.subscribe(() => {
      if (this.#publishingViewport || this.#closed) return
      this.#publishingViewport = true
      try {
        this.#replace({
          viewport: this.viewport.getSnapshot(),
          renderPlan: this.viewport.getRenderPlan(),
          selectedDetails: this.#detailsForSelection(
            this.viewport.getSnapshot().selected,
          ),
        })
      } finally {
        this.#publishingViewport = false
      }
    })
  }

  getSnapshot = (): TopologyControllerSnapshot => this.#snapshot

  subscribe = (listener: TopologyListener): (() => void) => {
    this.#assertOpen()
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  project(
    projection: TopologyProjectionView,
    options: { fit?: boolean; preserveFilters?: boolean } = {},
  ): TopologyControllerSnapshot {
    this.#assertOpen()
    if (projection.taskId !== this.#snapshot.model.taskId) {
      throw new TypeError(
        `Topology projection task ${projection.taskId} does not match controller task ${this.#snapshot.model.taskId}.`,
      )
    }
    const previousModel = this.#snapshot.model
    const staleProjection =
      projection.projectionRevision < previousModel.projectionRevision ||
      (projection.projectionRevision === previousModel.projectionRevision &&
        projection.graphRevision < previousModel.graphRevision) ||
      (projection.projectionRevision === previousModel.projectionRevision &&
        projection.graphRevision === previousModel.graphRevision &&
        projection.commitRevision < previousModel.commitRevision)
    if (staleProjection) {
      this.#rememberAnnouncement(
        `Ignored stale topology revision ${projection.projectionRevision}; current revision is ${previousModel.projectionRevision}.`,
      )
      return this.#snapshot
    }
    const model = buildTopologyGraphModel(projection)
    if (!options.preserveFilters) {
      this.#filters = normalizeTopologyFilters(this.#filters)
    }
    const layout = this.layout.compute(model)
    const clusters = this.clusters.compute(model, layout)
    const filtered = filterTopologyModel(model, this.#filters, this.search)
    const layers = buildTopologyLayers(model)
    this.viewport.bind(
      {
        model,
        layout,
        clusters,
        clusterEngine: this.clusters,
        filtered,
        activeLayer: layers.get(this.#activeLayer),
      },
      { fit: options.fit },
    )
    const announcement = announceProjectionChange(previousModel, model)
    if (announcement) this.#rememberAnnouncement(announcement)
    const receipts = this.controls?.observe(model) ?? freeze([])
    const error =
      projection.diagnostics.errors.length > 0
        ? projection.diagnostics.errors.join("; ")
        : undefined
    this.#replace({
      model,
      layout,
      clusters,
      filtered,
      layers,
      viewport: this.viewport.getSnapshot(),
      renderPlan: this.viewport.getRenderPlan(),
      receipts,
      selectedDetails: this.#detailsForSelection(
        this.viewport.getSnapshot().selected,
        model,
        clusters,
      ),
      loading: !projection.diagnostics.snapshotComplete,
      reconnecting:
        projection.diagnostics.snapshotComplete && !projection.diagnostics.connected,
      error,
    })
    return this.#snapshot
  }

  setFilters(
    patch: Partial<TopologyFilterState>,
  ): TopologyControllerSnapshot {
    this.#assertOpen()
    this.#filters = normalizeTopologyFilters({ ...this.#filters, ...patch })
    return this.#rebuildDerived()
  }

  resetFilters(): TopologyControllerSnapshot {
    this.#filters = DEFAULT_TOPOLOGY_FILTERS
    return this.#rebuildDerived()
  }

  setLayer(id: TopologyLayerId): boolean {
    if (!this.#snapshot.layers.has(id)) return false
    if (this.#activeLayer === id) return true
    this.#activeLayer = id
    this.viewport.bind({
      model: this.#snapshot.model,
      layout: this.#snapshot.layout,
      clusters: this.#snapshot.clusters,
      clusterEngine: this.clusters,
      filtered: this.#snapshot.filtered,
      activeLayer: this.#snapshot.layers.get(id),
    })
    this.#replace({
      activeLayer: id,
      viewport: this.viewport.getSnapshot(),
      renderPlan: this.viewport.getRenderPlan(),
    })
    this.#rememberAnnouncement(`Topology layer changed to ${id}.`)
    return true
  }

  select(
    kind: TopologySelection["kind"],
    id: string,
    source: TopologySelection["source"] = "programmatic",
  ): TopologySelection | undefined {
    const selection = this.viewport.select(kind, id, source, this.now())
    if (selection) this.#rememberAnnouncement(announceSelection(this.#snapshot.model, selection))
    return selection
  }

  clearSelection(): void {
    this.viewport.clearSelection()
    this.#rememberAnnouncement("Topology selection cleared.")
  }

  navigate(intent: NavigationIntent): boolean {
    const selection = selectionFromNavigation(intent, "programmatic", this.now())
    if (!selection) return false
    if (selection.kind === "node") this.viewport.expandToNode(selection.id)
    const result = this.select(selection.kind, selection.id, selection.source)
    if (result?.kind === "node") this.viewport.focusNode(result.id)
    return Boolean(result)
  }

  navigationIntents(): readonly NavigationIntent[] {
    return navigationIntentsForSelection(
      this.#snapshot.model,
      this.#snapshot.viewport.selected,
    )
  }

  resize(viewport: Rect): void {
    this.viewport.resize(viewport)
  }

  fit(): void {
    this.viewport.fit()
    this.#rememberAnnouncement(announceViewport(this.#snapshot))
  }

  pan(delta: Point): void {
    this.viewport.pan(delta)
  }

  zoomAt(screenPoint: Point, scale: number): void {
    this.viewport.zoomAt(screenPoint, scale)
  }

  movePinnedNode(nodeId: string, center: Point): boolean {
    const layout = this.layout.movePinnedNode(nodeId, center)
    if (!layout) return false
    const clusters = this.clusters.compute(this.#snapshot.model, layout)
    this.viewport.bind({
      model: this.#snapshot.model,
      layout,
      clusters,
      clusterEngine: this.clusters,
      filtered: this.#snapshot.filtered,
      activeLayer: this.#snapshot.layers.get(this.#activeLayer),
    })
    this.#replace({
      layout,
      clusters,
      viewport: this.viewport.getSnapshot(),
      renderPlan: this.viewport.getRenderPlan(),
    })
    return true
  }

  unpinNode(nodeId: string): boolean {
    const removed = this.layout.unpin(nodeId)
    if (removed) this.project(this.#snapshot.model.source, { preserveFilters: true })
    return removed
  }

  setConnectionState(input: {
    loading?: boolean
    reconnecting?: boolean
    error?: string
  }): void {
    this.#replace({
      loading: input.loading ?? this.#snapshot.loading,
      reconnecting: input.reconnecting ?? this.#snapshot.reconnecting,
      error: Object.prototype.hasOwnProperty.call(input, "error")
        ? input.error
        : this.#snapshot.error,
    })
    if (input.error) this.#rememberAnnouncement(`Topology error: ${input.error}`)
    else if (input.reconnecting) this.#rememberAnnouncement("Topology stream reconnecting.")
  }

  async submitControl(
    request: Omit<TopologyControlRequest, "taskId" | "runId"> & {
      taskId?: string
      runId?: string
    },
  ) {
    if (!this.controls) {
      throw new TypeError("Topology control transport is not configured.")
    }
    const runId =
      request.runId ?? this.#snapshot.model.source.runIds.at(-1)
    if (!runId) throw new TypeError("Topology control requires a canonical run id.")
    const receipt = await this.controls.submit({
      ...request,
      taskId: request.taskId ?? this.#snapshot.model.taskId,
      runId,
    })
    this.#rememberAnnouncement(receipt.summary)
    this.#refreshReceipts()
    return receipt
  }

  cancelControl(receiptId: string): boolean {
    return this.controls?.cancel(receiptId) ?? false
  }

  announcements(): readonly string[] {
    return freeze(this.#announcements)
  }

  takeAnnouncements(): readonly string[] {
    const values = freeze(this.#announcements)
    this.#announcements = []
    return values
  }

  disableProbe(): never {
    throw new Error(
      "TopologyWorkbenchController disabled: task topology interaction, virtualization, navigation and control receipts are unavailable.",
    )
  }

  close(reason = "Topology workbench controller closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#viewportUnsubscribe()
    this.#controlUnsubscribe?.()
    this.controls?.close(reason)
    this.#listeners.clear()
  }

  #rebuildDerived(): TopologyControllerSnapshot {
    const filtered = filterTopologyModel(
      this.#snapshot.model,
      this.#filters,
      this.search,
    )
    this.viewport.bind({
      model: this.#snapshot.model,
      layout: this.#snapshot.layout,
      clusters: this.#snapshot.clusters,
      clusterEngine: this.clusters,
      filtered,
      activeLayer: this.#snapshot.layers.get(this.#activeLayer),
    })
    this.#replace({
      filters: this.#filters,
      filtered,
      viewport: this.viewport.getSnapshot(),
      renderPlan: this.viewport.getRenderPlan(),
      selectedDetails: this.#detailsForSelection(
        this.viewport.getSnapshot().selected,
      ),
    })
    this.#rememberAnnouncement(announceViewport(this.#snapshot))
    return this.#snapshot
  }

  #detailsForSelection(
    selection: TopologySelection | undefined,
    model = this.#snapshot?.model,
    clusters = this.#snapshot?.clusters,
  ): TopologyEntityDetails | undefined {
    if (!selection || !model) return undefined
    return topologyEntityDetails(
      model,
      selection.kind,
      selection.id,
      selection.kind === "cluster"
        ? clusters?.clusterById.get(selection.id)
        : undefined,
    )
  }

  #refreshReceipts(): void {
    if (!this.controls || this.#closed) return
    this.#replace({ receipts: this.controls.receipts() })
  }

  #rememberAnnouncement(value: string): void {
    const message = value.trim()
    if (!message) return
    this.#announcements.push(message)
    if (this.#announcements.length > 100) {
      this.#announcements.splice(0, this.#announcements.length - 100)
    }
  }

  #replace(patch: Partial<TopologyControllerSnapshot>): void {
    this.#revision += 1
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      revision: this.#revision,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Observers cannot mutate topology runtime state.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Topology workbench controller is closed.")
  }
}
