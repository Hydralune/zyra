import {
  ProjectionDomain,
  ProjectionStatus,
  type CanonicalProjectionState,
  type CausalEventProjection,
  type ProjectionDomainValue,
  type ProjectionEntity,
} from "./contracts.ts"
import { checksumJson } from "./value.ts"

export type IntegritySeverity = "error" | "warning"

export interface ProjectionIntegrityIssue {
  severity: IntegritySeverity
  code: string
  message: string
  taskId?: string
  domain?: ProjectionDomainValue
  entityId?: string
  eventId?: string
}

export interface ProjectionIntegrityReport {
  valid: boolean
  errors: number
  warnings: number
  issues: readonly ProjectionIntegrityIssue[]
  digest: string
  counts: {
    tasks: number
    entities: number
    events: number
    mutations: number
    cursors: number
    tombstones: number
    orphans: number
  }
}

export function auditProjectionIntegrity(
  state: CanonicalProjectionState,
  options: { maxIssues?: number; requireEventForEntity?: boolean } = {},
): ProjectionIntegrityReport {
  const maxIssues = Math.max(1, Math.trunc(options.maxIssues ?? 1_000))
  const issues: ProjectionIntegrityIssue[] = []
  const add = (issue: ProjectionIntegrityIssue) => {
    if (issues.length < maxIssues) issues.push(issue)
  }
  if (state.schema !== "zyra.ui-projection/v1") {
    add({
      severity: "error",
      code: "STATE_SCHEMA",
      message: `Unexpected projection state schema ${state.schema}.`,
    })
  }
  if (!Number.isSafeInteger(state.revision) || state.revision < 0) {
    add({
      severity: "error",
      code: "STATE_REVISION",
      message: `Projection revision ${state.revision} is invalid.`,
    })
  }
  let entityCount = 0
  for (const [domain, table] of entityTables(state)) {
    entityCount += Object.keys(table).length
    for (const [id, entity] of Object.entries(table)) {
      auditEntity(state, domain, id, entity, add, options)
    }
  }
  auditTaskRelationships(state, add)
  auditCausalOrder(state, add)
  auditCausalIndices(state, add)
  auditMutations(state, add)
  auditCursors(state, add)
  auditTombstones(state, add)
  auditOrphans(state, add)
  auditOptimistic(state, add)
  auditPartials(state, add)
  const errors = issues.filter((issue) => issue.severity === "error").length
  const warnings = issues.length - errors
  const counts = {
    tasks: Object.keys(state.tasks).length,
    entities: entityCount,
    events: Object.keys(state.causality.byEvent).length,
    mutations: Object.keys(state.mutations).length,
    cursors: Object.keys(state.cursors).length,
    tombstones: Object.keys(state.tombstones).length,
    orphans: Object.keys(state.orphans).length,
  }
  return Object.freeze({
    valid: errors === 0,
    errors,
    warnings,
    issues: Object.freeze(issues),
    digest: checksumJson({
      revision: state.revision,
      counts,
      cursors: state.cursors,
      eventOrder: state.causality.eventOrder,
      entityRevisions: Object.fromEntries(
        entityTables(state).flatMap(([domain, table]) =>
          Object.values(table).map((entity) => [
            `${domain}:${entity.id}`,
            entity.revision,
          ]),
        ),
      ),
    }),
    counts,
  })
}

