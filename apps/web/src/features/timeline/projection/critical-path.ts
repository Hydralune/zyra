import type {
  TimelineCausalGraph,
  TimelineCriticalPath,
  TimelineEventFacts,
  TimelineRow,
} from "./contracts.ts"
import { TimelineEventKind, TimelinePhase } from "./contracts.ts"
import { compareTimelineOrder } from "./event-reader.ts"

interface PathState {
  eventIds: string[]
  totalWeight: number
}

function eventDuration(
  facts: TimelineEventFacts,
  previous: TimelineEventFacts | undefined,
): number {
  const explicit =
    facts.tool?.durationMs ??
    (typeof facts.safeAttributes.duration_ms === "number"
      ? facts.safeAttributes.duration_ms
      : undefined)
  if (explicit !== undefined && explicit >= 0) return explicit
  if (!previous) return 1
  const delta =
    facts.order.committedAtMs - previous.order.committedAtMs
  if (!Number.isFinite(delta) || delta <= 0) return 1
  return Math.min(delta, 60 * 60 * 1000)
}

function semanticWeight(facts: TimelineEventFacts): number {
  let weight = 1
  if (facts.kinds.includes(TimelineEventKind.FAILURE)) weight += 12
  if (facts.kinds.includes(TimelineEventKind.RECOVERY)) weight += 11
  if (facts.kinds.includes(TimelineEventKind.TOOL)) weight += 7
  if (facts.kinds.includes(TimelineEventKind.BROWSER_STEP)) weight += 6
  if (facts.kinds.includes(TimelineEventKind.POLICY)) weight += 5
  if (facts.kinds.includes(TimelineEventKind.ARTIFACT)) weight += 4
  if (facts.kinds.includes(TimelineEventKind.ROUTE)) weight += 4
  if (facts.kinds.includes(TimelineEventKind.PLACEMENT)) weight += 4
  if (facts.phase === TimelinePhase.FAILED) weight += 10
  if (facts.phase === TimelinePhase.RECOVERING) weight += 8
  if (facts.event.terminal) weight += 2
  if (facts.partial) weight -= 0.5
  return Math.max(0.5, weight)
}

function eventWeights(
  events: readonly TimelineEventFacts[],
): ReadonlyMap<string, number> {
  const sorted = [...events].sort((left, right) =>
    compareTimelineOrder(left.order, right.order),
  )
  const result = new Map<string, number>()
  let previous: TimelineEventFacts | undefined
  for (const facts of sorted) {
    const duration = eventDuration(facts, previous)
    const durationWeight = Math.log2(Math.max(1, duration) + 1)
    result.set(
      facts.event.eventId,
      semanticWeight(facts) + durationWeight,
    )
    previous = facts
  }
  return result
}

function successors(
  graph: TimelineCausalGraph,
): ReadonlyMap<string, readonly string[]> {
  const edgesById = new Map(graph.edges.map((edge) => [edge.id, edge]))
  const result = new Map<string, readonly string[]>()
  for (const eventId of graph.eventIds) {
    const values = (graph.outgoingByEvent[eventId] ?? [])
      .map((id) => edgesById.get(id))
      .filter((edge) => edge && !edge.missingTarget)
      .map((edge) => edge!.targetEventId)
    result.set(eventId, Object.freeze([...new Set(values)].sort()))
  }
  return result
}

function predecessors(
  graph: TimelineCausalGraph,
): ReadonlyMap<string, readonly string[]> {
  const edgesById = new Map(graph.edges.map((edge) => [edge.id, edge]))
  const result = new Map<string, readonly string[]>()
  for (const eventId of graph.eventIds) {
    const values = (graph.incomingByEvent[eventId] ?? [])
      .map((id) => edgesById.get(id))
      .filter((edge) => edge && !edge.missingSource)
      .map((edge) => edge!.sourceEventId)
    result.set(eventId, Object.freeze([...new Set(values)].sort()))
  }
  return result
}

