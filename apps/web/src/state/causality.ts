import type { IngressEvent } from "../events/ingress/index.ts"
import {
  ProjectionDomain,
  type CausalEventProjection,
  type MutableProjectionState,
  type ProjectionEventContext,
  type ProjectionLimits,
} from "./contracts.ts"
import {
  failureId,
  firstString,
  recoveryId,
  statePathId,
  uniqueStrings,
} from "./value.ts"

type MutableStringIndex = Record<string, string[]>

export function mutationIdentity(event: IngressEvent): string {
  const before = event.stateMutation.beforeDigest ?? "-"
  const after = event.stateMutation.afterDigest ?? "-"
  return `${statePathId(event)}:${event.stateMutation.operation}:${before}:${after}`
}

export function eventEntityRefs(
  state: MutableProjectionState,
  event: IngressEvent,
): string[] {
  const refs = [
    `task:${event.identity.taskId}`,
    event.identity.nodeId ? `node:${event.identity.nodeId}` : undefined,
    event.identity.workerId ? `worker:${event.identity.workerId}` : undefined,
    event.identity.toolCallId ? `tool:${event.identity.toolCallId}` : undefined,
    event.identity.sessionId ? `session:${event.identity.sessionId}` : undefined,
    event.identity.controlCommandId
      ? `command:${event.identity.controlCommandId}`
      : undefined,
    ...event.artifactRefs.map((artifact) => `artifact:${artifact.artifactId}`),
  ]
  const recovery = recoveryId(event)
  const failure = failureId(event)
  if (recovery && state.recoveries[recovery]) refs.push(`recovery:${recovery}`)
  if (failure && state.recoveries[failure]) refs.push(`recovery:${failure}`)
  for (const domain of [
    ProjectionDomain.TASK,
    ProjectionDomain.NODE,
    ProjectionDomain.WORKER,
    ProjectionDomain.TOOL,
    ProjectionDomain.ARTIFACT,
    ProjectionDomain.MEMORY,
    ProjectionDomain.SCHEDULER,
    ProjectionDomain.RECOVERY,
    ProjectionDomain.COMMAND,
    ProjectionDomain.PERMISSION,
    ProjectionDomain.OVERLAY,
    ProjectionDomain.SESSION,
  ] as const) {
    const table = tableForDomain(state, domain)
    for (const entity of Object.values(table)) {
      if (entity.lastEventId === event.eventId) refs.push(`${domain}:${entity.id}`)
    }
  }
  return uniqueStrings(refs)
}

function tableForDomain(
  state: MutableProjectionState,
  domain: Exclude<(typeof ProjectionDomain)[keyof typeof ProjectionDomain], "event">,
) {
  switch (domain) {
    case ProjectionDomain.TASK:
      return state.tasks
    case ProjectionDomain.NODE:
      return state.nodes
    case ProjectionDomain.WORKER:
      return state.workers
    case ProjectionDomain.TOOL:
      return state.tools
    case ProjectionDomain.ARTIFACT:
      return state.artifacts
    case ProjectionDomain.MEMORY:
      return state.memories
    case ProjectionDomain.SCHEDULER:
      return state.schedulers
    case ProjectionDomain.RECOVERY:
      return state.recoveries
    case ProjectionDomain.COMMAND:
      return state.commands
    case ProjectionDomain.PERMISSION:
      return state.permissions
    case ProjectionDomain.OVERLAY:
      return state.overlays
    case ProjectionDomain.SESSION:
      return state.sessions
  }
}

export function recordMutation(context: ProjectionEventContext): string {
  const { event, state } = context
  const id = mutationIdentity(event)
  state.mutations[id] = {
    id,
    taskId: event.identity.taskId,
    eventId: event.eventId,
    domain: event.stateMutation.domain,
    operation: event.stateMutation.operation,
    path: [...event.stateMutation.path],
    beforeDigest: event.stateMutation.beforeDigest,
    afterDigest: event.stateMutation.afterDigest,
    effective: event.stateMutation.effective,
    sequence: event.globalSequence,
    createdAt: event.committedAt,
    entityRefs: [],
  }
  context.changes.causalKeys.add(`mutation:${id}`)
  return id
}

