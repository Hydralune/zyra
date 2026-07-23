import type {
  CanonicalProjectionState,
  CausalEventProjection,
  MutationProjection,
} from "../../../state/contracts.ts"
import type {
  ProjectionRecord,
  TopologyEvidence,
  TopologyEvidenceResolver,
} from "./contracts.ts"
import {
  firstInteger,
  firstString,
  firstStringArray,
  namedRecords,
  recordCandidates,
  uniqueNumbers,
  uniqueStrings,
} from "./record-reader.ts"

const EMPTY_EVIDENCE: TopologyEvidence = Object.freeze({
  eventIds: Object.freeze([]),
  mutationIds: Object.freeze([]),
  correlationIds: Object.freeze([]),
  causationIds: Object.freeze([]),
  spanIds: Object.freeze([]),
  parentSpanIds: Object.freeze([]),
  checkpointIds: Object.freeze([]),
  controlCommandIds: Object.freeze([]),
  graphIds: Object.freeze([]),
  graphRevisions: Object.freeze([]),
  entityRefs: Object.freeze([]),
})

export function createEvidenceResolver(
  state: CanonicalProjectionState,
  taskId: string,
  maximumEvents: number,
): TopologyEvidenceResolver {
  const taskEvents = Object.values(state.causality.byEvent)
    .filter((event) => event.taskId === taskId)
    .sort(compareEvents)
  const allowedEvents = new Set(
    taskEvents
      .slice(Math.max(0, taskEvents.length - maximumEvents))
      .map((event) => event.eventId),
  )
  const eventById = state.causality.byEvent
  const mutationByEvent = mutationIndex(state.mutations, taskId)
  const neighbors = buildNeighborIndex(state, taskId, allowedEvents)
  const cache = new Map<string, TopologyEvidence>()

  const forEventIds = (eventIds: readonly string[]): TopologyEvidence => {
    const normalized = uniqueStrings(eventIds).filter((id) => allowedEvents.has(id))
    const cacheKey = normalized.join("\0")
    const cached = cache.get(cacheKey)
    if (cached) return cached
    const events = normalized
      .map((id) => eventById[id])
      .filter((event): event is CausalEventProjection => Boolean(event))
      .sort(compareEvents)
    const evidence = evidenceFromEvents(events, mutationByEvent)
    cache.set(cacheKey, evidence)
    return evidence
  }

  const relatedEventIds = (
    seedIds: readonly string[],
    limit = maximumEvents,
  ): readonly string[] => {
    const queue = uniqueStrings(seedIds).filter((id) => allowedEvents.has(id))
    const seen = new Set<string>()
    while (queue.length > 0 && seen.size < limit) {
      const eventId = queue.shift()!
      if (seen.has(eventId) || !allowedEvents.has(eventId)) continue
      seen.add(eventId)
      for (const neighbor of neighbors[eventId] ?? []) {
        if (!seen.has(neighbor)) queue.push(neighbor)
      }
    }
    return Object.freeze(
      [...seen].sort((left, right) => {
        const leftEvent = eventById[left]
        const rightEvent = eventById[right]
        return leftEvent && rightEvent
          ? compareEvents(leftEvent, rightEvent)
          : left.localeCompare(right)
      }),
    )
  }

  const forRecord = (
    record: ProjectionRecord,
    extraEventIds: readonly string[] = [],
  ): TopologyEvidence => {
    const candidates = recordCandidates(record)
    const named = [
      ...namedRecords(record, [
        "receipt",
        "decision",
        "delta",
        "checkpoint",
        "mutation",
        "conflict",
      ]),
      ...candidates,
    ]
    const ids = uniqueStrings([
      record.eventId,
      record.causationId,
      ...extraEventIds,
      ...named.flatMap((source) =>
        firstStringArray(
          [source],
          "event_ids",
          "emitted_event_ids",
          "causal_event_ids",
          "evidence_event_ids",
          "ancestor_event_ids",
          "descendant_event_ids",
        ),
      ),
      ...named.flatMap((source) => [
        firstString(
          [source],
          "event_id",
          "source_event_id",
          "topology_event_id",
          "cause_event_id",
          "trigger_event_id",
          "parent_event_id",
        ),
      ]),
    ])
    const direct = forEventIds(ids)
    return mergeEvidence(
      direct,
      Object.freeze({
        ...EMPTY_EVIDENCE,
        eventIds: Object.freeze(uniqueStrings([record.eventId, ...direct.eventIds])),
        mutationIds: Object.freeze(
          uniqueStrings([record.mutationId, ...direct.mutationIds]),
        ),
        correlationIds: Object.freeze(
          uniqueStrings([record.correlationId, ...direct.correlationIds]),
        ),
        causationIds: Object.freeze(
          uniqueStrings([record.causationId, ...direct.causationIds]),
        ),
        spanIds: Object.freeze(uniqueStrings([record.spanId, ...direct.spanIds])),
        parentSpanIds: Object.freeze(
          uniqueStrings([record.parentSpanId, ...direct.parentSpanIds]),
        ),
        checkpointIds: Object.freeze(
          uniqueStrings([record.checkpointId, ...direct.checkpointIds]),
        ),
        graphIds: Object.freeze(
          uniqueStrings([
            ...direct.graphIds,
            ...named.map((source) =>
              firstString([source], "graph_id", "graphId"),
            ),
          ]),
        ),
        graphRevisions: Object.freeze(
          uniqueNumbers([
            ...direct.graphRevisions,
            ...named.map((source) =>
              firstInteger(
                [source],
                "graph_revision",
                "graphRevision",
                "committed_revision",
              ),
            ),
          ]),
        ),
        entityRefs: Object.freeze(
          uniqueStrings([
            record.id,
            record.nodeId ? `node:${record.nodeId}` : undefined,
            record.workerId ? `worker:${record.workerId}` : undefined,
            ...direct.entityRefs,
          ]),
        ),
        firstSequence: direct.firstSequence ?? record.sequence,
        lastSequence: Math.max(direct.lastSequence ?? 0, record.sequence),
      }),
    )
  }

  return Object.freeze({
    forRecord,
    forEventIds,
    merge: mergeEvidence,
    relatedEventIds,
  })
}

