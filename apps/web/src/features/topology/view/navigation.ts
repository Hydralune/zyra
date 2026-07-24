import type {
  GraphEntityKind,
  NavigationIntent,
  TopologyEntityDetails,
  TopologyGraphModel,
  TopologySelection,
} from "./contracts.ts"
import { topologyEntityDetails } from "./model.ts"

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function intent(
  model: TopologyGraphModel,
  kind: NavigationIntent["kind"],
  entityId: string,
  input: Partial<NavigationIntent> = {},
): NavigationIntent {
  const evidence =
    model.source.evidenceByEntity[
      `${kind === "select-node" ? "node" : kind === "select-route" ? "route" : kind === "select-placement" ? "placement" : "node"}:${entityId}`
    ] ?? {
      eventIds: freeze([]),
      mutationIds: freeze([]),
      correlationIds: freeze([]),
      causationIds: freeze([]),
      spanIds: freeze([]),
      parentSpanIds: freeze([]),
      checkpointIds: freeze([]),
      controlCommandIds: freeze([]),
      graphIds: freeze([]),
      graphRevisions: freeze([]),
      entityRefs: freeze([]),
    }
  return Object.freeze({
    id: input.id ?? `${kind}:${entityId}:${input.eventId ?? input.artifactId ?? ""}`,
    kind,
    taskId: model.taskId,
    entityId,
    artifactId: input.artifactId,
    eventId: input.eventId,
    causationId: input.causationId,
    correlationId: input.correlationId,
    nodeId: input.nodeId,
    sequence: input.sequence,
    evidence: input.evidence ?? evidence,
  })
}

export function navigationIntentsForSelection(
  model: TopologyGraphModel,
  selection: TopologySelection | undefined,
): readonly NavigationIntent[] {
  if (!selection) return freeze([])
  const details = topologyEntityDetails(model, selection.kind, selection.id)
  if (!details) return freeze([])
  const result: NavigationIntent[] = [...details.actions]
  if (selection.kind === "node") {
    result.unshift(intent(model, "select-node", selection.id, { nodeId: selection.id }))
    const node = model.nodeById.get(selection.id)
    for (const routeId of node?.routeIds ?? []) {
      result.push(intent(model, "select-route", routeId, { nodeId: node?.id }))
    }
    for (const placementId of node?.placementIds ?? []) {
      result.push(intent(model, "select-placement", placementId, { nodeId: node?.id }))
    }
    result.push(intent(model, "focus-subgraph", selection.id, { nodeId: selection.id }))
  } else if (selection.kind === "edge") {
    const edge = model.edgeById.get(selection.id)
    if (edge) {
      result.push(intent(model, "select-node", edge.sourceId, { nodeId: edge.sourceId }))
      result.push(intent(model, "select-node", edge.targetId, { nodeId: edge.targetId }))
      result.push(intent(model, "focus-subgraph", edge.sourceId, { nodeId: edge.sourceId }))
    }
  } else if (selection.kind === "route") {
    const route = model.routeById.get(selection.id)
    if (route?.nodeId) {
      result.unshift(
        intent(model, "select-node", route.nodeId, { nodeId: route.nodeId }),
      )
    }
  } else if (selection.kind === "placement") {
    const placement = model.placementById.get(selection.id)
    if (placement?.nodeId) {
      result.unshift(
        intent(model, "select-node", placement.nodeId, {
          nodeId: placement.nodeId,
        }),
      )
    }
    if (placement?.routeId) {
      result.push(
        intent(model, "select-route", placement.routeId, {
          nodeId: placement.nodeId,
        }),
      )
    }
  } else if (selection.kind === "checkpoint") {
    const checkpoint = model.checkpointById.get(selection.id)
    if (checkpoint) {
      for (const eventId of checkpoint.evidence.eventIds) {
        result.push(
          intent(model, "open-timeline", selection.id, {
            eventId,
            sequence: checkpoint.sequence,
            evidence: checkpoint.evidence,
          }),
        )
      }
    }
  } else if (selection.kind === "branch") {
    const branch = model.branchById.get(selection.id)
    if (branch?.checkpointId) {
      result.unshift(
        intent(model, "select-checkpoint", branch.checkpointId, {
          evidence: branch.evidence,
        }),
      )
    }
  } else if (selection.kind === "change") {
    const change = model.changes.find((candidate) => candidate.id === selection.id)
    if (change) {
      for (const nodeId of change.affectedNodeIds) {
        result.push(intent(model, "select-node", nodeId, { nodeId }))
      }
      for (const routeId of change.routeIds) {
        result.push(intent(model, "select-route", routeId, { nodeId: change.replanNodeId }))
      }
      if (change.replanNodeId) {
        result.unshift(
          intent(model, "focus-subgraph", change.replanNodeId, {
            nodeId: change.replanNodeId,
          }),
        )
      }
    }
  }
  return deduplicateIntents(result)
}

function deduplicateIntents(values: readonly NavigationIntent[]): readonly NavigationIntent[] {
  const result = new Map<string, NavigationIntent>()
  for (const value of values) {
    const key = [
      value.kind,
      value.entityId,
      value.artifactId,
      value.eventId,
      value.causationId,
      value.nodeId,
    ].join("\0")
    if (!result.has(key)) result.set(key, value)
  }
  return freeze(result.values())
}

