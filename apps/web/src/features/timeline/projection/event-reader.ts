import type {
  ArtifactProjection,
  CanonicalProjectionState,
  CausalEventProjection,
  CommandProjection,
  MutationProjection,
  PermissionProjection,
  ProjectionEntity,
  RecoveryProjection,
  SchedulerProjection,
  ToolProjection,
  WorkerProjection,
} from "../../../state/contracts.ts"
import type {
  TimelineCanonicalOrder,
  TimelineEventFacts,
  TimelineEventKindValue,
  TimelinePhaseValue,
} from "./contracts.ts"
import {
  TimelineEventKind,
  TimelinePhase,
  lifecycleToTimelinePhase,
} from "./contracts.ts"

const BLOCKED_ATTRIBUTE_KEYS = new Set([
  "api_key",
  "apikey",
  "authorization",
  "cookie",
  "cookies",
  "credential",
  "credentials",
  "dom",
  "html",
  "password",
  "prompt",
  "raw",
  "secret",
  "token",
  "tool_arguments",
  "tool_input",
])

const SAFE_ATTRIBUTE_KEYS = new Set([
  "action",
  "action_id",
  "action_name",
  "attempt",
  "backend",
  "backend_id",
  "background_job_id",
  "browser_step_id",
  "candidate_id",
  "checkpoint_id",
  "command_id",
  "decision",
  "decision_id",
  "duration_ms",
  "error_code",
  "failure_id",
  "generation",
  "health",
  "job_id",
  "job_type",
  "lease_id",
  "lifecycle",
  "location",
  "model_id",
  "mutation_id",
  "node_id",
  "operation",
  "permission_id",
  "permission_kind",
  "phase",
  "placement_id",
  "provider_id",
  "reason",
  "recovery_id",
  "request_id",
  "resource_class",
  "result",
  "result_status",
  "revision",
  "role",
  "route_id",
  "status",
  "step_id",
  "strategy",
  "summary",
  "tool_call_id",
  "tool_name",
  "worker_id",
])

const FAILURE_TOKENS = new Set([
  "error",
  "failed",
  "failure",
  "fault",
  "lost",
  "timeout",
  "unhealthy",
  "watchdog",
])

const TERMINAL_SUCCESS_TOKENS = new Set([
  "complete",
  "completed",
  "committed",
  "done",
  "finished",
  "succeeded",
  "success",
])

const CANCEL_TOKENS = new Set([
  "abort",
  "aborted",
  "cancel",
  "cancelled",
  "canceled",
  "killed",
  "stopped",
])

const WAITING_TOOL_TOKENS = new Set([
  "tool",
  "action",
  "browser",
  "shell",
  "terminal",
])

const WAITING_POLICY_TOKENS = new Set([
  "approval",
  "permission",
  "policy",
  "confirmation",
  "interrupt",
])

