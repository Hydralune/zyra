import type { JsonObject, JsonValue } from "../events/ingress/index.ts"
import {
  EntityLifecycle,
  PROJECTION_SCHEMA,
  PROJECTION_SNAPSHOT_SCHEMA,
  ProjectionDomain,
  ProjectionStatus,
  ProjectionError,
  type CanonicalProjectionState,
  type CausalEventProjection,
  type MutableProjectionState,
  type ProjectionCursor,
  type ProjectionDiagnostics,
  type ProjectionEntity,
  type ProjectionRestoreResult,
  type ProjectionSnapshotEnvelope,
  type ProjectionTaskRuntime,
} from "./contracts.ts"
import {
  createEmptyProjectionState,
  emptyCausalIndex,
  emptyProjectionDiagnostics,
  freezeProjectionState,
} from "./state.ts"
import {
  checksumJson,
  isRecord,
  jsonRecord,
  numberValue,
  optionalString,
  stringArray,
  stringValue,
} from "./value.ts"

export const CURRENT_PROJECTION_SNAPSHOT_VERSION = 1

export function encodeProjectionSnapshot(
  storeId: string,
  state: CanonicalProjectionState,
  now = Date.now(),
): ProjectionSnapshotEnvelope {
  const json = stateToJson(state)
  return {
    schema: PROJECTION_SNAPSHOT_SCHEMA,
    version: CURRENT_PROJECTION_SNAPSHOT_VERSION,
    storeId,
    createdAtMs: now,
    revision: state.revision,
    checksum: checksumJson(json),
    state: json,
  }
}

export function stateToJson(state: CanonicalProjectionState): JsonObject {
  return JSON.parse(JSON.stringify(state)) as JsonObject
}

export function decodeProjectionSnapshot(
  serialized: string,
  expectedStoreId: string,
  now = Date.now(),
): ProjectionRestoreResult {
  let parsed: unknown
  try {
    parsed = JSON.parse(serialized)
  } catch (error) {
    throw new ProjectionError(
      "SNAPSHOT_PARSE_FAILED",
      `Projection snapshot is not valid JSON: ${errorMessage(error)}`,
    )
  }
  if (!isRecord(parsed)) {
    throw new ProjectionError(
      "SNAPSHOT_INVALID",
      "Projection snapshot envelope must be an object.",
    )
  }
  const schema = stringValue(parsed.schema as JsonValue | undefined)
  if (schema !== PROJECTION_SNAPSHOT_SCHEMA) {
    throw new ProjectionError(
      "SNAPSHOT_SCHEMA_MISMATCH",
      `Expected ${PROJECTION_SNAPSHOT_SCHEMA}, observed ${schema || "missing"}.`,
    )
  }
  const storeId = stringValue(parsed.storeId as JsonValue | undefined)
  if (storeId !== expectedStoreId) {
    throw new ProjectionError(
      "SNAPSHOT_STORE_MISMATCH",
      `Projection snapshot belongs to ${storeId || "an unknown store"}, not ${expectedStoreId}.`,
      { expectedStoreId, observedStoreId: storeId },
    )
  }
  const version = numberValue(parsed.version as JsonValue | undefined, -1)
  if (!Number.isSafeInteger(version) || version < 0) {
    throw new ProjectionError(
      "SNAPSHOT_VERSION_INVALID",
      "Projection snapshot version is invalid.",
      { version },
    )
  }
  if (version > CURRENT_PROJECTION_SNAPSHOT_VERSION) {
    throw new ProjectionError(
      "SNAPSHOT_VERSION_NEWER",
      `Projection snapshot version ${version} is newer than supported version ${CURRENT_PROJECTION_SNAPSHOT_VERSION}.`,
      { version, supported: CURRENT_PROJECTION_SNAPSHOT_VERSION },
    )
  }
  const rawState = jsonRecord(parsed.state)
  const expectedChecksum = stringValue(parsed.checksum as JsonValue | undefined)
  const observedChecksum = checksumJson(rawState)
  if (!expectedChecksum || expectedChecksum !== observedChecksum) {
    throw new ProjectionError(
      "SNAPSHOT_CHECKSUM_MISMATCH",
      "Projection snapshot checksum did not match the serialized state.",
      { expectedChecksum, observedChecksum },
    )
  }
  const migrated = migrateProjectionState(rawState, version, now)
  const state = hydrateProjectionState(migrated.state, now)
  return {
    state,
    migratedFrom:
      migrated.version === version &&
      version === CURRENT_PROJECTION_SNAPSHOT_VERSION
        ? undefined
        : version,
    taskIds: Object.freeze(Object.keys(state.tasks).sort()),
    cursorByTask: state.cursors,
  }
}

