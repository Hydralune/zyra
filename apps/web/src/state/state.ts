import {
  PROJECTION_SCHEMA,
  type CanonicalProjectionState,
  type CausalIndex,
  type MutableProjectionState,
  type ProjectionDiagnostics,
  type ProjectionLimits,
  type ProjectionTaskRuntime,
} from "./contracts.ts"

export const DEFAULT_PROJECTION_LIMITS: Readonly<ProjectionLimits> = Object.freeze({
  maxEvents: 20_000,
  maxEventsPerTask: 5_000,
  maxEntitiesPerDomain: 4_000,
  maxInactiveTasks: 40,
  maxTombstones: 4_000,
  maxOrphans: 1_000,
  maxOptimistic: 1_000,
  maxPartials: 2_000,
  maxMutations: 10_000,
  maxIndexEntries: 20_000,
  maxIndexValues: 5_000,
  tombstoneTtlMs: 24 * 60 * 60 * 1_000,
  orphanTtlMs: 15 * 60 * 1_000,
  optimisticTtlMs: 10 * 60 * 1_000,
  partialTtlMs: 30 * 60 * 1_000,
  inactiveTaskTtlMs: 60 * 60 * 1_000,
  persistenceDebounceMs: 50,
})

export function normalizeProjectionLimits(
  input: Partial<ProjectionLimits> = {},
): ProjectionLimits {
  const integer = (
    value: number | undefined,
    fallback: number,
    minimum: number,
    maximum: number,
  ) => {
    if (!Number.isFinite(value)) return fallback
    return Math.min(maximum, Math.max(minimum, Math.trunc(value!)))
  }
  return {
    maxEvents: integer(input.maxEvents, DEFAULT_PROJECTION_LIMITS.maxEvents, 100, 1_000_000),
    maxEventsPerTask: integer(
      input.maxEventsPerTask,
      DEFAULT_PROJECTION_LIMITS.maxEventsPerTask,
      50,
      250_000,
    ),
    maxEntitiesPerDomain: integer(
      input.maxEntitiesPerDomain,
      DEFAULT_PROJECTION_LIMITS.maxEntitiesPerDomain,
      50,
      250_000,
    ),
    maxInactiveTasks: integer(
      input.maxInactiveTasks,
      DEFAULT_PROJECTION_LIMITS.maxInactiveTasks,
      1,
      1_000,
    ),
    maxTombstones: integer(
      input.maxTombstones,
      DEFAULT_PROJECTION_LIMITS.maxTombstones,
      50,
      250_000,
    ),
    maxOrphans: integer(
      input.maxOrphans,
      DEFAULT_PROJECTION_LIMITS.maxOrphans,
      10,
      100_000,
    ),
    maxOptimistic: integer(
      input.maxOptimistic,
      DEFAULT_PROJECTION_LIMITS.maxOptimistic,
      10,
      100_000,
    ),
    maxPartials: integer(
      input.maxPartials,
      DEFAULT_PROJECTION_LIMITS.maxPartials,
      10,
      100_000,
    ),
    maxMutations: integer(
      input.maxMutations,
      DEFAULT_PROJECTION_LIMITS.maxMutations,
      100,
      500_000,
    ),
    maxIndexEntries: integer(
      input.maxIndexEntries,
      DEFAULT_PROJECTION_LIMITS.maxIndexEntries,
      100,
      1_000_000,
    ),
    maxIndexValues: integer(
      input.maxIndexValues,
      DEFAULT_PROJECTION_LIMITS.maxIndexValues,
      10,
      250_000,
    ),
    tombstoneTtlMs: integer(
      input.tombstoneTtlMs,
      DEFAULT_PROJECTION_LIMITS.tombstoneTtlMs,
      1_000,
      30 * 24 * 60 * 60 * 1_000,
    ),
    orphanTtlMs: integer(
      input.orphanTtlMs,
      DEFAULT_PROJECTION_LIMITS.orphanTtlMs,
      1_000,
      7 * 24 * 60 * 60 * 1_000,
    ),
    optimisticTtlMs: integer(
      input.optimisticTtlMs,
      DEFAULT_PROJECTION_LIMITS.optimisticTtlMs,
      1_000,
      7 * 24 * 60 * 60 * 1_000,
    ),
    partialTtlMs: integer(
      input.partialTtlMs,
      DEFAULT_PROJECTION_LIMITS.partialTtlMs,
      1_000,
      7 * 24 * 60 * 60 * 1_000,
    ),
    inactiveTaskTtlMs: integer(
      input.inactiveTaskTtlMs,
      DEFAULT_PROJECTION_LIMITS.inactiveTaskTtlMs,
      10_000,
      30 * 24 * 60 * 60 * 1_000,
    ),
    persistenceDebounceMs: integer(
      input.persistenceDebounceMs,
      DEFAULT_PROJECTION_LIMITS.persistenceDebounceMs,
      0,
      60_000,
    ),
  }
}

