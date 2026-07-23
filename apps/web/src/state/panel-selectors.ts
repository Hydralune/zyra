import {
  EntityLifecycle,
  ProjectionDomain,
  type ArtifactProjection,
  type CanonicalProjectionState,
  type CausalEventProjection,
  type CommandProjection,
  type OverlayProjection,
  type PermissionProjection,
  type ProjectionSelector,
  type SessionProjection,
  type ToolProjection,
  type WorkerProjection,
} from "./contracts.ts"
import { projectionSelector } from "./selectors.ts"
import { buildTopologyProjection } from "../features/topology/projection/projector.ts"

export interface TopologyPanelNode {
  id: string
  lifecycle: string
  terminal: boolean
  revision: number
  sequence: number
  role?: string
  dependencyIds: readonly string[]
  childNodeIds: readonly string[]
  workerIds: readonly string[]
  missingDependencyIds: readonly string[]
  incoming: number
  outgoing: number
}

export interface TopologyPanelEdge {
  id: string
  sourceId: string
  targetId: string
  kind: "dependency" | "parent" | "worker"
  missing: boolean
}

export interface TopologyPanelView {
  taskId: string
  revision: number
  nodes: readonly TopologyPanelNode[]
  workers: readonly WorkerProjection[]
  edges: readonly TopologyPanelEdge[]
  roots: readonly string[]
  leaves: readonly string[]
  missingDependencies: readonly string[]
  cycles: readonly (readonly string[])[]
}

export interface TimelinePanelOptions {
  beforeSequence?: number
  limit?: number
  includeNonEffective?: boolean
  domains?: readonly string[]
}

export interface TimelinePanelEntry {
  event: CausalEventProjection
  lane: string
  depth: number
  causePresent: boolean
  effectCount: number
  mutationIds: readonly string[]
}

export interface TimelinePanelView {
  taskId: string
  entries: readonly TimelinePanelEntry[]
  firstSequence?: number
  lastSequence?: number
  hasEarlier: boolean
  correlations: Readonly<Record<string, readonly string[]>>
  lanes: readonly string[]
}

export interface ArtifactLineageRow {
  artifact: ArtifactProjection
  producer?: CausalEventProjection
  tool?: ToolProjection
  ancestorEventIds: readonly string[]
  descendantEventIds: readonly string[]
  relatedMutationIds: readonly string[]
}

export interface ArtifactPanelView {
  taskId: string
  rows: readonly ArtifactLineageRow[]
  missingProducerIds: readonly string[]
  deletedCount: number
  totalBytes: number
}

export interface SessionTreeRow {
  session: SessionProjection
  depth: number
  parentPresent: boolean
  childIds: readonly string[]
  workerIds: readonly string[]
  cycle: boolean
}

export interface SessionPanelView {
  taskId: string
  rows: readonly SessionTreeRow[]
  rootIds: readonly string[]
  orphanIds: readonly string[]
  cycleIds: readonly string[]
  totalContextTokens: number
  compactionCount: number
}

export interface OperatorQueueRow {
  id: string
  kind: "permission" | "command"
  sequence: number
  priority: number
  label: string
  lifecycle: string
  permission?: PermissionProjection
  command?: CommandProjection
  overlay?: OverlayProjection
}

export interface OperatorQueueView {
  taskId: string
  rows: readonly OperatorQueueRow[]
  pendingPermissions: number
  queuedCommands: number
  blocking: boolean
}

export interface ProjectionReadinessView {
  taskId: string
  ready: boolean
  revision: number
  committedSequence: number
  highWatermark: number
  lag: number
  snapshotComplete: boolean
  connected: boolean
  missingSequences: readonly number[]
  unresolvedOrphans: readonly string[]
  pendingPartialKeys: readonly string[]
  pendingOptimisticKeys: readonly string[]
  integrityWarnings: readonly string[]
}