function mutationIndex(
  mutations: Readonly<Record<string, MutationProjection>>,
  taskId: string,
): Map<string, MutationProjection[]> {
  const output = new Map<string, MutationProjection[]>()
  for (const mutation of Object.values(mutations)) {
    if (mutation.taskId !== taskId) continue
    const values = output.get(mutation.eventId) ?? []
    values.push(mutation)
    output.set(mutation.eventId, values)
  }
  for (const values of output.values()) {
    values.sort(
      (left, right) =>
        left.sequence - right.sequence || left.id.localeCompare(right.id),
    )
  }
  return output
}

function buildNeighborIndex(
  state: CanonicalProjectionState,
  taskId: string,
  allowed: ReadonlySet<string>,
): Record<string, string[]> {
  const output: Record<string, string[]> = {}
  const link = (left: string | undefined, right: string | undefined) => {
    if (!left || !right || left === right) return
    if (!allowed.has(left) || !allowed.has(right)) return
    appendUnique(output, left, right)
    appendUnique(output, right, left)
  }
  for (const event of Object.values(state.causality.byEvent)) {
    if (event.taskId !== taskId || !allowed.has(event.eventId)) continue
    link(event.eventId, event.causationId)
    const sameCorrelation = state.causality.byCorrelation[event.correlationId] ?? []
    for (const related of boundedNeighbors(sameCorrelation, event.eventId, 8)) {
      link(event.eventId, related)
    }
    if (event.spanId) {
      for (const related of boundedNeighbors(
        state.causality.byParentSpan[event.spanId] ?? [],
        event.eventId,
        16,
      )) {
        link(event.eventId, related)
      }
    }
    if (event.parentSpanId) {
      for (const related of boundedNeighbors(
        state.causality.bySpan[event.parentSpanId] ?? [],
        event.eventId,
        16,
      )) {
        link(event.eventId, related)
      }
    }
    for (const index of [
      state.causality.byCheckpoint,
      state.causality.byMutation,
      state.causality.byControlCommand,
      state.causality.byNode,
      state.causality.byWorker,
    ]) {
      const keys = indexKeysForEvent(index, event.eventId)
      for (const key of keys) {
        for (const related of boundedNeighbors(index[key] ?? [], event.eventId, 8)) {
          link(event.eventId, related)
        }
      }
    }
  }
  for (const values of Object.values(output)) values.sort()
  return output
}