function topologicalOrder(
  graph: TimelineCausalGraph,
  eventsById: ReadonlyMap<string, TimelineEventFacts>,
): {
  order: readonly string[]
  ignoredCycleEdges: ReadonlySet<string>
} {
  const next = successors(graph)
  const incoming = predecessors(graph)
  const indegree = new Map<string, number>()
  for (const eventId of graph.eventIds) {
    indegree.set(eventId, (incoming.get(eventId) ?? []).length)
  }
  const compareIds = (left: string, right: string) => {
    const a = eventsById.get(left)
    const b = eventsById.get(right)
    if (!a || !b) return left.localeCompare(right)
    return (
      compareTimelineOrder(a.order, b.order) || left.localeCompare(right)
    )
  }
  const ready = graph.eventIds
    .filter((eventId) => (indegree.get(eventId) ?? 0) === 0)
    .sort(compareIds)
  const order: string[] = []
  while (ready.length) {
    const eventId = ready.shift()!
    order.push(eventId)
    for (const target of next.get(eventId) ?? []) {
      const remaining = Math.max(0, (indegree.get(target) ?? 0) - 1)
      indegree.set(target, remaining)
      if (remaining === 0) {
        ready.push(target)
        ready.sort(compareIds)
      }
    }
  }
  const unresolved = graph.eventIds
    .filter((eventId) => !order.includes(eventId))
    .sort(compareIds)
  const ignoredCycleEdges = new Set<string>()
  if (unresolved.length) {
    for (const eventId of unresolved) {
      for (const edgeId of graph.incomingByEvent[eventId] ?? []) {
        ignoredCycleEdges.add(edgeId)
      }
      order.push(eventId)
    }
  }
  return {
    order: Object.freeze(order),
    ignoredCycleEdges,
  }
}

function edgeWeight(
  graph: TimelineCausalGraph,
  source: string,
  target: string,
  ignored: ReadonlySet<string>,
): number {
  let result = 0
  for (const edge of graph.edges) {
    if (ignored.has(edge.id)) continue
    if (
      edge.sourceEventId === source &&
      edge.targetEventId === target &&
      !edge.missingSource &&
      !edge.missingTarget
    ) {
      result = Math.max(result, edge.weight)
    }
  }
  return result
}

function betterPath(
  candidate: PathState,
  current: PathState | undefined,
): boolean {
  if (!current) return true
  if (candidate.totalWeight !== current.totalWeight) {
    return candidate.totalWeight > current.totalWeight
  }
  if (candidate.eventIds.length !== current.eventIds.length) {
    return candidate.eventIds.length > current.eventIds.length
  }
  return candidate.eventIds.join("\0").localeCompare(current.eventIds.join("\0")) < 0
}

function dynamicPaths(
  graph: TimelineCausalGraph,
  events: readonly TimelineEventFacts[],
): {
  bestByEvent: ReadonlyMap<string, PathState>
  ignoredCycleEdges: ReadonlySet<string>
} {
  const eventsById = new Map(
    events.map((facts) => [facts.event.eventId, facts] as const),
  )
  const weights = eventWeights(events)
  const incoming = predecessors(graph)
  const { order, ignoredCycleEdges } = topologicalOrder(graph, eventsById)
  const bestByEvent = new Map<string, PathState>()
  for (const eventId of order) {
    const ownWeight = weights.get(eventId) ?? 1
    let best: PathState = { eventIds: [eventId], totalWeight: ownWeight }
    for (const parent of incoming.get(eventId) ?? []) {
      const parentPath = bestByEvent.get(parent)
      if (!parentPath) continue
      const edge = edgeWeight(graph, parent, eventId, ignoredCycleEdges)
      const candidate = {
        eventIds: [...parentPath.eventIds, eventId],
        totalWeight: parentPath.totalWeight + ownWeight + edge,
      }
      if (betterPath(candidate, best)) best = candidate
    }
    bestByEvent.set(eventId, best)
  }
  return { bestByEvent, ignoredCycleEdges }
}

function uniquePathCandidates(
  states: Iterable<PathState>,
  limit: number,
): readonly PathState[] {
  const bySignature = new Map<string, PathState>()
  for (const state of states) {
    const signature = state.eventIds.join("\0")
    const existing = bySignature.get(signature)
    if (!existing || state.totalWeight > existing.totalWeight) {
      bySignature.set(signature, state)
    }
  }
  return Object.freeze(
    [...bySignature.values()]
      .sort(
        (left, right) =>
          right.totalWeight - left.totalWeight ||
          right.eventIds.length - left.eventIds.length ||
          left.eventIds.join("\0").localeCompare(right.eventIds.join("\0")),
      )
      .slice(0, Math.max(1, limit)),
  )
}

