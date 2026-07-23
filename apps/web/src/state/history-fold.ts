import type { IngressBatch, IngressEvent } from "../events/ingress/index.ts"
import {
  ProjectionDomain,
  ProjectionError,
  type CanonicalProjectionState,
  type MutableProjectionState,
  type ProjectionCursor,
} from "./contracts.ts"
import { inferDomain, inferEntityId, tombstoneKey } from "./value.ts"

export type HistoryFoldDisposition =
  | "accept"
  | "duplicate"
  | "stale_generation"
  | "stale_cursor"
  | "stale_entity"
  | "tombstoned"

export interface HistoryFoldEntry {
  event: IngressEvent
  disposition: HistoryFoldDisposition
  reason: string
}

export interface HistoryFoldPlan {
  taskId: string
  generation: number
  entries: readonly HistoryFoldEntry[]
  accepted: readonly IngressEvent[]
  duplicates: readonly string[]
  stale: readonly string[]
  tombstoned: readonly string[]
  previousCursor?: ProjectionCursor
  nextSequence: number
  highWatermark: number
  caughtUp: boolean
}

export function planHistoryFold(
  state: CanonicalProjectionState | MutableProjectionState,
  batch: IngressBatch,
): HistoryFoldPlan {
  validateHistoryBatch(batch)
  const events = canonicalEventOrder(batch.events)
  const entries: HistoryFoldEntry[] = []
  const accepted: IngressEvent[] = []
  const duplicates: string[] = []
  const stale: string[] = []
  const tombstoned: string[] = []
  const observedIds = new Map<string, IngressEvent>()
  for (const event of events) {
    const sameBatch = observedIds.get(event.eventId)
    if (sameBatch) {
      if (sameBatch.contentDigest !== event.contentDigest) {
        throw new ProjectionError(
          "EVENT_ID_COLLISION",
          `Ingress batch contains conflicting payloads for event ${event.eventId}.`,
          {
            taskId: batch.taskId,
            eventId: event.eventId,
            firstDigest: sameBatch.contentDigest,
            secondDigest: event.contentDigest,
          },
        )
      }
      entries.push({
        event,
        disposition: "duplicate",
        reason: "The same event identity was already observed in this batch.",
      })
      duplicates.push(event.eventId)
      continue
    }
    observedIds.set(event.eventId, event)
    const decision = classifyHistoryEvent(state, batch, event)
    entries.push({ event, ...decision })
    if (decision.disposition === "accept") accepted.push(event)
    else if (decision.disposition === "duplicate") duplicates.push(event.eventId)
    else if (decision.disposition === "tombstoned") {
      tombstoned.push(event.eventId)
    } else {
      stale.push(event.eventId)
    }
  }
  const previousCursor = state.cursors[batch.taskId]
  const nextSequence = Math.max(
    previousCursor?.committedSequence ?? 0,
    batch.sequence,
    ...accepted.map((event) => event.globalSequence),
  )
  return Object.freeze({
    taskId: batch.taskId,
    generation: batch.generation,
    entries: Object.freeze(entries),
    accepted: Object.freeze(accepted),
    duplicates: Object.freeze(duplicates),
    stale: Object.freeze(stale),
    tombstoned: Object.freeze(tombstoned),
    previousCursor,
    nextSequence,
    highWatermark: Math.max(
      previousCursor?.highWatermark ?? 0,
      batch.highWatermark,
      nextSequence,
    ),
    caughtUp: batch.caughtUp,
  })
}

export function classifyHistoryEvent(
  state: CanonicalProjectionState | MutableProjectionState,
  batch: IngressBatch,
  event: IngressEvent,
): Pick<HistoryFoldEntry, "disposition" | "reason"> {
  if (state.causality.byEvent[event.eventId]) {
    return {
      disposition: "duplicate",
      reason: "The event identity is already present in the canonical causal index.",
    }
  }
  const runtime = state.runtimes[batch.taskId]
  if (runtime && batch.generation < runtime.generation) {
    return {
      disposition: "stale_generation",
      reason: `Batch generation ${batch.generation} is older than runtime generation ${runtime.generation}.`,
    }
  }
  const cursor = state.cursors[batch.taskId]
  if (cursor && batch.generation < cursor.generation) {
    return {
      disposition: "stale_generation",
      reason: `Batch generation ${batch.generation} is older than cursor generation ${cursor.generation}.`,
    }
  }
  if (
    cursor &&
    batch.generation === cursor.generation &&
    event.globalSequence <= cursor.committedSequence
  ) {
    return {
      disposition: "stale_cursor",
      reason: `Event sequence ${event.globalSequence} does not advance committed cursor ${cursor.committedSequence}.`,
    }
  }
  const domain = inferDomain(event)
  const entityId = inferEntityId(event, domain)
  const tombstone = state.tombstones[
    tombstoneKey(domain, event.identity.taskId, entityId)
  ]
  if (
    tombstone &&
    tombstone.sequence >= event.globalSequence &&
    !event.tombstoneTargetId
  ) {
    return {
      disposition: "tombstoned",
      reason: `Tombstone ${tombstone.eventId} at sequence ${tombstone.sequence} supersedes this history event.`,
    }
  }
  const entity = entityFromDomain(state, domain, entityId)
  if (
    entity &&
    event.globalSequence < entity.sequence &&
    event.settlement !== "partial"
  ) {
    return {
      disposition: "stale_entity",
      reason: `Entity ${domain}:${entityId} has a newer canonical sequence ${entity.sequence}.`,
    }
  }
  return {
    disposition: "accept",
    reason: "The event advances canonical projection history.",
  }
}

