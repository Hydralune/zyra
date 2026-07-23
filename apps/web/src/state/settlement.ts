import type { IngressEvent, JsonObject, JsonValue } from "../events/ingress/index.ts"
import {
  ProjectionStatus,
  type MutableProjectionState,
  type OptimisticRecord,
  type PartialRecord,
  type ProjectionDomainValue,
  type ProjectionEventContext,
  type ProjectionLimits,
  type TombstoneRecord,
} from "./contracts.ts"
import {
  domainFromTombstone,
  projectionTable,
  removeProjectedEntity,
} from "./projectors.ts"
import {
  inferDomain,
  inferEntityId,
  optimisticKey,
  partialKey,
  stringValue,
  tombstoneKey,
} from "./value.ts"

export function recordOptimistic(
  context: ProjectionEventContext,
  domain: ProjectionDomainValue,
  entityId: string,
  limits: ProjectionLimits,
): OptimisticRecord {
  const key = optimisticKey(context.event, entityId)
  const record: OptimisticRecord = {
    key,
    taskId: context.event.identity.taskId,
    domain,
    entityId,
    eventId: context.event.eventId,
    createdAtMs: context.now,
    expiresAtMs: context.now + limits.optimisticTtlMs,
  }
  context.state.optimistic[key] = record
  context.changes.entityKeys.add(`optimistic:${key}`)
  return record
}

export function confirmOptimistic(
  context: ProjectionEventContext,
  domain: ProjectionDomainValue,
  entityId: string,
): OptimisticRecord | undefined {
  const event = context.event
  const explicit =
    stringValue(event.inline.optimistic_key) ||
    stringValue(event.inline.optimisticKey) ||
    stringValue(event.metadata.optimistic_key) ||
    stringValue(event.metadata.optimisticKey)
  const candidates = explicit
    ? [explicit]
    : Object.keys(context.state.optimistic).filter((key) => {
        const item = context.state.optimistic[key]
        return (
          item?.taskId === event.identity.taskId &&
          item.domain === domain &&
          item.entityId === entityId
        )
      })
  let confirmed: OptimisticRecord | undefined
  for (const key of candidates) {
    const item = context.state.optimistic[key]
    if (!item) continue
    confirmed = { ...item, confirmedEventId: event.eventId }
    delete context.state.optimistic[key]
    context.changes.entityKeys.add(`optimistic:${key}`)
    const table = projectionTable(context.state, domain)
    const entity = table[entityId]
    if (entity?.status === ProjectionStatus.OPTIMISTIC) {
      table[entityId] = {
        ...entity,
        status: ProjectionStatus.AUTHORITATIVE,
        optimisticKey: undefined,
      }
    }
  }
  return confirmed
}

export function recordPartial(
  context: ProjectionEventContext,
  entityId: string,
  limits: ProjectionLimits,
): PartialRecord {
  const key = partialKey(context.event, entityId)
  const existing = context.state.partials[key]
  const parts = {
    ...(existing?.parts ?? {}),
    ...(context.event.inline.partial &&
    typeof context.event.inline.partial === "object" &&
    !Array.isArray(context.event.inline.partial)
      ? (context.event.inline.partial as JsonObject)
      : context.event.inline),
  }
  const record: PartialRecord = {
    key,
    taskId: context.event.identity.taskId,
    entityId,
    eventIds: existing
      ? [...existing.eventIds, context.event.eventId]
      : [context.event.eventId],
    parts,
    firstSequence: existing?.firstSequence ?? context.event.globalSequence,
    lastSequence: context.event.globalSequence,
    updatedAtMs: context.now,
    expiresAtMs: context.now + limits.partialTtlMs,
  }
  context.state.partials[key] = record
  context.changes.entityKeys.add(`partial:${key}`)
  return record
}

export function settlePartials(
  context: ProjectionEventContext,
  entityId: string,
): PartialRecord[] {
  const settled: PartialRecord[] = []
  for (const [key, item] of Object.entries(context.state.partials)) {
    if (
      item.taskId !== context.event.identity.taskId ||
      item.entityId !== entityId
    ) {
      continue
    }
    settled.push(item)
    delete context.state.partials[key]
    context.changes.entityKeys.add(`partial:${key}`)
  }
  return settled.sort(
    (left, right) =>
      left.firstSequence - right.firstSequence || left.key.localeCompare(right.key),
  )
}

export function mergedPartialPayload(
  records: readonly PartialRecord[],
  finalEvent: IngressEvent,
): JsonObject {
  const result: JsonObject = {}
  for (const record of records) {
    for (const [key, value] of Object.entries(record.parts)) {
      result[key] = mergePartValue(result[key], value)
    }
  }
  for (const [key, value] of Object.entries(finalEvent.inline)) {
    result[key] = mergePartValue(result[key], value)
  }
  return result
}

function mergePartValue(
  existing: JsonValue | undefined,
  incoming: JsonValue,
): JsonValue {
  if (typeof existing === "string" && typeof incoming === "string") {
    if (incoming.startsWith(existing)) return incoming
    if (existing.endsWith(incoming)) return existing
    return existing + incoming
  }
  if (
    existing &&
    incoming &&
    typeof existing === "object" &&
    typeof incoming === "object" &&
    !Array.isArray(existing) &&
    !Array.isArray(incoming)
  ) {
    const result: JsonObject = { ...(existing as JsonObject) }
    for (const [key, value] of Object.entries(incoming as JsonObject)) {
      result[key] = mergePartValue(result[key], value)
    }
    return result
  }
  if (Array.isArray(existing) && Array.isArray(incoming)) {
    return [...existing, ...incoming]
  }
  return incoming
}