export function selectTopologyPanel(
  taskId: string,
): ProjectionSelector<TopologyPanelView> {
  return projectionSelector(
    `panel.topology.${taskId}`,
    [`task:${taskId}`, "domain:node", "domain:worker"],
    (state) => buildTopologyPanel(state, taskId),
    sameTopology,
  )
}

export function buildTopologyPanel(
  state: CanonicalProjectionState,
  taskId: string,
): TopologyPanelView {
  const topology = buildTopologyProjection(state, taskId)
  const nodes = topology.nodes
  const workers = Object.values(state.workers)
    .filter((worker) => worker.taskId === taskId)
    .sort(compareSequence)
  const workersByNode = new Map<string, WorkerProjection[]>()
  for (const worker of workers) {
    if (!worker.nodeId) continue
    const values = workersByNode.get(worker.nodeId) ?? []
    values.push(worker)
    workersByNode.set(worker.nodeId, values)
  }
  const incoming = new Map<string, number>()
  const outgoing = new Map<string, number>()
  const graphEdges: TopologyPanelEdge[] = topology.edges
    .filter((edge) => edge.edgeKind === "dependency" || edge.edgeKind === "parent")
    .map((edge) => {
      incoming.set(edge.targetId, (incoming.get(edge.targetId) ?? 0) + 1)
      outgoing.set(edge.sourceId, (outgoing.get(edge.sourceId) ?? 0) + 1)
      return Object.freeze({
        id: edge.id,
        sourceId: edge.sourceId,
        targetId: edge.targetId,
        kind: edge.edgeKind as "dependency" | "parent",
        missing: edge.sourceMissing || edge.targetMissing,
      })
    })
  const workerEdges: TopologyPanelEdge[] = []
  for (const node of nodes) {
    for (const worker of workersByNode.get(node.id) ?? []) {
      workerEdges.push(
        Object.freeze({
          id: `worker:${node.id}->${worker.id}`,
          sourceId: node.id,
          targetId: worker.id,
          kind: "worker",
          missing: false,
        }),
      )
    }
  }
  const edges = [...graphEdges, ...workerEdges]
  edges.sort(compareEdge)
  const rows = nodes.map((node) =>
    Object.freeze({
      id: node.id,
      lifecycle: node.state,
      terminal: node.terminal,
      revision: node.revision,
      sequence: node.sequence,
      role: node.role,
      dependencyIds: Object.freeze([...node.dependencies]),
      childNodeIds: Object.freeze([...node.childNodeIds]),
      workerIds: Object.freeze(
        (workersByNode.get(node.id) ?? []).map((worker) => worker.id).sort(),
      ),
      missingDependencyIds: Object.freeze([...node.missingDependencyIds]),
      incoming: incoming.get(node.id) ?? 0,
      outgoing: outgoing.get(node.id) ?? 0,
    }),
  )
  return Object.freeze({
    taskId,
    revision: topology.projectionRevision,
    nodes: Object.freeze(rows),
    workers: Object.freeze(workers),
    edges: Object.freeze(edges),
    roots: Object.freeze(rows.filter((node) => node.incoming === 0).map((node) => node.id)),
    leaves: Object.freeze(rows.filter((node) => node.outgoing === 0).map((node) => node.id)),
    missingDependencies: topology.analysis.missingNodeIds,
    cycles: topology.analysis.cycles,
  })
}

export function selectTimelinePanel(
  taskId: string,
  options: TimelinePanelOptions = {},
): ProjectionSelector<TimelinePanelView> {
  const normalized = normalizeTimelineOptions(options)
  return projectionSelector(
    `panel.timeline.${taskId}.${JSON.stringify(normalized)}`,
    [`task:${taskId}`, `causal:task:${taskId}`, "domain:event"],
    (state) => buildTimelinePanel(state, taskId, normalized),
    sameTimeline,
  )
}