function auditEntity(
  state: CanonicalProjectionState,
  domain: ProjectionDomainValue,
  id: string,
  entity: ProjectionEntity,
  add: (issue: ProjectionIntegrityIssue) => void,
  options: { requireEventForEntity?: boolean },
): void {
  if (entity.id !== id) {
    add({
      severity: "error",
      code: "ENTITY_KEY_MISMATCH",
      message: `Entity table key ${id} does not match entity id ${entity.id}.`,
      taskId: entity.taskId,
      domain,
      entityId: id,
    })
  }
  if (entity.domain !== domain) {
    add({
      severity: "error",
      code: "ENTITY_DOMAIN_MISMATCH",
      message: `Entity ${id} declares ${entity.domain} inside ${domain}.`,
      taskId: entity.taskId,
      domain,
      entityId: id,
    })
  }
  if (domain !== ProjectionDomain.TASK && !state.tasks[entity.taskId]) {
    add({
      severity: "error",
      code: "ENTITY_TASK_MISSING",
      message: `Entity ${domain}:${id} references missing task ${entity.taskId}.`,
      taskId: entity.taskId,
      domain,
      entityId: id,
    })
  }
  if (!Number.isSafeInteger(entity.revision) || entity.revision < 1) {
    add({
      severity: "error",
      code: "ENTITY_REVISION",
      message: `Entity ${domain}:${id} has invalid revision ${entity.revision}.`,
      taskId: entity.taskId,
      domain,
      entityId: id,
    })
  }
  if (!Number.isSafeInteger(entity.sequence) || entity.sequence < 0) {
    add({
      severity: "error",
      code: "ENTITY_SEQUENCE",
      message: `Entity ${domain}:${id} has invalid sequence ${entity.sequence}.`,
      taskId: entity.taskId,
      domain,
      entityId: id,
    })
  }
  const lastEvent = state.causality.byEvent[entity.lastEventId]
  if (!lastEvent && options.requireEventForEntity) {
    add({
      severity: "error",
      code: "ENTITY_EVENT_MISSING",
      message: `Entity ${domain}:${id} last event ${entity.lastEventId} is absent.`,
      taskId: entity.taskId,
      domain,
      entityId: id,
      eventId: entity.lastEventId,
    })
  } else if (!lastEvent) {
    add({
      severity: "warning",
      code: "ENTITY_EVENT_RETAINED_OUT",
      message: `Entity ${domain}:${id} outlived retained event ${entity.lastEventId}.`,
      taskId: entity.taskId,
      domain,
      entityId: id,
      eventId: entity.lastEventId,
    })
  } else {
    if (lastEvent.taskId !== entity.taskId) {
      add({
        severity: "error",
        code: "ENTITY_EVENT_TASK_MISMATCH",
        message: `Entity ${domain}:${id} and event ${lastEvent.eventId} disagree on task.`,
        taskId: entity.taskId,
        domain,
        entityId: id,
        eventId: lastEvent.eventId,
      })
    }
    if (lastEvent.sequence > entity.sequence) {
      add({
        severity: "error",
        code: "ENTITY_BEHIND_EVENT",
        message: `Entity ${domain}:${id} sequence ${entity.sequence} is behind its last event ${lastEvent.sequence}.`,
        taskId: entity.taskId,
        domain,
        entityId: id,
        eventId: lastEvent.eventId,
      })
    }
  }
  if (
    entity.status === ProjectionStatus.TOMBSTONED &&
    !entity.terminal
  ) {
    add({
      severity: "error",
      code: "TOMBSTONED_ENTITY_ACTIVE",
      message: `Tombstoned entity ${domain}:${id} is not terminal.`,
      taskId: entity.taskId,
      domain,
      entityId: id,
    })
  }
}

function auditTaskRelationships(
  state: CanonicalProjectionState,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  for (const task of Object.values(state.tasks)) {
    auditRelation(state, task.id, "node", task.activeNodeIds, state.nodes, add)
    auditRelation(state, task.id, "worker", task.workerIds, state.workers, add)
    auditRelation(state, task.id, "artifact", task.artifactIds, state.artifacts, add)
    auditRelation(
      state,
      task.id,
      "permission",
      task.pendingPermissionIds,
      state.permissions,
      add,
    )
    auditRelation(state, task.id, "command", task.commandIds, state.commands, add)
    auditRelation(
      state,
      task.id,
      "recovery",
      task.recoveryIds,
      state.recoveries,
      add,
    )
    auditRelation(state, task.id, "session", task.sessionIds, state.sessions, add)
    for (const permissionId of task.pendingPermissionIds) {
      const permission = state.permissions[permissionId]
      if (permission?.resolvedAt || permission?.terminal) {
        add({
          severity: "error",
          code: "RESOLVED_PERMISSION_PENDING",
          message: `Task ${task.id} retains resolved permission ${permissionId} as pending.`,
          taskId: task.id,
          domain: ProjectionDomain.PERMISSION,
          entityId: permissionId,
        })
      }
    }
  }
}

function auditRelation<T extends ProjectionEntity>(
  _state: CanonicalProjectionState,
  taskId: string,
  domain: ProjectionDomainValue,
  ids: readonly string[],
  table: Readonly<Record<string, T>>,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  const seen = new Set<string>()
  for (const id of ids) {
    if (seen.has(id)) {
      add({
        severity: "error",
        code: "DUPLICATE_TASK_RELATION",
        message: `Task ${taskId} contains duplicate ${domain} relation ${id}.`,
        taskId,
        domain,
        entityId: id,
      })
    }
    seen.add(id)
    const entity = table[id]
    if (!entity) {
      add({
        severity: "error",
        code: "TASK_RELATION_MISSING",
        message: `Task ${taskId} references missing ${domain}:${id}.`,
        taskId,
        domain,
        entityId: id,
      })
    } else if (entity.taskId !== taskId) {
      add({
        severity: "error",
        code: "TASK_RELATION_CROSS_SCOPE",
        message: `Task ${taskId} references ${domain}:${id} owned by ${entity.taskId}.`,
        taskId,
        domain,
        entityId: id,
      })
    }
  }
}

