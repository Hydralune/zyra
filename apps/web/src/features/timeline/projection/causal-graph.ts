import type { CanonicalProjectionState } from "../../../state/contracts.ts"
import type {
  TimelineCausalGraph,
  TimelineEdge,
  TimelineEdgeKindValue,
  TimelineEventFacts,
} from "./contracts.ts"
import { TimelineEdgeKind } from "./contracts.ts"
import { compareTimelineOrder } from "./event-reader.ts"

interface MutableGraph {
  eventIds: string[]
  eventSet: Set<string>
  edges: TimelineEdge[]
  edgeIds: Set<string>
  incomingByEvent: Record<string, string[]>
  outgoingByEvent: Record<string, string[]>
  missingEventIds: Set<string>
}

interface EdgeInput {
  kind: TimelineEdgeKindValue
  sourceEventId: string
  targetEventId: string
  explicit: boolean
  weight: number
  label: string
  metadata?: Record<string, string | number | boolean>
}

function edgeId(input: EdgeInput): string {
  return [
    input.kind,
    input.sourceEventId,
    input.targetEventId,
    input.label,
  ].join(":")
}

function addEdge(graph: MutableGraph, input: EdgeInput): void {
  if (!input.sourceEventId || !input.targetEventId) return
  if (input.sourceEventId === input.targetEventId) return
  const id = edgeId(input)
  if (graph.edgeIds.has(id)) return
  graph.edgeIds.add(id)
  const missingSource = !graph.eventSet.has(input.sourceEventId)
  const missingTarget = !graph.eventSet.has(input.targetEventId)
  if (missingSource) graph.missingEventIds.add(input.sourceEventId)
  if (missingTarget) graph.missingEventIds.add(input.targetEventId)
  const edge: TimelineEdge = Object.freeze({
    id,
    kind: input.kind,
    sourceEventId: input.sourceEventId,
    targetEventId: input.targetEventId,
    explicit: input.explicit,
    missingSource,
    missingTarget,
    weight: Math.max(0, input.weight),
    label: input.label,
    metadata: Object.freeze({ ...(input.metadata ?? {}) }),
  })
  graph.edges.push(edge)
  if (!missingTarget) {
    const incoming = graph.incomingByEvent[input.targetEventId] ?? []
    incoming.push(id)
    graph.incomingByEvent[input.targetEventId] = incoming
  }
  if (!missingSource) {
    const outgoing = graph.outgoingByEvent[input.sourceEventId] ?? []
    outgoing.push(id)
    graph.outgoingByEvent[input.sourceEventId] = outgoing
  }
}

function groupBy<T>(
  values: readonly T[],
  key: (value: T) => string | undefined,
): Map<string, T[]> {
  const groups = new Map<string, T[]>()
  for (const value of values) {
    const id = key(value)
    if (!id) continue
    const group = groups.get(id) ?? []
    group.push(value)
    groups.set(id, group)
  }
  return groups
}

function orderedGroups(
  values: readonly TimelineEventFacts[],
  key: (value: TimelineEventFacts) => string | undefined,
): Map<string, TimelineEventFacts[]> {
  const groups = groupBy(values, key)
  for (const group of groups.values()) {
    group.sort((left, right) => compareTimelineOrder(left.order, right.order))
  }
  return groups
}

function addExplicitCausation(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  for (const facts of events) {
    const cause = facts.event.causationId
    if (!cause) continue
    addEdge(graph, {
      kind: TimelineEdgeKind.CAUSATION,
      sourceEventId: cause,
      targetEventId: facts.event.eventId,
      explicit: true,
      weight: 10,
      label: "caused",
      metadata: {
        correlationId: facts.event.correlationId,
      },
    })
  }
}

function addParentSpanEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  const bySpan = orderedGroups(events, (facts) => facts.event.spanId)
  for (const facts of events) {
    const parentSpanId = facts.event.parentSpanId
    if (!parentSpanId) continue
    const candidates = bySpan.get(parentSpanId) ?? []
    const source = [...candidates]
      .filter((candidate) => candidate.order.sequence <= facts.order.sequence)
      .sort((left, right) => compareTimelineOrder(right.order, left.order))[0]
    if (!source) {
      graph.missingEventIds.add(`span:${parentSpanId}`)
      continue
    }
    addEdge(graph, {
      kind: TimelineEdgeKind.SPAN_PARENT,
      sourceEventId: source.event.eventId,
      targetEventId: facts.event.eventId,
      explicit: true,
      weight: 7,
      label: "parent span",
      metadata: {
        parentSpanId,
        spanId: facts.event.spanId ?? "",
      },
    })
  }
}

function addSequentialEdges(
  graph: MutableGraph,
  groups: Map<string, TimelineEventFacts[]>,
  kind: TimelineEdgeKindValue,
  label: string,
  weight: number,
  metadataKey: string,
): void {
  for (const [groupId, events] of groups) {
    let previous: TimelineEventFacts | undefined
    for (const facts of events) {
      if (previous) {
        addEdge(graph, {
          kind,
          sourceEventId: previous.event.eventId,
          targetEventId: facts.event.eventId,
          explicit: false,
          weight,
          label,
          metadata: { [metadataKey]: groupId },
        })
      }
      previous = facts
    }
  }
}

function addCorrelationEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  const groups = orderedGroups(events, (facts) => facts.event.correlationId)
  for (const [correlationId, group] of groups) {
    if (group.length < 2) continue
    let previous = group[0]
    for (const facts of group.slice(1)) {
      const alreadyExplicit =
        facts.event.causationId === previous.event.eventId ||
        (graph.outgoingByEvent[previous.event.eventId] ?? []).some((id) => {
          const edge = graph.edges.find((candidate) => candidate.id === id)
          return edge?.targetEventId === facts.event.eventId
        })
      if (!alreadyExplicit) {
        addEdge(graph, {
          kind: TimelineEdgeKind.CORRELATION,
          sourceEventId: previous.event.eventId,
          targetEventId: facts.event.eventId,
          explicit: false,
          weight: 2,
          label: "same correlation",
          metadata: { correlationId },
        })
      }
      previous = facts
    }
  }
}

function addSpanSiblingEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  addSequentialEdges(
    graph,
    orderedGroups(events, (facts) => facts.event.spanId),
    TimelineEdgeKind.SPAN_SIBLING,
    "same span",
    4,
    "spanId",
  )
}

function addToolEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  addSequentialEdges(
    graph,
    orderedGroups(
      events,
      (facts) => facts.event.toolCallId ?? facts.tool?.id,
    ),
    TimelineEdgeKind.TOOL,
    "tool lifecycle",
    8,
    "toolCallId",
  )
}

function addFailureEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  addSequentialEdges(
    graph,
    orderedGroups(events, (facts) => facts.failureId),
    TimelineEdgeKind.FAILURE,
    "failure lifecycle",
    9,
    "failureId",
  )
}

function addRecoveryEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  const byRecovery = orderedGroups(events, (facts) => facts.recoveryId)
  addSequentialEdges(
    graph,
    byRecovery,
    TimelineEdgeKind.RECOVERY,
    "recovery attempt",
    10,
    "recoveryId",
  )
  const byFailure = orderedGroups(events, (facts) => facts.failureId)
  for (const [failureId, group] of byFailure) {
    const failure = group.find((facts) =>
      facts.kinds.includes("failure"),
    )
    if (!failure) continue
    for (const recovery of group.filter((facts) =>
      facts.kinds.includes("recovery"),
    )) {
      if (recovery.event.eventId === failure.event.eventId) continue
      addEdge(graph, {
        kind: TimelineEdgeKind.RECOVERY,
        sourceEventId: failure.event.eventId,
        targetEventId: recovery.event.eventId,
        explicit: Boolean(recovery.event.causationId),
        weight: 12,
        label: "failure recovery",
        metadata: {
          failureId,
          recoveryId: recovery.recoveryId ?? "",
        },
      })
    }
  }
}

