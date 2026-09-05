import {
  useEffect,
  useMemo,
  useRef,
  type KeyboardEvent,
  type PointerEvent,
  type WheelEvent,
} from "react"
import type {
  RenderCluster,
  RenderEdge,
  RenderNode,
  TopologyControllerSnapshot,
} from "./contracts.ts"
import type { TopologyWorkbenchController } from "./controller.ts"
import { buildAccessibleGraph, safeDomId } from "./accessibility.ts"
import { point, rect } from "./geometry.ts"
import { topologyLabel } from "./copy.ts"

function pathData(points: readonly { x: number; y: number }[]): string {
  if (points.length === 0) return ""
  return points
    .map((value, index) => `${index === 0 ? "M" : "L"}${value.x},${value.y}`)
    .join(" ")
}
function stateClass(value: string): string {
  return String(value || "unknown")
    .toLocaleLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
}

function layerClass(value: RenderNode["layer"]): string {
  return value ? `layer-${value.colorToken} severity-${value.severity}` : "layer-none"
}

function GraphEdge({
  value,
  controller,
}: {
  value: RenderEdge
  controller: TopologyWorkbenchController
}) {
  return (
    <g
      className={[
        "topology-edge",
        `topology-edge-${stateClass(value.edge.kind)}`,
        value.selected ? "is-selected" : "",
        value.highlighted ? "is-highlighted" : "",
        value.dimmed ? "is-dimmed" : "",
        value.edge.runtimeMutation ? "is-runtime-mutation" : "",
        value.edge.missing ? "is-missing" : "",
      ].filter(Boolean).join(" ")}
      data-topology-edge={value.edge.id}
      onPointerEnter={() => controller.viewport.select}
      onClick={(event) => {
        event.stopPropagation()
        controller.select("edge", value.edge.id, "pointer")
      }}
    >
      <path
        className="topology-edge-hit"
        d={pathData(value.layout.points)}
        fill="none"
        stroke="transparent"
        strokeWidth={14}
      />
      <path
        className="topology-edge-line"
        d={pathData(value.layout.points)}
        fill="none"
        markerEnd="url(#topology-arrow)"
      />
      {value.edge.runtimeMutation ? (
        <circle
          className="topology-edge-mutation"
          cx={value.layout.points[Math.floor(value.layout.points.length / 2)]?.x}
          cy={value.layout.points[Math.floor(value.layout.points.length / 2)]?.y}
          r={4}
        />
      ) : null}
    </g>
  )
}

