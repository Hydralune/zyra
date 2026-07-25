import type {
  CanonicalProjectionState,
  CausalEventProjection,
  CommandProjection,
  ProjectionSelector,
  RecoveryProjection,
  SessionProjection,
} from "../../state/contracts.ts"
import { projectionSelector } from "../../state/selectors.ts"
import {
  bounded,
  compareNumber,
  compareText,
  date,
  decimalFrom,
  fingerprint,
  integerFrom,
  listFrom,
  mergeRecords,
  ratio,
  record,
  sortStable,
  sum,
  textFrom,
  truthFrom,
  unique,
  values,
} from "./value.ts"

export type CheckpointDisposition = "committed" | "pending" | "restored" | "stale" | "conflict"
export type ContextPressure = "nominal" | "watch" | "compact-soon" | "critical" | "exceeded"

export interface SessionLineageNode {
  id: string
  taskId: string
  runId: string
  parentId?: string
  childIds: readonly string[]
  depth: number
  path: readonly string[]
  forked: boolean
  orphan: boolean
  cyclic: boolean
  active: boolean
  lifecycle: string
  revision: number
  sequence: number
  contextTokens: number
  contextLimit: number
  compactCount: number
  compactEpoch: number
  checkpointId?: string
  restoredCheckpointId?: string
  pendingCheckpointIds: readonly string[]
  workerIds: readonly string[]
  commandIds: readonly string[]
  eventIds: readonly string[]
}

export interface CheckpointRow {
  id: string
  sessionId: string
  taskId: string
  runId: string
  sequence: number
  eventId?: string
  commandId?: string
  recoveryId?: string
  disposition: CheckpointDisposition
  pendingWrites: number
  committedWrites: number
  workflowSignature?: string
  graphSignature?: string
  topologySignature?: string
  compactBoundaryId?: string
  source: string
  staleReason?: string
  conflictReason?: string
  exactResumeEligible: boolean
  correlationId?: string
}

export interface ContextCategory {
  id: string
  label: string
  tokens: number
  percentage: number
  retained: boolean
  compactable: boolean
  sourceIds: readonly string[]
  warning?: string
}

export interface ContextBudget {
  sessionId: string
  modelId?: string
  used: number
  limit: number
  reserve: number
  available: number
  percentage: number
  pressure: ContextPressure
  compactEpoch: number
  compactBoundaryId?: string
  categories: readonly ContextCategory[]
  estimated: boolean
  sourceEventIds: readonly string[]
}

export interface CompactSegment {
  id: string
  kind: string
  label: string
  tokens: number
  createdAt?: string
  sourceIds: readonly string[]
  decision: "retain" | "summarize" | "drop" | "restore"
  reason: string
  protected: boolean
  stale: boolean
}

export interface CompactPreview {
  sessionId: string
  revision: number
  currentTokens: number
  targetTokens: number
  projectedTokens: number
  savedTokens: number
  retainedTokens: number
  summarizedTokens: number
  droppedTokens: number
  restoreTokens: number
  eligible: boolean
  reason?: string
  fingerprint: string
  segments: readonly CompactSegment[]
  retainedSourceIds: readonly string[]
  droppedSourceIds: readonly string[]
  restoredSourceIds: readonly string[]
}

export interface SessionConsoleProjection {
  taskId: string
  revision: number
  committedSequence: number
  connected: boolean
  ready: boolean
  activeSessionId?: string
  lineage: readonly SessionLineageNode[]
  roots: readonly string[]
  orphans: readonly string[]
  cycles: readonly string[]
  checkpoints: readonly CheckpointRow[]
  context?: ContextBudget
  preview?: CompactPreview
  latestCommands: readonly CommandProjection[]
  conflictCount: number
  staleCount: number
  pendingWriteCount: number
  restoreSource?: string
}

export interface CompactPreviewOptions {
  targetTokens?: number
  reserveTokens?: number
  preserveRecentEvents?: number
  preserveKinds?: readonly string[]
}

const CONTEXT_COMMANDS = new Set([
  "/context",
  "/compact",
  "/resume",
  "/rewind",
  "/export",
])