export function emptyProjectionDiagnostics(): ProjectionDiagnostics {
  return {
    appliedTransactions: 0,
    rejectedTransactions: 0,
    duplicateEvents: 0,
    staleEvents: 0,
    migratedSnapshots: 0,
    rejectedSnapshots: 0,
    retentionRuns: 0,
    evictedEvents: 0,
    evictedEntities: 0,
    selectorNotifications: 0,
    persistedSnapshots: 0,
    persistenceFailures: 0,
  }
}

export function emptyCausalIndex(): CausalIndex {
  return {
    byEvent: {},
    byCorrelation: {},
    byCausation: {},
    bySpan: {},
    byParentSpan: {},
    byToolCall: {},
    byArtifact: {},
    byCheckpoint: {},
    byControlCommand: {},
    byMutation: {},
    byFailure: {},
    byRecovery: {},
    byTask: {},
    byRun: {},
    bySession: {},
    byNode: {},
    byWorker: {},
    eventOrder: [],
  }
}

export function createEmptyProjectionState(now = Date.now()): CanonicalProjectionState {
  return freezeProjectionState({
    schema: PROJECTION_SCHEMA,
    revision: 0,
    committedAtMs: now,
    tasks: {},
    nodes: {},
    workers: {},
    tools: {},
    artifacts: {},
    memories: {},
    schedulers: {},
    recoveries: {},
    commands: {},
    permissions: {},
    overlays: {},
    sessions: {},
    mutations: {},
    tombstones: {},
    orphans: {},
    optimistic: {},
    partials: {},
    causality: emptyCausalIndex() as MutableProjectionState["causality"],
    cursors: {},
    runtimes: {},
    diagnostics: emptyProjectionDiagnostics(),
  })
}

export function cloneProjectionState(
  source: CanonicalProjectionState,
): MutableProjectionState {
  return {
    schema: PROJECTION_SCHEMA,
    revision: source.revision,
    committedAtMs: source.committedAtMs,
    tasks: { ...source.tasks },
    nodes: { ...source.nodes },
    workers: { ...source.workers },
    tools: { ...source.tools },
    artifacts: { ...source.artifacts },
    memories: { ...source.memories },
    schedulers: { ...source.schedulers },
    recoveries: { ...source.recoveries },
    commands: { ...source.commands },
    permissions: { ...source.permissions },
    overlays: { ...source.overlays },
    sessions: { ...source.sessions },
    mutations: { ...source.mutations },
    tombstones: { ...source.tombstones },
    orphans: { ...source.orphans },
    optimistic: { ...source.optimistic },
    partials: { ...source.partials },
    causality: {
      byEvent: { ...source.causality.byEvent },
      byCorrelation: cloneLists(source.causality.byCorrelation),
      byCausation: cloneLists(source.causality.byCausation),
      bySpan: cloneLists(source.causality.bySpan),
      byParentSpan: cloneLists(source.causality.byParentSpan),
      byToolCall: cloneLists(source.causality.byToolCall),
      byArtifact: cloneLists(source.causality.byArtifact),
      byCheckpoint: cloneLists(source.causality.byCheckpoint),
      byControlCommand: cloneLists(source.causality.byControlCommand),
      byMutation: cloneLists(source.causality.byMutation),
      byFailure: cloneLists(source.causality.byFailure),
      byRecovery: cloneLists(source.causality.byRecovery),
      byTask: cloneLists(source.causality.byTask),
      byRun: cloneLists(source.causality.byRun),
      bySession: cloneLists(source.causality.bySession),
      byNode: cloneLists(source.causality.byNode),
      byWorker: cloneLists(source.causality.byWorker),
      eventOrder: [...source.causality.eventOrder],
    },
    cursors: { ...source.cursors },
    runtimes: { ...source.runtimes },
    diagnostics: {
      ...source.diagnostics,
      lastError: source.diagnostics.lastError
        ? { ...source.diagnostics.lastError }
        : undefined,
    },
  }
}