function GraphNode({
  value,
  controller,
  onPointerDown,
}: {
  value: RenderNode
  controller: TopologyWorkbenchController
  onPointerDown: (event: PointerEvent<SVGGElement>, nodeId: string) => void
}) {
  const node = value.node
  const layout = value.layout
  const title =
    topologyLabel(node.title).length > 32 ? `${topologyLabel(node.title).slice(0, 31)}…` : topologyLabel(node.title)
  const subtitle = `${topologyLabel(node.role)} · ${topologyLabel(node.location)}`
  return (
    <g
      id={`topology-node-${safeDomId(node.id)}`}
      className={[
        "topology-node",
        `status-${stateClass(node.state)}`,
        value.selected ? "is-selected" : "",
        value.hovered ? "is-hovered" : "",
        value.focused ? "is-focused" : "",
        value.dimmed ? "is-dimmed" : "",
        node.policyViolation ? "has-policy-violation" : "",
        node.openWorldCreated ? "is-open-world-created" : "",
        node.openWorldReplaced ? "is-open-world-replaced" : "",
        layout.pinned ? "is-pinned" : "",
        layerClass(value.layer),
      ].filter(Boolean).join(" ")}
      data-topology-node={node.id}
      transform={`translate(${layout.bounds.x} ${layout.bounds.y})`}
      role="treeitem"
      aria-level={layout.rank + 1}
      aria-selected={value.selected}
      aria-label={`${node.title}, ${node.state}, role ${node.role}, placement ${node.location}`}
      tabIndex={value.focused ? 0 : -1}
      onFocus={() => controller.viewport.setFocus(node.id, "keyboard")}
      onPointerDown={(event) => onPointerDown(event, node.id)}
      onPointerEnter={() => controller.viewport.setFocus(node.id, "pointer")}
      onClick={(event) => {
        event.stopPropagation()
        controller.select("node", node.id, "pointer")
      }}
      onDoubleClick={(event) => {
        event.stopPropagation()
        controller.viewport.focusNode(node.id, 1.15)
      }}
    >
      <rect
        className="topology-node-shell"
        width={layout.size.width}
        height={layout.size.height}
        rx={10}
      />
      <rect
        className="topology-node-state"
        width={5}
        height={layout.size.height}
        rx={2.5}
      />
      <text className="topology-node-title" x={17} y={26}>
        {title}
      </text>
      <text className="topology-node-subtitle" x={17} y={47}>
        {subtitle.length > 34 ? `${subtitle.slice(0, 33)}…` : subtitle}
      </text>
      <text
        className="topology-node-metrics"
        x={17}
        y={layout.size.height - 12}
      >
        {node.dependencyCount} 依赖 · {node.workerCount} 执行者 · {node.routeCount} 路由
      </text>
      {node.policyViolation ? (
        <g className="topology-node-alert" transform={`translate(${layout.size.width - 23} 16)`}>
          <circle r={9} />
          <text textAnchor="middle" y={4}>!</text>
        </g>
      ) : null}
      {node.openWorldCreated || node.openWorldReplaced ? (
        <text
          className="topology-node-open-world"
          x={layout.size.width - 11}
          y={layout.size.height - 11}
          textAnchor="end"
        >
          {node.openWorldCreated ? "NEW" : "REPLACED"}
        </text>
      ) : null}
      {layout.pinned ? (
        <circle className="topology-node-pin" cx={layout.size.width - 12} cy={layout.size.height - 12} r={3} />
      ) : null}
    </g>
  )
}

function GraphCluster({
  value,
  controller,
}: {
  value: RenderCluster
  controller: TopologyWorkbenchController
}) {
  const cluster = value.cluster
  return (
    <g
      className={[
        "topology-cluster",
        value.selected ? "is-selected" : "",
        value.hovered ? "is-hovered" : "",
        value.dimmed ? "is-dimmed" : "",
        value.layer ? `severity-${value.layer.severity}` : "",
      ].filter(Boolean).join(" ")}
      transform={`translate(${cluster.bounds.x} ${cluster.bounds.y})`}
      data-topology-cluster={cluster.id}
      role="treeitem"
      aria-label={`Cluster of ${cluster.nodeIds.length} nodes, ${cluster.policyViolationCount} policy violations`}
      tabIndex={-1}
      onClick={(event) => {
        event.stopPropagation()
        controller.select("cluster", cluster.id, "pointer")
      }}
      onDoubleClick={(event) => {
        event.stopPropagation()
        controller.viewport.toggleCluster(cluster.id)
      }}
    >
      <rect
        className="topology-cluster-shell"
        width={cluster.bounds.width}
        height={cluster.bounds.height}
        rx={18}
      />
      <circle className="topology-cluster-count" cx={28} cy={28} r={18} />
      <text className="topology-cluster-count-text" x={28} y={33} textAnchor="middle">
        {cluster.nodeIds.length > 999 ? "999+" : cluster.nodeIds.length}
      </text>
      <text className="topology-cluster-title" x={56} y={25}>
        Level {cluster.level} cluster
      </text>
      <text className="topology-cluster-subtitle" x={56} y={44}>
        {cluster.routeCount} routes · {cluster.changedCount} changed · {cluster.policyViolationCount} risk
      </text>
    </g>
  )
}

