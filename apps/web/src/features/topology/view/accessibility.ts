import type {
  GraphNodeRecord,
  LayoutSnapshot,
  TopologyControllerSnapshot,
  TopologyGraphModel,
  TopologySelection,
} from "./contracts.ts"

export interface AccessibleGraphRow {
  id: string
  level: number
  position: number
  setSize: number
  label: string
  description: string
  state: string
  selected: boolean
  focused: boolean
  expanded?: boolean
  controls?: string
}
export interface AccessibleGraphSnapshot {
  label: string
  description: string
  rows: readonly AccessibleGraphRow[]
  focusedId?: string
  selectedId?: string
  liveMessage: string
  instructions: readonly string[]
}

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function nodeStateDescription(node: GraphNodeRecord): string {
  const facts = [
    `state ${node.state}`,
    `role ${node.role}`,
    `namespace ${node.namespace}`,
    `placed ${node.location}`,
    `${node.dependencyCount} dependencies`,
    `${node.workerCount} workers`,
    `${node.routeCount} routes`,
  ]
  if (node.providerId) facts.push(`provider ${node.providerId}`)
  if (node.modelId) facts.push(`model ${node.modelId}`)
  if (node.policyViolation) facts.push("policy violation")
  if (node.openWorldCreated) facts.push("created during this run")
  if (node.openWorldRemoved) facts.push("removed during this run")
  if (node.openWorldReplaced) facts.push("replaced during this run")
  if (node.pending) facts.push("pending commit")
  if (node.terminal) facts.push("terminal")
  return facts.join(", ")
}

function orderNodes(
  model: TopologyGraphModel,
  layout: LayoutSnapshot,
  visibleIds?: ReadonlySet<string>,
): GraphNodeRecord[] {
  return model.nodes
    .filter(
      (node) =>
        (!visibleIds || visibleIds.has(node.id)) &&
        !node.removed &&
        layout.nodes.has(node.id),
    )
    .sort((left, right) => {
      const a = layout.nodes.get(left.id)!
      const b = layout.nodes.get(right.id)!
      return (
        a.rank - b.rank ||
        a.order - b.order ||
        left.sequence - right.sequence ||
        left.id.localeCompare(right.id)
      )
    })
}

export function buildAccessibleGraph(
  model: TopologyGraphModel,
  layout: LayoutSnapshot,
  input: {
    visibleIds?: ReadonlySet<string>
    focusedId?: string
    selection?: TopologySelection
    expandedClusterIds?: ReadonlySet<string>
    liveMessage?: string
  } = {},
): AccessibleGraphSnapshot {
  const nodes = orderNodes(model, layout, input.visibleIds)
  const countByRank = new Map<number, number>()
  for (const node of nodes) {
    const rank = layout.nodes.get(node.id)?.rank ?? node.depth
    countByRank.set(rank, (countByRank.get(rank) ?? 0) + 1)
  }
  const positionByRank = new Map<number, number>()
  const rows = nodes.map((node) => {
    const layoutNode = layout.nodes.get(node.id)!
    const rank = layoutNode.rank
    const position = (positionByRank.get(rank) ?? 0) + 1
    positionByRank.set(rank, position)
    return Object.freeze({
      id: node.id,
      level: rank + 1,
      position,
      setSize: countByRank.get(rank) ?? 1,
      label: node.title,
      description: nodeStateDescription(node),
      state: node.state,
      selected:
        input.selection?.kind === "node" && input.selection.id === node.id,
      focused: input.focusedId === node.id,
      controls: `topology-inspector-${safeDomId(node.id)}`,
    })
  })
  const warnings: string[] = []
  if (!model.diagnostics.snapshotComplete) warnings.push("snapshot is still loading")
  if (!model.diagnostics.connected) warnings.push("event stream disconnected")
  if (model.diagnostics.lag > 0) warnings.push(`${model.diagnostics.lag} events behind`)
  if (model.diagnostics.errors.length > 0) {
    warnings.push(`${model.diagnostics.errors.length} projection errors`)
  }
  return Object.freeze({
    label: `Task topology for ${model.taskId}`,
    description: [
      `${nodes.length} visible of ${model.nodes.length} nodes`,
      `${model.edges.length} edges`,
      `graph revision ${model.graphRevision}`,
      `commit revision ${model.commitRevision}`,
      ...warnings,
    ].join(". "),
    rows: freeze(rows),
    focusedId: input.focusedId,
    selectedId: input.selection?.id,
    liveMessage: input.liveMessage ?? "",
    instructions: freeze([
      "Use arrow keys to move between topology nodes.",
      "Press Enter or Space to select the focused node.",
      "Press Home or End to move to the first or last visible node.",
      "Press Page Up or Page Down to move between graph ranks.",
      "Press plus or minus to zoom and zero to fit the graph.",
      "Press Escape to clear selection and hover.",
    ]),
  })
}

export function safeDomId(value: string): string {
  const normalized = String(value)
    .normalize("NFKD")
    .replace(/[^\w-]+/g, "-")
    .replace(/^-+|-+$/g, "")
  return normalized || "entity"
}