function cloneLists(
  source: Readonly<Record<string, readonly string[]>>,
): Record<string, string[]> {
  const result: Record<string, string[]> = {}
  for (const [key, values] of Object.entries(source)) result[key] = [...values]
  return result
}

export function taskRuntime(
  state: MutableProjectionState,
  taskId: string,
  generation: number,
  now: number,
): ProjectionTaskRuntime {
  const existing = state.runtimes[taskId]
  if (existing && existing.generation > generation) return existing
  if (existing && existing.generation === generation) return existing
  const next: ProjectionTaskRuntime = {
    taskId,
    generation,
    firstSequence: 0,
    lastSequence: 0,
    eventCount: 0,
    duplicateCount: existing?.duplicateCount ?? 0,
    staleCount: existing?.staleCount ?? 0,
    orphanCount: 0,
    tombstoneCount: 0,
    lastEventAtMs: now,
    pinned: existing?.pinned ?? false,
    connected: existing?.connected ?? false,
  }
  state.runtimes[taskId] = next
  return next
}

export function updateTaskRuntime(
  state: MutableProjectionState,
  taskId: string,
  patch: Partial<ProjectionTaskRuntime>,
): ProjectionTaskRuntime {
  const existing =
    state.runtimes[taskId] ??
    taskRuntime(state, taskId, patch.generation ?? 0, Date.now())
  const next = { ...existing, ...patch, taskId }
  state.runtimes[taskId] = next
  return next
}

export function freezeProjectionState(
  source: MutableProjectionState,
): CanonicalProjectionState {
  freezeEntityTables(source)
  freezeIndex(source)
  freezeProjectionRecords(source)
  for (const cursor of Object.values(source.cursors)) Object.freeze(cursor)
  Object.freeze(source.cursors)
  for (const runtime of Object.values(source.runtimes)) Object.freeze(runtime)
  Object.freeze(source.runtimes)
  if (source.diagnostics.lastError) {
    Object.freeze(source.diagnostics.lastError)
  }
  Object.freeze(source.diagnostics)
  return Object.freeze(source) as CanonicalProjectionState
}

function freezeEntityTables(source: MutableProjectionState): void {
  for (const table of [
    source.tasks,
    source.nodes,
    source.workers,
    source.tools,
    source.artifacts,
    source.memories,
    source.schedulers,
    source.recoveries,
    source.commands,
    source.permissions,
    source.overlays,
    source.sessions,
  ]) {
    for (const entity of Object.values(table)) {
      deepFreeze(entity.attributes)
      deepFreeze(entity.metadata)
      Object.freeze(entity.artifactIds)
      freezeDomainArrays(entity)
      Object.freeze(entity)
    }
    Object.freeze(table)
  }
}