export function migrateProjectionState(
  raw: JsonObject,
  fromVersion: number,
  now = Date.now(),
): { state: JsonObject; version: number } {
  let state = raw
  let version = fromVersion
  while (version < CURRENT_PROJECTION_SNAPSHOT_VERSION) {
    if (version === 0) {
      state = migrateVersionZero(state, now)
      version = 1
      continue
    }
    throw new ProjectionError(
      "SNAPSHOT_MIGRATION_MISSING",
      `No projection snapshot migration exists for version ${version}.`,
      { version },
    )
  }
  return { state, version }
}

function migrateVersionZero(raw: JsonObject, now: number): JsonObject {
  const base = JSON.parse(
    JSON.stringify(createEmptyProjectionState(now)),
  ) as JsonObject
  const legacyEntities = jsonRecord(raw.entities)
  const domainAliases: Record<string, string> = {
    tasks: "tasks",
    task: "tasks",
    nodes: "nodes",
    node: "nodes",
    workers: "workers",
    worker: "workers",
    tools: "tools",
    tool: "tools",
    artifacts: "artifacts",
    artifact: "artifacts",
    memories: "memories",
    memory: "memories",
    schedulers: "schedulers",
    scheduler: "schedulers",
    routes: "schedulers",
    recoveries: "recoveries",
    recovery: "recoveries",
    commands: "commands",
    command: "commands",
    permissions: "permissions",
    permission: "permissions",
    approvals: "permissions",
    overlays: "overlays",
    overlay: "overlays",
    sessions: "sessions",
    session: "sessions",
  }
  for (const [legacyKey, target] of Object.entries(domainAliases)) {
    const direct = jsonRecord(raw[legacyKey])
    const nested = jsonRecord(legacyEntities[legacyKey])
    const table = { ...jsonRecord(base[target]), ...nested, ...direct }
    base[target] = table
  }
  const legacyCursor = jsonRecord(raw.cursor)
  const cursors = jsonRecord(raw.cursors)
  if (Object.keys(legacyCursor).length > 0) {
    const taskId =
      optionalString(legacyCursor.taskId) ??
      Object.keys(jsonRecord(base.tasks))[0] ??
      "legacy"
    const cursor: JsonObject = {
      taskId,
      generation: numberValue(legacyCursor.generation, 1),
      committedSequence: numberValue(
        legacyCursor.committedSequence ?? legacyCursor.sequence,
        0,
      ),
      highWatermark: numberValue(
        legacyCursor.highWatermark ?? legacyCursor.sequence,
        0,
      ),
      snapshotComplete: true,
      committedAtMs: numberValue(legacyCursor.committedAtMs, now),
    }
    const cursorToken = optionalString(legacyCursor.cursor)
    const lastEventId = optionalString(legacyCursor.lastEventId)
    if (cursorToken) cursor.cursor = cursorToken
    if (lastEventId) cursor.lastEventId = lastEventId
    cursors[taskId] = cursor
  }
  base.cursors = cursors
  base.revision = numberValue(raw.revision, 0)
  base.committedAtMs = numberValue(raw.committedAtMs, now)
  base.schema = PROJECTION_SCHEMA
  if (isRecord(raw.causality)) base.causality = raw.causality as JsonObject
  if (isRecord(raw.mutations)) base.mutations = raw.mutations as JsonObject
  if (isRecord(raw.tombstones)) base.tombstones = raw.tombstones as JsonObject
  if (isRecord(raw.orphans)) base.orphans = raw.orphans as JsonObject
  if (isRecord(raw.optimistic)) base.optimistic = raw.optimistic as JsonObject
  if (isRecord(raw.partials)) base.partials = raw.partials as JsonObject
  if (isRecord(raw.runtimes)) base.runtimes = raw.runtimes as JsonObject
  if (isRecord(raw.diagnostics)) base.diagnostics = raw.diagnostics as JsonObject
  return base
}

