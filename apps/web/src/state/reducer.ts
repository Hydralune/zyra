import type { IngressBatch, IngressEvent, JsonObject } from "../events/ingress/index.ts"
import {
  ProjectionError,
  ProjectionStatus,
  type ApplyBatchOptions,
  type CanonicalProjectionState,
  type MutableProjectionState,
  type ProjectionChangeSet,
  type ProjectionEventContext,
  type ProjectionLimits,
  type ProjectionTransactionResult,
} from "./contracts.ts"
import { recordCausality, recordMutation } from "./causality.ts"
import {
  countTaskOrphans,
  parentExists,
  recordOrphan,
  resolveOrphansForParent,
} from "./orphans.ts"
import { projectEvent, projectionTable } from "./projectors.ts"
import { enforceRetention } from "./retention.ts"
import {
  applyTombstone,
  clearTombstoneForNewerAuthoritative,
  confirmOptimistic,
  isTombstoned,
  mergedPartialPayload,
  recordOptimistic,
  recordPartial,
  settlePartials,
} from "./settlement.ts"
import { planHistoryFold } from "./history-fold.ts"
import {
  cloneProjectionState,
  freezeProjectionState,
  taskRuntime,
  updateTaskRuntime,
} from "./state.ts"
import {
  inferDomain,
  inferEntityId,
  parseTimestamp,
} from "./value.ts"

interface MutableChanges {
  taskIds: Set<string>
  domains: Set<
    "task" | "node" | "worker" | "tool" | "artifact" | "memory" | "scheduler" | "recovery" | "command" | "permission" | "overlay" | "session" | "event"
  >
  entityKeys: Set<string>
  eventIds: Set<string>
  causalKeys: Set<string>
  cursorTaskIds: Set<string>
  diagnostic: boolean
}

export class ProjectionReducer {
  readonly #limits: ProjectionLimits
  readonly #now: () => number
  #disabled = false

  constructor(limits: ProjectionLimits, now: () => number = Date.now) {
    this.#limits = limits
    this.#now = now
  }

  disable(): void {
    this.#disabled = true
  }

  enable(): void {
    this.#disabled = false
  }

  get disabled(): boolean {
    return this.#disabled
  }