function addArtifactEdges(
  graph: MutableGraph,
  state: CanonicalProjectionState,
  events: readonly TimelineEventFacts[],
): void {
  const byArtifact = new Map<
    string,
    Array<{ facts: TimelineEventFacts; artifactId: string }>
  >()
  for (const facts of events) {
    for (const artifact of facts.artifacts) {
      const values = byArtifact.get(artifact.id) ?? []
      values.push({ facts, artifactId: artifact.id })
      byArtifact.set(artifact.id, values)
    }
  }
  for (const values of byArtifact.values()) {
    values.sort((left, right) =>
      compareTimelineOrder(left.facts.order, right.facts.order),
    )
  }
  for (const [artifactId, values] of byArtifact) {
    const artifact = state.artifacts[artifactId]
    const producerId = artifact?.producerEventId
    if (producerId && !graph.eventSet.has(producerId)) {
      graph.missingEventIds.add(producerId)
    }
    for (const value of values) {
      const targetId = value.facts.event.eventId
      if (producerId && producerId !== targetId) {
        addEdge(graph, {
          kind: TimelineEdgeKind.ARTIFACT,
          sourceEventId: producerId,
          targetEventId: targetId,
          explicit: true,
          weight: 8,
          label: "artifact lineage",
          metadata: { artifactId },
        })
      }
    }
    for (let index = 1; index < values.length; index += 1) {
      const previous = values[index - 1]?.facts
      const current = values[index]?.facts
      if (!previous || !current) continue
      addEdge(graph, {
        kind: TimelineEdgeKind.ARTIFACT,
        sourceEventId: previous.event.eventId,
        targetEventId: current.event.eventId,
        explicit: false,
        weight: 5,
        label: "artifact evidence",
        metadata: { artifactId },
      })
    }
  }
}

function addMutationEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  addSequentialEdges(
    graph,
    orderedGroups(
      events,
      (facts) => facts.mutation?.id ?? facts.event.mutationId,
    ),
    TimelineEdgeKind.MUTATION,
    "state mutation",
    6,
    "mutationId",
  )
}

function addWorkerEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  addSequentialEdges(
    graph,
    orderedGroups(
      events,
      (facts) => facts.event.workerId ?? facts.worker?.id,
    ),
    TimelineEdgeKind.WORKER,
    "worker lifecycle",
    3,
    "workerId",
  )
}

function addLeaseReplacementEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  const byWorker = orderedGroups(
    events,
    (facts) => facts.event.workerId ?? facts.worker?.id,
  )
  for (const [workerId, group] of byWorker) {
    let previousLease: string | undefined
    let previousFacts: TimelineEventFacts | undefined
    for (const facts of group) {
      const lease = facts.leaseId
      const explicitReplacement =
        facts.event.eventType.toLowerCase().includes("lease.replaced") ||
        facts.event.eventType.toLowerCase().includes("lease.reassigned")
      if (previousFacts && explicitReplacement) {
        addEdge(graph, {
          kind: TimelineEdgeKind.LEASE_REPLACEMENT,
          sourceEventId: previousFacts.event.eventId,
          targetEventId: facts.event.eventId,
          explicit: true,
          weight: 11,
          label: "lease replaced",
          metadata: {
            workerId,
            previousLeaseId: previousLease ?? "unknown",
            replacementLeaseId: lease ?? "unknown",
          },
        })
      }
      if (
        previousFacts &&
        previousLease &&
        lease &&
        lease !== previousLease &&
        !explicitReplacement
      ) {
        addEdge(graph, {
          kind: TimelineEdgeKind.LEASE_REPLACEMENT,
          sourceEventId: previousFacts.event.eventId,
          targetEventId: facts.event.eventId,
          explicit: false,
          weight: 11,
          label: "lease replaced",
          metadata: {
            workerId,
            previousLeaseId: previousLease,
            replacementLeaseId: lease,
          },
        })
      }
      if (lease) previousLease = lease
      previousFacts = facts
    }
  }
}

function addWorkerReplacementEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  const byRecovery = orderedGroups(events, (facts) => facts.recoveryId)
  for (const [recoveryId, group] of byRecovery) {
    const replacement = group.find(
      (facts) =>
        facts.recovery?.previousWorkerId &&
        facts.recovery?.replacementWorkerId,
    )
    if (!replacement?.recovery) continue
    const previousWorkerId = replacement.recovery.previousWorkerId
    const replacementWorkerId = replacement.recovery.replacementWorkerId
    if (!previousWorkerId || !replacementWorkerId) continue
    const previous = [...events]
      .filter(
        (facts) =>
          (facts.event.workerId ?? facts.worker?.id) === previousWorkerId,
      )
      .filter(
        (facts) => facts.order.sequence <= replacement.order.sequence,
      )
      .sort((left, right) => compareTimelineOrder(right.order, left.order))[0]
    const next = events
      .filter(
        (facts) =>
          (facts.event.workerId ?? facts.worker?.id) === replacementWorkerId,
      )
      .filter(
        (facts) => facts.order.sequence >= replacement.order.sequence,
      )
      .sort((left, right) => compareTimelineOrder(left.order, right.order))[0]
    if (!previous || !next) continue
    addEdge(graph, {
      kind: TimelineEdgeKind.WORKER_REPLACEMENT,
      sourceEventId: previous.event.eventId,
      targetEventId: next.event.eventId,
      explicit: true,
      weight: 13,
      label: "worker replaced",
      metadata: {
        recoveryId,
        previousWorkerId,
        replacementWorkerId,
      },
    })
  }
}

function addBackgroundReviveEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  const groups = orderedGroups(events, (facts) => facts.backgroundJobId)
  for (const [jobId, group] of groups) {
    let parked: TimelineEventFacts | undefined
    for (const facts of group) {
      const type = facts.event.eventType.toLowerCase()
      if (type.includes("park") || type.includes("suspend")) {
        parked = facts
        continue
      }
      if (
        parked &&
        (type.includes("revive") ||
          type.includes("resume") ||
          type.includes("restored"))
      ) {
        addEdge(graph, {
          kind: TimelineEdgeKind.BACKGROUND_REVIVE,
          sourceEventId: parked.event.eventId,
          targetEventId: facts.event.eventId,
          explicit: Boolean(facts.event.causationId),
          weight: 12,
          label: "background revived",
          metadata: { jobId },
        })
        parked = undefined
      }
    }
  }
}

function addBrowserStepEdges(
  graph: MutableGraph,
  events: readonly TimelineEventFacts[],
): void {
  addSequentialEdges(
    graph,
    orderedGroups(events, (facts) => facts.browserStepId),
    TimelineEdgeKind.BROWSER_STEP,
    "browser step",
    8,
    "browserStepId",
  )
}

function edgesById(edges: readonly TimelineEdge[]): Map<string, TimelineEdge> {
  return new Map(edges.map((edge) => [edge.id, edge]))
}

function adjacency(
  eventIds: readonly string[],
  edges: readonly TimelineEdge[],
): Map<string, string[]> {
  const result = new Map(eventIds.map((eventId) => [eventId, [] as string[]]))
  for (const edge of edges) {
    if (edge.missingSource || edge.missingTarget) continue
    const values = result.get(edge.sourceEventId)
    if (!values) continue
    if (!values.includes(edge.targetEventId)) values.push(edge.targetEventId)
  }
  return result
}

function reverseAdjacency(
  eventIds: readonly string[],
  edges: readonly TimelineEdge[],
): Map<string, string[]> {
  const result = new Map(eventIds.map((eventId) => [eventId, [] as string[]]))
  for (const edge of edges) {
    if (edge.missingSource || edge.missingTarget) continue
    const values = result.get(edge.targetEventId)
    if (!values) continue
    if (!values.includes(edge.sourceEventId)) values.push(edge.sourceEventId)
  }
  return result
}

