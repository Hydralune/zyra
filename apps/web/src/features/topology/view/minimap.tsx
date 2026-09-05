import { useMemo, useRef, type PointerEvent } from "react"
import type { TopologyControllerSnapshot } from "./contracts.ts"
import type { TopologyWorkbenchController } from "./controller.ts"
import { buildMinimapProjection } from "./interaction.ts"
import { point, rect } from "./geometry.ts"

export function TopologyMinimap({
  controller,
  snapshot,
}: {
  controller: TopologyWorkbenchController
  snapshot: TopologyControllerSnapshot
}) {
  const svg = useRef<SVGSVGElement>(null)
  const bounds = rect(0, 0, 220, 132)
  const projection = useMemo(
    () =>
      buildMinimapProjection(
        snapshot.model,
        snapshot.layout,
        snapshot.clusters,
        controller.viewport,
        bounds,
      ),
    [
      snapshot.model,
      snapshot.layout,
      snapshot.clusters,
      snapshot.viewport,
      controller,
    ],
  )
  const navigate = (event: PointerEvent<SVGSVGElement>) => {
    const client = svg.current?.getBoundingClientRect()
    const scaleX = bounds.width / Math.max(1, client?.width ?? bounds.width)
    const scaleY = bounds.height / Math.max(1, client?.height ?? bounds.height)
    controller.viewport.minimapNavigate(
      point(
        (event.clientX - (client?.left ?? 0)) * scaleX,
        (event.clientY - (client?.top ?? 0)) * scaleY,
      ),
      bounds,
    )
  }
  return (
    <aside className="topology-minimap" aria-label="Topology minimap">
      <svg
        ref={svg}
        viewBox={`0 0 ${bounds.width} ${bounds.height}`}
        role="img"
        aria-label={`Minimap of ${snapshot.model.nodes.length} nodes`}
        onPointerDown={navigate}
        onPointerMove={(event) => {
          if (event.buttons === 1) navigate(event)
        }}
      >
        <rect className="topology-minimap-background" width={bounds.width} height={bounds.height} rx={8} />
        {projection.clusters.map((cluster) => (
          <rect
            key={cluster.id}
            className={cluster.severity > 0.25 ? "topology-minimap-cluster has-risk" : "topology-minimap-cluster"}
            x={cluster.bounds.x}
            y={cluster.bounds.y}
            width={Math.max(2, cluster.bounds.width)}
            height={Math.max(2, cluster.bounds.height)}
            rx={2}
          />
        ))}
        {projection.nodes.length <= 2_500
          ? projection.nodes.map((node) => (
              <circle
                key={node.id}
                className={[
                  "topology-minimap-node",
                  node.selected ? "is-selected" : "",
                  node.policyViolation ? "has-risk" : "",
                ].filter(Boolean).join(" ")}
                cx={node.center.x}
                cy={node.center.y}
                r={node.selected ? 2.2 : 1.15}
              />
            ))
          : null}
        <rect
          className="topology-minimap-viewport"
          x={projection.viewport.x}
          y={projection.viewport.y}
          width={Math.max(3, projection.viewport.width)}
          height={Math.max(3, projection.viewport.height)}
          rx={2}
        />
      </svg>
      <span>{snapshot.model.nodes.length.toLocaleString()} 个节点</span>
    </aside>
  )
}