export function announceSelection(
  model: TopologyGraphModel,
  selection: TopologySelection | undefined,
): string {
  if (!selection) return "Topology selection cleared."
  if (selection.kind === "node") {
    const node = model.nodeById.get(selection.id)
    return node
      ? `Selected ${node.title}. ${nodeStateDescription(node)}.`
      : `Selected node ${selection.id}, which is no longer in the current projection.`
  }
  if (selection.kind === "edge") {
    const edge = model.edgeById.get(selection.id)
    return edge
      ? `Selected ${edge.kind} edge from ${edge.sourceId} to ${edge.targetId}.`
      : `Selected edge ${selection.id}, which is no longer in the current projection.`
  }
  if (selection.kind === "route") {
    const route = model.routeById.get(selection.id)
    return route
      ? `Selected route ${route.id}. ${route.location}, ${route.candidateCount} candidates, ${route.accepted ? "accepted" : "rejected"}.`
      : `Selected route ${selection.id}, which is no longer in the current projection.`
  }
  if (selection.kind === "placement") {
    const placement = model.placementById.get(selection.id)
    return placement
      ? `Selected placement ${placement.id}. ${placement.location}, privacy ${placement.privacyClass}, SLA ${placement.slaSatisfied ? "satisfied" : "violated"}.`
      : `Selected placement ${selection.id}, which is no longer in the current projection.`
  }
  if (selection.kind === "checkpoint") {
    const checkpoint = model.checkpointById.get(selection.id)
    return checkpoint
      ? `Selected checkpoint ${checkpoint.id}. ${checkpoint.pendingWrites} pending and ${checkpoint.committedWrites} committed writes.`
      : `Selected checkpoint ${selection.id}, which is no longer in the current projection.`
  }
  if (selection.kind === "branch") {
    const branch = model.branchById.get(selection.id)
    return branch
      ? `Selected branch ${branch.id}. Outcome ${branch.outcome}, ${branch.conflictCount} conflicts.`
      : `Selected branch ${selection.id}, which is no longer in the current projection.`
  }
  if (selection.kind === "change") {
    const change = model.changes.find((candidate) => candidate.id === selection.id)
    return change
      ? `Selected requirement change ${change.id}. ${change.affectedNodeIds.length} affected nodes.`
      : `Selected change ${selection.id}, which is no longer in the current projection.`
  }
  return `Selected ${selection.kind} ${selection.id}.`
}

export function announceProjectionChange(
  previous: TopologyGraphModel | undefined,
  current: TopologyGraphModel,
): string {
  if (!previous) {
    return `Topology loaded with ${current.nodes.length} nodes and ${current.edges.length} edges.`
  }
  const previousNodes = new Set(previous.nodes.map((node) => node.id))
  const currentNodes = new Set(current.nodes.map((node) => node.id))
  const added = [...currentNodes].filter((id) => !previousNodes.has(id))
  const removed = [...previousNodes].filter((id) => !currentNodes.has(id))
  const changed = current.nodes.filter((node) => {
    const before = previous.nodeById.get(node.id)
    return Boolean(
      before &&
        (before.revision !== node.revision ||
          before.state !== node.state ||
          before.selectedRouteId !== node.selectedRouteId ||
          before.selectedPlacementId !== node.selectedPlacementId),
    )
  })
  const messages: string[] = []
  if (added.length > 0) messages.push(`${added.length} nodes added`)
  if (removed.length > 0) messages.push(`${removed.length} nodes removed`)
  if (changed.length > 0) messages.push(`${changed.length} nodes changed`)
  if (current.graphRevision !== previous.graphRevision) {
    messages.push(`graph revision ${current.graphRevision}`)
  }
  if (current.commitRevision !== previous.commitRevision) {
    messages.push(`commit revision ${current.commitRevision}`)
  }
  return messages.length > 0 ? `Topology updated: ${messages.join(", ")}.` : ""
}

export function announceViewport(snapshot: TopologyControllerSnapshot): string {
  const plan = snapshot.renderPlan
  const clusterPart =
    plan.lodLevel >= 0
      ? `${plan.clusters.length} level ${plan.lodLevel} clusters`
      : `${plan.nodes.length} nodes`
  return [
    `Showing ${clusterPart}`,
    `${plan.edges.length} edges`,
    `${snapshot.filtered.hiddenNodeCount} filtered nodes`,
    plan.clipped ? "render limit reached" : "",
    `zoom ${Math.round(snapshot.viewport.transform.scale * 100)} percent`,
  ]
    .filter(Boolean)
    .join(", ")
}

export function nextAccessibleNode(
  rows: readonly AccessibleGraphRow[],
  currentId: string | undefined,
  key: string,
): string | undefined {
  if (rows.length === 0) return undefined
  const currentIndex = Math.max(0, rows.findIndex((row) => row.id === currentId))
  if (key === "Home") return rows[0]!.id
  if (key === "End") return rows.at(-1)!.id
  if (key === "ArrowDown" || key === "ArrowRight") {
    return rows[Math.min(rows.length - 1, currentIndex + 1)]!.id
  }
  if (key === "ArrowUp" || key === "ArrowLeft") {
    return rows[Math.max(0, currentIndex - 1)]!.id
  }
  if (key === "PageDown") {
    const current = rows[currentIndex]!
    return rows.find((row) => row.level > current.level)?.id ?? rows.at(-1)!.id
  }
  if (key === "PageUp") {
    const current = rows[currentIndex]!
    return (
      [...rows].reverse().find((row) => row.level < current.level)?.id ??
      rows[0]!.id
    )
  }
  return currentId ?? rows[0]!.id
}

export function focusRepair(
  rows: readonly AccessibleGraphRow[],
  previousId: string | undefined,
): string | undefined {
  if (rows.length === 0) return undefined
  if (previousId && rows.some((row) => row.id === previousId)) return previousId
  return rows[0]!.id
}