export function selectSessionConsole(
  taskId: string,
  activeSessionId?: string,
  previewOptions: CompactPreviewOptions = {},
): ProjectionSelector<SessionConsoleProjection> {
  const optionKey = fingerprint([
    previewOptions.targetTokens,
    previewOptions.reserveTokens,
    previewOptions.preserveRecentEvents,
    previewOptions.preserveKinds,
  ])
  return projectionSelector(
    `session-console:${taskId}:${activeSessionId ?? "*"}:${optionKey}`,
    [
      `task:${taskId}`,
      "domain:session",
      "domain:command",
      "domain:recovery",
      "domain:event",
      "domain:worker",
      "domain:memory",
      "domain:artifact",
    ],
    (state) => buildSessionConsoleProjection(state, taskId, activeSessionId, previewOptions),
    sameSessionConsole,
  )
}

export function buildSessionConsoleProjection(
  state: CanonicalProjectionState,
  taskId: string,
  requestedSessionId?: string,
  previewOptions: CompactPreviewOptions = {},
): SessionConsoleProjection {
  const task = state.tasks[taskId]
  const sessions = Object.values(state.sessions)
    .filter((session) => session.taskId === taskId)
    .sort(compareSession)
  const eventIds = state.causality.byTask[taskId] ?? []
  const events = eventIds
    .map((eventId) => state.causality.byEvent[eventId])
    .filter((event): event is CausalEventProjection => Boolean(event))
    .sort((left, right) => left.sequence - right.sequence || left.eventId.localeCompare(right.eventId))
  const commands = Object.values(state.commands)
    .filter((command) => command.taskId === taskId)
    .filter((command) => CONTEXT_COMMANDS.has(command.commandName ?? ""))
    .sort((left, right) => right.sequence - left.sequence || right.id.localeCompare(left.id))
  const recoveries = Object.values(state.recoveries)
    .filter((recovery) => recovery.taskId === taskId)
    .sort((left, right) => left.sequence - right.sequence || left.id.localeCompare(right.id))
  const activeSessionId = chooseActiveSession(sessions, requestedSessionId, task?.sessionId)
  const lineageResult = buildLineage(state, sessions, events, commands, activeSessionId)
  const checkpoints = buildCheckpoints(
    state,
    taskId,
    activeSessionId,
    sessions,
    events,
    recoveries,
    commands,
  )
  const context = activeSessionId
    ? buildContextBudget(state, taskId, activeSessionId, events)
    : undefined
  const preview = context
    ? buildCompactPreview(state, context, events, previewOptions)
    : undefined
  const runtime = state.runtimes[taskId]
  const cursor = state.cursors[taskId]
  return Object.freeze({
    taskId,
    revision: state.revision,
    committedSequence: cursor?.committedSequence ?? runtime?.lastSequence ?? 0,
    connected: runtime?.connected ?? false,
    ready: Boolean(cursor?.snapshotComplete && runtime?.connected),
    activeSessionId,
    lineage: Object.freeze(lineageResult.rows),
    roots: Object.freeze(lineageResult.roots),
    orphans: Object.freeze(lineageResult.orphans),
    cycles: Object.freeze(lineageResult.cycles),
    checkpoints: Object.freeze(checkpoints),
    context,
    preview,
    latestCommands: Object.freeze(commands.slice(0, 50)),
    conflictCount: checkpoints.filter((checkpoint) => checkpoint.disposition === "conflict").length,
    staleCount: checkpoints.filter((checkpoint) => checkpoint.disposition === "stale").length,
    pendingWriteCount: sum(checkpoints.map((checkpoint) => checkpoint.pendingWrites)),
    restoreSource: restoreSource(state, activeSessionId, checkpoints),
  })
}

function chooseActiveSession(
  sessions: readonly SessionProjection[],
  requestedSessionId?: string,
  taskSessionId?: string,
): string | undefined {
  if (requestedSessionId && sessions.some((session) => session.id === requestedSessionId)) {
    return requestedSessionId
  }
  if (taskSessionId && sessions.some((session) => session.id === taskSessionId)) {
    return taskSessionId
  }
  const running = sessions
    .filter((session) => ["running", "waiting_tool", "waiting_policy", "recovering"].includes(session.lifecycle))
    .sort((left, right) => right.sequence - left.sequence)
  if (running[0]) return running[0].id
  return [...sessions].sort((left, right) => right.sequence - left.sequence)[0]?.id
}