export function buildTimelinePanel(
  state: CanonicalProjectionState,
  taskId: string,
  options: TimelinePanelOptions = {},
): TimelinePanelView {
  const normalized = normalizeTimelineOptions(options)
  const allowedDomains = new Set(normalized.domains)
  const all = (state.causality.byTask[taskId] ?? [])
    .map((id) => state.causality.byEvent[id])
    .filter((event): event is CausalEventProjection => Boolean(event))
    .filter((event) => normalized.includeNonEffective || event.effective || event.terminal)
    .filter(
      (event) =>
        normalized.beforeSequence === undefined ||
        event.sequence < normalized.beforeSequence,
    )
    .filter(
      (event) =>
        allowedDomains.size === 0 ||
        event.entityRefs.some((ref) =>
          allowedDomains.has(ref.slice(0, ref.indexOf(":"))),
        ),
    )
    .sort(compareEventRecent)
  const selected = all.slice(0, normalized.limit).reverse()
  const selectedIds = new Set(selected.map((event) => event.eventId))
  const correlations: Record<string, string[]> = {}
  const lanes = new Set<string>()
  const entries = selected.map((event) => {
    const lane = timelineLane(state, event)
    lanes.add(lane)
    const correlation = correlations[event.correlationId] ?? []
    correlation.push(event.eventId)
    correlations[event.correlationId] = correlation
    return Object.freeze({
      event,
      lane,
      depth: causalDepth(state, event),
      causePresent: Boolean(event.causationId && selectedIds.has(event.causationId)),
      effectCount: (state.causality.byCausation[event.eventId] ?? []).length,
      mutationIds: Object.freeze([event.mutationId]),
    })
  })
  const frozenCorrelations: Record<string, readonly string[]> = {}
  for (const [key, ids] of Object.entries(correlations)) {
    frozenCorrelations[key] = Object.freeze(ids)
  }
  return Object.freeze({
    taskId,
    entries: Object.freeze(entries),
    firstSequence: selected[0]?.sequence,
    lastSequence: selected.at(-1)?.sequence,
    hasEarlier: all.length > selected.length,
    correlations: Object.freeze(frozenCorrelations),
    lanes: Object.freeze([...lanes].sort()),
  })
}

function normalizeTimelineOptions(
  options: TimelinePanelOptions,
): Required<Omit<TimelinePanelOptions, "beforeSequence">> & {
  beforeSequence?: number
} {
  const beforeSequence =
    Number.isSafeInteger(options.beforeSequence) &&
    Number(options.beforeSequence) > 0
      ? Number(options.beforeSequence)
      : undefined
  return {
    beforeSequence,
    limit: Number.isSafeInteger(options.limit)
      ? Math.min(1_000, Math.max(1, Number(options.limit)))
      : 200,
    includeNonEffective: options.includeNonEffective === true,
    domains: Object.freeze([...new Set(options.domains ?? [])].sort()),
  }
}

function causalDepth(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): number {
  let current = event
  let depth = 0
  const visited = new Set([event.eventId])
  while (current.causationId && depth < 256) {
    if (visited.has(current.causationId)) return depth + 1
    const cause = state.causality.byEvent[current.causationId]
    if (!cause) return depth + 1
    visited.add(cause.eventId)
    current = cause
    depth += 1
  }
  return depth
}

function timelineLane(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): string {
  const mutationDomain = state.mutations[event.mutationId]?.domain
  if (
    mutationDomain &&
    Object.values(ProjectionDomain).includes(
      mutationDomain as (typeof ProjectionDomain)[keyof typeof ProjectionDomain],
    )
  ) {
    return mutationDomain
  }
  const order = [
    ProjectionDomain.PERMISSION,
    ProjectionDomain.COMMAND,
    ProjectionDomain.RECOVERY,
    ProjectionDomain.TOOL,
    ProjectionDomain.ARTIFACT,
    ProjectionDomain.WORKER,
    ProjectionDomain.NODE,
    ProjectionDomain.SESSION,
    ProjectionDomain.TASK,
  ]
  for (const domain of order) {
    if (event.entityRefs.some((ref) => ref.startsWith(`${domain}:`))) return domain
  }
  return ProjectionDomain.EVENT
}