function closure(
  startId: string,
  next: Map<string, string[]>,
  maximumDepth: number,
): readonly string[] {
  const result: string[] = []
  const visited = new Set([startId])
  const queue: Array<{ id: string; depth: number }> = [
    { id: startId, depth: 0 },
  ]
  while (queue.length) {
    const current = queue.shift()
    if (!current || current.depth >= maximumDepth) continue
    for (const id of next.get(current.id) ?? []) {
      if (visited.has(id)) continue
      visited.add(id)
      result.push(id)
      queue.push({ id, depth: current.depth + 1 })
    }
  }
  return Object.freeze(result)
}

function stronglyConnectedComponents(
  eventIds: readonly string[],
  next: Map<string, string[]>,
): readonly (readonly string[])[] {
  let index = 0
  const indexes = new Map<string, number>()
  const low = new Map<string, number>()
  const stack: string[] = []
  const onStack = new Set<string>()
  const components: string[][] = []
  const visit = (eventId: string) => {
    indexes.set(eventId, index)
    low.set(eventId, index)
    index += 1
    stack.push(eventId)
    onStack.add(eventId)
    for (const target of next.get(eventId) ?? []) {
      if (!indexes.has(target)) {
        visit(target)
        low.set(
          eventId,
          Math.min(low.get(eventId) ?? 0, low.get(target) ?? 0),
        )
      } else if (onStack.has(target)) {
        low.set(
          eventId,
          Math.min(low.get(eventId) ?? 0, indexes.get(target) ?? 0),
        )
      }
    }
    if (low.get(eventId) !== indexes.get(eventId)) return
    const component: string[] = []
    while (stack.length) {
      const candidate = stack.pop()
      if (!candidate) break
      onStack.delete(candidate)
      component.push(candidate)
      if (candidate === eventId) break
    }
    component.sort()
    components.push(component)
  }
  for (const eventId of eventIds) {
    if (!indexes.has(eventId)) visit(eventId)
  }
  return Object.freeze(
    components
      .sort((left, right) => left[0]!.localeCompare(right[0]!))
      .map((component) => Object.freeze(component)),
  )
}

function depthMap(
  eventIds: readonly string[],
  reverse: Map<string, string[]>,
  maximumDepth: number,
): Readonly<Record<string, number>> {
  const result: Record<string, number> = {}
  const visiting = new Set<string>()
  const depth = (eventId: string, budget: number): number => {
    if (result[eventId] !== undefined) return result[eventId]!
    if (budget <= 0 || visiting.has(eventId)) return 0
    visiting.add(eventId)
    let value = 0
    for (const parent of reverse.get(eventId) ?? []) {
      value = Math.max(value, depth(parent, budget - 1) + 1)
    }
    visiting.delete(eventId)
    result[eventId] = value
    return value
  }
  for (const eventId of eventIds) depth(eventId, maximumDepth)
  return Object.freeze(result)
}

function freezeIndex(
  eventIds: readonly string[],
  source: Record<string, string[]>,
): Readonly<Record<string, readonly string[]>> {
  const result: Record<string, readonly string[]> = {}
  for (const eventId of eventIds) {
    result[eventId] = Object.freeze([...(source[eventId] ?? [])].sort())
  }
  return Object.freeze(result)
}