export function recordCausality(
  context: ProjectionEventContext,
  mutationId: string,
): CausalEventProjection {
  const { event, state } = context
  const entities = eventEntityRefs(state, event)
  const failure = failureId(event)
  const recovery = recoveryId(event)
  const projection: CausalEventProjection = {
    eventId: event.eventId,
    eventType: event.eventType,
    taskId: event.identity.taskId,
    runId: event.identity.runId,
    sessionId: event.identity.sessionId,
    nodeId: event.identity.nodeId,
    workerId: event.identity.workerId,
    spanId: event.identity.spanId,
    parentSpanId: event.identity.parentSpanId,
    toolCallId: event.identity.toolCallId,
    artifactIds: event.artifactRefs.map((artifact) => artifact.artifactId),
    checkpointId: event.identity.checkpointId,
    controlCommandId: event.identity.controlCommandId,
    correlationId: event.correlationId,
    causationId: event.causationId,
    mutationId,
    failureId: failure,
    recoveryId: recovery,
    sequence: event.globalSequence,
    aggregateSequence: event.aggregateSequence,
    createdAt: event.createdAt,
    committedAt: event.committedAt,
    summary: /^tool_call_(started|completed) for /.test(event.summary)
      ? `${firstString(event.inline, "tool_name") ?? "工具"} · ${event.eventType.endsWith("called") ? "开始执行" : event.eventType.endsWith("failed") ? "执行失败" : "执行完成"}`
      : event.summary,
    terminal: event.terminal,
    effective: event.effective,
    entityRefs: entities,
  }
  state.causality.byEvent[event.eventId] = projection
  insertIndex(state.causality.byCorrelation, event.correlationId, event.eventId)
  insertIndex(state.causality.byCausation, event.causationId, event.eventId)
  insertIndex(state.causality.bySpan, event.identity.spanId, event.eventId)
  insertIndex(
    state.causality.byParentSpan,
    event.identity.parentSpanId,
    event.eventId,
  )
  insertIndex(state.causality.byToolCall, event.identity.toolCallId, event.eventId)
  insertIndex(
    state.causality.byCheckpoint,
    event.identity.checkpointId,
    event.eventId,
  )
  insertIndex(
    state.causality.byControlCommand,
    event.identity.controlCommandId,
    event.eventId,
  )
  insertIndex(state.causality.byMutation, mutationId, event.eventId)
  insertIndex(state.causality.byFailure, failure, event.eventId)
  insertIndex(state.causality.byRecovery, recovery, event.eventId)
  insertIndex(state.causality.byTask, event.identity.taskId, event.eventId)
  insertIndex(state.causality.byRun, event.identity.runId, event.eventId)
  insertIndex(state.causality.bySession, event.identity.sessionId, event.eventId)
  insertIndex(state.causality.byNode, event.identity.nodeId, event.eventId)
  insertIndex(state.causality.byWorker, event.identity.workerId, event.eventId)
  for (const artifact of event.artifactRefs) {
    insertIndex(state.causality.byArtifact, artifact.artifactId, event.eventId)
  }
  insertEventOrder(state, event.eventId)
  const mutation = state.mutations[mutationId]
  if (mutation) state.mutations[mutationId] = { ...mutation, entityRefs: entities }
  markCausalChanges(context, projection)
  return projection
}

function markCausalChanges(
  context: ProjectionEventContext,
  event: CausalEventProjection,
): void {
  const keys = [
    `event:${event.eventId}`,
    `correlation:${event.correlationId}`,
    event.causationId ? `causation:${event.causationId}` : undefined,
    event.spanId ? `span:${event.spanId}` : undefined,
    event.parentSpanId ? `parent-span:${event.parentSpanId}` : undefined,
    event.toolCallId ? `tool:${event.toolCallId}` : undefined,
    event.checkpointId ? `checkpoint:${event.checkpointId}` : undefined,
    event.controlCommandId ? `command:${event.controlCommandId}` : undefined,
    event.failureId ? `failure:${event.failureId}` : undefined,
    event.recoveryId ? `recovery:${event.recoveryId}` : undefined,
    `task:${event.taskId}`,
    `run:${event.runId}`,
    event.sessionId ? `session:${event.sessionId}` : undefined,
    event.nodeId ? `node:${event.nodeId}` : undefined,
    event.workerId ? `worker:${event.workerId}` : undefined,
    ...event.artifactIds.map((id) => `artifact:${id}`),
  ]
  for (const key of keys) {
    if (key) context.changes.causalKeys.add(key)
  }
}

function insertIndex(
  index: MutableStringIndex,
  key: string | undefined,
  eventId: string,
): void {
  if (!key) return
  const current = index[key]
  if (!current) {
    index[key] = [eventId]
    return
  }
  if (current.includes(eventId)) return
  current.push(eventId)
}