export function hydrateProjectionState(
  raw: JsonObject,
  now = Date.now(),
): CanonicalProjectionState {
  const mutable = mutableFromRaw(raw, now)
  validateCrossReferences(mutable)
  rebuildMissingCausalOrder(mutable)
  rebuildTaskRelationships(mutable)
  return freezeProjectionState(mutable)
}

function mutableFromRaw(raw: JsonObject, now: number): MutableProjectionState {
  const diagnostics = hydrateDiagnostics(jsonRecord(raw.diagnostics))
  const state: MutableProjectionState = {
    schema: PROJECTION_SCHEMA,
    revision: safeInteger(raw.revision, 0),
    committedAtMs: safeInteger(raw.committedAtMs, now),
    tasks: hydrateEntityTable(raw.tasks, ProjectionDomain.TASK) as MutableProjectionState["tasks"],
    nodes: hydrateEntityTable(raw.nodes, ProjectionDomain.NODE) as MutableProjectionState["nodes"],
    workers: hydrateEntityTable(raw.workers, ProjectionDomain.WORKER) as MutableProjectionState["workers"],
    tools: hydrateEntityTable(raw.tools, ProjectionDomain.TOOL) as MutableProjectionState["tools"],
    artifacts: hydrateEntityTable(raw.artifacts, ProjectionDomain.ARTIFACT) as MutableProjectionState["artifacts"],
    memories: hydrateEntityTable(raw.memories, ProjectionDomain.MEMORY) as MutableProjectionState["memories"],
    schedulers: hydrateEntityTable(raw.schedulers, ProjectionDomain.SCHEDULER) as MutableProjectionState["schedulers"],
    recoveries: hydrateEntityTable(raw.recoveries, ProjectionDomain.RECOVERY) as MutableProjectionState["recoveries"],
    commands: hydrateEntityTable(raw.commands, ProjectionDomain.COMMAND) as MutableProjectionState["commands"],
    permissions: hydrateEntityTable(raw.permissions, ProjectionDomain.PERMISSION) as MutableProjectionState["permissions"],
    overlays: hydrateEntityTable(raw.overlays, ProjectionDomain.OVERLAY) as MutableProjectionState["overlays"],
    sessions: hydrateEntityTable(raw.sessions, ProjectionDomain.SESSION) as MutableProjectionState["sessions"],
    mutations: hydratePlainTable(raw.mutations) as unknown as MutableProjectionState["mutations"],
    tombstones: hydratePlainTable(raw.tombstones) as unknown as MutableProjectionState["tombstones"],
    orphans: hydratePlainTable(raw.orphans) as unknown as MutableProjectionState["orphans"],
    optimistic: hydratePlainTable(raw.optimistic) as unknown as MutableProjectionState["optimistic"],
    partials: hydratePlainTable(raw.partials) as unknown as MutableProjectionState["partials"],
    causality: hydrateCausality(jsonRecord(raw.causality)),
    cursors: hydrateCursors(jsonRecord(raw.cursors), now),
    runtimes: hydrateRuntimes(jsonRecord(raw.runtimes), now),
    diagnostics,
  }
  return state
}