export function buildTimelineCausalGraph(
  state: CanonicalProjectionState,
  events: readonly TimelineEventFacts[],
  maximumDepth: number,
): TimelineCausalGraph {
  const eventIds = events.map((facts) => facts.event.eventId)
  const graph: MutableGraph = {
    eventIds,
    eventSet: new Set(eventIds),
    edges: [],
    edgeIds: new Set(),
    incomingByEvent: Object.fromEntries(eventIds.map((id) => [id, []])),
    outgoingByEvent: Object.fromEntries(eventIds.map((id) => [id, []])),
    missingEventIds: new Set(),
  }
  addExplicitCausation(graph, events)
  addParentSpanEdges(graph, events)
  addCorrelationEdges(graph, events)
  addSpanSiblingEdges(graph, events)
  addToolEdges(graph, events)
  addArtifactEdges(graph, state, events)
  addFailureEdges(graph, events)
  addRecoveryEdges(graph, events)
  addMutationEdges(graph, events)
  addWorkerEdges(graph, events)
  addLeaseReplacementEdges(graph, events)
  addWorkerReplacementEdges(graph, events)
  addBackgroundReviveEdges(graph, events)
  addBrowserStepEdges(graph, events)
  graph.edges.sort(
    (left, right) =>
      left.sourceEventId.localeCompare(right.sourceEventId) ||
      left.targetEventId.localeCompare(right.targetEventId) ||
      left.kind.localeCompare(right.kind) ||
      left.id.localeCompare(right.id),
  )
  const next = adjacency(eventIds, graph.edges)
  const reverse = reverseAdjacency(eventIds, graph.edges)
  const components = stronglyConnectedComponents(eventIds, next)
  const cycles = components
    .filter((component) => component.length > 1)
    .flatMap((component) => component)
  const ancestorsByEvent: Record<string, readonly string[]> = {}
  const descendantsByEvent: Record<string, readonly string[]> = {}
  for (const eventId of eventIds) {
    ancestorsByEvent[eventId] = closure(eventId, reverse, maximumDepth)
    descendantsByEvent[eventId] = closure(eventId, next, maximumDepth)
  }
  const roots = eventIds.filter((eventId) => (reverse.get(eventId) ?? []).length === 0)
  const leaves = eventIds.filter((eventId) => (next.get(eventId) ?? []).length === 0)
  const knownEdges = edgesById(graph.edges)
  for (const [eventId, ids] of Object.entries(graph.incomingByEvent)) {
    ids.sort((left, right) => {
      const a = knownEdges.get(left)
      const b = knownEdges.get(right)
      return (
        (a?.sourceEventId ?? "").localeCompare(b?.sourceEventId ?? "") ||
        left.localeCompare(right)
      )
    })
    graph.incomingByEvent[eventId] = [...new Set(ids)]
  }
  for (const [eventId, ids] of Object.entries(graph.outgoingByEvent)) {
    ids.sort((left, right) => {
      const a = knownEdges.get(left)
      const b = knownEdges.get(right)
      return (
        (a?.targetEventId ?? "").localeCompare(b?.targetEventId ?? "") ||
        left.localeCompare(right)
      )
    })
    graph.outgoingByEvent[eventId] = [...new Set(ids)]
  }
  return Object.freeze({
    eventIds: Object.freeze(eventIds),
    edges: Object.freeze(graph.edges),
    incomingByEvent: freezeIndex(eventIds, graph.incomingByEvent),
    outgoingByEvent: freezeIndex(eventIds, graph.outgoingByEvent),
    ancestorsByEvent: Object.freeze(ancestorsByEvent),
    descendantsByEvent: Object.freeze(descendantsByEvent),
    depthByEvent: depthMap(eventIds, reverse, maximumDepth),
    components,
    roots: Object.freeze(roots),
    leaves: Object.freeze(leaves),
    missingEventIds: Object.freeze([...graph.missingEventIds].sort()),
    cycleEventIds: Object.freeze([...new Set(cycles)].sort()),
  })
}

export function attachRowKeysToEdges(
  graph: TimelineCausalGraph,
  rowKeyByEvent: Readonly<Record<string, string>>,
): TimelineCausalGraph {
  const edges = graph.edges.map((edge) =>
    Object.freeze({
      ...edge,
      sourceRowKey: rowKeyByEvent[edge.sourceEventId],
      targetRowKey: rowKeyByEvent[edge.targetEventId],
    }),
  )
  return Object.freeze({ ...graph, edges: Object.freeze(edges) })
}
