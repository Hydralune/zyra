import type {
  ClusterSnapshot,
  FilterResult,
  GraphEntityKind,
  LayerSnapshot,
  LayoutSnapshot,
  Point,
  Rect,
  RenderCluster,
  RenderEdge,
  RenderNode,
  RenderPlan,
  TopologyGraphModel,
  TopologySelection,
  Transform,
  ViewportSnapshot,
} from "./contracts.ts"
import {
  clamp,
  clampTransformToBounds,
  containsPoint,
  expandRect,
  fitTransform,
  graphRectToScreen,
  graphToScreen,
  panTransform,
  point,
  quantizeTransform,
  rect,
  screenRectToGraph,
  screenToGraph,
  zoomTransformAt,
} from "./geometry.ts"
import { TopologyClusterEngine } from "./clustering.ts"
import { spatialItemsFromLayout, TopologySpatialIndex } from "./spatial-index.ts"

export interface ViewportOptions {
  viewport?: Partial<Rect>
  transform?: Partial<Transform>
  minimumScale?: number
  maximumScale?: number
  overscanPixels?: number
  maximumRenderedNodes?: number
  maximumRenderedEdges?: number
  clusterThreshold?: number
  wheelSensitivity?: number
}
interface NormalizedOptions {
  minimumScale: number
  maximumScale: number
  overscanPixels: number
  maximumRenderedNodes: number
  maximumRenderedEdges: number
  clusterThreshold: number
  wheelSensitivity: number
}

interface PointerSession {
  pointerId: number
  screenStart: Point
  graphStart: Point
  lastScreen: Point
  startTransform: Transform
  mode: "pan" | "node-drag" | "select-box"
  nodeId?: string
  moved: boolean
}

interface RenderContext {
  model: TopologyGraphModel
  layout: LayoutSnapshot
  clusters: ClusterSnapshot
  clusterEngine: TopologyClusterEngine
  filtered: FilterResult
  activeLayer?: LayerSnapshot
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

function normalizeOptions(options: ViewportOptions): NormalizedOptions {
  const minimumScale = finiteBounded(options.minimumScale, 0.025, 0.001, 10)
  const maximumScale = finiteBounded(options.maximumScale, 6, minimumScale, 100)
  return Object.freeze({
    minimumScale,
    maximumScale,
    overscanPixels: finiteBounded(options.overscanPixels, 160, 0, 4_000),
    maximumRenderedNodes: Math.floor(
      finiteBounded(options.maximumRenderedNodes, 800, 16, 20_000),
    ),
    maximumRenderedEdges: Math.floor(
      finiteBounded(options.maximumRenderedEdges, 1_600, 16, 100_000),
    ),
    clusterThreshold: Math.floor(
      finiteBounded(options.clusterThreshold, 1_000, 50, 1_000_000),
    ),
    wheelSensitivity: finiteBounded(options.wheelSensitivity, 0.0018, 0.0001, 0.1),
  })
}

function initialViewport(options: ViewportOptions): Rect {
  return rect(
    options.viewport?.x ?? 0,
    options.viewport?.y ?? 0,
    finiteBounded(options.viewport?.width, 1_000, 1, 100_000),
    finiteBounded(options.viewport?.height, 700, 1, 100_000),
  )
}

function initialTransform(options: ViewportOptions): Transform {
  return Object.freeze({
    x: Number.isFinite(options.transform?.x) ? Number(options.transform?.x) : 0,
    y: Number.isFinite(options.transform?.y) ? Number(options.transform?.y) : 0,
    scale: finiteBounded(options.transform?.scale, 1, 0.001, 100),
  })
}

function emptyQuery() {
  return Object.freeze({
    itemIds: Object.freeze([]),
    nodeIds: Object.freeze([]),
    edgeIds: Object.freeze([]),
    clusterIds: Object.freeze([]),
    visitedCells: 0,
    candidateCount: 0,
    clipped: false,
  })
}

function emptyRenderPlan(): RenderPlan {
  return Object.freeze({
    revision: 0,
    nodes: Object.freeze([]),
    edges: Object.freeze([]),
    clusters: Object.freeze([]),
    totalNodeCount: 0,
    totalEdgeCount: 0,
    virtualizedNodeCount: 0,
    virtualizedEdgeCount: 0,
    clipped: false,
    lodLevel: -1,
    query: emptyQuery(),
  })
}

export class TopologyViewportController {
  readonly options: NormalizedOptions
  readonly spatial = new TopologySpatialIndex()
  #snapshot: ViewportSnapshot
  #pointer?: PointerSession
  #listeners = new Set<() => void>()
  #renderPlan: RenderPlan = emptyRenderPlan()
  #context?: RenderContext
  #revision = 0