function hydrateEntityTable(
  value: JsonValue | undefined,
  domain: Exclude<
    (typeof ProjectionDomain)[keyof typeof ProjectionDomain],
    "event"
  >,
): Record<string, ProjectionEntity> {
  const table = jsonRecord(value)
  const result: Record<string, ProjectionEntity> = {}
  for (const [key, rawValue] of Object.entries(table)) {
    if (!isRecord(rawValue)) continue
    const raw = rawValue as JsonObject
    const id = optionalString(raw.id) ?? key
    const taskId = optionalString(raw.taskId ?? raw.task_id)
    const runId = optionalString(raw.runId ?? raw.run_id)
    if (!id || !taskId || !runId) continue
    const lifecycle = normalizeLifecycleValue(stringValue(raw.lifecycle))
    const status = normalizeStatusValue(stringValue(raw.status))
    const entity = {
      ...raw,
      id,
      domain,
      taskId,
      runId,
      lifecycle,
      status,
      revision: safeInteger(raw.revision, 1),
      sequence: safeInteger(raw.sequence, 0),
      aggregateSequence: safeInteger(raw.aggregateSequence, 0),
      createdAt: stringValue(raw.createdAt, new Date(0).toISOString()),
      updatedAt: stringValue(raw.updatedAt, new Date(0).toISOString()),
      firstEventId: stringValue(raw.firstEventId, `restore:${domain}:${id}:first`),
      lastEventId: stringValue(raw.lastEventId, `restore:${domain}:${id}:last`),
      correlationId: stringValue(raw.correlationId, `restore:${taskId}`),
      artifactIds: stringArray(raw.artifactIds),
      title: stringValue(raw.title, `${domain} ${id}`),
      summary: stringValue(raw.summary, "restored projection"),
      terminal: Boolean(raw.terminal),
      effective: raw.effective !== false,
      attributes: jsonRecord(raw.attributes),
      metadata: jsonRecord(raw.metadata),
    } as unknown as ProjectionEntity
    hydrateDomainArrays(entity, raw)
    result[id] = entity
  }
  return result
}

function hydrateDomainArrays(entity: ProjectionEntity, raw: JsonObject): void {
  const mutable = entity as unknown as Record<string, unknown>
  for (const key of [
    "activeNodeIds",
    "workerIds",
    "sessionIds",
    "pendingPermissionIds",
    "commandIds",
    "recoveryIds",
    "capabilityRefs",
    "dependencyIds",
    "childNodeIds",
    "resultArtifactIds",
    "sourceArtifactIds",
    "candidateIds",
    "childSessionIds",
  ]) {
    mutable[key] = stringArray(raw[key])
  }
  if (entity.domain === ProjectionDomain.COMMAND) {
    const priority = stringValue(raw.priority, "next")
    mutable.priority = ["now", "next", "later"].includes(priority)
      ? priority
      : "next"
  }
  if (entity.domain === ProjectionDomain.PERMISSION) {
    mutable.requestId = stringValue(raw.requestId, entity.id)
  }
  if (entity.domain === ProjectionDomain.OVERLAY) {
    mutable.modal = raw.modal !== false
    mutable.openedAt = stringValue(raw.openedAt, entity.createdAt)
  }
  if (entity.domain === ProjectionDomain.SESSION) {
    mutable.compactCount = safeInteger(raw.compactCount, 0)
  }
  if (entity.domain === ProjectionDomain.RECOVERY) {
    mutable.attempt = safeInteger(raw.attempt, 0)
  }
  if (entity.domain === ProjectionDomain.ARTIFACT) {
    mutable.digest = stringValue(raw.digest, "")
    mutable.mediaType = stringValue(raw.mediaType, "application/octet-stream")
    mutable.sizeBytes = safeInteger(raw.sizeBytes, 0)
    mutable.producerEventId = stringValue(raw.producerEventId, entity.lastEventId)
    mutable.version = safeInteger(raw.version, 1)
    mutable.deleted = Boolean(raw.deleted)
  }
}

function hydratePlainTable(
  value: JsonValue | undefined,
): Record<string, Record<string, unknown>> {
  const table = jsonRecord(value)
  const result: Record<string, Record<string, unknown>> = {}
  for (const [key, item] of Object.entries(table)) {
    if (isRecord(item)) result[key] = { ...item }
  }
  return result
}