function pathTimes(
  eventIds: readonly string[],
  eventsById: ReadonlyMap<string, TimelineEventFacts>,
): {
  startedAt?: string
  endedAt?: string
  durationMs: number
} {
  const events = eventIds
    .map((eventId) => eventsById.get(eventId))
    .filter((facts): facts is TimelineEventFacts => Boolean(facts))
  if (!events.length) return { durationMs: 0 }
  const first = events[0]!
  const last = events.at(-1)!
  return {
    startedAt: first.event.createdAt,
    endedAt: last.event.committedAt,
    durationMs: Math.max(
      0,
      last.order.committedAtMs - first.order.createdAtMs,
    ),
  }
}

function pathComplete(
  eventIds: readonly string[],
  eventsById: ReadonlyMap<string, TimelineEventFacts>,
  graph: TimelineCausalGraph,
): boolean {
  if (!eventIds.length) return false
  if (
    eventIds.some((eventId) => graph.missingEventIds.includes(eventId))
  ) {
    return false
  }
  const last = eventsById.get(eventIds.at(-1)!)
  return Boolean(
    last?.event.terminal ||
      last?.phase === TimelinePhase.COMPLETED ||
      last?.phase === TimelinePhase.FAILED ||
      last?.phase === TimelinePhase.CANCELLED,
  )
}

function workerIds(
  eventIds: readonly string[],
  eventsById: ReadonlyMap<string, TimelineEventFacts>,
): readonly string[] {
  return Object.freeze(
    [
      ...new Set(
        eventIds
          .map((eventId) => {
            const facts = eventsById.get(eventId)
            return facts?.event.workerId ?? facts?.worker?.id
          })
          .filter((value): value is string => Boolean(value)),
      ),
    ],
  )
}

function rowKeys(
  eventIds: readonly string[],
  rowKeyByEvent: Readonly<Record<string, string>>,
): readonly string[] {
  return Object.freeze(
    [
      ...new Set(
        eventIds
          .map((eventId) => rowKeyByEvent[eventId])
          .filter((value): value is string => Boolean(value)),
      ),
    ],
  )
}

export function buildTimelineCriticalPath(
  graph: TimelineCausalGraph,
  events: readonly TimelineEventFacts[],
  rows: readonly TimelineRow[],
  rowKeyByEvent: Readonly<Record<string, string>>,
  maximumAlternativePaths: number,
): TimelineCriticalPath {
  if (!events.length) {
    return Object.freeze({
      eventIds: Object.freeze([]),
      rowKeys: Object.freeze([]),
      workerIds: Object.freeze([]),
      totalWeight: 0,
      durationMs: 0,
      complete: false,
      missingEventIds: graph.missingEventIds,
      alternatives: Object.freeze([]),
    })
  }
  const eventsById = new Map(
    events.map((facts) => [facts.event.eventId, facts] as const),
  )
  const { bestByEvent } = dynamicPaths(graph, events)
  const candidates = uniquePathCandidates(
    [
      ...graph.leaves
        .map((eventId) => bestByEvent.get(eventId))
        .filter((value): value is PathState => Boolean(value)),
      ...bestByEvent.values(),
    ],
    maximumAlternativePaths + 1,
  )
  const best = candidates[0] ?? {
    eventIds: [events[0]!.event.eventId],
    totalWeight: 1,
  }
  const times = pathTimes(best.eventIds, eventsById)
  return Object.freeze({
    eventIds: Object.freeze(best.eventIds),
    rowKeys: rowKeys(best.eventIds, rowKeyByEvent),
    workerIds: workerIds(best.eventIds, eventsById),
    totalWeight: best.totalWeight,
    ...times,
    complete: pathComplete(best.eventIds, eventsById, graph),
    missingEventIds: Object.freeze(
      graph.missingEventIds.filter((id) => best.eventIds.includes(id)),
    ),
    alternatives: Object.freeze(
      candidates.slice(1).map((candidate) =>
        Object.freeze({
          eventIds: Object.freeze(candidate.eventIds),
          totalWeight: candidate.totalWeight,
        }),
      ),
    ),
  })
}