export function selectArtifactPanel(
  taskId: string,
): ProjectionSelector<ArtifactPanelView> {
  return projectionSelector(
    `panel.artifacts.${taskId}`,
    [`task:${taskId}`, "domain:artifact", "domain:tool", "domain:event"],
    (state) => buildArtifactPanel(state, taskId),
    sameArtifactPanel,
  )
}

export function buildArtifactPanel(
  state: CanonicalProjectionState,
  taskId: string,
): ArtifactPanelView {
  const artifacts = Object.values(state.artifacts)
    .filter((artifact) => artifact.taskId === taskId)
    .sort(compareSequenceRecent)
  const missing = new Set<string>()
  let deletedCount = 0
  let totalBytes = 0
  const rows = artifacts.map((artifact) => {
    totalBytes += artifact.sizeBytes
    if (artifact.deleted) deletedCount += 1
    const producer = state.causality.byEvent[artifact.producerEventId]
    if (!producer) missing.add(artifact.producerEventId)
    const tool = artifact.producerToolCallId
      ? state.tools[artifact.producerToolCallId]
      : undefined
    const relevantEvents = unique([
      ...(state.causality.byArtifact[artifact.id] ?? []),
      artifact.producerEventId,
    ])
    const relatedMutationIds = unique(
      relevantEvents
        .map((eventId) => state.causality.byEvent[eventId]?.mutationId ?? "")
        .filter(Boolean),
    )
    return Object.freeze({
      artifact,
      producer,
      tool,
      ancestorEventIds: Object.freeze(causalAncestors(state, artifact.producerEventId)),
      descendantEventIds: Object.freeze(causalDescendants(state, artifact.producerEventId)),
      relatedMutationIds: Object.freeze(relatedMutationIds),
    })
  })
  return Object.freeze({
    taskId,
    rows: Object.freeze(rows),
    missingProducerIds: Object.freeze([...missing].sort()),
    deletedCount,
    totalBytes,
  })
}

function causalAncestors(
  state: CanonicalProjectionState,
  eventId: string,
): string[] {
  const result: string[] = []
  const visited = new Set<string>()
  let current = state.causality.byEvent[eventId]
  while (current?.causationId && result.length < 512) {
    if (visited.has(current.causationId)) break
    visited.add(current.causationId)
    result.push(current.causationId)
    current = state.causality.byEvent[current.causationId]
  }
  return result
}

function causalDescendants(
  state: CanonicalProjectionState,
  eventId: string,
): string[] {
  const result: string[] = []
  const visited = new Set<string>()
  const queue = [...(state.causality.byCausation[eventId] ?? [])]
  while (queue.length > 0 && result.length < 2_000) {
    const id = queue.shift()!
    if (visited.has(id)) continue
    visited.add(id)
    result.push(id)
    queue.push(...(state.causality.byCausation[id] ?? []))
  }
  return result.sort((left, right) => {
    const leftSequence = state.causality.byEvent[left]?.sequence ?? 0
    const rightSequence = state.causality.byEvent[right]?.sequence ?? 0
    return leftSequence - rightSequence || left.localeCompare(right)
  })
}

export function selectSessionPanel(
  taskId: string,
): ProjectionSelector<SessionPanelView> {
  return projectionSelector(
    `panel.sessions.${taskId}`,
    [`task:${taskId}`, "domain:session", "domain:worker"],
    (state) => buildSessionPanel(state, taskId),
    sameSessionPanel,
  )
}