  constructor(options: ViewportOptions = {}) {
    this.options = normalizeOptions(options)
    this.#snapshot = Object.freeze({
      viewport: initialViewport(options),
      transform: initialTransform(options),
      graphBounds: rect(),
      minimumScale: this.options.minimumScale,
      maximumScale: this.options.maximumScale,
      interacting: false,
      expandedClusterIds: Object.freeze([]),
      lastInput: "programmatic",
      revision: 0,
    })
  }

  getSnapshot = (): ViewportSnapshot => this.#snapshot

  getRenderPlan = (): RenderPlan => this.#renderPlan

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  bind(context: RenderContext, options: { fit?: boolean } = {}): RenderPlan {
    const previousBounds = this.#snapshot.graphBounds
    this.#context = context
    this.spatial.rebuild(
      spatialItemsFromLayout(context.layout, context.clusters.clusters),
      context.layout.bounds,
    )
    let transform = this.#snapshot.transform
    if (
      options.fit ||
      (previousBounds.width === 0 &&
        previousBounds.height === 0 &&
        context.layout.bounds.width > 0)
    ) {
      transform = fitTransform(
        context.layout.bounds,
        this.#snapshot.viewport,
        56,
        this.options.minimumScale,
        this.options.maximumScale,
      )
    } else {
      transform = clampTransformToBounds(
        transform,
        context.layout.bounds,
        this.#snapshot.viewport,
      )
    }
    const selection = this.#repairSelection(this.#snapshot.selected, context)
    const focusEntityId = this.#repairFocus(this.#snapshot.focusEntityId, context)
    this.#replace({
      graphBounds: context.layout.bounds,
      transform: quantizeTransform(transform),
      selection,
      focusEntityId,
      hoverEntityId:
        this.#snapshot.hoverEntityId &&
        this.#entityExists(this.#snapshot.hoverEntityId, context)
          ? this.#snapshot.hoverEntityId
          : undefined,
      lastInput: "programmatic",
    })
    this.#rebuildRenderPlan()
    return this.#renderPlan
  }

  resize(viewport: Rect): ViewportSnapshot {
    const next = rect(viewport.x, viewport.y, viewport.width, viewport.height)
    const transform = this.#context
      ? clampTransformToBounds(
          this.#snapshot.transform,
          this.#context.layout.bounds,
          next,
        )
      : this.#snapshot.transform
    this.#replace({
      viewport: next,
      transform: quantizeTransform(transform),
      lastInput: "programmatic",
    })
    this.#rebuildRenderPlan()
    return this.#snapshot
  }

  fit(padding = 56): ViewportSnapshot {
    if (!this.#context || this.#context.layout.nodes.size === 0) return this.#snapshot
    this.#replace({
      transform: quantizeTransform(
        fitTransform(
          this.#context.layout.bounds,
          this.#snapshot.viewport,
          padding,
          this.options.minimumScale,
          this.options.maximumScale,
        ),
      ),
      lastInput: "programmatic",
    })
    this.#rebuildRenderPlan()
    return this.#snapshot
  }

  focusBounds(bounds: Rect, padding = 72): ViewportSnapshot {
    this.#replace({
      transform: quantizeTransform(
        fitTransform(
          bounds,
          this.#snapshot.viewport,
          padding,
          this.options.minimumScale,
          this.options.maximumScale,
        ),
      ),
      lastInput: "programmatic",
    })
    this.#rebuildRenderPlan()
    return this.#snapshot
  }

  focusNode(nodeId: string, desiredScale?: number): boolean {
    const node = this.#context?.layout.nodes.get(nodeId)
    if (!node) return false
    const scale = clamp(
      desiredScale ?? Math.max(0.8, this.#snapshot.transform.scale),
      this.options.minimumScale,
      this.options.maximumScale,
    )
    const viewportCenter = point(
      this.#snapshot.viewport.x + this.#snapshot.viewport.width / 2,
      this.#snapshot.viewport.y + this.#snapshot.viewport.height / 2,
    )
    this.#replace({
      transform: quantizeTransform({
        x: viewportCenter.x - node.center.x * scale,
        y: viewportCenter.y - node.center.y * scale,
        scale,
      }),
      focusEntityId: nodeId,
      lastInput: "programmatic",
    })
    this.#rebuildRenderPlan()
    return true
  }

  pan(delta: Point, input: ViewportSnapshot["lastInput"] = "programmatic"): ViewportSnapshot {
    let transform = panTransform(this.#snapshot.transform, delta)
    if (this.#context) {
      transform = clampTransformToBounds(
        transform,
        this.#context.layout.bounds,
        this.#snapshot.viewport,
      )
    }
    this.#replace({ transform: quantizeTransform(transform), lastInput: input })
    this.#rebuildRenderPlan()
    return this.#snapshot
  }

  zoomAt(
    screenPoint: Point,
    scale: number,
    input: ViewportSnapshot["lastInput"] = "programmatic",
  ): ViewportSnapshot {
    let transform = zoomTransformAt(
      this.#snapshot.transform,
      screenPoint,
      scale,
      this.options.minimumScale,
      this.options.maximumScale,
    )
    if (this.#context) {
      transform = clampTransformToBounds(
        transform,
        this.#context.layout.bounds,
        this.#snapshot.viewport,
      )
    }
    this.#replace({ transform: quantizeTransform(transform), lastInput: input })
    this.#rebuildRenderPlan()
    return this.#snapshot
  }

  wheel(screenPoint: Point, deltaY: number): ViewportSnapshot {
    const factor = Math.exp(-deltaY * this.options.wheelSensitivity)
    return this.zoomAt(
      screenPoint,
      this.#snapshot.transform.scale * factor,
      "wheel",
    )
  }

  pointerDown(input: {
    pointerId: number
    screen: Point
    targetNodeId?: string
    shiftKey?: boolean
    button?: number
  }): boolean {
    if (this.#pointer || (input.button ?? 0) !== 0) return false
    const mode: PointerSession["mode"] = input.targetNodeId
      ? "node-drag"
      : input.shiftKey
        ? "select-box"
        : "pan"
    this.#pointer = {
      pointerId: input.pointerId,
      screenStart: input.screen,
      graphStart: screenToGraph(input.screen, this.#snapshot.transform),
      lastScreen: input.screen,
      startTransform: this.#snapshot.transform,
      mode,
      nodeId: input.targetNodeId,
      moved: false,
    }
    this.#replace({
      interacting: true,
      pointerId: input.pointerId,
      lastInput: "pointer",
    })
    return true
  }

  pointerMove(input: {
    pointerId: number
    screen: Point
    onNodeDrag?: (nodeId: string, graphPoint: Point) => void
  }): boolean {
    const session = this.#pointer
    if (!session || session.pointerId !== input.pointerId) return false
    const delta = {
      x: input.screen.x - session.lastScreen.x,
      y: input.screen.y - session.lastScreen.y,
    }
    session.lastScreen = input.screen
    if (Math.abs(input.screen.x - session.screenStart.x) > 3 ||
        Math.abs(input.screen.y - session.screenStart.y) > 3) {
      session.moved = true
    }
    if (session.mode === "pan") {
      this.pan(delta, "pointer")
    } else if (session.mode === "node-drag" && session.nodeId) {
      input.onNodeDrag?.(
        session.nodeId,
        screenToGraph(input.screen, this.#snapshot.transform),
      )
    }
    return true
  }

  pointerUp(input: {
    pointerId: number
    screen: Point
    now?: number
  }): { handled: boolean; click: boolean; selectionBox?: Rect } {
    const session = this.#pointer
    if (!session || session.pointerId !== input.pointerId) {
      return { handled: false, click: false }
    }
    this.#pointer = undefined
    this.#replace({
      interacting: false,
      pointerId: undefined,
      lastInput: "pointer",
    })
    if (session.mode === "select-box" && session.moved) {
      const end = screenToGraph(input.screen, this.#snapshot.transform)
      const selectionBox = rect(
        Math.min(session.graphStart.x, end.x),
        Math.min(session.graphStart.y, end.y),
        Math.abs(end.x - session.graphStart.x),
        Math.abs(end.y - session.graphStart.y),
      )
      return { handled: true, click: false, selectionBox }
    }
    return { handled: true, click: !session.moved }
  }

  pointerCancel(pointerId: number): boolean {
    if (!this.#pointer || this.#pointer.pointerId !== pointerId) return false
    this.#pointer = undefined
    this.#replace({
      interacting: false,
      pointerId: undefined,
      lastInput: "pointer",
    })
    return true
  }

  hoverAt(screen: Point): string | undefined {
    if (!this.#context) return undefined
    const graphPoint = screenToGraph(screen, this.#snapshot.transform)
    const radius = Math.max(4, 14 / this.#snapshot.transform.scale)
    const hit = this.spatial.hitTest(graphPoint, {
      radius,
      maximum: 1,
      kinds: ["node", "cluster", "edge"],
    })[0]
    const next = hit?.id
    if (next !== this.#snapshot.hoverEntityId) {
      this.#replace({
        hoverEntityId: next,
        lastInput: "pointer",
      })
      this.#rebuildRenderPlan()
    }
    return next
  }

  clearHover(): void {
    if (!this.#snapshot.hoverEntityId) return
    this.#replace({ hoverEntityId: undefined, lastInput: "pointer" })
    this.#rebuildRenderPlan()
  }

  select(
    kind: GraphEntityKind,
    id: string,
    source: TopologySelection["source"] = "programmatic",
    now = Date.now(),
  ): TopologySelection | undefined {
    if (!this.#context || !this.#selectionExists(kind, id, this.#context)) return undefined
    const selection = Object.freeze({ kind, id, source, selectedAt: now })
    this.#replace({
      selection,
      focusEntityId: kind === "node" ? id : this.#snapshot.focusEntityId,
      lastInput:
        source === "pointer"
          ? "pointer"
          : source === "keyboard"
            ? "keyboard"
            : source === "minimap"
              ? "minimap"
              : "programmatic",
    })
    this.#rebuildRenderPlan()
    return selection
  }

  clearSelection(): void {
    if (!this.#snapshot.selected) return
    this.#replace({ selection: undefined, lastInput: "programmatic" })
    this.#rebuildRenderPlan()
  }

  setFocus(entityId: string | undefined, input: ViewportSnapshot["lastInput"] = "keyboard"): void {
    const repaired =
      entityId && this.#context && this.#entityExists(entityId, this.#context)
        ? entityId
        : undefined
    this.#replace({ focusEntityId: repaired, lastInput: input })
    this.#rebuildRenderPlan()
  }

  moveFocus(direction: "left" | "right" | "up" | "down"): string | undefined {
    if (!this.#context || this.#context.layout.nodes.size === 0) return undefined
    const nodes = [...this.#context.layout.nodes.values()]
    const visible = new Set(this.#context.filtered.nodeIds)
    const candidates = nodes.filter((node) => visible.has(node.id))
    if (candidates.length === 0) return undefined
    const current =
      candidates.find((node) => node.id === this.#snapshot.focusEntityId) ??
      candidates.sort((left, right) => left.rank - right.rank || left.order - right.order)[0]!
    const axis = direction === "left" || direction === "right" ? "x" : "y"
    const sign = direction === "left" || direction === "up" ? -1 : 1
    let selected: typeof current | undefined
    let selectedScore = Number.POSITIVE_INFINITY
    for (const candidate of candidates) {
      if (candidate.id === current.id) continue
      const primary =
        axis === "x"
          ? candidate.center.x - current.center.x
          : candidate.center.y - current.center.y
      if (primary * sign <= 0) continue
      const secondary =
        axis === "x"
          ? Math.abs(candidate.center.y - current.center.y)
          : Math.abs(candidate.center.x - current.center.x)
      const score = Math.abs(primary) + secondary * 2.5
      if (
        score < selectedScore ||
        (score === selectedScore &&
          (!selected || candidate.id.localeCompare(selected.id) < 0))
      ) {
        selected = candidate
        selectedScore = score
      }
    }
    if (!selected) return current.id
    this.setFocus(selected.id, "keyboard")
    if (!this.#nodeVisibleOnScreen(selected.bounds)) this.focusNode(selected.id)
    return selected.id
  }

  keyboard(input: {
    key: string
    shiftKey?: boolean
    ctrlKey?: boolean
    metaKey?: boolean
    now?: number
  }): boolean {
    const key = input.key
    if (key === "ArrowLeft") return Boolean(this.moveFocus("left"))
    if (key === "ArrowRight") return Boolean(this.moveFocus("right"))
    if (key === "ArrowUp") return Boolean(this.moveFocus("up"))
    if (key === "ArrowDown") return Boolean(this.moveFocus("down"))
    if (key === "Enter" || key === " ") {
      if (!this.#snapshot.focusEntityId) return false
      return Boolean(
        this.select(
          "node",
          this.#snapshot.focusEntityId,
          "keyboard",
          input.now ?? Date.now(),
        ),
      )
    }
    if (key === "Escape") {
      this.clearHover()
      this.clearSelection()
      return true
    }
    if (key === "0") {
      this.fit()
      return true
    }
    if (key === "+" || key === "=") {
      const center = point(
        this.#snapshot.viewport.x + this.#snapshot.viewport.width / 2,
        this.#snapshot.viewport.y + this.#snapshot.viewport.height / 2,
      )
      this.zoomAt(center, this.#snapshot.transform.scale * 1.25, "keyboard")
      return true
    }
    if (key === "-" || key === "_") {
      const center = point(
        this.#snapshot.viewport.x + this.#snapshot.viewport.width / 2,
        this.#snapshot.viewport.y + this.#snapshot.viewport.height / 2,
      )
      this.zoomAt(center, this.#snapshot.transform.scale / 1.25, "keyboard")
      return true
    }
    if (key === "Home") {
      const first = this.#orderedVisibleNodeIds()[0]
      if (!first) return false
      this.setFocus(first, "keyboard")
      this.focusNode(first)
      return true
    }
    if (key === "End") {
      const last = this.#orderedVisibleNodeIds().at(-1)
      if (!last) return false
      this.setFocus(last, "keyboard")
      this.focusNode(last)
      return true
    }
    if (key === "PageUp" || key === "PageDown") {
      const amount = this.#snapshot.viewport.height * (key === "PageUp" ? 0.8 : -0.8)
      this.pan(point(0, amount), "keyboard")
      return true
    }
    return false
  }

  toggleCluster(clusterId: string): boolean {
    if (!this.#context?.clusters.clusterById.has(clusterId)) return false
    const values = new Set(this.#snapshot.expandedClusterIds)
    if (values.has(clusterId)) values.delete(clusterId)
    else values.add(clusterId)
    this.#replace({
      expandedClusterIds: Object.freeze([...values].sort()),
      lastInput: "programmatic",
    })
    this.#rebuildRenderPlan()
    return true
  }

  expandToNode(nodeId: string): boolean {
    if (!this.#context) return false
    const values = new Set(this.#snapshot.expandedClusterIds)
    for (const id of this.#context.clusterEngine.expandPath(nodeId)) values.add(id)
    this.#replace({
      expandedClusterIds: Object.freeze([...values].sort()),
      lastInput: "programmatic",
    })
    this.#rebuildRenderPlan()
    return true
  }

  collapseAllClusters(): void {
    if (this.#snapshot.expandedClusterIds.length === 0) return
    this.#replace({
      expandedClusterIds: Object.freeze([]),
      lastInput: "programmatic",
    })
    this.#rebuildRenderPlan()
  }

  minimapNavigate(
    minimapPoint: Point,
    minimapBounds: Rect,
    graphBounds = this.#snapshot.graphBounds,
  ): boolean {
    if (minimapBounds.width <= 0 || minimapBounds.height <= 0) return false
    const graphPoint = point(
      graphBounds.x +
        ((minimapPoint.x - minimapBounds.x) / minimapBounds.width) *
          graphBounds.width,
      graphBounds.y +
        ((minimapPoint.y - minimapBounds.y) / minimapBounds.height) *
          graphBounds.height,
    )
    const viewportCenter = point(
      this.#snapshot.viewport.x + this.#snapshot.viewport.width / 2,
      this.#snapshot.viewport.y + this.#snapshot.viewport.height / 2,
    )
    const scale = this.#snapshot.transform.scale
    this.#replace({
      transform: quantizeTransform({
        x: viewportCenter.x - graphPoint.x * scale,
        y: viewportCenter.y - graphPoint.y * scale,
        scale,
      }),
      lastInput: "minimap",
    })
    this.#rebuildRenderPlan()
    return true
  }

  minimapViewport(minimapBounds: Rect): Rect {
    const graphViewport = screenRectToGraph(
      this.#snapshot.viewport,
      this.#snapshot.transform,
    )
    const graph = this.#snapshot.graphBounds
    if (graph.width <= 0 || graph.height <= 0) return rect()
    return rect(
      minimapBounds.x +
        ((graphViewport.x - graph.x) / graph.width) * minimapBounds.width,
      minimapBounds.y +
        ((graphViewport.y - graph.y) / graph.height) * minimapBounds.height,
      (graphViewport.width / graph.width) * minimapBounds.width,
      (graphViewport.height / graph.height) * minimapBounds.height,
    )
  }

  graphPoint(screenPoint: Point): Point {
    return screenToGraph(screenPoint, this.#snapshot.transform)
  }

  screenPoint(graphPoint: Point): Point {
    return graphToScreen(graphPoint, this.#snapshot.transform)
  }

  visibleGraphRect(): Rect {
    return screenRectToGraph(this.#snapshot.viewport, this.#snapshot.transform)
  }

  #orderedVisibleNodeIds(): readonly string[] {
    if (!this.#context) return Object.freeze([])
    const visible = new Set(this.#context.filtered.nodeIds)
    return Object.freeze(
      [...this.#context.layout.nodes.values()]
        .filter((node) => visible.has(node.id))
        .sort(
          (left, right) =>
            left.rank - right.rank ||
            left.order - right.order ||
            left.id.localeCompare(right.id),
        )
        .map((node) => node.id),
    )
  }

  #nodeVisibleOnScreen(bounds: Rect): boolean {
    const screen = graphRectToScreen(bounds, this.#snapshot.transform)
    const viewport = expandRect(this.#snapshot.viewport, -16)
    return !(
      screen.x + screen.width < viewport.x ||
      viewport.x + viewport.width < screen.x ||
      screen.y + screen.height < viewport.y ||
      viewport.y + viewport.height < screen.y
    )
  }

  #selectionExists(
    kind: GraphEntityKind,
    id: string,
    context: RenderContext,
  ): boolean {
    if (kind === "node") return context.model.nodeById.has(id)
    if (kind === "edge") return context.model.edgeById.has(id)
    if (kind === "route") return context.model.routeById.has(id)
    if (kind === "placement") return context.model.placementById.has(id)
    if (kind === "checkpoint") return context.model.checkpointById.has(id)
    if (kind === "branch") return context.model.branchById.has(id)
    if (kind === "change") return context.model.changes.some((value) => value.id === id)
    return context.clusters.clusterById.has(id)
  }

  #entityExists(id: string, context: RenderContext): boolean {
    return (
      context.model.nodeById.has(id) ||
      context.model.edgeById.has(id) ||
      context.model.routeById.has(id) ||
      context.model.placementById.has(id) ||
      context.model.checkpointById.has(id) ||
      context.model.branchById.has(id) ||
      context.model.changes.some((value) => value.id === id) ||
      context.clusters.clusterById.has(id)
    )
  }

  #repairSelection(
    selection: TopologySelection | undefined,
    context: RenderContext,
  ): TopologySelection | undefined {
    return selection && this.#selectionExists(selection.kind, selection.id, context)
      ? selection
      : undefined
  }

  #repairFocus(
    focus: string | undefined,
    context: RenderContext,
  ): string | undefined {
    if (focus && context.model.nodeById.has(focus)) return focus
    return context.filtered.nodeIds[0]
  }

  #replace(
    patch: Partial<Omit<ViewportSnapshot, "selected">> & {
      selection?: TopologySelection
      selected?: never
    },
  ): void {
    this.#revision += 1
    const { selection, ...rest } = patch
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...rest,
      ...(Object.prototype.hasOwnProperty.call(patch, "selection")
        ? { selected: selection }
        : {}),
      revision: this.#revision,
    })
    for (const listener of this.#listeners) listener()
  }

  #rebuildRenderPlan(): void {
    const context = this.#context
    if (!context) {
      this.#renderPlan = emptyRenderPlan()
      return
    }
    const graphViewport = screenRectToGraph(
      this.#snapshot.viewport,
      this.#snapshot.transform,
    )
    const overscan = this.options.overscanPixels / this.#snapshot.transform.scale
    const filteredNodes = new Set(context.filtered.nodeIds)
    const filteredEdges = new Set(context.filtered.edgeIds)
    // Small task graphs must remain inspectable when fitted into a narrow pane.
    const useClusters = context.model.nodes.length >= this.options.clusterThreshold
    const lodLevel = useClusters
      ? context.clusterEngine.levelForScale(
          this.#snapshot.transform.scale,
          context.model.nodes.length,
        )
      : -1
    const expanded = new Set(this.#snapshot.expandedClusterIds)
    const visibleClusters =
      lodLevel >= 0
        ? context.clusterEngine.visibleClusters(
            lodLevel,
            expanded,
            expandRect(graphViewport, overscan),
          )
        : Object.freeze([])
    const hiddenByCluster = new Set<string>()
    for (const cluster of visibleClusters) {
      if (expanded.has(cluster.id)) continue
      for (const id of cluster.nodeIds) hiddenByCluster.add(id)
    }
    const query = this.spatial.query({
      viewport: graphViewport,
      overscan,
      maximum: this.options.maximumRenderedNodes + this.options.maximumRenderedEdges,
      kinds: lodLevel >= 0 ? ["node", "edge", "cluster"] : ["node", "edge"],
    })
    const nodeIds = query.nodeIds
      .filter((id) => filteredNodes.has(id) && !hiddenByCluster.has(id))
      .slice(0, this.options.maximumRenderedNodes)
    const renderedNodeIds = new Set(nodeIds)
    const clusterNodeIds = new Set(visibleClusters.flatMap((cluster) => cluster.nodeIds))
    const edgeIds = query.edgeIds
      .filter((id) => {
        if (!filteredEdges.has(id)) return false
        const edge = context.model.edgeById.get(id)
        if (!edge) return false
        if (renderedNodeIds.has(edge.sourceId) && renderedNodeIds.has(edge.targetId)) {
          return true
        }
        return (
          lodLevel >= 0 &&
          clusterNodeIds.has(edge.sourceId) &&
          clusterNodeIds.has(edge.targetId)
        )
      })
      .slice(0, this.options.maximumRenderedEdges)
    const nodes: RenderNode[] = []
    for (const id of nodeIds) {
      const node = context.model.nodeById.get(id)
      const layout = context.layout.nodes.get(id)
      if (!node || !layout) continue
      const selected =
        this.#snapshot.selected?.kind === "node" &&
        this.#snapshot.selected.id === id
      const hovered = this.#snapshot.hoverEntityId === id
      const focused = this.#snapshot.focusEntityId === id
      const selection = this.#snapshot.selected
      const dimmed = Boolean(
        selection &&
          selection.kind === "node" &&
          selection.id !== id &&
          !node.dependencyIds.includes(selection.id) &&
          !context.model.nodeById.get(selection.id)?.dependencyIds.includes(id),
      )
      nodes.push(
        Object.freeze({
          node,
          layout,
          selected,
          hovered,
          focused,
          dimmed,
          layer: context.activeLayer?.data.get(id),
        }),
      )
    }
    const edges: RenderEdge[] = []
    for (const id of edgeIds) {
      const edge = context.model.edgeById.get(id)
      const layout = context.layout.edges.get(id)
      if (!edge || !layout) continue
      const selected =
        this.#snapshot.selected?.kind === "edge" &&
        this.#snapshot.selected.id === id
      const highlighted =
        this.#snapshot.hoverEntityId === id ||
        (this.#snapshot.selected?.kind === "node" &&
          (edge.sourceId === this.#snapshot.selected.id ||
            edge.targetId === this.#snapshot.selected.id))
      const dimmed = Boolean(this.#snapshot.selected && !selected && !highlighted)
      edges.push(
        Object.freeze({
          edge,
          layout,
          selected,
          highlighted,
          dimmed,
          layer:
            context.activeLayer?.data.get(edge.sourceId) ??
            context.activeLayer?.data.get(edge.targetId),
        }),
      )
    }
    const clusters: RenderCluster[] = visibleClusters.map((cluster) =>
      Object.freeze({
        cluster,
        selected:
          this.#snapshot.selected?.kind === "cluster" &&
          this.#snapshot.selected.id === cluster.id,
        hovered: this.#snapshot.hoverEntityId === cluster.id,
        dimmed: Boolean(
          this.#snapshot.selected &&
            this.#snapshot.selected.kind === "node" &&
            !cluster.nodeIds.includes(this.#snapshot.selected.id),
        ),
        layer: aggregateClusterLayer(cluster.nodeIds, context.activeLayer),
      }),
    )
    this.#renderPlan = Object.freeze({
      revision: this.#renderPlan.revision + 1,
      nodes: Object.freeze(nodes),
      edges: Object.freeze(edges),
      clusters: Object.freeze(clusters),
      totalNodeCount: context.filtered.nodeIds.length,
      totalEdgeCount: context.filtered.edgeIds.length,
      virtualizedNodeCount: nodes.length,
      virtualizedEdgeCount: edges.length,
      clipped:
        query.clipped ||
        nodeIds.length >= this.options.maximumRenderedNodes ||
        edgeIds.length >= this.options.maximumRenderedEdges,
      lodLevel,
      query,
    })
    for (const listener of this.#listeners) listener()
  }
}

function aggregateClusterLayer(
  nodeIds: readonly string[],
  layer: LayerSnapshot | undefined,
) {
  if (!layer || nodeIds.length === 0) return undefined
  const values = nodeIds
    .map((id) => layer.data.get(id))
    .filter((value): value is NonNullable<typeof value> => Boolean(value))
  if (values.length === 0) return undefined
  return values.sort(
    (left, right) =>
      severityRank(right.severity) - severityRank(left.severity) ||
      right.value - left.value ||
      left.entityId.localeCompare(right.entityId),
  )[0]
}

function severityRank(value: "none" | "info" | "warning" | "critical"): number {
  if (value === "critical") return 3
  if (value === "warning") return 2
  if (value === "info") return 1
  return 0
}

export interface MinimapProjection {
  bounds: Rect
  viewport: Rect
  nodes: readonly {
    id: string
    center: Point
    state: string
    selected: boolean
    policyViolation: boolean
  }[]
  clusters: readonly {
    id: string
    bounds: Rect
    count: number
    severity: number
  }[]
}

export function buildMinimapProjection(
  model: TopologyGraphModel,
  layout: LayoutSnapshot,
  clusters: ClusterSnapshot,
  viewport: TopologyViewportController,
  minimapBounds: Rect,
): MinimapProjection {
  const graph = layout.bounds
  const mapPoint = (value: Point) =>
    point(
      minimapBounds.x + ((value.x - graph.x) / Math.max(1, graph.width)) * minimapBounds.width,
      minimapBounds.y + ((value.y - graph.y) / Math.max(1, graph.height)) * minimapBounds.height,
    )
  const mapRect = (value: Rect) => {
    const start = mapPoint(point(value.x, value.y))
    const end = mapPoint(point(value.x + value.width, value.y + value.height))
    return rect(start.x, start.y, end.x - start.x, end.y - start.y)
  }
  const topLevel = clusters.levels.at(-1) ?? 0
  return Object.freeze({
    bounds: minimapBounds,
    viewport: viewport.minimapViewport(minimapBounds),
    nodes: Object.freeze(
      model.nodes
        .filter((node) => layout.nodes.has(node.id))
        .map((node) =>
          Object.freeze({
            id: node.id,
            center: mapPoint(layout.nodes.get(node.id)!.center),
            state: node.state,
            selected:
              viewport.getSnapshot().selected?.kind === "node" &&
              viewport.getSnapshot().selected?.id === node.id,
            policyViolation: node.policyViolation,
          }),
        ),
    ),
    clusters: Object.freeze(
      clusters.clusters
        .filter((cluster) => cluster.level === topLevel)
        .map((cluster) =>
          Object.freeze({
            id: cluster.id,
            bounds: mapRect(cluster.bounds),
            count: cluster.nodeIds.length,
            severity:
              cluster.nodeIds.length === 0
                ? 0
                : cluster.policyViolationCount / cluster.nodeIds.length,
          }),
        ),
    ),
  })
}

export function selectionBoxNodeIds(
  layout: LayoutSnapshot,
  selectionBox: Rect,
  visibleIds?: ReadonlySet<string>,
): readonly string[] {
  const result: string[] = []
  for (const node of layout.nodes.values()) {
    if (visibleIds && !visibleIds.has(node.id)) continue
    if (
      containsPoint(selectionBox, node.center) ||
      containsPoint(node.bounds, point(selectionBox.x, selectionBox.y)) ||
      containsPoint(
        node.bounds,
        point(
          selectionBox.x + selectionBox.width,
          selectionBox.y + selectionBox.height,
        ),
      )
    ) {
      result.push(node.id)
    }
  }
  return Object.freeze(result.sort())
}