export function applyTombstone(
  context: ProjectionEventContext,
  limits: ProjectionLimits,
): TombstoneRecord | undefined {
  const event = context.event
  const targetId = event.tombstoneTargetId
  if (!targetId) return undefined
  const domain = domainFromTombstone(event)
  const key = tombstoneKey(domain, event.identity.taskId, targetId)
  const existing = context.state.tombstones[key]
  if (existing && existing.sequence > event.globalSequence) return existing
  const record: TombstoneRecord = {
    identity: key,
    domain,
    taskId: event.identity.taskId,
    targetId,
    eventId: event.eventId,
    sequence: event.globalSequence,
    generation: context.batch.generation,
    observedAtMs: context.now,
    expiresAtMs: context.now + limits.tombstoneTtlMs,
    reason: event.summary || "canonical tombstone",
  }
  context.state.tombstones[key] = record
  removeProjectedEntity(context, domain, targetId)
  removeOptimisticForEntity(context.state, event.identity.taskId, domain, targetId)
  removePartialsForEntity(context.state, event.identity.taskId, targetId)
  context.changes.entityKeys.add(`tombstone:${key}`)
  return record
}

export function isTombstoned(
  state: MutableProjectionState,
  event: IngressEvent,
  domain = inferDomain(event),
  entityId = inferEntityId(event, domain),
): boolean {
  const record = state.tombstones[
    tombstoneKey(domain, event.identity.taskId, entityId)
  ]
  if (!record) return false
  if (record.sequence >= event.globalSequence) return true
  if (event.tombstoneTargetId) return true
  return false
}

export function clearTombstoneForNewerAuthoritative(
  context: ProjectionEventContext,
  domain: ProjectionDomainValue,
  entityId: string,
): boolean {
  const key = tombstoneKey(domain, context.event.identity.taskId, entityId)
  const record = context.state.tombstones[key]
  if (!record) return false
  if (record.sequence >= context.event.globalSequence) return false
  if (
    context.event.settlement === "partial" ||
    context.event.metadata.optimistic === true ||
    context.event.inline.optimistic === true
  ) {
    return false
  }
  delete context.state.tombstones[key]
  context.changes.entityKeys.add(`tombstone:${key}`)
  return true
}

function removeOptimisticForEntity(
  state: MutableProjectionState,
  taskId: string,
  domain: ProjectionDomainValue,
  entityId: string,
): void {
  for (const [key, item] of Object.entries(state.optimistic)) {
    if (
      item.taskId === taskId &&
      item.domain === domain &&
      item.entityId === entityId
    ) {
      delete state.optimistic[key]
    }
  }
}

function removePartialsForEntity(
  state: MutableProjectionState,
  taskId: string,
  entityId: string,
): void {
  for (const [key, item] of Object.entries(state.partials)) {
    if (item.taskId === taskId && item.entityId === entityId) {
      delete state.partials[key]
    }
  }
}

export function pruneSettlements(
  state: MutableProjectionState,
  now: number,
  limits: ProjectionLimits,
): {
  tombstones: number
  optimistic: number
  partials: number
} {
  let tombstones = 0
  let optimistic = 0
  let partials = 0
  for (const [key, item] of Object.entries(state.tombstones)) {
    if (item.expiresAtMs > now) continue
    delete state.tombstones[key]
    tombstones += 1
  }
  for (const [key, item] of Object.entries(state.optimistic)) {
    if (item.expiresAtMs > now) continue
    delete state.optimistic[key]
    const table = projectionTable(state, item.domain)
    const entity = table[item.entityId]
    if (entity?.status === ProjectionStatus.OPTIMISTIC) {
      table[item.entityId] = {
        ...entity,
        status: ProjectionStatus.STALE,
      }
    }
    optimistic += 1
  }
  for (const [key, item] of Object.entries(state.partials)) {
    if (item.expiresAtMs > now) continue
    delete state.partials[key]
    partials += 1
  }
  tombstones += trimOldest(state.tombstones, limits.maxTombstones)
  optimistic += trimOldest(state.optimistic, limits.maxOptimistic)
  partials += trimOldest(state.partials, limits.maxPartials)
  return { tombstones, optimistic, partials }
}

function trimOldest<T extends { observedAtMs?: number; createdAtMs?: number; updatedAtMs?: number }>(
  table: Record<string, T>,
  limit: number,
): number {
  const entries = Object.entries(table)
  if (entries.length <= limit) return 0
  entries.sort(
    ([leftKey, left], [rightKey, right]) =>
      timestamp(left) - timestamp(right) || leftKey.localeCompare(rightKey),
  )
  const remove = entries.slice(0, entries.length - limit)
  for (const [key] of remove) delete table[key]
  return remove.length
}

function timestamp(value: {
  observedAtMs?: number
  createdAtMs?: number
  updatedAtMs?: number
}): number {
  return value.observedAtMs ?? value.createdAtMs ?? value.updatedAtMs ?? 0
}