function auditCausalOrder(
  state: CanonicalProjectionState,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  const seen = new Set<string>()
  let previous: CausalEventProjection | undefined
  for (const eventId of state.causality.eventOrder) {
    if (seen.has(eventId)) {
      add({
        severity: "error",
        code: "DUPLICATE_EVENT_ORDER",
        message: `Causal event order contains ${eventId} more than once.`,
        eventId,
      })
      continue
    }
    seen.add(eventId)
    const event = state.causality.byEvent[eventId]
    if (!event) {
      add({
        severity: "error",
        code: "EVENT_ORDER_DANGLING",
        message: `Causal event order references missing ${eventId}.`,
        eventId,
      })
      continue
    }
    if (
      previous &&
      (event.sequence < previous.sequence ||
        (event.sequence === previous.sequence &&
          event.eventId.localeCompare(previous.eventId) < 0))
    ) {
      add({
        severity: "error",
        code: "EVENT_ORDER_REGRESSION",
        message: `Causal event order regresses from ${previous.eventId} to ${event.eventId}.`,
        taskId: event.taskId,
        eventId: event.eventId,
      })
    }
    previous = event
  }
  for (const eventId of Object.keys(state.causality.byEvent)) {
    if (!seen.has(eventId)) {
      add({
        severity: "error",
        code: "EVENT_ORDER_OMISSION",
        message: `Causal event ${eventId} is absent from event order.`,
        eventId,
      })
    }
  }
}

function auditCausalIndices(
  state: CanonicalProjectionState,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  const indices = causalIndices(state)
  for (const [name, index] of indices) {
    for (const [key, eventIds] of Object.entries(index)) {
      const seen = new Set<string>()
      for (const eventId of eventIds) {
        if (seen.has(eventId)) {
          add({
            severity: "error",
            code: "CAUSAL_INDEX_DUPLICATE",
            message: `${name}:${key} contains duplicate event ${eventId}.`,
            eventId,
          })
        }
        seen.add(eventId)
        const event = state.causality.byEvent[eventId]
        if (!event) {
          add({
            severity: "error",
            code: "CAUSAL_INDEX_DANGLING",
            message: `${name}:${key} references missing event ${eventId}.`,
            eventId,
          })
          continue
        }
        if (!eventMatchesIndex(name, key, event)) {
          add({
            severity: "error",
            code: "CAUSAL_INDEX_MISMATCH",
            message: `${name}:${key} does not match event ${eventId}.`,
            taskId: event.taskId,
            eventId,
          })
        }
      }
    }
  }
  for (const event of Object.values(state.causality.byEvent)) {
    auditReverseIndex(state, event, add)
  }
}

function auditReverseIndex(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  const expectations: [string, string | undefined, readonly string[] | undefined][] = [
    ["correlation", event.correlationId, state.causality.byCorrelation[event.correlationId]],
    ["causation", event.causationId, event.causationId ? state.causality.byCausation[event.causationId] : undefined],
    ["span", event.spanId, event.spanId ? state.causality.bySpan[event.spanId] : undefined],
    ["tool", event.toolCallId, event.toolCallId ? state.causality.byToolCall[event.toolCallId] : undefined],
    ["mutation", event.mutationId, state.causality.byMutation[event.mutationId]],
    ["task", event.taskId, state.causality.byTask[event.taskId]],
    ["run", event.runId, state.causality.byRun[event.runId]],
  ]
  for (const [name, key, values] of expectations) {
    if (!key) continue
    if (!values?.includes(event.eventId)) {
      add({
        severity: "error",
        code: "CAUSAL_REVERSE_MISSING",
        message: `Event ${event.eventId} is absent from ${name}:${key}.`,
        taskId: event.taskId,
        eventId: event.eventId,
      })
    }
  }
  for (const artifactId of event.artifactIds) {
    if (!state.causality.byArtifact[artifactId]?.includes(event.eventId)) {
      add({
        severity: "error",
        code: "ARTIFACT_REVERSE_MISSING",
        message: `Event ${event.eventId} is absent from artifact:${artifactId}.`,
        taskId: event.taskId,
        eventId: event.eventId,
      })
    }
  }
}