export function TopologyGraphCanvas({
  controller,
  snapshot,
}: {
  controller: TopologyWorkbenchController
  snapshot: TopologyControllerSnapshot
}) {
  const container = useRef<HTMLDivElement>(null)
  const svg = useRef<SVGSVGElement>(null)
  const pointerActive = useRef<number | undefined>(undefined)
  const pointerOrigin = useRef<{ kind: "node" | "edge" | "cluster"; id: string } | undefined>(undefined)
  const accessible = useMemo(
    () =>
      buildAccessibleGraph(snapshot.model, snapshot.layout, {
        visibleIds: new Set(snapshot.filtered.nodeIds),
        focusedId: snapshot.viewport.focusEntityId,
        selection: snapshot.viewport.selected,
        expandedClusterIds: new Set(snapshot.viewport.expandedClusterIds),
      }),
    [
      snapshot.model,
      snapshot.layout,
      snapshot.filtered.nodeIds,
      snapshot.viewport.focusEntityId,
      snapshot.viewport.selected,
      snapshot.viewport.expandedClusterIds,
    ],
  )
  useEffect(() => {
    const element = container.current
    if (!element) return
    const update = () => {
      const bounds = element.getBoundingClientRect()
      if (bounds.width > 0 && bounds.height > 0) {
        controller.resize(rect(0, 0, bounds.width, bounds.height))
      }
    }
    update()
    const observer = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(update)
    observer?.observe(element)
    return () => observer?.disconnect()
  }, [controller])
  const screenPoint = (clientX: number, clientY: number) => {
    const bounds = svg.current?.getBoundingClientRect()
    return point(clientX - (bounds?.left ?? 0), clientY - (bounds?.top ?? 0))
  }
  const pointerDown = (
    event: PointerEvent<SVGSVGElement | SVGGElement>,
    nodeId?: string,
  ) => {
    if (
      controller.viewport.pointerDown({
        pointerId: event.pointerId,
        screen: screenPoint(event.clientX, event.clientY),
        targetNodeId: nodeId,
        shiftKey: event.shiftKey,
        button: event.button,
      })
    ) {
      pointerActive.current = event.pointerId
      const target = event.target instanceof Element ? event.target.closest("[data-topology-node], [data-topology-edge], [data-topology-cluster]") : null
      const edgeId = target?.getAttribute("data-topology-edge")
      const clusterId = target?.getAttribute("data-topology-cluster")
      pointerOrigin.current = nodeId ? { kind: "node", id: nodeId }
        : edgeId ? { kind: "edge", id: edgeId } : clusterId ? { kind: "cluster", id: clusterId } : undefined
      svg.current?.setPointerCapture(event.pointerId)
      event.preventDefault()
    }
  }
  const pointerMove = (event: PointerEvent<SVGSVGElement>) => {
    const screen = screenPoint(event.clientX, event.clientY)
    const moved = controller.viewport.pointerMove({
      pointerId: event.pointerId,
      screen,
      onNodeDrag: (nodeId, graphPoint) => controller.movePinnedNode(nodeId, graphPoint),
    })
    if (!moved) controller.viewport.hoverAt(screen)
  }
  const pointerUp = (event: PointerEvent<SVGSVGElement>) => {
    const result = controller.viewport.pointerUp({
      pointerId: event.pointerId,
      screen: screenPoint(event.clientX, event.clientY),
    })
    pointerActive.current = undefined
    if (svg.current?.hasPointerCapture(event.pointerId)) {
      svg.current.releasePointerCapture(event.pointerId)
    }
    // Pointer capture retargets pointerup/click to the SVG. Use the original
    // entity so a real mouse click does not immediately clear its selection.
    if (result.click) {
      if (pointerOrigin.current) controller.select(pointerOrigin.current.kind, pointerOrigin.current.id, "pointer")
      else controller.clearSelection()
    }
  }
  const keyDown = (event: KeyboardEvent<SVGSVGElement>) => {
    if (
      controller.viewport.keyboard({
        key: event.key,
        shiftKey: event.shiftKey,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
      })
    ) {
      event.preventDefault()
    }
  }
  const wheel = (event: WheelEvent<SVGSVGElement>) => {
    controller.viewport.wheel(
      screenPoint(event.clientX, event.clientY),
      event.deltaY,
    )
    event.preventDefault()
  }
  const transform = snapshot.viewport.transform
  return (
    <div
      className="topology-canvas"
      ref={container}
      data-large-graph={snapshot.model.nodes.length >= 1_000 ? "true" : "false"}
    >
      <svg
        ref={svg}
        className="topology-svg"
        width="100%"
        height="100%"
        role="tree"
        aria-label={accessible.label}
        aria-describedby="topology-canvas-description topology-keyboard-instructions"
        tabIndex={0}
        onPointerDown={(event) => pointerDown(event)}
        onPointerMove={pointerMove}
        onPointerUp={pointerUp}
        onPointerCancel={(event) => { controller.viewport.pointerCancel(event.pointerId); pointerActive.current = undefined; pointerOrigin.current = undefined }}
        onDoubleClick={() => {
          if (pointerOrigin.current?.kind === "node") controller.viewport.focusNode(pointerOrigin.current.id, 1.15)
          if (pointerOrigin.current?.kind === "cluster") controller.viewport.toggleCluster(pointerOrigin.current.id)
        }}
        onPointerLeave={() => {
          if (pointerActive.current === undefined) controller.viewport.clearHover()
        }}
        onWheel={wheel}
        onKeyDown={keyDown}
      >
        <defs>
          <marker
            id="topology-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" />
          </marker>
          <pattern id="topology-grid" width="24" height="24" patternUnits="userSpaceOnUse">
            <path d="M 24 0 L 0 0 0 24" fill="none" />
          </pattern>
        </defs>
        <rect
          className="topology-grid"
          x={0}
          y={0}
          width="100%"
          height="100%"
          fill="url(#topology-grid)"
        />
        <g transform={`translate(${transform.x} ${transform.y}) scale(${transform.scale})`}>
          <g className="topology-edge-layer">
            {snapshot.renderPlan.edges.map((value) => (
              <GraphEdge key={value.edge.id} value={value} controller={controller} />
            ))}
          </g>
          <g className="topology-cluster-layer">
            {snapshot.renderPlan.clusters.map((value) => (
              <GraphCluster key={value.cluster.id} value={value} controller={controller} />
            ))}
          </g>
          <g className="topology-node-layer">
            {snapshot.renderPlan.nodes.map((value) => (
              <GraphNode
                key={value.node.id}
                value={value}
                controller={controller}
                onPointerDown={(event, nodeId) => pointerDown(event, nodeId)}
              />
            ))}
          </g>
        </g>
      </svg>
      <p id="topology-canvas-description" className="visually-hidden">
        {accessible.description}
      </p>
      <ul id="topology-keyboard-instructions" className="visually-hidden">
        {accessible.instructions.map((instruction) => (
          <li key={instruction}>{instruction}</li>
        ))}
      </ul>
      <div className="topology-canvas-stats" aria-hidden="true">
        <span>{snapshot.renderPlan.virtualizedNodeCount}/{snapshot.renderPlan.totalNodeCount} 节点</span>
        <span>{snapshot.renderPlan.virtualizedEdgeCount}/{snapshot.renderPlan.totalEdgeCount} 关系</span>
        <span>{Math.round(transform.scale * 100)}%</span>
        {snapshot.renderPlan.lodLevel >= 0 ? <span>LOD {snapshot.renderPlan.lodLevel}</span> : null}
        {snapshot.renderPlan.clipped ? <span className="warning">render cap</span> : null}
      </div>
    </div>
  )
}