function buildLineage(
  state: CanonicalProjectionState,
  sessions: readonly SessionProjection[],
  events: readonly CausalEventProjection[],
  commands: readonly CommandProjection[],
  activeSessionId?: string,
): {
  rows: SessionLineageNode[]
  roots: string[]
  orphans: string[]
  cycles: string[]
} {
  const byId = new Map(sessions.map((session) => [session.id, session]))
  const children = new Map<string, string[]>()
  const orphans: string[] = []
  for (const session of sessions) {
    if (!session.parentSessionId) continue
    const entries = children.get(session.parentSessionId) ?? []
    entries.push(session.id)
    children.set(session.parentSessionId, entries)
    if (!byId.has(session.parentSessionId)) orphans.push(session.id)
  }
  for (const session of sessions) {
    for (const childId of session.childSessionIds) {
      if (!byId.has(childId)) continue
      const entries = children.get(session.id) ?? []
      if (!entries.includes(childId)) entries.push(childId)
      children.set(session.id, entries)
    }
  }
  for (const entries of children.values()) entries.sort()
  const cycleIds = sessionCycleIds(sessions, byId)
  const roots = sessions
    .filter((session) => !session.parentSessionId || !byId.has(session.parentSessionId))
    .map((session) => session.id)
    .sort()
  const rows: SessionLineageNode[] = []
  const emitted = new Set<string>()
  const emit = (sessionId: string, depth: number, path: readonly string[]) => {
    if (emitted.has(sessionId)) return
    const session = byId.get(sessionId)
    if (!session) return
    emitted.add(sessionId)
    const nextPath = [...path, sessionId]
    const sessionEvents = events.filter((event) => event.sessionId === sessionId)
    const sessionCommands = commands.filter((command) => command.sessionId === sessionId)
    const attributes = mergeRecords(session.attributes, session.metadata)
    const workerIds = Object.values(state.workers)
      .filter((worker) => worker.sessionId === sessionId)
      .map((worker) => worker.id)
      .sort()
    const pendingCheckpointIds = checkpointIdsFromRecords(
      sessionEvents.filter((event) => /pending|write/i.test(event.eventType)),
    )
    rows.push(Object.freeze({
      id: session.id,
      taskId: session.taskId,
      runId: session.runId,
      parentId: session.parentSessionId,
      childIds: Object.freeze([...(children.get(session.id) ?? [])]),
      depth,
      path: Object.freeze(nextPath),
      forked: Boolean(session.parentSessionId),
      orphan: orphans.includes(session.id),
      cyclic: cycleIds.has(session.id),
      active: session.id === activeSessionId,
      lifecycle: session.lifecycle,
      revision: session.revision,
      sequence: session.sequence,
      contextTokens: session.contextTokens ?? integerFrom(attributes, ["context_tokens", "used_tokens"]),
      contextLimit: session.contextLimit ?? integerFrom(attributes, ["context_limit", "max_tokens"]),
      compactCount: session.compactCount,
      compactEpoch: integerFrom(attributes, ["compact_epoch", "context_epoch"], session.compactCount),
      checkpointId: session.checkpointId,
      restoredCheckpointId: session.restoredCheckpointId,
      pendingCheckpointIds: Object.freeze(pendingCheckpointIds),
      workerIds: Object.freeze(workerIds),
      commandIds: Object.freeze(sessionCommands.map((command) => command.id)),
      eventIds: Object.freeze(sessionEvents.map((event) => event.eventId)),
    }))
    for (const childId of children.get(session.id) ?? []) {
      emit(childId, depth + 1, nextPath)
    }
  }
  for (const root of roots) emit(root, 0, [])
  for (const session of sessions) emit(session.id, 0, [])
  return {
    rows,
    roots,
    orphans: unique(orphans).sort(),
    cycles: [...cycleIds].sort(),
  }
}

function sessionCycleIds(
  sessions: readonly SessionProjection[],
  byId: ReadonlyMap<string, SessionProjection>,
): Set<string> {
  const cycles = new Set<string>()
  for (const session of sessions) {
    const path: string[] = []
    const positions = new Map<string, number>()
    let current: SessionProjection | undefined = session
    while (current) {
      const position = positions.get(current.id)
      if (position !== undefined) {
        for (const id of path.slice(position)) cycles.add(id)
        break
      }
      positions.set(current.id, path.length)
      path.push(current.id)
      current = current.parentSessionId ? byId.get(current.parentSessionId) : undefined
    }
  }
  return cycles
}