function insertEventOrder(state: MutableProjectionState, eventId: string): void {
  if (state.causality.eventOrder.includes(eventId)) return
  const event = state.causality.byEvent[eventId]
  if (!event) return
  let low = 0
  let high = state.causality.eventOrder.length
  while (low < high) {
    const middle = (low + high) >>> 1
    const other = state.causality.byEvent[state.causality.eventOrder[middle]!]
    const compare =
      (other?.sequence ?? 0) - event.sequence ||
      (other?.committedAt ?? "").localeCompare(event.committedAt) ||
      (other?.eventId ?? "").localeCompare(event.eventId)
    if (compare <= 0) low = middle + 1
    else high = middle
  }
  state.causality.eventOrder.splice(low, 0, eventId)
}

export function causalEvents(
  state: MutableProjectionState,
  index: MutableStringIndex,
  key: string,
): CausalEventProjection[] {
  return (index[key] ?? [])
    .map((eventId) => state.causality.byEvent[eventId])
    .filter((event): event is CausalEventProjection => Boolean(event))
    .sort(compareCausalEvents)
}

export function compareCausalEvents(
  left: CausalEventProjection,
  right: CausalEventProjection,
): number {
  return (
    left.sequence - right.sequence ||
    left.committedAt.localeCompare(right.committedAt) ||
    left.eventId.localeCompare(right.eventId)
  )
}

export function causalAncestors(
  state: MutableProjectionState,
  eventId: string,
  limit = 1_000,
): CausalEventProjection[] {
  const result: CausalEventProjection[] = []
  const seen = new Set<string>()
  let current: CausalEventProjection | undefined =
    state.causality.byEvent[eventId]
  while (current && result.length < limit) {
    if (seen.has(current.eventId)) break
    seen.add(current.eventId)
    result.push(current)
    const parent: string | undefined =
      current.causationId ??
      (current.parentSpanId
        ? state.causality.byParentSpan[current.parentSpanId]?.at(-1)
        : undefined)
    current = parent ? state.causality.byEvent[parent] : undefined
  }
  return result
}

export function causalDescendants(
  state: MutableProjectionState,
  eventId: string,
  limit = 5_000,
): CausalEventProjection[] {
  const result: CausalEventProjection[] = []
  const queue = [eventId]
  const seen = new Set<string>()
  while (queue.length > 0 && result.length < limit) {
    const parent = queue.shift()!
    if (seen.has(parent)) continue
    seen.add(parent)
    const children = state.causality.byCausation[parent] ?? []
    for (const childId of children) {
      const child = state.causality.byEvent[childId]
      if (!child || seen.has(childId)) continue
      result.push(child)
      queue.push(childId)
      if (result.length >= limit) break
    }
  }
  return result.sort(compareCausalEvents)
}

export function causalPath(
  state: MutableProjectionState,
  fromEventId: string,
  toEventId: string,
  limit = 5_000,
): CausalEventProjection[] {
  if (fromEventId === toEventId) {
    const event = state.causality.byEvent[fromEventId]
    return event ? [event] : []
  }
  const queue: string[] = [fromEventId]
  const previous = new Map<string, string | undefined>([[fromEventId, undefined]])
  while (queue.length > 0 && previous.size < limit) {
    const current = queue.shift()!
    const event = state.causality.byEvent[current]
    if (!event) continue
    const neighbors = uniqueStrings([
      ...(state.causality.byCausation[current] ?? []),
      event.causationId,
      ...(event.spanId ? state.causality.byParentSpan[event.spanId] ?? [] : []),
      ...(event.parentSpanId ? state.causality.bySpan[event.parentSpanId] ?? [] : []),
    ])
    for (const neighbor of neighbors) {
      if (previous.has(neighbor)) continue
      previous.set(neighbor, current)
      if (neighbor === toEventId) {
        return buildPath(state, previous, toEventId)
      }
      queue.push(neighbor)
    }
  }
  return []
}

function buildPath(
  state: MutableProjectionState,
  previous: Map<string, string | undefined>,
  end: string,
): CausalEventProjection[] {
  const ids: string[] = []
  let current: string | undefined = end
  while (current) {
    ids.push(current)
    current = previous.get(current)
  }
  ids.reverse()
  return ids
    .map((id) => state.causality.byEvent[id])
    .filter((event): event is CausalEventProjection => Boolean(event))
}