function parseTime(value: string): number {
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function canonicalOrder(
  event: CausalEventProjection,
): TimelineCanonicalOrder {
  return Object.freeze({
    sequence: event.sequence,
    aggregateSequence: event.aggregateSequence,
    committedAtMs: parseTime(event.committedAt),
    createdAtMs: parseTime(event.createdAt),
    eventId: event.eventId,
  })
}

export function compareTimelineEvents(
  left: CausalEventProjection,
  right: CausalEventProjection,
): number {
  return (
    left.sequence - right.sequence ||
    left.aggregateSequence - right.aggregateSequence ||
    parseTime(left.committedAt) - parseTime(right.committedAt) ||
    left.eventId.localeCompare(right.eventId)
  )
}

export function compareTimelineOrder(
  left: TimelineCanonicalOrder,
  right: TimelineCanonicalOrder,
): number {
  return (
    left.sequence - right.sequence ||
    left.aggregateSequence - right.aggregateSequence ||
    left.committedAtMs - right.committedAtMs ||
    left.eventId.localeCompare(right.eventId)
  )
}

export function eventTokens(value: string): readonly string[] {
  return Object.freeze(
    value
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter(Boolean),
  )
}

export function hasToken(
  event: CausalEventProjection,
  candidates: ReadonlySet<string>,
): boolean {
  for (const token of eventTokens(event.eventType)) {
    if (candidates.has(token)) return true
  }
  return false
}

export function entityRefIds(
  event: CausalEventProjection,
  domain: string,
): readonly string[] {
  const prefix = `${domain}:`
  return Object.freeze(
    event.entityRefs
      .filter((value) => value.startsWith(prefix))
      .map((value) => value.slice(prefix.length))
      .filter(Boolean),
  )
}

export function firstEntityRef(
  event: CausalEventProjection,
  domain: string,
): string | undefined {
  return entityRefIds(event, domain)[0]
}

function plainRecord(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
  return value as Record<string, unknown>
}

export function readString(
  source: unknown,
  ...keys: readonly string[]
): string | undefined {
  const record = plainRecord(source)
  if (!record) return undefined
  for (const key of keys) {
    const value = record[key]
    if (typeof value === "string" && value.trim()) return value.trim()
    if (typeof value === "number" && Number.isFinite(value)) return String(value)
  }
  return undefined
}

export function readNumber(
  source: unknown,
  ...keys: readonly string[]
): number | undefined {
  const record = plainRecord(source)
  if (!record) return undefined
  for (const key of keys) {
    const value = record[key]
    if (typeof value === "number" && Number.isFinite(value)) return value
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value)
      if (Number.isFinite(parsed)) return parsed
    }
  }
  return undefined
}

export function readBoolean(
  source: unknown,
  ...keys: readonly string[]
): boolean | undefined {
  const record = plainRecord(source)
  if (!record) return undefined
  for (const key of keys) {
    const value = record[key]
    if (typeof value === "boolean") return value
    if (value === "true" || value === "1" || value === 1) return true
    if (value === "false" || value === "0" || value === 0) return false
  }
  return undefined
}

export function readStringArray(
  source: unknown,
  ...keys: readonly string[]
): readonly string[] {
  const record = plainRecord(source)
  if (!record) return Object.freeze([])
  for (const key of keys) {
    const value = record[key]
    if (!Array.isArray(value)) continue
    return Object.freeze(
      value
        .filter(
          (candidate): candidate is string | number =>
            typeof candidate === "string" || typeof candidate === "number",
        )
        .map(String)
        .filter(Boolean),
    )
  }
  return Object.freeze([])
}

export function safeEntityAttributes(
  ...entities: readonly (ProjectionEntity | undefined)[]
): Readonly<Record<string, unknown>> {
  const result: Record<string, unknown> = {}
  for (const entity of entities) {
    if (!entity) continue
    const sources = [entity.attributes, entity.metadata]
    for (const source of sources) {
      for (const [rawKey, value] of Object.entries(source)) {
        const key = rawKey.toLowerCase()
        if (BLOCKED_ATTRIBUTE_KEYS.has(key)) continue
        if (!SAFE_ATTRIBUTE_KEYS.has(key)) continue
        if (
          typeof value === "string" ||
          typeof value === "number" ||
          typeof value === "boolean"
        ) {
          result[key] = value
        } else if (
          Array.isArray(value) &&
          value.length <= 32 &&
          value.every(
            (candidate) =>
              typeof candidate === "string" ||
              typeof candidate === "number" ||
              typeof candidate === "boolean",
          )
        ) {
          result[key] = Object.freeze([...value])
        }
      }
    }
  }
  return Object.freeze(result)
}

export function workerForEvent(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): WorkerProjection | undefined {
  if (event.workerId && state.workers[event.workerId]) {
    return state.workers[event.workerId]
  }
  const ref = firstEntityRef(event, "worker")
  if (ref && state.workers[ref]) return state.workers[ref]
  const candidates = Object.values(state.workers)
    .filter((worker) => worker.taskId === event.taskId)
    .filter((worker) => worker.nodeId && worker.nodeId === event.nodeId)
    .filter((worker) => worker.sequence <= event.sequence)
    .sort(
      (left, right) =>
        right.sequence - left.sequence || left.id.localeCompare(right.id),
    )
  return candidates[0]
}