function buildCheckpoints(
  state: CanonicalProjectionState,
  taskId: string,
  activeSessionId: string | undefined,
  sessions: readonly SessionProjection[],
  events: readonly CausalEventProjection[],
  recoveries: readonly RecoveryProjection[],
  commands: readonly CommandProjection[],
): CheckpointRow[] {
  const ids = new Set<string>()
  for (const event of events) if (event.checkpointId) ids.add(event.checkpointId)
  for (const session of sessions) {
    if (session.checkpointId) ids.add(session.checkpointId)
    if (session.restoredCheckpointId) ids.add(session.restoredCheckpointId)
    for (const id of listFrom(mergeRecords(session.attributes, session.metadata), [
      "checkpoint_ids",
      "pending_checkpoint_ids",
      "committed_checkpoint_ids",
    ])) ids.add(id)
  }
  for (const recovery of recoveries) if (recovery.resumedCheckpointId) ids.add(recovery.resumedCheckpointId)
  const rows = [...ids].map((id) => {
    const checkpointEvents = (state.causality.byCheckpoint[id] ?? [])
      .map((eventId) => state.causality.byEvent[eventId])
      .filter((event): event is CausalEventProjection => Boolean(event))
      .sort((left, right) => left.sequence - right.sequence)
    const latest = checkpointEvents.at(-1)
    const recovery = recoveries.find((entry) => entry.resumedCheckpointId === id)
    const command = commands.find((entry) =>
      entry.checkpointId === id ||
      checkpointEvents.some((event) => event.controlCommandId === entry.id))
    const session = sessions.find((entry) =>
      entry.checkpointId === id ||
      entry.restoredCheckpointId === id ||
      checkpointEvents.some((event) => event.sessionId === entry.id))
    const combined = mergeRecords(
      session?.attributes,
      session?.metadata,
      recovery?.attributes,
      recovery?.metadata,
      command?.attributes,
      command?.metadata,
    )
    const pendingWrites = countWrites(checkpointEvents, "pending") +
      integerFrom(combined, ["pending_writes", "pending_write_count"])
    const committedWrites = countWrites(checkpointEvents, "committed") +
      integerFrom(combined, ["committed_writes", "committed_write_count"])
    const conflictReason = checkpointConflictReason(
      taskId,
      activeSessionId,
      session,
      latest,
      combined,
    )
    const staleReason = conflictReason ? undefined : checkpointStaleReason(session, latest, combined)
    const restored = Boolean(
      session?.restoredCheckpointId === id ||
      recovery?.resumedCheckpointId === id ||
      checkpointEvents.some((event) => /resume|restore|rewind/i.test(event.eventType)),
    )
    const disposition: CheckpointDisposition = conflictReason
      ? "conflict"
      : staleReason
        ? "stale"
        : restored
          ? "restored"
          : pendingWrites > committedWrites
            ? "pending"
            : "committed"
    const sessionId = session?.id ?? latest?.sessionId ?? activeSessionId ?? ""
    return Object.freeze({
      id,
      sessionId,
      taskId,
      runId: session?.runId ?? latest?.runId ?? "",
      sequence: latest?.sequence ?? session?.sequence ?? 0,
      eventId: latest?.eventId,
      commandId: command?.id ?? latest?.controlCommandId,
      recoveryId: recovery?.id ?? latest?.recoveryId,
      disposition,
      pendingWrites,
      committedWrites,
      workflowSignature: textFrom(combined, ["workflow_signature"]),
      graphSignature: textFrom(combined, ["graph_signature"]),
      topologySignature: textFrom(combined, ["topology_signature"]),
      compactBoundaryId: textFrom(combined, ["compact_boundary_id", "boundary_id"]),
      source: textFrom(combined, ["checkpoint_source", "restore_source"], restored ? "recovery" : "event"),
      staleReason,
      conflictReason,
      exactResumeEligible: disposition !== "conflict" && disposition !== "stale" && Boolean(sessionId),
      correlationId: latest?.correlationId,
    })
  })
  return sortStable(rows, (left, right) =>
    compareNumber(right.sequence, left.sequence) ||
    compareText(left.id, right.id))
}