export function removeCausalEvent(
  state: MutableProjectionState,
  eventId: string,
): boolean {
  const event = state.causality.byEvent[eventId]
  if (!event) return false
  delete state.causality.byEvent[eventId]
  removeIndex(state.causality.byCorrelation, event.correlationId, eventId)
  removeIndex(state.causality.byCausation, event.causationId, eventId)
  removeIndex(state.causality.bySpan, event.spanId, eventId)
  removeIndex(state.causality.byParentSpan, event.parentSpanId, eventId)
  removeIndex(state.causality.byToolCall, event.toolCallId, eventId)
  removeIndex(state.causality.byCheckpoint, event.checkpointId, eventId)
  removeIndex(state.causality.byControlCommand, event.controlCommandId, eventId)
  removeIndex(state.causality.byMutation, event.mutationId, eventId)
  removeIndex(state.causality.byFailure, event.failureId, eventId)
  removeIndex(state.causality.byRecovery, event.recoveryId, eventId)
  removeIndex(state.causality.byTask, event.taskId, eventId)
  removeIndex(state.causality.byRun, event.runId, eventId)
  removeIndex(state.causality.bySession, event.sessionId, eventId)
  removeIndex(state.causality.byNode, event.nodeId, eventId)
  removeIndex(state.causality.byWorker, event.workerId, eventId)
  for (const artifactId of event.artifactIds) {
    removeIndex(state.causality.byArtifact, artifactId, eventId)
  }
  const orderIndex = state.causality.eventOrder.indexOf(eventId)
  if (orderIndex >= 0) state.causality.eventOrder.splice(orderIndex, 1)
  return true
}

function removeIndex(
  index: MutableStringIndex,
  key: string | undefined,
  eventId: string,
): void {
  if (!key) return
  const current = index[key]
  if (!current) return
  const position = current.indexOf(eventId)
  if (position >= 0) current.splice(position, 1)
  if (current.length === 0) delete index[key]
}

export function trimCausalIndex(
  state: MutableProjectionState,
  limits: ProjectionLimits,
  protectedEventIds: ReadonlySet<string>,
): number {
  let removed = 0
  const taskCounts = new Map<string, number>()
  for (let index = state.causality.eventOrder.length - 1; index >= 0; index -= 1) {
    const eventId = state.causality.eventOrder[index]!
    const event = state.causality.byEvent[eventId]
    if (!event) continue
    taskCounts.set(event.taskId, (taskCounts.get(event.taskId) ?? 0) + 1)
  }
  for (const eventId of [...state.causality.eventOrder]) {
    if (state.causality.eventOrder.length <= limits.maxEvents) break
    if (protectedEventIds.has(eventId)) continue
    if (removeCausalEvent(state, eventId)) removed += 1
  }
  for (const eventId of [...state.causality.eventOrder]) {
    const event = state.causality.byEvent[eventId]
    if (!event) continue
    const count = taskCounts.get(event.taskId) ?? 0
    if (count <= limits.maxEventsPerTask) continue
    if (protectedEventIds.has(eventId)) continue
    if (removeCausalEvent(state, eventId)) {
      removed += 1
      taskCounts.set(event.taskId, count - 1)
    }
  }
  for (const index of allIndices(state)) {
    trimIndexKeys(index, limits.maxIndexEntries)
    trimIndexValues(index, limits.maxIndexValues)
  }
  return removed
}

function allIndices(state: MutableProjectionState): MutableStringIndex[] {
  return [
    state.causality.byCorrelation,
    state.causality.byCausation,
    state.causality.bySpan,
    state.causality.byParentSpan,
    state.causality.byToolCall,
    state.causality.byArtifact,
    state.causality.byCheckpoint,
    state.causality.byControlCommand,
    state.causality.byMutation,
    state.causality.byFailure,
    state.causality.byRecovery,
    state.causality.byTask,
    state.causality.byRun,
    state.causality.bySession,
    state.causality.byNode,
    state.causality.byWorker,
  ]
}

function trimIndexKeys(index: MutableStringIndex, limit: number): void {
  const keys = Object.keys(index)
  if (keys.length <= limit) return
  keys.sort((left, right) => {
    const leftTail = index[left]?.at(-1) ?? ""
    const rightTail = index[right]?.at(-1) ?? ""
    return leftTail.localeCompare(rightTail) || left.localeCompare(right)
  })
  for (const key of keys.slice(0, keys.length - limit)) delete index[key]
}

function trimIndexValues(index: MutableStringIndex, limit: number): void {
  for (const [key, values] of Object.entries(index)) {
    if (values.length <= limit) continue
    index[key] = values.slice(values.length - limit)
  }
}

export function inferFailureAndRecoveryLinks(event: IngressEvent): {
  failureId?: string
  recoveryId?: string
  previousWorkerId?: string
  replacementWorkerId?: string
} {
  return {
    failureId: failureId(event),
    recoveryId: recoveryId(event),
    previousWorkerId:
      firstString(event.inline, "previous_worker_id", "previousWorkerId") ??
      firstString(event.metadata, "previous_worker_id", "previousWorkerId"),
    replacementWorkerId:
      firstString(event.inline, "replacement_worker_id", "replacementWorkerId") ??
      firstString(event.metadata, "replacement_worker_id", "replacementWorkerId"),
  }
}