export function canonicalEventOrder(
  events: readonly IngressEvent[],
): IngressEvent[] {
  return [...events].sort(
    (left, right) =>
      left.globalSequence - right.globalSequence ||
      left.aggregateSequence - right.aggregateSequence ||
      left.producerSequence - right.producerSequence ||
      left.committedAt.localeCompare(right.committedAt) ||
      left.eventId.localeCompare(right.eventId),
  )
}

export function mergeHistoryAndLiveBatches(
  history: readonly IngressBatch[],
  live: readonly IngressBatch[],
): IngressBatch[] {
  const all = [...history, ...live]
  if (all.length === 0) return []
  const taskId = all[0]!.taskId
  if (all.some((batch) => batch.taskId !== taskId)) {
    throw new ProjectionError(
      "CROSS_TASK_HISTORY_FOLD",
      "History and live batches must belong to the same task.",
      { taskId },
    )
  }
  const generations = [...new Set(all.map((batch) => batch.generation))].sort(
    (left, right) => left - right,
  )
  const result: IngressBatch[] = []
  for (const generation of generations) {
    const batches = all
      .filter((batch) => batch.generation === generation)
      .sort(compareBatches)
    const eventsById = new Map<string, IngressEvent>()
    const sourcesById = new Map<string, IngressBatch>()
    for (const batch of batches) {
      for (const event of canonicalEventOrder(batch.events)) {
        const previous = eventsById.get(event.eventId)
        if (previous && previous.contentDigest !== event.contentDigest) {
          throw new ProjectionError(
            "EVENT_ID_COLLISION",
            `History/live merge observed conflicting event ${event.eventId}.`,
            {
              taskId,
              eventId: event.eventId,
              firstDigest: previous.contentDigest,
              secondDigest: event.contentDigest,
            },
          )
        }
        if (!previous || compareEventFreshness(previous, event) <= 0) {
          eventsById.set(event.eventId, event)
          sourcesById.set(event.eventId, batch)
        }
      }
    }
    const ordered = canonicalEventOrder([...eventsById.values()])
    if (ordered.length === 0) continue
    const source = sourcesById.get(ordered.at(-1)!.eventId) ?? batches.at(-1)!
    result.push({
      taskId,
      generation,
      events: Object.freeze(ordered),
      receipts: Object.freeze(
        batches.flatMap((batch) => batch.receipts),
      ),
      cursor: source.cursor,
      fromSequence: Math.min(...batches.map((batch) => batch.fromSequence)),
      sequence: Math.max(
        ...batches.map((batch) => batch.sequence),
        ...ordered.map((event) => event.globalSequence),
      ),
      highWatermark: Math.max(
        ...batches.map((batch) => batch.highWatermark),
      ),
      receivedAt: Math.max(...batches.map((batch) => batch.receivedAt)),
      transport: source.transport,
      snapshot: batches.some((batch) => batch.snapshot),
      caughtUp: batches.some((batch) => batch.caughtUp),
    })
  }
  return result.sort(compareBatches)
}

function compareBatches(left: IngressBatch, right: IngressBatch): number {
  return (
    left.generation - right.generation ||
    left.fromSequence - right.fromSequence ||
    left.sequence - right.sequence ||
    left.receivedAt - right.receivedAt
  )
}

function compareEventFreshness(left: IngressEvent, right: IngressEvent): number {
  const settlementRank = { partial: 0, atomic: 1, final: 2, tombstone: 3 }
  return (
    left.globalSequence - right.globalSequence ||
    left.aggregateSequence - right.aggregateSequence ||
    settlementRank[left.settlement] - settlementRank[right.settlement] ||
    left.committedAt.localeCompare(right.committedAt)
  )
}

function entityFromDomain(
  state: CanonicalProjectionState | MutableProjectionState,
  domain: string,
  id: string,
) {
  switch (domain) {
    case ProjectionDomain.TASK:
      return state.tasks[id]
    case ProjectionDomain.NODE:
      return state.nodes[id]
    case ProjectionDomain.WORKER:
      return state.workers[id]
    case ProjectionDomain.TOOL:
      return state.tools[id]
    case ProjectionDomain.ARTIFACT:
      return state.artifacts[id]
    case ProjectionDomain.MEMORY:
      return state.memories[id]
    case ProjectionDomain.SCHEDULER:
      return state.schedulers[id]
    case ProjectionDomain.RECOVERY:
      return state.recoveries[id]
    case ProjectionDomain.COMMAND:
      return state.commands[id]
    case ProjectionDomain.PERMISSION:
      return state.permissions[id]
    case ProjectionDomain.OVERLAY:
      return state.overlays[id]
    case ProjectionDomain.SESSION:
      return state.sessions[id]
    default:
      return undefined
  }
}

function validateHistoryBatch(batch: IngressBatch): void {
  if (!batch.taskId.trim()) {
    throw new ProjectionError(
      "INVALID_HISTORY_BATCH",
      "History fold requires a non-empty task id.",
    )
  }
  if (!Number.isSafeInteger(batch.generation) || batch.generation < 0) {
    throw new ProjectionError(
      "INVALID_HISTORY_BATCH",
      "History fold generation must be a non-negative safe integer.",
      { generation: batch.generation },
    )
  }
  for (const event of batch.events) {
    if (event.identity.taskId !== batch.taskId) {
      throw new ProjectionError(
        "CROSS_TASK_HISTORY_EVENT",
        `History event ${event.eventId} belongs to ${event.identity.taskId}.`,
        {
          taskId: batch.taskId,
          eventTaskId: event.identity.taskId,
          eventId: event.eventId,
        },
      )
    }
  }
}