function checkpointIdsFromRecords(events: readonly CausalEventProjection[]): string[] {
  return unique(events.map((event) => event.checkpointId ?? "").filter(Boolean)).sort()
}

function countWrites(events: readonly CausalEventProjection[], kind: "pending" | "committed"): number {
  return events.filter((event) => new RegExp(kind, "i").test(event.eventType)).length
}

function checkpointConflictReason(
  taskId: string,
  activeSessionId: string | undefined,
  session: SessionProjection | undefined,
  event: CausalEventProjection | undefined,
  attributes: Record<string, unknown>,
): string | undefined {
  if (session && session.taskId !== taskId) return "checkpoint task differs from active task"
  if (event && event.taskId !== taskId) return "checkpoint event belongs to another task"
  if (
    activeSessionId &&
    session &&
    session.id !== activeSessionId &&
    !truthFrom(attributes, ["cross_session_resume_allowed"])
  ) return "checkpoint belongs to another session"
  if (truthFrom(attributes, ["checkpoint_conflict", "write_conflict", "lineage_conflict"])) {
    return textFrom(attributes, ["conflict_reason"], "checkpoint write-set conflict")
  }
  const expected = textFrom(attributes, ["expected_topology_signature"])
  const actual = textFrom(attributes, ["topology_signature"])
  if (expected && actual && expected !== actual) return "topology signature changed"
  return undefined
}

function checkpointStaleReason(
  session: SessionProjection | undefined,
  event: CausalEventProjection | undefined,
  attributes: Record<string, unknown>,
): string | undefined {
  if (truthFrom(attributes, ["stale", "checkpoint_stale"])) {
    return textFrom(attributes, ["stale_reason"], "checkpoint marked stale")
  }
  const expectedRevision = integerFrom(attributes, ["expected_revision"], -1)
  const actualRevision = integerFrom(attributes, ["checkpoint_revision", "revision"], -1)
  if (expectedRevision >= 0 && actualRevision >= 0 && expectedRevision !== actualRevision) {
    return "checkpoint revision does not match expected revision"
  }
  if (session && event && event.sequence < session.aggregateSequence - 100000) {
    return "checkpoint precedes retained session history"
  }
  return undefined
}

export function buildContextBudget(
  state: CanonicalProjectionState,
  taskId: string,
  sessionId: string,
  taskEvents?: readonly CausalEventProjection[],
): ContextBudget {
  const session = state.sessions[sessionId]
  const attributes = mergeRecords(session?.attributes, session?.metadata)
  const events = taskEvents ?? (state.causality.byTask[taskId] ?? [])
    .map((eventId) => state.causality.byEvent[eventId])
    .filter((event): event is CausalEventProjection => Boolean(event))
  const sessionEvents = events.filter((event) => !event.sessionId || event.sessionId === sessionId)
  const categories = buildContextCategories(state, taskId, sessionId, sessionEvents, attributes)
  const categoryTotal = sum(categories.map((category) => category.tokens))
  const used = session?.contextTokens ??
    integerFrom(attributes, ["context_tokens", "used_tokens", "total_tokens"], categoryTotal)
  const limit = session?.contextLimit ??
    integerFrom(attributes, ["context_limit", "raw_max_tokens", "max_tokens"], Math.max(used, 200000))
  const reserve = integerFrom(
    attributes,
    ["compact_reserve_tokens", "autocompact_buffer", "reserve_tokens"],
    Math.min(20000, Math.round(limit * 0.1)),
  )
  const percentage = ratio(used, limit)
  const pressure = contextPressure(used, limit, reserve)
  const compactEpoch = integerFrom(attributes, ["compact_epoch", "context_epoch"], session?.compactCount ?? 0)
  const compactBoundaryId = textFrom(attributes, ["compact_boundary_id", "boundary_id"]) || undefined
  return Object.freeze({
    sessionId,
    modelId: textFrom(attributes, ["model_id", "model"]) || undefined,
    used,
    limit,
    reserve,
    available: Math.max(0, limit - used),
    percentage,
    pressure,
    compactEpoch,
    compactBoundaryId,
    categories: Object.freeze(categories),
    estimated: session?.contextTokens === undefined,
    sourceEventIds: Object.freeze(sessionEvents.slice(-1000).map((event) => event.eventId)),
  })
}