function indexKeysForEvent(
  index: Readonly<Record<string, readonly string[]>>,
  eventId: string,
): string[] {
  const output: string[] = []
  for (const [key, values] of Object.entries(index)) {
    if (values.includes(eventId)) output.push(key)
  }
  return output
}

function boundedNeighbors(
  values: readonly string[],
  target: string,
  radius: number,
): string[] {
  const position = values.indexOf(target)
  if (position < 0) return values.slice(-radius)
  return values.slice(
    Math.max(0, position - radius),
    Math.min(values.length, position + radius + 1),
  )
}

function appendUnique(
  index: Record<string, string[]>,
  key: string,
  value: string,
): void {
  const values = index[key] ?? (index[key] = [])
  if (!values.includes(value)) values.push(value)
}

function evidenceFromEvents(
  events: readonly CausalEventProjection[],
  mutationByEvent: ReadonlyMap<string, readonly MutationProjection[]>,
): TopologyEvidence {
  if (events.length === 0) return EMPTY_EVIDENCE
  const mutations = events.flatMap(
    (event) => mutationByEvent.get(event.eventId) ?? [],
  )
  return Object.freeze({
    eventIds: Object.freeze(events.map((event) => event.eventId)),
    mutationIds: Object.freeze(
      uniqueStrings([
        ...events.map((event) => event.mutationId),
        ...mutations.map((mutation) => mutation.id),
      ]),
    ),
    correlationIds: Object.freeze(
      uniqueStrings(events.map((event) => event.correlationId)),
    ),
    causationIds: Object.freeze(
      uniqueStrings(events.map((event) => event.causationId)),
    ),
    spanIds: Object.freeze(uniqueStrings(events.map((event) => event.spanId))),
    parentSpanIds: Object.freeze(
      uniqueStrings(events.map((event) => event.parentSpanId)),
    ),
    checkpointIds: Object.freeze(
      uniqueStrings(events.map((event) => event.checkpointId)),
    ),
    controlCommandIds: Object.freeze(
      uniqueStrings(events.map((event) => event.controlCommandId)),
    ),
    graphIds: Object.freeze(
      uniqueStrings(
        mutations.flatMap((mutation) =>
          graphIdsFromPath(mutation.path, mutation.entityRefs),
        ),
      ),
    ),
    graphRevisions: Object.freeze([]),
    entityRefs: Object.freeze(
      uniqueStrings([
        ...events.flatMap((event) => event.entityRefs),
        ...mutations.flatMap((mutation) => mutation.entityRefs),
      ]),
    ),
    firstSequence: events[0]?.sequence,
    lastSequence: events.at(-1)?.sequence,
  })
}

function graphIdsFromPath(
  path: readonly string[],
  refs: readonly string[],
): string[] {
  const output: string[] = []
  for (let index = 0; index < path.length; index += 1) {
    if (/graph/i.test(path[index] ?? "") && path[index + 1]) {
      output.push(path[index + 1]!)
    }
  }
  for (const ref of refs) {
    if (ref.startsWith("graph:")) output.push(ref.slice("graph:".length))
  }
  return output
}