function eventMatchesIndex(
  name: string,
  key: string,
  event: CausalEventProjection,
): boolean {
  switch (name) {
    case "correlation":
      return event.correlationId === key
    case "causation":
      return event.causationId === key
    case "span":
      return event.spanId === key
    case "parentSpan":
      return event.parentSpanId === key
    case "tool":
      return event.toolCallId === key
    case "artifact":
      return event.artifactIds.includes(key)
    case "checkpoint":
      return event.checkpointId === key
    case "command":
      return event.controlCommandId === key
    case "mutation":
      return event.mutationId === key
    case "failure":
      return event.failureId === key
    case "recovery":
      return event.recoveryId === key
    case "task":
      return event.taskId === key
    case "run":
      return event.runId === key
    case "session":
      return event.sessionId === key
    case "node":
      return event.nodeId === key
    case "worker":
      return event.workerId === key
    default:
      return false
  }
}

function auditMutations(
  state: CanonicalProjectionState,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  for (const mutation of Object.values(state.mutations)) {
    const event = state.causality.byEvent[mutation.eventId]
    if (!event) {
      add({
        severity: "warning",
        code: "MUTATION_EVENT_RETAINED_OUT",
        message: `Mutation ${mutation.id} outlived event ${mutation.eventId}.`,
        taskId: mutation.taskId,
        eventId: mutation.eventId,
      })
    } else if (event.mutationId !== mutation.id) {
      add({
        severity: "error",
        code: "MUTATION_EVENT_MISMATCH",
        message: `Mutation ${mutation.id} and event ${event.eventId} disagree.`,
        taskId: mutation.taskId,
        eventId: event.eventId,
      })
    }
  }
}

function auditCursors(
  state: CanonicalProjectionState,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  for (const [taskId, cursor] of Object.entries(state.cursors)) {
    if (cursor.taskId !== taskId) {
      add({
        severity: "error",
        code: "CURSOR_TASK_MISMATCH",
        message: `Cursor key ${taskId} declares task ${cursor.taskId}.`,
        taskId,
      })
    }
    const runtime = state.runtimes[taskId]
    if (runtime && runtime.generation > cursor.generation) {
      add({
        severity: "error",
        code: "CURSOR_GENERATION_STALE",
        message: `Cursor generation ${cursor.generation} is behind runtime ${runtime.generation}.`,
        taskId,
      })
    }
    if (runtime && cursor.committedSequence < runtime.lastSequence) {
      add({
        severity: "error",
        code: "CURSOR_BEHIND_RUNTIME",
        message: `Cursor ${cursor.committedSequence} is behind runtime ${runtime.lastSequence}.`,
        taskId,
      })
    }
    if (cursor.lastEventId) {
      const event = state.causality.byEvent[cursor.lastEventId]
      if (
        event &&
        (event.taskId !== taskId || event.sequence > cursor.committedSequence)
      ) {
        add({
          severity: "error",
          code: "CURSOR_LAST_EVENT_INVALID",
          message: `Cursor last event ${cursor.lastEventId} is inconsistent.`,
          taskId,
          eventId: cursor.lastEventId,
        })
      }
    }
  }
}

function auditTombstones(
  state: CanonicalProjectionState,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  for (const tombstone of Object.values(state.tombstones)) {
    const entity = tableForDomain(state, tombstone.domain)[tombstone.targetId]
    if (
      entity &&
      entity.sequence <= tombstone.sequence &&
      entity.status !== ProjectionStatus.TOMBSTONED
    ) {
      add({
        severity: "error",
        code: "TOMBSTONE_RESURRECTION",
        message: `Entity ${tombstone.domain}:${tombstone.targetId} was resurrected behind tombstone ${tombstone.eventId}.`,
        taskId: tombstone.taskId,
        domain: tombstone.domain,
        entityId: tombstone.targetId,
        eventId: tombstone.eventId,
      })
    }
  }
}

function auditOrphans(
  state: CanonicalProjectionState,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  for (const orphan of Object.values(state.orphans)) {
    if (state.causality.byEvent[orphan.parentId]) {
      add({
        severity: "error",
        code: "ORPHAN_PARENT_PRESENT",
        message: `Orphan ${orphan.identity} remains after parent ${orphan.parentId} arrived.`,
        taskId: orphan.taskId,
        eventId: orphan.parentId,
      })
    }
    for (const eventId of orphan.eventIds) {
      if (!state.causality.byEvent[eventId]) {
        add({
          severity: "warning",
          code: "ORPHAN_EVENT_RETAINED_OUT",
          message: `Orphan ${orphan.identity} outlived event ${eventId}.`,
          taskId: orphan.taskId,
          eventId,
        })
      }
    }
  }
}