function buildContextCategories(
  state: CanonicalProjectionState,
  taskId: string,
  sessionId: string,
  events: readonly CausalEventProjection[],
  attributes: Record<string, unknown>,
): ContextCategory[] {
  const explicit = values(attributes.context_categories ?? attributes.categories)
  const categories: ContextCategory[] = explicit.map((entry, index) => {
    const item = record(entry)
    const id = textFrom(item, ["id", "name", "kind"], `category-${index}`)
    return {
      id,
      label: textFrom(item, ["label", "name"], id),
      tokens: integerFrom(item, ["tokens", "token_count"]),
      percentage: decimalFrom(item, ["percentage", "ratio"]),
      retained: truthFrom(item, ["retained"], true),
      compactable: truthFrom(item, ["compactable"], true),
      sourceIds: Object.freeze(listFrom(item, ["source_ids", "sources"])),
      warning: textFrom(item, ["warning"]) || undefined,
    }
  })
  if (!categories.length) {
    const memory = Object.values(state.memories).filter((entry) => entry.taskId === taskId)
    const tools = Object.values(state.tools).filter((entry) => entry.taskId === taskId)
    const artifacts = Object.values(state.artifacts).filter((entry) => entry.taskId === taskId)
    const session = state.sessions[sessionId]
    categories.push(
      {
        id: "system",
        label: "System and policy",
        tokens: integerFrom(attributes, ["system_tokens"], 2500),
        percentage: 0,
        retained: true,
        compactable: false,
        sourceIds: Object.freeze([]),
      },
      {
        id: "messages",
        label: "Conversation",
        tokens: integerFrom(attributes, ["message_tokens"], events.length * 160),
        percentage: 0,
        retained: true,
        compactable: true,
        sourceIds: Object.freeze(events.map((event) => event.eventId)),
      },
      {
        id: "tools",
        label: "Tools and results",
        tokens: integerFrom(attributes, ["tool_tokens"], tools.length * 220),
        percentage: 0,
        retained: true,
        compactable: true,
        sourceIds: Object.freeze(tools.map((tool) => tool.id)),
      },
      {
        id: "memory",
        label: "Memory",
        tokens: integerFrom(attributes, ["memory_tokens"], sum(memory.map((entry) => entry.tokenCount ?? 0))),
        percentage: 0,
        retained: true,
        compactable: false,
        sourceIds: Object.freeze(memory.map((entry) => entry.id)),
      },
      {
        id: "artifacts",
        label: "Artifact references",
        tokens: integerFrom(attributes, ["artifact_tokens"], artifacts.length * 48),
        percentage: 0,
        retained: true,
        compactable: true,
        sourceIds: Object.freeze(artifacts.map((artifact) => artifact.id)),
      },
      {
        id: "reserve",
        label: "Auto-compact reserve",
        tokens: integerFrom(attributes, ["autocompact_buffer", "reserve_tokens"]),
        percentage: 0,
        retained: true,
        compactable: false,
        sourceIds: Object.freeze(session ? [session.id] : []),
      },
    )
  }
  const total = sum(categories.map((category) => category.tokens))
  return categories.map((category) => Object.freeze({
    ...category,
    percentage: category.percentage > 1
      ? bounded(category.percentage / 100, 0, 1)
      : category.percentage > 0
        ? bounded(category.percentage, 0, 1)
        : ratio(category.tokens, total),
  }))
}

function contextPressure(used: number, limit: number, reserve: number): ContextPressure {
  if (used >= limit) return "exceeded"
  const available = limit - used
  if (available <= Math.max(512, reserve * 0.25)) return "critical"
  if (available <= reserve) return "compact-soon"
  if (used / Math.max(1, limit) >= 0.7) return "watch"
  return "nominal"
}

