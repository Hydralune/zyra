import {
  EntityLifecycle,
  ProjectionDomain,
  ProjectionStatus,
  type MutableProjectionState,
  type ProjectionEntity,
  type ProjectionLimits,
} from "./contracts.ts"
import { trimCausalIndex } from "./causality.ts"
import { countTaskOrphans, pruneOrphans } from "./orphans.ts"
import { projectionTable } from "./projectors.ts"
import { pruneSettlements } from "./settlement.ts"
import { parseTimestamp } from "./value.ts"

export interface RetentionResult {
  events: number
  entities: number
  tombstones: number
  orphans: number
  optimistic: number
  partials: number
  mutations: number
  tasks: number
}

export function enforceRetention(
  state: MutableProjectionState,
  limits: ProjectionLimits,
  now: number,
): RetentionResult {
  const protectedEntities = collectProtectedEntityKeys(state)
  const protectedEvents = collectProtectedEventIds(state, protectedEntities)
  const settlements = pruneSettlements(state, now, limits)
  const orphans = pruneOrphans(state, now, limits)
  const events = trimCausalIndex(state, limits, protectedEvents)
  const mutations = trimMutations(state, limits, protectedEvents)
  let entities = 0
  for (const domain of Object.values(ProjectionDomain)) {
    if (domain === ProjectionDomain.EVENT || domain === ProjectionDomain.TASK) {
      continue
    }
    entities += trimEntityTable(
      state,
      domain,
      limits.maxEntitiesPerDomain,
      protectedEntities,
    )
  }
  const tasks = trimInactiveTasks(state, limits, now, protectedEntities)
  state.diagnostics = {
    ...state.diagnostics,
    retentionRuns: state.diagnostics.retentionRuns + 1,
    evictedEvents: state.diagnostics.evictedEvents + events,
    evictedEntities: state.diagnostics.evictedEntities + entities + tasks,
  }
  for (const [taskId, runtime] of Object.entries(state.runtimes)) {
    state.runtimes[taskId] = {
      ...runtime,
      orphanCount: countTaskOrphans(state, taskId),
      tombstoneCount: Object.values(state.tombstones).filter(
        (item) => item.taskId === taskId,
      ).length,
    }
  }
  return {
    events,
    entities,
    tombstones: settlements.tombstones,
    orphans,
    optimistic: settlements.optimistic,
    partials: settlements.partials,
    mutations,
    tasks,
  }
}

export function collectProtectedEntityKeys(
  state: MutableProjectionState,
): Set<string> {
  const protectedKeys = new Set<string>()
  for (const task of Object.values(state.tasks)) {
    if (!task.terminal || state.runtimes[task.id]?.pinned) {
      protectedKeys.add(`task:${task.id}`)
      task.activeNodeIds.forEach((id) => protectedKeys.add(`node:${id}`))
      task.workerIds.forEach((id) => protectedKeys.add(`worker:${id}`))
      task.sessionIds.forEach((id) => protectedKeys.add(`session:${id}`))
      task.pendingPermissionIds.forEach((id) =>
        protectedKeys.add(`permission:${id}`),
      )
      task.commandIds.forEach((id) => {
        const command = state.commands[id]
        if (command && !command.terminal) protectedKeys.add(`command:${id}`)
      })
      task.recoveryIds.forEach((id) => {
        const recovery = state.recoveries[id]
        if (recovery && !recovery.terminal) protectedKeys.add(`recovery:${id}`)
      })
    }
  }
  for (const worker of Object.values(state.workers)) {
    if (!worker.terminal || worker.lifecycle === EntityLifecycle.RECOVERING) {
      protectedKeys.add(`worker:${worker.id}`)
      if (worker.activeToolCallId) {
        protectedKeys.add(`tool:${worker.activeToolCallId}`)
      }
    }
  }
  for (const permission of Object.values(state.permissions)) {
    if (!permission.resolvedAt && !permission.terminal) {
      protectedKeys.add(`permission:${permission.id}`)
      if (permission.toolCallId) protectedKeys.add(`tool:${permission.toolCallId}`)
    }
  }
  for (const command of Object.values(state.commands)) {
    if (!command.terminal) protectedKeys.add(`command:${command.id}`)
  }
  for (const overlay of Object.values(state.overlays)) {
    if (!overlay.closedAt) protectedKeys.add(`overlay:${overlay.id}`)
  }
  for (const item of Object.values(state.optimistic)) {
    protectedKeys.add(`${item.domain}:${item.entityId}`)
  }
  for (const item of Object.values(state.partials)) {
    for (const domain of Object.values(ProjectionDomain)) {
      if (domain === ProjectionDomain.EVENT) continue
      const table = projectionTable(state, domain)
      if (table[item.entityId]) protectedKeys.add(`${domain}:${item.entityId}`)
    }
  }
  return protectedKeys
}