export function selectionFromNavigation(
  navigation: NavigationIntent,
  source: TopologySelection["source"] = "programmatic",
  now = Date.now(),
): TopologySelection | undefined {
  let kind: GraphEntityKind | undefined
  let id = navigation.entityId
  if (navigation.kind === "select-node" || navigation.kind === "focus-subgraph") {
    kind = "node"
    id = navigation.nodeId ?? navigation.entityId
  } else if (navigation.kind === "select-route") {
    kind = "route"
  } else if (navigation.kind === "select-placement") {
    kind = "placement"
  } else if (navigation.kind === "select-checkpoint") {
    kind = "checkpoint"
  }
  return kind
    ? Object.freeze({ kind, id, source, selectedAt: now })
    : undefined
}

export interface NavigationTarget {
  panel: "topology" | "artifacts" | "timeline" | "causation"
  taskId: string
  entityId?: string
  artifactId?: string
  eventId?: string
  causationId?: string
  correlationId?: string
  sequence?: number
  query: Readonly<Record<string, string>>
}

export function resolveNavigationTarget(intent: NavigationIntent): NavigationTarget {
  if (intent.kind === "open-artifact") {
    return Object.freeze({
      panel: "artifacts",
      taskId: intent.taskId,
      entityId: intent.entityId,
      artifactId: intent.artifactId,
      query: Object.freeze({
        artifact: intent.artifactId ?? "",
        source: "topology",
      }),
    })
  }
  if (intent.kind === "open-timeline") {
    return Object.freeze({
      panel: "timeline",
      taskId: intent.taskId,
      entityId: intent.entityId,
      eventId: intent.eventId,
      sequence: intent.sequence,
      query: Object.freeze({
        event: intent.eventId ?? "",
        sequence: intent.sequence === undefined ? "" : String(intent.sequence),
        source: "topology",
      }),
    })
  }
  if (intent.kind === "open-causation") {
    return Object.freeze({
      panel: "causation",
      taskId: intent.taskId,
      entityId: intent.entityId,
      eventId: intent.eventId,
      causationId: intent.causationId,
      correlationId: intent.correlationId,
      query: Object.freeze({
        event: intent.eventId ?? "",
        cause: intent.causationId ?? "",
        correlation: intent.correlationId ?? "",
        source: "topology",
      }),
    })
  }
  return Object.freeze({
    panel: "topology",
    taskId: intent.taskId,
    entityId: intent.nodeId ?? intent.entityId,
    query: Object.freeze({
      entity: intent.nodeId ?? intent.entityId,
      source: "topology",
    }),
  })
}

export function causalNavigationIntents(
  model: TopologyGraphModel,
  details: TopologyEntityDetails,
  maximum = 50,
): readonly NavigationIntent[] {
  const result: NavigationIntent[] = []
  const eventIds = [...details.eventIds].slice(-maximum)
  for (const eventId of eventIds) {
    result.push(
      intent(model, "open-timeline", details.id, {
        eventId,
        evidence: model.source.evidenceByEntity[`${details.kind}:${details.id}`],
      }),
    )
  }
  const evidence = model.source.evidenceByEntity[`${details.kind}:${details.id}`]
  for (const causationId of evidence?.causationIds.slice(-maximum) ?? []) {
    result.push(
      intent(model, "open-causation", details.id, {
        causationId,
        evidence,
      }),
    )
  }
  for (const correlationId of evidence?.correlationIds.slice(-maximum) ?? []) {
    result.push(
      intent(model, "open-causation", details.id, {
        correlationId,
        evidence,
      }),
    )
  }
  return deduplicateIntents(result).slice(0, maximum)
}

export function relatedSelectionOrder(
  model: TopologyGraphModel,
  selection: TopologySelection,
): readonly TopologySelection[] {
  const ids: readonly (readonly [GraphEntityKind, string])[] =
    selection.kind === "node"
      ? [
          ...(model.incomingByNode.get(selection.id) ?? []).map(
            (id) => ["edge", id] as const,
          ),
          ...(model.outgoingByNode.get(selection.id) ?? []).map(
            (id) => ["edge", id] as const,
          ),
          ...(model.routesByNode.get(selection.id) ?? []).map(
            (id) => ["route", id] as const,
          ),
          ...(model.placementsByNode.get(selection.id) ?? []).map(
            (id) => ["placement", id] as const,
          ),
          ...(model.changesByNode.get(selection.id) ?? []).map(
            (id) => ["change", id] as const,
          ),
        ]
      : selection.kind === "edge"
        ? (() => {
            const edge = model.edgeById.get(selection.id)
            return edge
              ? ([
                  ["node", edge.sourceId],
                  ["node", edge.targetId],
                ] as const)
              : []
          })()
        : []
  return freeze(
    ids.map(([kind, id], index) =>
      Object.freeze({
        kind,
        id,
        source: "causation" as const,
        selectedAt: selection.selectedAt + index + 1,
      }),
    ),
  )
}