function hydrateCausality(raw: JsonObject): MutableProjectionState["causality"] {
  const empty = emptyCausalIndex()
  const byEvent: Record<string, CausalEventProjection> = {}
  for (const [eventId, value] of Object.entries(jsonRecord(raw.byEvent))) {
    if (!isRecord(value)) continue
    const event = value as JsonObject
    const taskId = optionalString(event.taskId)
    const runId = optionalString(event.runId)
    if (!taskId || !runId) continue
    byEvent[eventId] = {
      ...(event as unknown as CausalEventProjection),
      eventId,
      eventType: stringValue(event.eventType, "restored.event"),
      taskId,
      runId,
      artifactIds: stringArray(event.artifactIds),
      correlationId: stringValue(event.correlationId, `restore:${taskId}`),
      mutationId: stringValue(event.mutationId, `restore:${eventId}`),
      sequence: safeInteger(event.sequence, 0),
      aggregateSequence: safeInteger(event.aggregateSequence, 0),
      createdAt: stringValue(event.createdAt, new Date(0).toISOString()),
      committedAt: stringValue(event.committedAt, new Date(0).toISOString()),
      summary: stringValue(event.summary, "restored event"),
      terminal: Boolean(event.terminal),
      effective: event.effective !== false,
      entityRefs: stringArray(event.entityRefs),
    }
  }
  return {
    byEvent,
    byCorrelation: hydrateStringIndex(raw.byCorrelation),
    byCausation: hydrateStringIndex(raw.byCausation),
    bySpan: hydrateStringIndex(raw.bySpan),
    byParentSpan: hydrateStringIndex(raw.byParentSpan),
    byToolCall: hydrateStringIndex(raw.byToolCall),
    byArtifact: hydrateStringIndex(raw.byArtifact),
    byCheckpoint: hydrateStringIndex(raw.byCheckpoint),
    byControlCommand: hydrateStringIndex(raw.byControlCommand),
    byMutation: hydrateStringIndex(raw.byMutation),
    byFailure: hydrateStringIndex(raw.byFailure),
    byRecovery: hydrateStringIndex(raw.byRecovery),
    byTask: hydrateStringIndex(raw.byTask),
    byRun: hydrateStringIndex(raw.byRun),
    bySession: hydrateStringIndex(raw.bySession),
    byNode: hydrateStringIndex(raw.byNode),
    byWorker: hydrateStringIndex(raw.byWorker),
    eventOrder: stringArray(raw.eventOrder ?? empty.eventOrder as unknown as JsonValue),
  }
}

function hydrateStringIndex(
  value: JsonValue | undefined,
): Record<string, string[]> {
  const table = jsonRecord(value)
  const result: Record<string, string[]> = {}
  for (const [key, item] of Object.entries(table)) {
    const values = stringArray(item)
    if (values.length > 0) result[key] = values
  }
  return result
}

function hydrateCursors(raw: JsonObject, now: number): Record<string, ProjectionCursor> {
  const result: Record<string, ProjectionCursor> = {}
  for (const [taskId, value] of Object.entries(raw)) {
    if (!isRecord(value)) continue
    const item = value as JsonObject
    result[taskId] = {
      taskId,
      generation: safeInteger(item.generation, 0),
      cursor: optionalString(item.cursor),
      committedSequence: safeInteger(item.committedSequence, 0),
      highWatermark: safeInteger(item.highWatermark, 0),
      snapshotComplete: Boolean(item.snapshotComplete),
      lastEventId: optionalString(item.lastEventId),
      committedAtMs: safeInteger(item.committedAtMs, now),
    }
  }
  return result
}

function hydrateRuntimes(
  raw: JsonObject,
  now: number,
): Record<string, ProjectionTaskRuntime> {
  const result: Record<string, ProjectionTaskRuntime> = {}
  for (const [taskId, value] of Object.entries(raw)) {
    if (!isRecord(value)) continue
    const item = value as JsonObject
    result[taskId] = {
      taskId,
      generation: safeInteger(item.generation, 0),
      firstSequence: safeInteger(item.firstSequence, 0),
      lastSequence: safeInteger(item.lastSequence, 0),
      eventCount: safeInteger(item.eventCount, 0),
      duplicateCount: safeInteger(item.duplicateCount, 0),
      staleCount: safeInteger(item.staleCount, 0),
      orphanCount: safeInteger(item.orphanCount, 0),
      tombstoneCount: safeInteger(item.tombstoneCount, 0),
      lastEventAtMs: safeInteger(item.lastEventAtMs, now),
      pinned: Boolean(item.pinned),
      connected: Boolean(item.connected),
    }
  }
  return result
}