  apply(
    current: CanonicalProjectionState,
    batch: IngressBatch,
    options: ApplyBatchOptions = {},
  ): ProjectionTransactionResult {
    if (this.#disabled) {
      throw new ProjectionError(
        "PROJECTION_DISABLED",
        "Canonical projection reduction is disabled.",
        { taskId: batch.taskId },
      )
    }
    if (
      options.expectedRevision !== undefined &&
      options.expectedRevision !== current.revision
    ) {
      throw new ProjectionError(
        "REVISION_CONFLICT",
        `Expected projection revision ${options.expectedRevision}, observed ${current.revision}.`,
        {
          expectedRevision: options.expectedRevision,
          observedRevision: current.revision,
          taskId: batch.taskId,
        },
      )
    }
    validateBatch(batch)
    const now = this.#now()
    const state = cloneProjectionState(current)
    const changes = emptyChanges()
    const applied: string[] = []
    const duplicates: string[] = []
    const stale: string[] = []
    const orphans: string[] = []
    const tombstones: string[] = []
    const runtime = taskRuntime(state, batch.taskId, batch.generation, now)
    if (runtime.generation > batch.generation) {
      return staleBatchResult(
        current,
        batch,
        changes,
        "Batch generation is older than the restored projection generation.",
      )
    }
    const fold = planHistoryFold(state, batch)
    for (const entry of fold.entries) {
      const event = entry.event
      if (entry.disposition === "duplicate") {
        duplicates.push(event.eventId)
        incrementDuplicate(state, batch.taskId)
        continue
      }
      if (entry.disposition !== "accept") {
        stale.push(event.eventId)
        incrementStale(state, batch.taskId)
        continue
      }
      const context: ProjectionEventContext = {
        event,
        batch,
        now,
        state,
        changes,
      }
      const parentMissing = !parentExists(state, event)
      const tombstone = applyTombstone(context, this.#limits)
      let projectedEvent = event
      const domain = inferDomain(event)
      const entityId = inferEntityId(event, domain)
      if (!tombstone && !isTombstoned(state, event, domain, entityId)) {
        if (event.settlement === "partial") {
          recordPartial(context, entityId, this.#limits)
        } else if (event.settlement === "final" || event.terminal) {
          const parts = settlePartials(context, entityId)
          if (parts.length > 0) {
            projectedEvent = {
              ...event,
              inline: mergedPartialPayload(parts, event),
            }
            context.event = projectedEvent
          }
        }
        clearTombstoneForNewerAuthoritative(context, domain, entityId)
        const dispatch = projectEvent(context)
        if (
          event.metadata.optimistic === true ||
          event.inline.optimistic === true
        ) {
          recordOptimistic(
            context,
            dispatch.domain,
            dispatch.primaryEntityId,
            this.#limits,
          )
        } else {
          confirmOptimistic(context, dispatch.domain, dispatch.primaryEntityId)
        }
        if (parentMissing) {
          const orphan = recordOrphan(context, this.#limits)
          if (orphan) orphans.push(event.eventId)
        }
      } else if (tombstone) {
        tombstones.push(tombstone.targetId)
      }
      const mutation = recordMutation(context)
      recordCausality(context, mutation)
      resolveOrphansForParent(context)
      updateRuntimeForEvent(state, batch, event, now)
      changes.eventIds.add(event.eventId)
      applied.push(event.eventId)
    }
    commitCursor(state, batch, applied, now, changes)
    enforceRetention(state, this.#limits, now)
    state.revision = current.revision + 1
    state.committedAtMs = now
    state.diagnostics = {
      ...state.diagnostics,
      appliedTransactions: state.diagnostics.appliedTransactions + 1,
      duplicateEvents: state.diagnostics.duplicateEvents + duplicates.length,
      staleEvents: state.diagnostics.staleEvents + stale.length,
    }
    changes.diagnostic = true
    const frozen = freezeProjectionState(state)
    return {
      state: frozen,
      changes: freezeChanges(changes, frozen.revision),
      appliedEventIds: Object.freeze(applied),
      duplicateEventIds: Object.freeze(duplicates),
      staleEventIds: Object.freeze(stale),
      orphanEventIds: Object.freeze(orphans),
      tombstonedEntityIds: Object.freeze(tombstones),
    }
  }

  applyOptimistic(
    current: CanonicalProjectionState,
    event: IngressEvent,
    generation = current.runtimes[event.identity.taskId]?.generation ?? 1,
  ): ProjectionTransactionResult {
    const sequence = Math.max(
      event.globalSequence,
      current.cursors[event.identity.taskId]?.committedSequence ?? 0,
    )
    const optimistic: IngressEvent = {
      ...event,
      globalSequence: sequence,
      settlement: "atomic",
      metadata: { ...event.metadata, optimistic: true },
    }
    const batch: IngressBatch = {
      taskId: event.identity.taskId,
      generation,
      events: [optimistic],
      receipts: [],
      cursor: current.cursors[event.identity.taskId]?.cursor,
      fromSequence: sequence,
      sequence,
      highWatermark: sequence,
      receivedAt: this.#now(),
      transport: "long_poll",
      snapshot: false,
      caughtUp: false,
    }
    return this.apply(current, batch, {
      expectedRevision: current.revision,
      source: "optimistic",
    })
  }
}

function validateBatch(batch: IngressBatch): void {
  if (!batch.taskId.trim()) {
    throw new ProjectionError("INVALID_BATCH", "Ingress batch task id is empty.")
  }
  if (!Number.isSafeInteger(batch.generation) || batch.generation < 0) {
    throw new ProjectionError(
      "INVALID_BATCH",
      "Ingress batch generation must be a non-negative safe integer.",
      { generation: batch.generation },
    )
  }
  let previous = -1
  for (const event of [...batch.events].sort(compareEvents)) {
    if (event.identity.taskId !== batch.taskId) {
      throw new ProjectionError(
        "CROSS_TASK_EVENT",
        `Event ${event.eventId} belongs to ${event.identity.taskId}, not ${batch.taskId}.`,
        {
          taskId: batch.taskId,
          eventTaskId: event.identity.taskId,
          eventId: event.eventId,
        },
      )
    }
    if (!Number.isSafeInteger(event.globalSequence) || event.globalSequence < 0) {
      throw new ProjectionError(
        "INVALID_EVENT_SEQUENCE",
        `Event ${event.eventId} has an invalid global sequence.`,
        { eventId: event.eventId, sequence: event.globalSequence },
      )
    }
    if (event.globalSequence < previous) {
      throw new ProjectionError(
        "UNORDERED_BATCH",
        "Sorted ingress events regressed unexpectedly.",
        { eventId: event.eventId, sequence: event.globalSequence, previous },
      )
    }
    previous = event.globalSequence
  }
}

function compareEvents(left: IngressEvent, right: IngressEvent): number {
  return (
    left.globalSequence - right.globalSequence ||
    left.aggregateSequence - right.aggregateSequence ||
    left.committedAt.localeCompare(right.committedAt) ||
    left.eventId.localeCompare(right.eventId)
  )
}

function updateRuntimeForEvent(
  state: MutableProjectionState,
  batch: IngressBatch,
  event: IngressEvent,
  now: number,
): void {
  const runtime = taskRuntime(state, batch.taskId, batch.generation, now)
  updateTaskRuntime(state, batch.taskId, {
    generation: Math.max(runtime.generation, batch.generation),
    firstSequence:
      runtime.eventCount === 0
        ? event.globalSequence
        : Math.min(runtime.firstSequence, event.globalSequence),
    lastSequence: Math.max(runtime.lastSequence, event.globalSequence),
    eventCount: runtime.eventCount + 1,
    lastEventAtMs: Math.max(
      runtime.lastEventAtMs,
      parseTimestamp(event.committedAt, now),
    ),
    connected: true,
    orphanCount: countTaskOrphans(state, batch.taskId),
  })
}

function incrementDuplicate(state: MutableProjectionState, taskId: string): void {
  const runtime = state.runtimes[taskId]
  if (!runtime) return
  state.runtimes[taskId] = {
    ...runtime,
    duplicateCount: runtime.duplicateCount + 1,
  }
}

function incrementStale(state: MutableProjectionState, taskId: string): void {
  const runtime = state.runtimes[taskId]
  if (!runtime) return
  state.runtimes[taskId] = {
    ...runtime,
    staleCount: runtime.staleCount + 1,
  }
}

function commitCursor(
  state: MutableProjectionState,
  batch: IngressBatch,
  applied: readonly string[],
  now: number,
  changes: MutableChanges,
): void {
  const existing = state.cursors[batch.taskId]
  const sequence = Math.max(
    existing?.committedSequence ?? 0,
    batch.sequence,
    ...batch.events.map((event) => event.globalSequence),
  )
  const generation = Math.max(existing?.generation ?? 0, batch.generation)
  if (
    existing &&
    generation === existing.generation &&
    sequence < existing.committedSequence
  ) {
    throw new ProjectionError(
      "CURSOR_REGRESSION",
      "Projection cursor commit attempted to move backward.",
      {
        taskId: batch.taskId,
        previous: existing.committedSequence,
        next: sequence,
      },
    )
  }
  state.cursors[batch.taskId] = {
    taskId: batch.taskId,
    generation,
    cursor: batch.cursor ?? existing?.cursor,
    committedSequence: sequence,
    highWatermark: Math.max(existing?.highWatermark ?? 0, batch.highWatermark),
    snapshotComplete:
      existing?.snapshotComplete === true ||
      (batch.snapshot && batch.caughtUp) ||
      (!batch.snapshot && batch.caughtUp),
    lastEventId: applied.at(-1) ?? existing?.lastEventId,
    committedAtMs: now,
  }
  changes.cursorTaskIds.add(batch.taskId)
  changes.taskIds.add(batch.taskId)
}

function emptyChanges(): MutableChanges {
  return {
    taskIds: new Set(),
    domains: new Set(),
    entityKeys: new Set(),
    eventIds: new Set(),
    causalKeys: new Set(),
    cursorTaskIds: new Set(),
    diagnostic: false,
  }
}

function freezeChanges(
  changes: MutableChanges,
  revision: number,
): ProjectionChangeSet {
  return Object.freeze({
    revision,
    taskIds: new Set(changes.taskIds),
    domains: new Set(changes.domains),
    entityKeys: new Set(changes.entityKeys),
    eventIds: new Set(changes.eventIds),
    causalKeys: new Set(changes.causalKeys),
    cursorTaskIds: new Set(changes.cursorTaskIds),
    diagnostic: changes.diagnostic,
  })
}

function staleBatchResult(
  current: CanonicalProjectionState,
  batch: IngressBatch,
  changes: MutableChanges,
  message: string,
): ProjectionTransactionResult {
  const stale = batch.events.map((event) => event.eventId)
  const state = {
    ...current,
    diagnostics: Object.freeze({
      ...current.diagnostics,
      rejectedTransactions: current.diagnostics.rejectedTransactions + 1,
      staleEvents: current.diagnostics.staleEvents + stale.length,
      lastError: Object.freeze({
        code: "STALE_BATCH",
        message,
        atMs: Date.now(),
        taskId: batch.taskId,
      }),
    }),
  }
  return {
    state: Object.freeze(state),
    changes: freezeChanges(changes, current.revision),
    appliedEventIds: Object.freeze([]),
    duplicateEventIds: Object.freeze([]),
    staleEventIds: Object.freeze(stale),
    orphanEventIds: Object.freeze([]),
    tombstonedEntityIds: Object.freeze([]),
  }
}