export function buildCompactPreview(
  state: CanonicalProjectionState,
  context: ContextBudget,
  events: readonly CausalEventProjection[],
  options: CompactPreviewOptions = {},
): CompactPreview {
  const targetTokens = bounded(
    options.targetTokens ?? Math.max(1024, context.limit - Math.max(context.reserve * 2, context.limit * 0.25)),
    512,
    Math.max(512, context.limit),
  )
  const preserveRecent = bounded(options.preserveRecentEvents ?? 12, 0, 1000)
  const preserveKinds = new Set(options.preserveKinds ?? [
    "system",
    "policy",
    "permission",
    "checkpoint",
    "memory",
    "requirement",
  ])
  const candidates = compactSegments(state, context, events)
  const protectedRecent = new Set(
    [...events]
      .sort((left, right) => right.sequence - left.sequence)
      .slice(0, preserveRecent)
      .map((event) => event.eventId),
  )
  let projected = context.used
  const selected = [...candidates]
    .sort((left, right) => compactPriority(left, protectedRecent, preserveKinds) -
      compactPriority(right, protectedRecent, preserveKinds))
    .map((segment) => {
      if (segment.kind === "restore") {
        return { ...segment, decision: "restore" as const, protected: true }
      }
      const protectedSegment =
        segment.protected ||
        preserveKinds.has(segment.kind) ||
        segment.sourceIds.some((sourceId) => protectedRecent.has(sourceId))
      if (protectedSegment) return { ...segment, decision: "retain" as const, protected: true }
      if (projected <= targetTokens) return { ...segment, decision: "retain" as const }
      if (segment.kind === "tool-result" || segment.kind === "artifact-detail") {
        projected -= segment.tokens
        return { ...segment, decision: "drop" as const, reason: "replaced by canonical reference" }
      }
      const summarizedTokens = Math.max(64, Math.round(segment.tokens * 0.18))
      projected -= Math.max(0, segment.tokens - summarizedTokens)
      return { ...segment, decision: "summarize" as const, reason: "collapsed into compact summary" }
    })
  const segments = sortStable(selected, (left, right) =>
    compareNumber(
      Date.parse(left.createdAt ?? "") || 0,
      Date.parse(right.createdAt ?? "") || 0,
    ) || compareText(left.id, right.id))
  const droppedTokens = sum(segments.filter((entry) => entry.decision === "drop").map((entry) => entry.tokens))
  const summarizedTokens = sum(
    segments.filter((entry) => entry.decision === "summarize").map((entry) => entry.tokens),
  )
  const retainedTokens = sum(segments.filter((entry) => entry.decision === "retain").map((entry) => entry.tokens))
  const restoreTokens = sum(segments.filter((entry) => entry.decision === "restore").map((entry) => entry.tokens))
  const savedTokens = Math.max(0, context.used - projected)
  const eligible = context.used > targetTokens && savedTokens > 0
  return Object.freeze({
    sessionId: context.sessionId,
    revision: state.revision,
    currentTokens: context.used,
    targetTokens,
    projectedTokens: Math.max(0, projected),
    savedTokens,
    retainedTokens,
    summarizedTokens,
    droppedTokens,
    restoreTokens,
    eligible,
    reason: eligible ? undefined : context.used <= targetTokens
      ? "context is already below the requested target"
      : "no compactable segments are available",
    fingerprint: fingerprint([
      context.sessionId,
      state.revision,
      context.compactEpoch,
      targetTokens,
      segments.map((entry) => [entry.id, entry.decision, entry.tokens]),
    ]),
    segments: Object.freeze(segments.map((entry) => Object.freeze(entry))),
    retainedSourceIds: Object.freeze(unique(segments.filter((entry) => entry.decision === "retain").flatMap((entry) => entry.sourceIds))),
    droppedSourceIds: Object.freeze(unique(segments.filter((entry) => entry.decision === "drop").flatMap((entry) => entry.sourceIds))),
    restoredSourceIds: Object.freeze(unique(segments.filter((entry) => entry.decision === "restore").flatMap((entry) => entry.sourceIds))),
  })
}