export function buildSessionPanel(
  state: CanonicalProjectionState,
  taskId: string,
): SessionPanelView {
  const sessions = Object.values(state.sessions)
    .filter((session) => session.taskId === taskId)
    .sort(compareSequence)
  const byId = new Map(sessions.map((session) => [session.id, session]))
  const workers = Object.values(state.workers).filter(
    (worker) => worker.taskId === taskId,
  )
  const roots = sessions.filter(
    (session) => !session.parentSessionId || !byId.has(session.parentSessionId),
  )
  const orphanIds = sessions
    .filter((session) => Boolean(session.parentSessionId && !byId.has(session.parentSessionId)))
    .map((session) => session.id)
  const cycleIds = findSessionCycles(sessions, byId)
  const rows: SessionTreeRow[] = []
  const emitted = new Set<string>()
  const emit = (session: SessionProjection, depth: number) => {
    if (emitted.has(session.id)) return
    emitted.add(session.id)
    const childIds = unique([
      ...session.childSessionIds,
      ...sessions
        .filter((candidate) => candidate.parentSessionId === session.id)
        .map((candidate) => candidate.id),
    ]).filter((id) => byId.has(id))
    rows.push(
      Object.freeze({
        session,
        depth,
        parentPresent: !session.parentSessionId || byId.has(session.parentSessionId),
        childIds: Object.freeze(childIds),
        workerIds: Object.freeze(
          workers
            .filter((worker) => worker.sessionId === session.id)
            .map((worker) => worker.id)
            .sort(),
        ),
        cycle: cycleIds.has(session.id),
      }),
    )
    for (const childId of childIds) emit(byId.get(childId)!, depth + 1)
  }
  for (const root of roots) emit(root, 0)
  for (const session of sessions) emit(session, 0)
  return Object.freeze({
    taskId,
    rows: Object.freeze(rows),
    rootIds: Object.freeze(roots.map((session) => session.id)),
    orphanIds: Object.freeze(orphanIds),
    cycleIds: Object.freeze([...cycleIds].sort()),
    totalContextTokens: sessions.reduce(
      (total, session) => total + (session.contextTokens ?? 0),
      0,
    ),
    compactionCount: sessions.reduce(
      (total, session) => total + session.compactCount,
      0,
    ),
  })
}

function findSessionCycles(
  sessions: readonly SessionProjection[],
  byId: ReadonlyMap<string, SessionProjection>,
): Set<string> {
  const cycleIds = new Set<string>()
  for (const session of sessions) {
    const path: string[] = []
    const offsets = new Map<string, number>()
    let current: SessionProjection | undefined = session
    while (current) {
      const offset = offsets.get(current.id)
      if (offset !== undefined) {
        for (const id of path.slice(offset)) cycleIds.add(id)
        break
      }
      offsets.set(current.id, path.length)
      path.push(current.id)
      current = current.parentSessionId
        ? byId.get(current.parentSessionId)
        : undefined
    }
  }
  return cycleIds
}

export function selectOperatorQueue(
  taskId: string,
): ProjectionSelector<OperatorQueueView> {
  return projectionSelector(
    `panel.operator-queue.${taskId}`,
    [`task:${taskId}`, "domain:permission", "domain:command", "domain:overlay"],
    (state) => buildOperatorQueue(state, taskId),
    sameOperatorQueue,
  )
}

export function buildOperatorQueue(
  state: CanonicalProjectionState,
  taskId: string,
): OperatorQueueView {
  const overlays = Object.values(state.overlays).filter(
    (overlay) => overlay.taskId === taskId && !overlay.closedAt,
  )
  const overlayByPermission = new Map<string, OverlayProjection>()
  const overlayByCommand = new Map<string, OverlayProjection>()
  for (const overlay of overlays) {
    if (overlay.permissionId) overlayByPermission.set(overlay.permissionId, overlay)
    if (overlay.commandId) overlayByCommand.set(overlay.commandId, overlay)
  }
  const rows: OperatorQueueRow[] = []
  for (const permission of Object.values(state.permissions)) {
    if (permission.taskId !== taskId || permission.resolvedAt) continue
    rows.push(
      Object.freeze({
        id: permission.id,
        kind: "permission",
        sequence: permission.sequence,
        priority: permissionPriority(permission, state.committedAtMs),
        label: permission.toolName ?? permission.permissionKind ?? permission.title,
        lifecycle: permission.lifecycle,
        permission,
        overlay: overlayByPermission.get(permission.id),
      }),
    )
  }
  for (const command of Object.values(state.commands)) {
    if (command.taskId !== taskId || command.terminal) continue
    rows.push(
      Object.freeze({
        id: command.id,
        kind: "command",
        sequence: command.sequence,
        priority: commandPriority(command),
        label: command.commandName ?? command.title,
        lifecycle: command.lifecycle,
        command,
        overlay: overlayByCommand.get(command.id),
      }),
    )
  }
  rows.sort(
    (left, right) =>
      left.priority - right.priority ||
      left.sequence - right.sequence ||
      left.id.localeCompare(right.id),
  )
  const pendingPermissions = rows.filter((row) => row.kind === "permission").length
  return Object.freeze({
    taskId,
    rows: Object.freeze(rows),
    pendingPermissions,
    queuedCommands: rows.length - pendingPermissions,
    blocking:
      pendingPermissions > 0 ||
      rows.some(
        (row) =>
          row.command?.priority === "now" ||
          row.lifecycle === EntityLifecycle.WAITING_POLICY,
      ),
  })
}