export function toolForEvent(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): ToolProjection | undefined {
  if (event.toolCallId && state.tools[event.toolCallId]) {
    return state.tools[event.toolCallId]
  }
  const ref = firstEntityRef(event, "tool")
  if (ref && state.tools[ref]) return state.tools[ref]
  return undefined
}

export function artifactsForEvent(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
  tool?: ToolProjection,
): readonly ArtifactProjection[] {
  const ids = new Set<string>([
    ...event.artifactIds,
    ...(tool?.artifactIds ?? []),
    ...(tool?.resultArtifactIds ?? []),
    ...entityRefIds(event, "artifact"),
  ])
  return Object.freeze(
    [...ids]
      .map((id) => state.artifacts[id])
      .filter((value): value is ArtifactProjection => Boolean(value))
      .sort(
        (left, right) =>
          left.sequence - right.sequence || left.id.localeCompare(right.id),
      ),
  )
}

export function recoveryForEvent(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): RecoveryProjection | undefined {
  if (event.recoveryId && state.recoveries[event.recoveryId]) {
    return state.recoveries[event.recoveryId]
  }
  const ref = firstEntityRef(event, "recovery")
  if (ref && state.recoveries[ref]) return state.recoveries[ref]
  if (event.failureId) {
    return Object.values(state.recoveries)
      .filter((item) => item.taskId === event.taskId)
      .filter((item) => item.failureId === event.failureId)
      .sort(
        (left, right) =>
          left.sequence - right.sequence || left.id.localeCompare(right.id),
      )[0]
  }
  return undefined
}

export function permissionForEvent(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
  tool?: ToolProjection,
): PermissionProjection | undefined {
  const id =
    tool?.permissionId ??
    firstEntityRef(event, "permission") ??
    firstEntityRef(event, "policy")
  if (id && state.permissions[id]) return state.permissions[id]
  return undefined
}

export function schedulerForEvent(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): SchedulerProjection | undefined {
  const ref =
    firstEntityRef(event, "scheduler") ??
    firstEntityRef(event, "route") ??
    firstEntityRef(event, "placement")
  if (ref && state.schedulers[ref]) return state.schedulers[ref]
  const worker = workerForEvent(state, event)
  if (!worker) return undefined
  return Object.values(state.schedulers)
    .filter((item) => item.taskId === event.taskId)
    .filter(
      (item) =>
        (worker.routeId && item.routeId === worker.routeId) ||
        (worker.placementId && item.placementId === worker.placementId),
    )
    .sort(
      (left, right) =>
        right.sequence - left.sequence || left.id.localeCompare(right.id),
    )[0]
}

export function commandForEvent(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): CommandProjection | undefined {
  const id =
    event.controlCommandId ??
    firstEntityRef(event, "command") ??
    firstEntityRef(event, "control")
  return id ? state.commands[id] : undefined
}

export function mutationForEvent(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): MutationProjection | undefined {
  return state.mutations[event.mutationId]
}