function freezeDomainArrays(
  entity: MutableProjectionState["tasks"][string] |
    MutableProjectionState["nodes"][string] |
    MutableProjectionState["workers"][string] |
    MutableProjectionState["tools"][string] |
    MutableProjectionState["artifacts"][string] |
    MutableProjectionState["memories"][string] |
    MutableProjectionState["schedulers"][string] |
    MutableProjectionState["recoveries"][string] |
    MutableProjectionState["commands"][string] |
    MutableProjectionState["permissions"][string] |
    MutableProjectionState["overlays"][string] |
    MutableProjectionState["sessions"][string],
): void {
  switch (entity.domain) {
    case "task":
      Object.freeze(entity.activeNodeIds)
      Object.freeze(entity.workerIds)
      Object.freeze(entity.sessionIds)
      Object.freeze(entity.pendingPermissionIds)
      Object.freeze(entity.commandIds)
      Object.freeze(entity.recoveryIds)
      break
    case "node":
      Object.freeze(entity.capabilityRefs)
      Object.freeze(entity.dependencyIds)
      Object.freeze(entity.childNodeIds)
      break
    case "worker":
      Object.freeze(entity.capabilityRefs)
      break
    case "tool":
      Object.freeze(entity.resultArtifactIds)
      break
    case "memory":
      Object.freeze(entity.sourceArtifactIds)
      break
    case "scheduler":
      Object.freeze(entity.candidateIds)
      break
    case "session":
      Object.freeze(entity.childSessionIds)
      break
  }
}

function freezeProjectionRecords(source: MutableProjectionState): void {
  for (const mutation of Object.values(source.mutations)) {
    Object.freeze(mutation.path)
    Object.freeze(mutation.entityRefs)
    Object.freeze(mutation)
  }
  Object.freeze(source.mutations)
  for (const tombstone of Object.values(source.tombstones)) {
    Object.freeze(tombstone)
  }
  Object.freeze(source.tombstones)
  for (const orphan of Object.values(source.orphans)) {
    Object.freeze(orphan.eventIds)
    Object.freeze(orphan)
  }
  Object.freeze(source.orphans)
  for (const optimistic of Object.values(source.optimistic)) {
    Object.freeze(optimistic)
  }
  Object.freeze(source.optimistic)
  for (const partial of Object.values(source.partials)) {
    Object.freeze(partial.eventIds)
    deepFreeze(partial.parts)
    Object.freeze(partial)
  }
  Object.freeze(source.partials)
}

function deepFreeze<T>(value: T): T {
  if (
    value === null ||
    (typeof value !== "object" && typeof value !== "function") ||
    Object.isFrozen(value)
  ) {
    return value
  }
  for (const item of Object.values(value as Record<string, unknown>)) {
    deepFreeze(item)
  }
  return Object.freeze(value)
}

function freezeIndex(source: MutableProjectionState): void {
  for (const table of [
    source.causality.byCorrelation,
    source.causality.byCausation,
    source.causality.bySpan,
    source.causality.byParentSpan,
    source.causality.byToolCall,
    source.causality.byArtifact,
    source.causality.byCheckpoint,
    source.causality.byControlCommand,
    source.causality.byMutation,
    source.causality.byFailure,
    source.causality.byRecovery,
    source.causality.byTask,
    source.causality.byRun,
    source.causality.bySession,
    source.causality.byNode,
    source.causality.byWorker,
  ]) {
    for (const values of Object.values(table)) Object.freeze(values)
    Object.freeze(table)
  }
  for (const event of Object.values(source.causality.byEvent)) {
    Object.freeze(event.artifactIds)
    Object.freeze(event.entityRefs)
    Object.freeze(event)
  }
  Object.freeze(source.causality.byEvent)
  Object.freeze(source.causality.eventOrder)
  Object.freeze(source.causality)
}

export function replaceDiagnostics(
  state: CanonicalProjectionState,
  diagnostics: ProjectionDiagnostics,
): CanonicalProjectionState {
  return Object.freeze({
    ...state,
    diagnostics: Object.freeze({
      ...diagnostics,
      lastError: diagnostics.lastError
        ? Object.freeze({ ...diagnostics.lastError })
        : undefined,
    }),
  })
}