export function collectProtectedEventIds(
  state: MutableProjectionState,
  protectedEntities = collectProtectedEntityKeys(state),
): Set<string> {
  const eventIds = new Set<string>()
  for (const key of protectedEntities) {
    const separator = key.indexOf(":")
    if (separator <= 0) continue
    const domain = key.slice(0, separator) as keyof typeof ProjectionDomain
    const id = key.slice(separator + 1)
    const normalized = Object.values(ProjectionDomain).find(
      (value) => value === domain.toLowerCase(),
    )
    if (!normalized || normalized === ProjectionDomain.EVENT) continue
    const entity = projectionTable(state, normalized)[id]
    if (!entity) continue
    eventIds.add(entity.firstEventId)
    eventIds.add(entity.lastEventId)
    const causal = state.causality.byEvent[entity.lastEventId]
    if (causal?.causationId) eventIds.add(causal.causationId)
  }
  for (const item of Object.values(state.optimistic)) eventIds.add(item.eventId)
  for (const item of Object.values(state.partials)) {
    item.eventIds.forEach((eventId) => eventIds.add(eventId))
  }
  for (const item of Object.values(state.orphans)) {
    item.eventIds.forEach((eventId) => eventIds.add(eventId))
    eventIds.add(item.parentId)
  }
  for (const item of Object.values(state.tombstones)) eventIds.add(item.eventId)
  return eventIds
}

function trimMutations(
  state: MutableProjectionState,
  limits: ProjectionLimits,
  protectedEvents: ReadonlySet<string>,
): number {
  const entries = Object.entries(state.mutations)
  if (entries.length <= limits.maxMutations) return 0
  entries.sort(
    ([leftKey, left], [rightKey, right]) =>
      left.sequence - right.sequence ||
      left.createdAt.localeCompare(right.createdAt) ||
      leftKey.localeCompare(rightKey),
  )
  let removeCount = entries.length - limits.maxMutations
  let removed = 0
  for (const [key, mutation] of entries) {
    if (removeCount <= 0) break
    if (protectedEvents.has(mutation.eventId)) continue
    delete state.mutations[key]
    removeCount -= 1
    removed += 1
  }
  return removed
}

function trimEntityTable(
  state: MutableProjectionState,
  domain: Exclude<
    (typeof ProjectionDomain)[keyof typeof ProjectionDomain],
    "event" | "task"
  >,
  limit: number,
  protectedKeys: ReadonlySet<string>,
): number {
  const table = projectionTable(state, domain)
  const entries = Object.entries(table)
  if (entries.length <= limit) return 0
  entries.sort(([, left], [, right]) => compareEviction(left, right))
  let removeCount = entries.length - limit
  let removed = 0
  for (const [id, entity] of entries) {
    if (removeCount <= 0) break
    if (protectedKeys.has(`${domain}:${id}`)) continue
    if (!entity.terminal && entity.status !== ProjectionStatus.STALE) continue
    delete table[id]
    unlinkEvictedEntity(state, domain, id, entity.taskId)
    removeCount -= 1
    removed += 1
  }
  return removed
}