export function inferTimelinePhase(
  event: CausalEventProjection,
  entities: {
    worker?: WorkerProjection
    tool?: ToolProjection
    recovery?: RecoveryProjection
    permission?: PermissionProjection
    command?: CommandProjection
  },
): TimelinePhaseValue {
  const tokens = eventTokens(event.eventType)
  const type = event.eventType.toLowerCase()
  if (hasToken(event, CANCEL_TOKENS)) return TimelinePhase.CANCELLED
  if (
    event.failureId ||
    hasToken(event, FAILURE_TOKENS) ||
    entities.tool?.errorCode ||
    entities.command?.errorCode
  ) {
    if (event.eventType.includes("recovery") && !event.terminal) {
      return TimelinePhase.RECOVERING
    }
    return TimelinePhase.FAILED
  }
  if (
    event.recoveryId ||
    entities.recovery ||
    event.eventType.includes("recover") ||
    event.eventType.includes("retry") ||
    event.eventType.includes("resume")
  ) {
    if (event.terminal && hasToken(event, TERMINAL_SUCCESS_TOKENS)) {
      return TimelinePhase.COMPLETED
    }
    return TimelinePhase.RECOVERING
  }
  if (tokens.includes("queued") || tokens.includes("submitted")) {
    return TimelinePhase.QUEUED
  }
  if (tokens.includes("admitted") || tokens.includes("assigned")) {
    return TimelinePhase.ADMITTED
  }
  if (
    tokens.includes("starting") ||
    tokens.includes("initializing") ||
    tokens.includes("spawning") ||
    (tokens.includes("started") &&
      (type.startsWith("worker.") || type.includes("runtime")))
  ) {
    return TimelinePhase.STARTING
  }
  if (
    !type.includes("permission") &&
    !type.includes("policy") &&
    (entities.tool || event.toolCallId) &&
    (type.includes("started") ||
      type.includes("requested") ||
      type.includes("running") ||
      type.includes("invoked") ||
      type.includes("action"))
  ) {
    return TimelinePhase.WAITING_TOOL
  }
  if (
    entities.permission ||
    hasToken(event, WAITING_POLICY_TOKENS) ||
    event.eventType.includes("policy")
  ) {
    if (
      type.includes("asked") ||
      type.includes("requested") ||
      type.includes("pending") ||
      type.includes("awaiting")
    ) {
      return TimelinePhase.WAITING_POLICY
    }
    if (entities.permission?.resolvedAt || entities.permission?.terminal) {
      return TimelinePhase.RUNNING
    }
    return TimelinePhase.WAITING_POLICY
  }
  if (
    entities.tool ||
    event.toolCallId ||
    hasToken(event, WAITING_TOOL_TOKENS)
  ) {
    if (
      type.includes("started") ||
      type.includes("requested") ||
      type.includes("running") ||
      type.includes("invoked")
    ) {
      return TimelinePhase.WAITING_TOOL
    }
    if (entities.tool?.terminal || event.terminal) return TimelinePhase.RUNNING
    return TimelinePhase.WAITING_TOOL
  }
  // Canonical entity projections intentionally expose the latest worker
  // lifecycle. Historical timeline rows must therefore prefer an explicit
  // event-local terminal or running transition before consulting that latest
  // entity snapshot, otherwise a later failure rewrites earlier activity.
  if (
    event.terminal &&
    hasToken(event, TERMINAL_SUCCESS_TOKENS)
  ) {
    return TimelinePhase.COMPLETED
  }
  if (tokens.includes("running") || tokens.includes("active")) {
    return TimelinePhase.RUNNING
  }
  const lifecycle = entities.worker?.lifecycle
  if (lifecycle) {
    const phase = lifecycleToTimelinePhase(lifecycle)
    if (phase !== TimelinePhase.UNKNOWN) return phase
  }
  if (
    event.terminal &&
    (hasToken(event, TERMINAL_SUCCESS_TOKENS) || event.effective)
  ) {
    return TimelinePhase.COMPLETED
  }
  if (tokens.includes("running") || event.effective) {
    return TimelinePhase.RUNNING
  }
  return TimelinePhase.UNKNOWN
}