function auditOptimistic(
  state: CanonicalProjectionState,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  for (const optimistic of Object.values(state.optimistic)) {
    const entity = tableForDomain(state, optimistic.domain)[optimistic.entityId]
    if (!entity) {
      add({
        severity: "error",
        code: "OPTIMISTIC_ENTITY_MISSING",
        message: `Optimistic record ${optimistic.key} has no entity.`,
        taskId: optimistic.taskId,
        domain: optimistic.domain,
        entityId: optimistic.entityId,
        eventId: optimistic.eventId,
      })
    } else if (entity.status !== ProjectionStatus.OPTIMISTIC) {
      add({
        severity: "error",
        code: "OPTIMISTIC_STATUS_MISMATCH",
        message: `Optimistic record ${optimistic.key} points to ${entity.status}.`,
        taskId: optimistic.taskId,
        domain: optimistic.domain,
        entityId: optimistic.entityId,
        eventId: optimistic.eventId,
      })
    }
  }
}

function auditPartials(
  state: CanonicalProjectionState,
  add: (issue: ProjectionIntegrityIssue) => void,
): void {
  for (const partial of Object.values(state.partials)) {
    if (partial.firstSequence > partial.lastSequence) {
      add({
        severity: "error",
        code: "PARTIAL_SEQUENCE_REGRESSION",
        message: `Partial ${partial.key} has inverted sequence bounds.`,
        taskId: partial.taskId,
      })
    }
    if (partial.eventIds.length === 0) {
      add({
        severity: "error",
        code: "PARTIAL_EMPTY",
        message: `Partial ${partial.key} has no source event.`,
        taskId: partial.taskId,
      })
    }
  }
}

function entityTables(
  state: CanonicalProjectionState,
): [ProjectionDomainValue, Readonly<Record<string, ProjectionEntity>>][] {
  return [
    [ProjectionDomain.TASK, state.tasks],
    [ProjectionDomain.NODE, state.nodes],
    [ProjectionDomain.WORKER, state.workers],
    [ProjectionDomain.TOOL, state.tools],
    [ProjectionDomain.ARTIFACT, state.artifacts],
    [ProjectionDomain.MEMORY, state.memories],
    [ProjectionDomain.SCHEDULER, state.schedulers],
    [ProjectionDomain.RECOVERY, state.recoveries],
    [ProjectionDomain.COMMAND, state.commands],
    [ProjectionDomain.PERMISSION, state.permissions],
    [ProjectionDomain.OVERLAY, state.overlays],
    [ProjectionDomain.SESSION, state.sessions],
  ]
}

function tableForDomain(
  state: CanonicalProjectionState,
  domain: ProjectionDomainValue,
): Readonly<Record<string, ProjectionEntity>> {
  return entityTables(state).find(([value]) => value === domain)?.[1] ?? {}
}

function causalIndices(
  state: CanonicalProjectionState,
): [string, Readonly<Record<string, readonly string[]>>][] {
  return [
    ["correlation", state.causality.byCorrelation],
    ["causation", state.causality.byCausation],
    ["span", state.causality.bySpan],
    ["parentSpan", state.causality.byParentSpan],
    ["tool", state.causality.byToolCall],
    ["artifact", state.causality.byArtifact],
    ["checkpoint", state.causality.byCheckpoint],
    ["command", state.causality.byControlCommand],
    ["mutation", state.causality.byMutation],
    ["failure", state.causality.byFailure],
    ["recovery", state.causality.byRecovery],
    ["task", state.causality.byTask],
    ["run", state.causality.byRun],
    ["session", state.causality.bySession],
    ["node", state.causality.byNode],
    ["worker", state.causality.byWorker],
  ]
}

export function assertProjectionIntegrity(
  state: CanonicalProjectionState,
  options: { requireEventForEntity?: boolean } = {},
): ProjectionIntegrityReport {
  const report = auditProjectionIntegrity(state, {
    ...options,
    maxIssues: 100,
  })
  if (!report.valid) {
    const first = report.issues.find((issue) => issue.severity === "error")
    throw new Error(
      `Projection integrity failed with ${report.errors} errors: ${first?.code ?? "UNKNOWN"} ${first?.message ?? ""}`,
    )
  }
  return report
}