function compareEviction(left: ProjectionEntity, right: ProjectionEntity): number {
  const leftProtected = left.terminal ? 0 : 1
  const rightProtected = right.terminal ? 0 : 1
  return (
    leftProtected - rightProtected ||
    parseTimestamp(left.updatedAt, 0) - parseTimestamp(right.updatedAt, 0) ||
    left.sequence - right.sequence ||
    left.id.localeCompare(right.id)
  )
}

function unlinkEvictedEntity(
  state: MutableProjectionState,
  domain: string,
  id: string,
  taskId: string,
): void {
  const task = state.tasks[taskId]
  if (!task) return
  if (domain === ProjectionDomain.NODE) {
    state.tasks[taskId] = {
      ...task,
      activeNodeIds: task.activeNodeIds.filter((value) => value !== id),
    }
  } else if (domain === ProjectionDomain.WORKER) {
    state.tasks[taskId] = {
      ...task,
      workerIds: task.workerIds.filter((value) => value !== id),
    }
  } else if (domain === ProjectionDomain.ARTIFACT) {
    state.tasks[taskId] = {
      ...task,
      artifactIds: task.artifactIds.filter((value) => value !== id),
    }
  } else if (domain === ProjectionDomain.PERMISSION) {
    state.tasks[taskId] = {
      ...task,
      pendingPermissionIds: task.pendingPermissionIds.filter(
        (value) => value !== id,
      ),
    }
  } else if (domain === ProjectionDomain.COMMAND) {
    state.tasks[taskId] = {
      ...task,
      commandIds: task.commandIds.filter((value) => value !== id),
    }
  } else if (domain === ProjectionDomain.RECOVERY) {
    state.tasks[taskId] = {
      ...task,
      recoveryIds: task.recoveryIds.filter((value) => value !== id),
    }
  } else if (domain === ProjectionDomain.SESSION) {
    state.tasks[taskId] = {
      ...task,
      sessionIds: task.sessionIds.filter((value) => value !== id),
    }
  }
}

function trimInactiveTasks(
  state: MutableProjectionState,
  limits: ProjectionLimits,
  now: number,
  protectedKeys: ReadonlySet<string>,
): number {
  const inactive = Object.values(state.tasks)
    .filter((task) => task.terminal)
    .filter((task) => !state.runtimes[task.id]?.pinned)
    .filter((task) => !protectedKeys.has(`task:${task.id}`))
    .sort(compareEviction)
  const stale = inactive.filter(
    (task) =>
      now - parseTimestamp(task.updatedAt, now) >= limits.inactiveTaskTtlMs,
  )
  const overflow = inactive.slice(
    0,
    Math.max(0, inactive.length - limits.maxInactiveTasks),
  )
  const remove = new Set([...stale, ...overflow].map((task) => task.id))
  for (const taskId of remove) dropTask(state, taskId)
  return remove.size
}

export function dropTask(state: MutableProjectionState, taskId: string): void {
  delete state.tasks[taskId]
  delete state.runtimes[taskId]
  delete state.cursors[taskId]
  for (const domain of Object.values(ProjectionDomain)) {
    if (domain === ProjectionDomain.EVENT || domain === ProjectionDomain.TASK) {
      continue
    }
    const table = projectionTable(state, domain)
    for (const [id, entity] of Object.entries(table)) {
      if (entity.taskId === taskId) delete table[id]
    }
  }
  for (const [id, mutation] of Object.entries(state.mutations)) {
    if (mutation.taskId === taskId) delete state.mutations[id]
  }
  for (const [id, item] of Object.entries(state.tombstones)) {
    if (item.taskId === taskId) delete state.tombstones[id]
  }
  for (const [id, item] of Object.entries(state.orphans)) {
    if (item.taskId === taskId) delete state.orphans[id]
  }
  for (const [id, item] of Object.entries(state.optimistic)) {
    if (item.taskId === taskId) delete state.optimistic[id]
  }
  for (const [id, item] of Object.entries(state.partials)) {
    if (item.taskId === taskId) delete state.partials[id]
  }
}