export function inferTimelineKinds(
  event: CausalEventProjection,
  facts: {
    worker?: WorkerProjection
    tool?: ToolProjection
    artifacts: readonly ArtifactProjection[]
    recovery?: RecoveryProjection
    permission?: PermissionProjection
    scheduler?: SchedulerProjection
    command?: CommandProjection
    mutation?: MutationProjection
    leaseId?: string
    backgroundJobId?: string
    browserStepId?: string
  },
): readonly TimelineEventKindValue[] {
  const result = new Set<TimelineEventKindValue>()
  const type = event.eventType.toLowerCase()
  if (facts.worker || event.workerId || type.startsWith("worker.")) {
    result.add(TimelineEventKind.WORKER)
  }
  if (facts.leaseId || type.includes("lease")) result.add(TimelineEventKind.LEASE)
  if (
    facts.scheduler?.routeId ||
    type.includes("route") ||
    entityRefIds(event, "route").length
  ) {
    result.add(TimelineEventKind.ROUTE)
  }
  if (
    facts.scheduler?.placementId ||
    type.includes("placement") ||
    entityRefIds(event, "placement").length
  ) {
    result.add(TimelineEventKind.PLACEMENT)
  }
  if (facts.tool || event.toolCallId || type.includes("tool")) {
    result.add(TimelineEventKind.TOOL)
  }
  if (
    facts.permission ||
    type.includes("permission") ||
    type.includes("policy") ||
    type.includes("approval")
  ) {
    result.add(TimelineEventKind.POLICY)
  }
  if (facts.artifacts.length || type.includes("artifact")) {
    result.add(TimelineEventKind.ARTIFACT)
  }
  if (
    event.failureId ||
    hasToken(event, FAILURE_TOKENS) ||
    type.includes("watchdog")
  ) {
    result.add(TimelineEventKind.FAILURE)
  }
  if (
    event.recoveryId ||
    facts.recovery ||
    type.includes("recover") ||
    type.includes("retry") ||
    type.includes("resume")
  ) {
    result.add(TimelineEventKind.RECOVERY)
  }
  if (event.checkpointId || type.includes("checkpoint")) {
    result.add(TimelineEventKind.CHECKPOINT)
  }
  if (facts.backgroundJobId || type.includes("background") || type.includes("park")) {
    result.add(TimelineEventKind.BACKGROUND)
  }
  if (
    facts.browserStepId ||
    type.includes("browser") ||
    type.includes("dom.") ||
    type.includes("page.")
  ) {
    result.add(TimelineEventKind.BROWSER_STEP)
  }
  if (
    type.includes("topology") ||
    type.includes("graph.") ||
    type.includes("node.")
  ) {
    result.add(TimelineEventKind.TOPOLOGY)
  }
  if (facts.mutation) result.add(TimelineEventKind.MUTATION)
  if (facts.command || event.controlCommandId) {
    result.add(TimelineEventKind.COMMAND)
  }
  if (event.sessionId || type.includes("session")) {
    result.add(TimelineEventKind.SESSION)
  }
  if (type.startsWith("task.") || type.includes("requirement")) {
    result.add(TimelineEventKind.TASK)
  }
  if (result.size === 0) result.add(TimelineEventKind.OTHER)
  return Object.freeze([...result])
}

function safeTitle(
  event: CausalEventProjection,
  entities: {
    worker?: WorkerProjection
    tool?: ToolProjection
    recovery?: RecoveryProjection
    permission?: PermissionProjection
    scheduler?: SchedulerProjection
    command?: CommandProjection
  },
): string {
  if (entities.tool?.toolName) return entities.tool.toolName
  if (entities.permission?.toolName) {
    return `Permission: ${entities.permission.toolName}`
  }
  if (entities.recovery?.strategy) {
    return `Recovery: ${entities.recovery.strategy}`
  }
  if (entities.command?.commandName) {
    return `Command: ${entities.command.commandName}`
  }
  if (entities.scheduler?.routeId) {
    return `Route: ${entities.scheduler.routeId}`
  }
  if (entities.worker?.title && entities.worker.title !== entities.worker.id) {
    return entities.worker.title
  }
  return event.eventType
}

function safeSummary(
  event: CausalEventProjection,
  entities: {
    worker?: WorkerProjection
    tool?: ToolProjection
    recovery?: RecoveryProjection
    permission?: PermissionProjection
    command?: CommandProjection
  },
): string {
  const candidates = [
    event.summary,
    entities.recovery?.reason,
    entities.permission?.reason,
    entities.command?.summary,
    entities.tool?.summary,
    entities.worker?.summary,
  ]
  for (const candidate of candidates) {
    if (!candidate?.trim()) continue
    return candidate.trim().slice(0, 600)
  }
  return event.eventType
}