function hydrateDiagnostics(raw: JsonObject): ProjectionDiagnostics {
  const empty = emptyProjectionDiagnostics()
  const result = { ...empty }
  for (const key of Object.keys(empty) as (keyof ProjectionDiagnostics)[]) {
    if (key === "lastError") continue
    const value = raw[key]
    if (typeof value === "number" && Number.isFinite(value)) {
      ;(result[key] as number) = Math.max(0, Math.trunc(value))
    }
  }
  if (isRecord(raw.lastError)) {
    result.lastError = {
      code: stringValue(raw.lastError.code, "RESTORED_ERROR"),
      message: stringValue(raw.lastError.message, "restored projection error"),
      atMs: safeInteger(raw.lastError.atMs, 0),
      taskId: optionalString(raw.lastError.taskId),
      eventId: optionalString(raw.lastError.eventId),
    }
  }
  return result
}

function validateCrossReferences(state: MutableProjectionState): void {
  for (const [taskId, cursor] of Object.entries(state.cursors)) {
    if (cursor.taskId !== taskId) {
      state.cursors[taskId] = { ...cursor, taskId }
    }
    const runtime = state.runtimes[taskId]
    if (runtime && runtime.generation > cursor.generation) {
      delete state.cursors[taskId]
    }
  }
  for (const eventId of Object.keys(state.causality.byEvent)) {
    const event = state.causality.byEvent[eventId]!
    if (event.eventId !== eventId) {
      state.causality.byEvent[eventId] = { ...event, eventId }
    }
  }
  for (const table of causalIndexTables(state)) {
    for (const [key, values] of Object.entries(table)) {
      table[key] = values.filter((eventId) => Boolean(state.causality.byEvent[eventId]))
      if (table[key]!.length === 0) delete table[key]
    }
  }
}

function rebuildMissingCausalOrder(state: MutableProjectionState): void {
  const valid = new Set(
    state.causality.eventOrder.filter((id) => Boolean(state.causality.byEvent[id])),
  )
  for (const eventId of Object.keys(state.causality.byEvent)) valid.add(eventId)
  state.causality.eventOrder = [...valid].sort((left, right) => {
    const a = state.causality.byEvent[left]!
    const b = state.causality.byEvent[right]!
    return (
      a.sequence - b.sequence ||
      a.committedAt.localeCompare(b.committedAt) ||
      left.localeCompare(right)
    )
  })
}

function rebuildTaskRelationships(state: MutableProjectionState): void {
  for (const [taskId, task] of Object.entries(state.tasks)) {
    state.tasks[taskId] = {
      ...task,
      activeNodeIds: uniqueTaskIds(state.nodes, taskId, (item) => !item.terminal),
      workerIds: uniqueTaskIds(state.workers, taskId),
      sessionIds: uniqueTaskIds(state.sessions, taskId),
      artifactIds: uniqueTaskIds(state.artifacts, taskId, (item) => !item.deleted),
      pendingPermissionIds: uniqueTaskIds(
        state.permissions,
        taskId,
        (item) => !item.resolvedAt && !item.terminal,
      ),
      commandIds: uniqueTaskIds(state.commands, taskId),
      recoveryIds: uniqueTaskIds(state.recoveries, taskId),
    }
  }
}

function uniqueTaskIds<T extends { id: string; taskId: string }>(
  table: Record<string, T>,
  taskId: string,
  filter: (item: T) => boolean = () => true,
): string[] {
  return Object.values(table)
    .filter((item) => item.taskId === taskId && filter(item))
    .map((item) => item.id)
    .sort()
}

function causalIndexTables(
  state: MutableProjectionState,
): Record<string, string[]>[] {
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

function normalizeLifecycleValue(value: string) {
  return Object.values(EntityLifecycle).includes(
    value as (typeof EntityLifecycle)[keyof typeof EntityLifecycle],
  )
    ? (value as (typeof EntityLifecycle)[keyof typeof EntityLifecycle])
    : EntityLifecycle.UNKNOWN
}

function normalizeStatusValue(value: string) {
  return Object.values(ProjectionStatus).includes(
    value as (typeof ProjectionStatus)[keyof typeof ProjectionStatus],
  )
    ? (value as (typeof ProjectionStatus)[keyof typeof ProjectionStatus])
    : ProjectionStatus.AUTHORITATIVE
}

function safeInteger(value: JsonValue | undefined, fallback: number): number {
  const normalized = numberValue(value, fallback)
  return Number.isSafeInteger(normalized) && normalized >= 0
    ? normalized
    : fallback
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