function permissionPriority(
  permission: PermissionProjection,
  observedAtMs: number,
): number {
  if (permission.expiresAt) {
    const remaining = Date.parse(permission.expiresAt) - observedAtMs
    if (Number.isFinite(remaining) && remaining <= 0) return 0
    if (Number.isFinite(remaining) && remaining < 60_000) return 1
  }
  if (permission.decision === "ask") return 2
  return 3
}

function commandPriority(command: CommandProjection): number {
  if (command.priority === "now") return 4
  if (command.priority === "next") return 5
  return 6
}

export function selectProjectionReadiness(
  taskId: string,
): ProjectionSelector<ProjectionReadinessView> {
  return projectionSelector(
    `panel.projection-readiness.${taskId}`,
    [`task:${taskId}`, `cursor:${taskId}`, `causal:task:${taskId}`, "diagnostics"],
    (state) => buildProjectionReadiness(state, taskId),
    sameReadiness,
  )
}

export function buildProjectionReadiness(
  state: CanonicalProjectionState,
  taskId: string,
): ProjectionReadinessView {
  const cursor = state.cursors[taskId]
  const runtime = state.runtimes[taskId]
  const events = (state.causality.byTask[taskId] ?? [])
    .map((id) => state.causality.byEvent[id])
    .filter((event): event is CausalEventProjection => Boolean(event))
    .sort(compareEvent)
  const missingSequences: number[] = []
  if (events.length > 1) {
    let previous = events[0]!.aggregateSequence
    for (const event of events.slice(1)) {
      for (
        let sequence = previous + 1;
        sequence < event.aggregateSequence &&
        missingSequences.length < 1_000;
        sequence += 1
      ) {
        missingSequences.push(sequence)
      }
      previous = Math.max(previous, event.aggregateSequence)
    }
  }
  const unresolvedOrphans = Object.values(state.orphans)
    .filter((orphan) => orphan.taskId === taskId)
    .map((orphan) => orphan.identity)
    .sort()
  const pendingPartialKeys = Object.values(state.partials)
    .filter((partial) => partial.taskId === taskId)
    .map((partial) => partial.key)
    .sort()
  const pendingOptimisticKeys = Object.values(state.optimistic)
    .filter((record) => record.taskId === taskId && !record.confirmedEventId)
    .map((record) => record.key)
    .sort()
  const warnings: string[] = []
  if (!cursor) warnings.push("cursor_missing")
  if (cursor && !cursor.snapshotComplete) warnings.push("snapshot_incomplete")
  if (missingSequences.length > 0) warnings.push("sequence_gap")
  if (unresolvedOrphans.length > 0) warnings.push("unresolved_orphan")
  if (pendingPartialKeys.length > 0) warnings.push("partial_unsettled")
  if (state.diagnostics.lastError?.taskId === taskId) {
    warnings.push(`projection_error:${state.diagnostics.lastError.code}`)
  }
  const committedSequence = cursor?.committedSequence ?? 0
  const highWatermark = cursor?.highWatermark ?? 0
  return Object.freeze({
    taskId,
    ready:
      Boolean(cursor?.snapshotComplete) &&
      missingSequences.length === 0 &&
      unresolvedOrphans.length === 0 &&
      !state.diagnostics.lastError,
    revision: state.revision,
    committedSequence,
    highWatermark,
    lag: Math.max(0, highWatermark - committedSequence),
    snapshotComplete: cursor?.snapshotComplete ?? false,
    connected: runtime?.connected ?? false,
    missingSequences: Object.freeze(missingSequences),
    unresolvedOrphans: Object.freeze(unresolvedOrphans),
    pendingPartialKeys: Object.freeze(pendingPartialKeys),
    pendingOptimisticKeys: Object.freeze(pendingOptimisticKeys),
    integrityWarnings: Object.freeze(unique(warnings)),
  })
}