function missingReferences(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
  facts: {
    worker?: WorkerProjection
    tool?: ToolProjection
    artifacts: readonly ArtifactProjection[]
    recovery?: RecoveryProjection
    permission?: PermissionProjection
    scheduler?: SchedulerProjection
    command?: CommandProjection
    mutation?: MutationProjection
  },
): readonly string[] {
  const missing = new Set<string>()
  if (event.causationId && !state.causality.byEvent[event.causationId]) {
    missing.add(`event:${event.causationId}`)
  }
  if (event.workerId && !facts.worker) missing.add(`worker:${event.workerId}`)
  if (event.toolCallId && !facts.tool) missing.add(`tool:${event.toolCallId}`)
  if (event.recoveryId && !facts.recovery) {
    missing.add(`recovery:${event.recoveryId}`)
  }
  if (event.mutationId && !facts.mutation) {
    missing.add(`mutation:${event.mutationId}`)
  }
  if (event.controlCommandId && !facts.command) {
    missing.add(`command:${event.controlCommandId}`)
  }
  const foundArtifacts = new Set(facts.artifacts.map((artifact) => artifact.id))
  for (const artifactId of event.artifactIds) {
    if (!foundArtifacts.has(artifactId)) missing.add(`artifact:${artifactId}`)
  }
  for (const ref of event.entityRefs) {
    const [domain, id] = ref.split(":", 2)
    if (!id) continue
    if (domain === "permission" && !state.permissions[id]) missing.add(ref)
    if (domain === "scheduler" && !state.schedulers[id]) missing.add(ref)
    if (domain === "worker" && !state.workers[id]) missing.add(ref)
    if (domain === "tool" && !state.tools[id]) missing.add(ref)
    if (domain === "artifact" && !state.artifacts[id]) missing.add(ref)
    if (domain === "recovery" && !state.recoveries[id]) missing.add(ref)
  }
  return Object.freeze([...missing].sort())
}

export function readTimelineEventFacts(
  state: CanonicalProjectionState,
  event: CausalEventProjection,
): TimelineEventFacts {
  const worker = workerForEvent(state, event)
  const tool = toolForEvent(state, event)
  const artifacts = artifactsForEvent(state, event, tool)
  const recovery = recoveryForEvent(state, event)
  const permission = permissionForEvent(state, event, tool)
  const scheduler = schedulerForEvent(state, event)
  const command = commandForEvent(state, event)
  const mutation = mutationForEvent(state, event)
  const safeAttributes = safeEntityAttributes(
    worker,
    tool,
    recovery,
    permission,
    scheduler,
    command,
  )
  const leaseId =
    worker?.leaseId ??
    readString(safeAttributes, "lease_id") ??
    firstEntityRef(event, "lease")
  const routeId =
    worker?.routeId ??
    scheduler?.routeId ??
    readString(safeAttributes, "route_id") ??
    firstEntityRef(event, "route")
  const placementId =
    worker?.placementId ??
    scheduler?.placementId ??
    readString(safeAttributes, "placement_id") ??
    firstEntityRef(event, "placement")
  const failureId =
    event.failureId ??
    recovery?.failureId ??
    readString(safeAttributes, "failure_id") ??
    firstEntityRef(event, "failure")
  const recoveryEventType = event.eventType.toLowerCase()
  const isRecoveryEvent =
    Boolean(event.recoveryId) ||
    recoveryEventType.includes("recover") ||
    recoveryEventType.includes("retry") ||
    recoveryEventType.includes("resume") ||
    recoveryEventType.includes("replan")
  const recoveryId =
    event.recoveryId ??
    (isRecoveryEvent ? recovery?.id : undefined) ??
    (isRecoveryEvent
      ? readString(safeAttributes, "recovery_id") ??
        firstEntityRef(event, "recovery")
      : undefined)
  const permissionId =
    permission?.id ??
    tool?.permissionId ??
    readString(safeAttributes, "permission_id", "request_id") ??
    firstEntityRef(event, "permission")
  const backgroundJobId =
    readString(safeAttributes, "background_job_id", "job_id") ??
    firstEntityRef(event, "background-job") ??
    firstEntityRef(event, "job")
  const browserStepId =
    readString(safeAttributes, "browser_step_id", "step_id", "action_id") ??
    firstEntityRef(event, "browser-step")
  const related = {
    worker,
    tool,
    artifacts,
    recovery,
    permission,
    scheduler,
    command,
    mutation,
    leaseId,
    backgroundJobId,
    browserStepId,
  }
  const kinds = inferTimelineKinds(event, related)
  const phase = inferTimelinePhase(event, related)
  const missingRefs = missingReferences(state, event, related)
  return Object.freeze({
    event,
    order: canonicalOrder(event),
    kinds,
    phase,
    worker,
    tool,
    artifacts,
    recovery,
    permission,
    scheduler,
    command,
    mutation,
    leaseId,
    routeId,
    placementId,
    failureId,
    recoveryId,
    permissionId,
    backgroundJobId,
    browserStepId,
    title: safeTitle(event, related),
    summary: safeSummary(event, related),
    partial: missingRefs.length > 0,
    missingRefs,
    safeAttributes,
  })
}