function compactSegments(
  state: CanonicalProjectionState,
  context: ContextBudget,
  events: readonly CausalEventProjection[],
): CompactSegment[] {
  const selectedEvents = events.filter((event) => !event.sessionId || event.sessionId === context.sessionId)
  const eventSegments: CompactSegment[] = selectedEvents.map((event) => ({
    id: `event:${event.eventId}`,
    kind: classifyEventSegment(event),
    label: event.summary || event.eventType,
    tokens: estimateEventTokens(event),
    createdAt: date(event.createdAt),
    sourceIds: Object.freeze([event.eventId]),
    decision: "retain",
    reason: "part of active conversation",
    protected: Boolean(event.checkpointId || /permission|requirement|policy/i.test(event.eventType)),
    stale: !event.effective,
  }))
  const memorySegments: CompactSegment[] = Object.values(state.memories)
    .filter((memory) => memory.taskId === state.sessions[context.sessionId]?.taskId)
    .map((memory) => ({
      id: `memory:${memory.id}`,
      kind: "memory",
      label: memory.title || memory.key || memory.id,
      tokens: memory.tokenCount ?? 0,
      createdAt: memory.updatedAt,
      sourceIds: Object.freeze([memory.id, ...memory.sourceArtifactIds]),
      decision: "retain",
      reason: "canonical memory reference",
      protected: true,
      stale: memory.status === "stale",
    }))
  const artifactSegments: CompactSegment[] = Object.values(state.artifacts)
    .filter((artifact) => artifact.taskId === state.sessions[context.sessionId]?.taskId)
    .map((artifact) => ({
      id: `artifact:${artifact.id}`,
      kind: "artifact-detail",
      label: artifact.title || artifact.id,
      tokens: Math.max(16, Math.round(artifact.sizeBytes / 8)),
      createdAt: artifact.updatedAt,
      sourceIds: Object.freeze([artifact.id, artifact.producerEventId]),
      decision: "retain",
      reason: "artifact detail can be replaced by canonical reference",
      protected: false,
      stale: artifact.deleted,
    }))
  return [...eventSegments, ...memorySegments, ...artifactSegments]
}

function classifyEventSegment(event: CausalEventProjection): string {
  const kind = event.eventType.toLowerCase()
  if (kind.includes("tool") && kind.includes("result")) return "tool-result"
  if (kind.includes("tool")) return "tool"
  if (kind.includes("permission")) return "permission"
  if (kind.includes("checkpoint")) return "checkpoint"
  if (kind.includes("memory")) return "memory"
  if (kind.includes("requirement")) return "requirement"
  if (kind.includes("system") || kind.includes("policy")) return "system"
  if (kind.includes("restore") || kind.includes("resume")) return "restore"
  return "message"
}

function estimateEventTokens(event: CausalEventProjection): number {
  const summaryBytes = new TextEncoder().encode(event.summary ?? "").byteLength
  const identityBytes = new TextEncoder().encode(event.eventType + event.eventId).byteLength
  return Math.max(16, Math.ceil((summaryBytes + identityBytes) / 3.5))
}

function compactPriority(
  segment: CompactSegment,
  protectedRecent: ReadonlySet<string>,
  preserveKinds: ReadonlySet<string>,
): number {
  if (segment.protected || preserveKinds.has(segment.kind)) return 10000
  if (segment.sourceIds.some((sourceId) => protectedRecent.has(sourceId))) return 9000
  if (segment.stale) return -100
  if (segment.kind === "tool-result") return 0
  if (segment.kind === "artifact-detail") return 10
  if (segment.kind === "message") return 100
  return 500
}

function restoreSource(
  state: CanonicalProjectionState,
  sessionId: string | undefined,
  checkpoints: readonly CheckpointRow[],
): string | undefined {
  if (!sessionId) return undefined
  const session = state.sessions[sessionId]
  const attributes = mergeRecords(session?.attributes, session?.metadata)
  const explicit = textFrom(attributes, ["restore_source", "snapshot_source", "resume_source"])
  if (explicit) return explicit
  const restored = checkpoints.find((checkpoint) =>
    checkpoint.sessionId === sessionId && checkpoint.disposition === "restored")
  if (restored) return restored.source
  const runtime = state.runtimes[session.taskId]
  if (runtime?.connected) return "live-event-stream"
  if (state.cursors[session.taskId]?.snapshotComplete) return "persisted-projection-snapshot"
  return undefined
}

function compareSession(left: SessionProjection, right: SessionProjection): number {
  if (left.parentSessionId === right.id) return 1
  if (right.parentSessionId === left.id) return -1
  return left.sequence - right.sequence || left.id.localeCompare(right.id)
}

function sameSessionConsole(
  left: SessionConsoleProjection,
  right: SessionConsoleProjection,
): boolean {
  return left === right ||
    (
      left.revision === right.revision &&
      left.committedSequence === right.committedSequence &&
      left.activeSessionId === right.activeSessionId &&
      left.connected === right.connected &&
      left.preview?.fingerprint === right.preview?.fingerprint
    )
}