function compareSequence(
  left: { sequence: number; id: string },
  right: { sequence: number; id: string },
): number {
  return left.sequence - right.sequence || left.id.localeCompare(right.id)
}

function compareSequenceRecent(
  left: { sequence: number; id: string },
  right: { sequence: number; id: string },
): number {
  return right.sequence - left.sequence || left.id.localeCompare(right.id)
}

function compareEvent(
  left: CausalEventProjection,
  right: CausalEventProjection,
): number {
  return (
    left.sequence - right.sequence ||
    left.aggregateSequence - right.aggregateSequence ||
    left.eventId.localeCompare(right.eventId)
  )
}

function compareEventRecent(
  left: CausalEventProjection,
  right: CausalEventProjection,
): number {
  return compareEvent(right, left)
}

function compareEdge(
  left: TopologyPanelEdge,
  right: TopologyPanelEdge,
): number {
  return (
    left.sourceId.localeCompare(right.sourceId) ||
    left.targetId.localeCompare(right.targetId) ||
    left.kind.localeCompare(right.kind)
  )
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values.filter(Boolean))]
}

function sameTopology(
  left: TopologyPanelView,
  right: TopologyPanelView,
): boolean {
  return left.taskId === right.taskId && left.revision === right.revision
}

function sameTimeline(
  left: TimelinePanelView,
  right: TimelinePanelView,
): boolean {
  return (
    left.entries.length === right.entries.length &&
    left.entries.every(
      (entry, index) =>
        entry.event.eventId === right.entries[index]?.event.eventId,
    )
  )
}

function sameArtifactPanel(
  left: ArtifactPanelView,
  right: ArtifactPanelView,
): boolean {
  return (
    left.rows.length === right.rows.length &&
    left.rows.every(
      (row, index) =>
        row.artifact.id === right.rows[index]?.artifact.id &&
        row.artifact.revision === right.rows[index]?.artifact.revision,
    )
  )
}

function sameSessionPanel(
  left: SessionPanelView,
  right: SessionPanelView,
): boolean {
  return (
    left.rows.length === right.rows.length &&
    left.rows.every(
      (row, index) =>
        row.session.id === right.rows[index]?.session.id &&
        row.session.revision === right.rows[index]?.session.revision &&
        row.depth === right.rows[index]?.depth,
    )
  )
}

function sameOperatorQueue(
  left: OperatorQueueView,
  right: OperatorQueueView,
): boolean {
  return (
    left.rows.length === right.rows.length &&
    left.rows.every(
      (row, index) =>
        row.id === right.rows[index]?.id &&
        row.sequence === right.rows[index]?.sequence &&
        row.lifecycle === right.rows[index]?.lifecycle,
    )
  )
}

function sameReadiness(
  left: ProjectionReadinessView,
  right: ProjectionReadinessView,
): boolean {
  return (
    left.revision === right.revision &&
    left.committedSequence === right.committedSequence &&
    left.highWatermark === right.highWatermark &&
    left.integrityWarnings.join("\0") === right.integrityWarnings.join("\0")
  )
}