export function readTaskTimelineEvents(
  state: CanonicalProjectionState,
  taskId: string,
  options: {
    includeNonEffective: boolean
    includePartial: boolean
    maximumEvents: number
  },
): readonly TimelineEventFacts[] {
  const ids = state.causality.byTask[taskId] ?? []
  const events = ids
    .map((eventId) => state.causality.byEvent[eventId])
    .filter((event): event is CausalEventProjection => Boolean(event))
    .filter(
      (event) =>
        options.includeNonEffective || event.effective || event.terminal,
    )
    .sort(compareTimelineEvents)
  const bounded =
    events.length > options.maximumEvents
      ? events.slice(events.length - options.maximumEvents)
      : events
  const facts = bounded.map((event) => readTimelineEventFacts(state, event))
  if (options.includePartial) return Object.freeze(facts)
  return Object.freeze(facts.filter((item) => !item.partial))
}

export function phaseSeverity(phase: TimelinePhaseValue): number {
  switch (phase) {
    case TimelinePhase.FAILED:
      return 100
    case TimelinePhase.CANCELLED:
      return 90
    case TimelinePhase.RECOVERING:
      return 80
    case TimelinePhase.WAITING_POLICY:
      return 70
    case TimelinePhase.WAITING_TOOL:
      return 60
    case TimelinePhase.RUNNING:
      return 50
    case TimelinePhase.STARTING:
      return 40
    case TimelinePhase.ADMITTED:
      return 30
    case TimelinePhase.QUEUED:
      return 20
    case TimelinePhase.COMPLETED:
      return 10
    default:
      return 0
  }
}

export function primaryKind(
  kinds: readonly TimelineEventKindValue[],
): TimelineEventKindValue {
  const priority: readonly TimelineEventKindValue[] = [
    TimelineEventKind.FAILURE,
    TimelineEventKind.RECOVERY,
    TimelineEventKind.POLICY,
    TimelineEventKind.TOOL,
    TimelineEventKind.BROWSER_STEP,
    TimelineEventKind.BACKGROUND,
    TimelineEventKind.ARTIFACT,
    TimelineEventKind.LEASE,
    TimelineEventKind.ROUTE,
    TimelineEventKind.PLACEMENT,
    TimelineEventKind.TOPOLOGY,
    TimelineEventKind.WORKER,
    TimelineEventKind.COMMAND,
    TimelineEventKind.CHECKPOINT,
    TimelineEventKind.SESSION,
    TimelineEventKind.TASK,
    TimelineEventKind.MUTATION,
    TimelineEventKind.OTHER,
  ]
  for (const candidate of priority) {
    if (kinds.includes(candidate)) return candidate
  }
  return TimelineEventKind.OTHER
}