export function mergeEvidence(
  ...items: readonly (TopologyEvidence | undefined)[]
): TopologyEvidence {
  const values = items.filter((item): item is TopologyEvidence => Boolean(item))
  if (values.length === 0) return EMPTY_EVIDENCE
  const firstSequences = values
    .map((item) => item.firstSequence)
    .filter((value): value is number => value !== undefined)
  const lastSequences = values
    .map((item) => item.lastSequence)
    .filter((value): value is number => value !== undefined)
  return Object.freeze({
    eventIds: Object.freeze(uniqueStrings(values.flatMap((item) => item.eventIds))),
    mutationIds: Object.freeze(
      uniqueStrings(values.flatMap((item) => item.mutationIds)),
    ),
    correlationIds: Object.freeze(
      uniqueStrings(values.flatMap((item) => item.correlationIds)),
    ),
    causationIds: Object.freeze(
      uniqueStrings(values.flatMap((item) => item.causationIds)),
    ),
    spanIds: Object.freeze(uniqueStrings(values.flatMap((item) => item.spanIds))),
    parentSpanIds: Object.freeze(
      uniqueStrings(values.flatMap((item) => item.parentSpanIds)),
    ),
    checkpointIds: Object.freeze(
      uniqueStrings(values.flatMap((item) => item.checkpointIds)),
    ),
    controlCommandIds: Object.freeze(
      uniqueStrings(values.flatMap((item) => item.controlCommandIds)),
    ),
    graphIds: Object.freeze(uniqueStrings(values.flatMap((item) => item.graphIds))),
    graphRevisions: Object.freeze(
      uniqueNumbers(values.flatMap((item) => item.graphRevisions)),
    ),
    entityRefs: Object.freeze(
      uniqueStrings(values.flatMap((item) => item.entityRefs)),
    ),
    firstSequence:
      firstSequences.length > 0 ? Math.min(...firstSequences) : undefined,
    lastSequence:
      lastSequences.length > 0 ? Math.max(...lastSequences) : undefined,
  })
}

export function evidenceWithGraph(
  evidence: TopologyEvidence,
  graphId: string | undefined,
  graphRevision: number | undefined,
  entityRefs: readonly string[] = [],
): TopologyEvidence {
  return mergeEvidence(
    evidence,
    Object.freeze({
      ...EMPTY_EVIDENCE,
      graphIds: Object.freeze(uniqueStrings([graphId])),
      graphRevisions: Object.freeze(uniqueNumbers([graphRevision])),
      entityRefs: Object.freeze(uniqueStrings(entityRefs)),
      firstSequence: evidence.firstSequence,
      lastSequence: evidence.lastSequence,
    }),
  )
}

export function evidenceForEntity(
  resolver: TopologyEvidenceResolver,
  record: ProjectionRecord,
  entityKind: string,
  entityId: string,
  graphId?: string,
  graphRevision?: number,
): TopologyEvidence {
  const base = resolver.forRecord(record)
  return evidenceWithGraph(
    base,
    graphId,
    graphRevision,
    [`${entityKind}:${entityId}`],
  )
}

export function compareEvidence(
  left: TopologyEvidence,
  right: TopologyEvidence,
): number {
  return (
    (left.firstSequence ?? Number.MAX_SAFE_INTEGER) -
      (right.firstSequence ?? Number.MAX_SAFE_INTEGER) ||
    (left.lastSequence ?? 0) - (right.lastSequence ?? 0) ||
    (left.eventIds[0] ?? "").localeCompare(right.eventIds[0] ?? "")
  )
}

function compareEvents(
  left: CausalEventProjection,
  right: CausalEventProjection,
): number {
  return (
    left.sequence - right.sequence ||
    left.aggregateSequence - right.aggregateSequence ||
    left.committedAt.localeCompare(right.committedAt) ||
    left.eventId.localeCompare(right.eventId)
  )
}

export function emptyEvidence(): TopologyEvidence {
  return EMPTY_EVIDENCE
}
