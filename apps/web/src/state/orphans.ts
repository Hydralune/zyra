import type { IngressEvent } from "../events/ingress/index.ts"
import {
  ProjectionStatus,
  type MutableProjectionState,
  type OrphanRecord,
  type ProjectionDomainValue,
  type ProjectionEventContext,
  type ProjectionLimits,
} from "./contracts.ts"
import { projectionTable } from "./projectors.ts"
import { entityKey, inferDomain, inferEntityId } from "./value.ts"

export function orphanIdentity(event: IngressEvent): string | undefined {
  const parent = event.parentEventId
  if (!parent) return undefined
  return `${event.identity.taskId}:${parent}`
}

export function parentExists(
  state: MutableProjectionState,
  event: IngressEvent,
): boolean {
  if (!event.parentEventId) return true
  return Boolean(state.causality.byEvent[event.parentEventId])
}

export function recordOrphan(
  context: ProjectionEventContext,
  limits: ProjectionLimits,
): OrphanRecord | undefined {
  const parentId = context.event.parentEventId
  if (!parentId || parentExists(context.state, context.event)) return undefined
  const identity = `${context.event.identity.taskId}:${parentId}`
  const existing = context.state.orphans[identity]
  const record: OrphanRecord = {
    identity,
    taskId: context.event.identity.taskId,
    parentId,
    eventIds: existing
      ? appendEvent(existing.eventIds, context.event.eventId)
      : [context.event.eventId],
    firstSequence: Math.min(
      existing?.firstSequence ?? context.event.globalSequence,
      context.event.globalSequence,
    ),
    lastSequence: Math.max(
      existing?.lastSequence ?? context.event.globalSequence,
      context.event.globalSequence,
    ),
    observedAtMs: existing?.observedAtMs ?? context.now,
    expiresAtMs: context.now + limits.orphanTtlMs,
  }
  context.state.orphans[identity] = record
  context.changes.entityKeys.add(`orphan:${identity}`)
  markProjectionOrphaned(context)
  return record
}

function appendEvent(values: readonly string[], eventId: string): string[] {
  if (values.includes(eventId)) return [...values]
  return [...values, eventId]
}

function markProjectionOrphaned(context: ProjectionEventContext): void {
  const domain = inferDomain(context.event)
  const id = inferEntityId(context.event, domain)
  const table = projectionTable(context.state, domain)
  const entity = table[id]
  if (!entity) return
  table[id] = {
    ...entity,
    status: ProjectionStatus.ORPHANED,
    parentId: context.event.parentEventId,
  }
  context.changes.domains.add(domain)
  context.changes.entityKeys.add(entityKey(domain, id))
}

export function resolveOrphansForParent(
  context: ProjectionEventContext,
): readonly string[] {
  const identity = `${context.event.identity.taskId}:${context.event.eventId}`
  const record = context.state.orphans[identity]
  if (!record) return []
  delete context.state.orphans[identity]
  context.changes.entityKeys.add(`orphan:${identity}`)
  const resolved: string[] = []
  for (const eventId of record.eventIds) {
    const causal = context.state.causality.byEvent[eventId]
    if (!causal) continue
    for (const ref of causal.entityRefs) {
      const separator = ref.indexOf(":")
      if (separator <= 0) continue
      const domain = ref.slice(0, separator) as ProjectionDomainValue
      const id = ref.slice(separator + 1)
      const table = projectionTable(context.state, domain)
      const entity = table[id]
      if (!entity || entity.status !== ProjectionStatus.ORPHANED) continue
      if (entity.parentId !== context.event.eventId) continue
      table[id] = {
        ...entity,
        status: entity.terminal
          ? ProjectionStatus.FINAL
          : ProjectionStatus.AUTHORITATIVE,
      }
      context.changes.domains.add(domain)
      context.changes.entityKeys.add(entityKey(domain, id))
    }
    resolved.push(eventId)
  }
  return resolved
}

export function pruneOrphans(
  state: MutableProjectionState,
  now: number,
  limits: ProjectionLimits,
): number {
  let removed = 0
  const entries = Object.entries(state.orphans)
  for (const [key, item] of entries) {
    if (item.expiresAtMs > now) continue
    expireOrphan(state, key, item)
    removed += 1
  }
  const remaining = Object.entries(state.orphans)
  if (remaining.length <= limits.maxOrphans) return removed
  remaining.sort(
    ([leftKey, left], [rightKey, right]) =>
      left.observedAtMs - right.observedAtMs ||
      left.firstSequence - right.firstSequence ||
      leftKey.localeCompare(rightKey),
  )
  for (const [key, item] of remaining.slice(
    0,
    remaining.length - limits.maxOrphans,
  )) {
    expireOrphan(state, key, item)
    removed += 1
  }
  return removed
}

function expireOrphan(
  state: MutableProjectionState,
  key: string,
  item: OrphanRecord,
): void {
  delete state.orphans[key]
  for (const eventId of item.eventIds) {
    const causal = state.causality.byEvent[eventId]
    if (!causal) continue
    for (const ref of causal.entityRefs) {
      const separator = ref.indexOf(":")
      if (separator <= 0) continue
      const domain = ref.slice(0, separator) as ProjectionDomainValue
      const id = ref.slice(separator + 1)
      const table = projectionTable(state, domain)
      const entity = table[id]
      if (!entity || entity.status !== ProjectionStatus.ORPHANED) continue
      table[id] = { ...entity, status: ProjectionStatus.STALE }
    }
  }
}

export function countTaskOrphans(
  state: MutableProjectionState,
  taskId: string,
): number {
  return Object.values(state.orphans).filter((item) => item.taskId === taskId)
    .length
}
