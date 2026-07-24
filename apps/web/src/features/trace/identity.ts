import type {
  CanonicalProjectionState,
  CausalEventProjection,
  ProjectionEntity,
} from "../../state/contracts.ts"
import type { TimelineEventFacts } from "../timeline/projection/contracts.ts"
import {
  TraceCompleteness,
  TraceNodeKind,
  TraceSemanticKind,
  type TraceCompletenessValue,
  type TraceEventAdmission,
  type TraceIdentity,
  type TraceNodeKindValue,
  type TraceSemanticKindValue,
  type TraceTypedRefs,
} from "./contracts.ts"

const MAX_ID_LENGTH = 512
const MAX_ATTRIBUTE_STRING = 1_024
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/

const ENTITY_KIND_BY_DOMAIN: Readonly<Record<string, TraceNodeKindValue>> =
  Object.freeze({
    task: TraceNodeKind.TASK,
    node: TraceNodeKind.NODE,
    worker: TraceNodeKind.WORKER,
    tool: TraceNodeKind.TOOL,
    artifact: TraceNodeKind.ARTIFACT,
    scheduler: TraceNodeKind.PLACEMENT,
    recovery: TraceNodeKind.RECOVERY,
    command: TraceNodeKind.COMMAND,
    permission: TraceNodeKind.PERMISSION,
    session: TraceNodeKind.SESSION,
  })

const TYPED_ATTRIBUTE_KEYS = Object.freeze({
  permissionId: ["permission_id", "permission_request_id", "permissionId"],
  routeId: ["route_id", "routeId"],
  placementId: ["placement_id", "placementId"],
  providerId: ["provider_id", "providerId"],
  mcpCallId: ["mcp_call_id", "mcpCallId", "request_id"],
  mcpServerId: ["mcp_server_id", "server_id", "serverId"],
  skillId: ["skill_id", "skillId", "skill_name"],
  subagentId: ["subagent_id", "subagentId", "agent_id"],
  parentToolCallId: ["parent_tool_call_id", "parentToolCallId"],
  yieldId: ["yield_id", "yieldId"],
  terminalSessionId: ["terminal_session_id", "pty_session_id", "terminalSessionId"],
  terminalFrameId: ["terminal_frame_id", "pty_frame_id", "frame_id"],
  browserSessionId: ["browser_session_id", "browserSessionId"],
  browserStepId: ["browser_step_id", "step_id", "browserStepId"],
  browserActionId: ["browser_action_id", "action_id", "browserActionId"],
  backgroundJobId: ["background_job_id", "job_id", "backgroundJobId"],
} as const)

type TypedAttributeName = keyof typeof TYPED_ATTRIBUTE_KEYS

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

export function normalizeTraceId(
  value: unknown,
  field: string,
): string | undefined {
  if (typeof value !== "string") return undefined
  const normalized = value.trim()
  if (!normalized || normalized.length > MAX_ID_LENGTH) return undefined
  if (!SAFE_ID.test(normalized)) return undefined
  if (normalized === "." || normalized === "..") return undefined
  if (normalized.includes("//") || normalized.includes("\\")) return undefined
  if (/^[A-Za-z]+:/.test(normalized) && !normalized.includes(":")) return undefined
  void field
  return normalized
}

export function traceIdentity(
  kind: TraceNodeKindValue,
  value: unknown,
): TraceIdentity | undefined {
  const id = normalizeTraceId(value, kind)
  if (!id) return undefined
  return Object.freeze({ kind, id, key: `${kind}:${id}` })
}

export function traceIdentityKey(
  kind: TraceNodeKindValue,
  value: unknown,
): string | undefined {
  return traceIdentity(kind, value)?.key
}

export function parseEntityReference(
  reference: unknown,
): TraceIdentity | undefined {
  if (typeof reference !== "string") return undefined
  const separator = reference.indexOf(":")
  if (separator <= 0 || separator === reference.length - 1) return undefined
  const domain = reference.slice(0, separator)
  const id = reference.slice(separator + 1)
  const kind = ENTITY_KIND_BY_DOMAIN[domain]
  return kind ? traceIdentity(kind, id) : undefined
}

function readPath(
  source: Readonly<Record<string, unknown>>,
  key: string,
): unknown {
  if (Object.prototype.hasOwnProperty.call(source, key)) return source[key]
  const segments = key.split(".")
  let current: unknown = source
  for (const segment of segments) {
    if (!isRecord(current)) return undefined
    current = current[segment]
  }
  return current
}

function readTypedAttribute(
  sources: readonly Readonly<Record<string, unknown>>[],
  name: TypedAttributeName,
): string | undefined {
  for (const source of sources) {
    for (const key of TYPED_ATTRIBUTE_KEYS[name]) {
      const value = normalizeTraceId(readPath(source, key), name)
      if (value) return value
    }
  }
  return undefined
}

function attributeSources(
  facts: TimelineEventFacts,
): readonly Readonly<Record<string, unknown>>[] {
  const sources: Readonly<Record<string, unknown>>[] = []
  sources.push(facts.safeAttributes)
  const entities: Array<ProjectionEntity | undefined> = [
    facts.worker,
    facts.tool,
    facts.recovery,
    facts.permission,
    facts.scheduler,
    facts.command,
  ]
  for (const entity of entities) {
    if (!entity) continue
    sources.push(entity.attributes)
    sources.push(entity.metadata)
  }
  return sources
}

function firstDefinedId(
  field: string,
  ...values: readonly unknown[]
): string | undefined {
  for (const value of values) {
    const id = normalizeTraceId(value, field)
    if (id) return id
  }
  return undefined
}

function inferPermissionId(facts: TimelineEventFacts): string | undefined {
  return firstDefinedId(
    "permission",
    facts.permission?.id,
    facts.permission?.requestId,
    facts.tool?.permissionId,
    facts.permissionId,
    readTypedAttribute(attributeSources(facts), "permissionId"),
  )
}

function inferRouteId(facts: TimelineEventFacts): string | undefined {
  return firstDefinedId(
    "route",
    facts.routeId,
    facts.scheduler?.routeId,
    facts.worker?.routeId,
    readTypedAttribute(attributeSources(facts), "routeId"),
  )
}

function inferPlacementId(facts: TimelineEventFacts): string | undefined {
  return firstDefinedId(
    "placement",
    facts.placementId,
    facts.scheduler?.placementId,
    facts.worker?.placementId,
    readTypedAttribute(attributeSources(facts), "placementId"),
  )
}

function inferProviderId(facts: TimelineEventFacts): string | undefined {
  return firstDefinedId(
    "provider",
    facts.scheduler?.providerId,
    readTypedAttribute(attributeSources(facts), "providerId"),
  )
}

export function extractTraceRefs(facts: TimelineEventFacts): TraceTypedRefs {
  const event = facts.event
  const sources = attributeSources(facts)
  return Object.freeze({
    taskId: event.taskId,
    runId: event.runId,
    sessionId: firstDefinedId("session", event.sessionId),
    nodeId: firstDefinedId("node", event.nodeId, facts.worker?.nodeId),
    workerId: firstDefinedId("worker", event.workerId, facts.worker?.workerId),
    spanId: firstDefinedId("span", event.spanId),
    parentSpanId: firstDefinedId("span", event.parentSpanId),
    toolCallId: firstDefinedId("tool", event.toolCallId, facts.tool?.toolCallId),
    permissionId: inferPermissionId(facts),
    artifactIds: Object.freeze(
      [...new Set(event.artifactIds.map((id) => normalizeTraceId(id, "artifact")))].filter(
        (id): id is string => Boolean(id),
      ),
    ),
    mutationId: firstDefinedId("mutation", event.mutationId, facts.mutation?.id),
    checkpointId: firstDefinedId(
      "checkpoint",
      event.checkpointId,
      facts.recovery?.resumedCheckpointId,
    ),
    commandId: firstDefinedId("command", event.controlCommandId, facts.command?.id),
    routeId: inferRouteId(facts),
    placementId: inferPlacementId(facts),
    providerId: inferProviderId(facts),
    failureId: firstDefinedId("failure", event.failureId, facts.failureId, facts.recovery?.failureId),
    recoveryId: firstDefinedId("recovery", event.recoveryId, facts.recovery?.id),
    mcpCallId: readTypedAttribute(sources, "mcpCallId"),
    mcpServerId: readTypedAttribute(sources, "mcpServerId"),
    skillId: readTypedAttribute(sources, "skillId"),
    subagentId: readTypedAttribute(sources, "subagentId"),
    parentToolCallId: readTypedAttribute(sources, "parentToolCallId"),
    yieldId: readTypedAttribute(sources, "yieldId"),
    terminalSessionId: readTypedAttribute(sources, "terminalSessionId"),
    terminalFrameId: readTypedAttribute(sources, "terminalFrameId"),
    browserSessionId: readTypedAttribute(sources, "browserSessionId"),
    browserStepId: firstDefinedId(
      "browser-step",
      facts.browserStepId,
      readTypedAttribute(sources, "browserStepId"),
    ),
    browserActionId: readTypedAttribute(sources, "browserActionId"),
    backgroundJobId: firstDefinedId(
      "background-job",
      facts.backgroundJobId,
      readTypedAttribute(sources, "backgroundJobId"),
    ),
  })
}

function eventTypeTokens(eventType: string): ReadonlySet<string> {
  return new Set(
    eventType
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter(Boolean),
  )
}

function hasAny(tokens: ReadonlySet<string>, values: readonly string[]): boolean {
  return values.some((value) => tokens.has(value))
}

export function classifyTraceSemantics(
  facts: TimelineEventFacts,
  refs: TraceTypedRefs,
): readonly TraceSemanticKindValue[] {
  const tokens = eventTypeTokens(facts.event.eventType)
  const semantics = new Set<TraceSemanticKindValue>()
  if (hasAny(tokens, ["task", "goal", "plan"])) semantics.add(TraceSemanticKind.TASK)
  if (facts.worker || refs.workerId || hasAny(tokens, ["worker", "lease"])) {
    semantics.add(TraceSemanticKind.WORKER)
  }
  if (facts.tool || refs.toolCallId || tokens.has("tool")) semantics.add(TraceSemanticKind.TOOL)
  if (facts.permission || refs.permissionId || hasAny(tokens, ["permission", "policy", "approval"])) {
    semantics.add(TraceSemanticKind.PERMISSION)
  }
  if (hasAny(tokens, ["compact", "compaction"])) semantics.add(TraceSemanticKind.COMPACT)
  if (refs.checkpointId || tokens.has("checkpoint")) semantics.add(TraceSemanticKind.CHECKPOINT)
  if (hasAny(tokens, ["restore", "resume", "resumed"])) semantics.add(TraceSemanticKind.RESTORE)
  if (facts.scheduler || refs.routeId || refs.placementId || hasAny(tokens, ["scheduler", "route", "placement", "dispatch"])) {
    semantics.add(TraceSemanticKind.PLACEMENT)
  }
  if (refs.providerId || tokens.has("provider") || tokens.has("model")) {
    semantics.add(TraceSemanticKind.PROVIDER)
  }
  if (
    semantics.has(TraceSemanticKind.PROVIDER) &&
    hasAny(tokens, ["retry", "retried", "fallback", "failover", "backoff"])
  ) {
    semantics.add(TraceSemanticKind.PROVIDER_RETRY)
  }
  if (refs.failureId || hasAny(tokens, ["fault", "failure", "failed", "crash", "timeout", "lost"])) {
    semantics.add(TraceSemanticKind.FAULT)
  }
  if (facts.recovery || refs.recoveryId || hasAny(tokens, ["recovery", "recover", "replacement"])) {
    semantics.add(TraceSemanticKind.RECOVERY)
  }
  if (refs.mcpCallId || refs.mcpServerId || tokens.has("mcp")) semantics.add(TraceSemanticKind.MCP)
  if (
    semantics.has(TraceSemanticKind.MCP) &&
    hasAny(tokens, ["reconnect", "reconnected", "disconnect", "disconnected", "reauth"])
  ) {
    semantics.add(TraceSemanticKind.MCP_RECONNECT)
  }
  if (refs.skillId || tokens.has("skill")) semantics.add(TraceSemanticKind.SKILL)
  if (refs.subagentId || hasAny(tokens, ["subagent", "agenttool", "spawn"])) {
    semantics.add(TraceSemanticKind.SUBAGENT)
  }
  if (
    refs.yieldId ||
    (semantics.has(TraceSemanticKind.SUBAGENT) && hasAny(tokens, ["yield", "partial", "final"]))
  ) {
    semantics.add(TraceSemanticKind.SUBAGENT_YIELD)
  }
  if (refs.terminalSessionId || hasAny(tokens, ["terminal", "shell", "bash"])) {
    semantics.add(TraceSemanticKind.TERMINAL)
  }
  if (refs.terminalFrameId || tokens.has("pty")) semantics.add(TraceSemanticKind.PTY)
  if (refs.browserSessionId || refs.browserStepId || refs.browserActionId || tokens.has("browser")) {
    semantics.add(TraceSemanticKind.BROWSER)
  }
  if (refs.artifactIds.length || tokens.has("artifact")) semantics.add(TraceSemanticKind.ARTIFACT)
  if (refs.mutationId || hasAny(tokens, ["mutation", "state", "delta", "commit"])) {
    semantics.add(TraceSemanticKind.MUTATION)
  }
  if (hasAny(tokens, ["topology", "node", "edge", "role", "capability"])) {
    semantics.add(TraceSemanticKind.TOPOLOGY)
  }
  if (facts.command || refs.commandId || tokens.has("command")) semantics.add(TraceSemanticKind.COMMAND)
  if (refs.backgroundJobId || tokens.has("background")) semantics.add(TraceSemanticKind.BACKGROUND)
  if (!semantics.size) semantics.add(TraceSemanticKind.OTHER)
  return Object.freeze([...semantics])
}

function safeScalar(value: unknown): string | number | boolean | undefined {
  if (typeof value === "string") {
    const trimmed = value.trim()
    if (!trimmed || trimmed.length > MAX_ATTRIBUTE_STRING) return undefined
    if (/bearer\s|api[_-]?key|secret|password|authorization/i.test(trimmed)) return undefined
    return trimmed
  }
  if (typeof value === "boolean") return value
  if (typeof value === "number" && Number.isFinite(value)) return value
  return undefined
}

export function safeTraceAttributes(
  facts: TimelineEventFacts,
  maximum: number,
): Readonly<Record<string, string | number | boolean>> {
  const output: Record<string, string | number | boolean> = {}
  const blocked = /token|secret|password|authorization|cookie|credential|api[_-]?key/i
  const sources = attributeSources(facts)
  for (const source of sources) {
    for (const [key, raw] of Object.entries(source)) {
      if (Object.keys(output).length >= maximum) return Object.freeze(output)
      if (blocked.test(key) || key in output) continue
      const value = safeScalar(raw)
      if (value !== undefined) output[key] = value
    }
  }
  return Object.freeze(output)
}

function expectedIdentityConflicts(
  event: CausalEventProjection,
  facts: TimelineEventFacts,
  refs: TraceTypedRefs,
): string[] {
  const conflicts: string[] = []
  const entities: Array<ProjectionEntity | undefined> = [
    facts.worker,
    facts.tool,
    facts.recovery,
    facts.permission,
    facts.scheduler,
    facts.command,
    ...facts.artifacts,
  ]
  for (const entity of entities) {
    if (!entity) continue
    if (entity.taskId !== event.taskId) {
      conflicts.push(`${entity.domain}:${entity.id}:task:${entity.taskId}`)
    }
    if (entity.runId !== event.runId) {
      conflicts.push(`${entity.domain}:${entity.id}:run:${entity.runId}`)
    }
  }
  if (refs.parentSpanId && refs.spanId === refs.parentSpanId) {
    conflicts.push(`span:${refs.spanId}:self-parent`)
  }
  if (refs.parentToolCallId && refs.parentToolCallId === refs.toolCallId) {
    conflicts.push(`tool:${refs.toolCallId}:self-parent`)
  }
  return conflicts
}

function partialReasons(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
  refs: TraceTypedRefs,
): string[] {
  const reasons = [...facts.missingRefs]
  const event = facts.event
  if (!refs.spanId && (refs.toolCallId || refs.workerId || refs.browserActionId)) {
    reasons.push("missing-span")
  }
  if (event.causationId && !state.causality.byEvent[event.causationId]) {
    reasons.push(`missing-cause:${event.causationId}`)
  }
  for (const artifactId of refs.artifactIds) {
    if (!state.artifacts[artifactId]) reasons.push(`missing-artifact:${artifactId}`)
  }
  if (refs.mutationId && !state.mutations[refs.mutationId]) {
    reasons.push(`missing-mutation:${refs.mutationId}`)
  }
  if (facts.partial) reasons.push("canonical-partial")
  return [...new Set(reasons)]
}

function completenessFor(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
  reasons: readonly string[],
  conflicts: readonly string[],
  lateTolerance: number,
): TraceCompletenessValue {
  if (conflicts.length) return TraceCompleteness.QUARANTINED
  const event = facts.event
  const cursor = state.cursors[event.taskId]
  if (event.causationId && !state.causality.byEvent[event.causationId]) {
    return TraceCompleteness.ORPHAN
  }
  if (reasons.length) return TraceCompleteness.PARTIAL
  if (
    cursor &&
    event.sequence + lateTolerance < cursor.committedSequence &&
    Date.parse(event.committedAt) > state.committedAtMs
  ) {
    return TraceCompleteness.LATE
  }
  return TraceCompleteness.COMPLETE
}

export function admitTraceEvent(
  state: CanonicalProjectionState,
  facts: TimelineEventFacts,
  options: { maximumAttributesPerEvent: number; lateSequenceTolerance: number },
): TraceEventAdmission {
  const refs = extractTraceRefs(facts)
  const conflicts = expectedIdentityConflicts(facts.event, facts, refs)
  const reasons = partialReasons(state, facts, refs)
  return Object.freeze({
    event: facts.event,
    refs,
    semantics: classifyTraceSemantics(facts, refs),
    completeness: completenessFor(
      state,
      facts,
      reasons,
      conflicts,
      options.lateSequenceTolerance,
    ),
    partialReasons: Object.freeze(reasons),
    identityConflicts: Object.freeze(conflicts),
    safeAttributes: safeTraceAttributes(facts, options.maximumAttributesPerEvent),
  })
}

export function traceRefsToIdentities(refs: TraceTypedRefs): readonly TraceIdentity[] {
  const identities: TraceIdentity[] = []
  const add = (kind: TraceNodeKindValue, value: unknown) => {
    const identity = traceIdentity(kind, value)
    if (identity) identities.push(identity)
  }
  add(TraceNodeKind.TASK, refs.taskId)
  add(TraceNodeKind.RUN, refs.runId)
  add(TraceNodeKind.SESSION, refs.sessionId)
  add(TraceNodeKind.NODE, refs.nodeId)
  add(TraceNodeKind.WORKER, refs.workerId)
  add(TraceNodeKind.SPAN, refs.spanId)
  add(TraceNodeKind.TOOL, refs.toolCallId)
  add(TraceNodeKind.PERMISSION, refs.permissionId)
  refs.artifactIds.forEach((id) => add(TraceNodeKind.ARTIFACT, id))
  add(TraceNodeKind.MUTATION, refs.mutationId)
  add(TraceNodeKind.CHECKPOINT, refs.checkpointId)
  add(TraceNodeKind.COMMAND, refs.commandId)
  add(TraceNodeKind.ROUTE, refs.routeId)
  add(TraceNodeKind.PLACEMENT, refs.placementId)
  add(TraceNodeKind.PROVIDER, refs.providerId)
  add(TraceNodeKind.FAILURE, refs.failureId)
  add(TraceNodeKind.RECOVERY, refs.recoveryId)
  add(TraceNodeKind.MCP, refs.mcpCallId ?? refs.mcpServerId)
  add(TraceNodeKind.SKILL, refs.skillId)
  add(TraceNodeKind.SUBAGENT, refs.subagentId)
  add(TraceNodeKind.TERMINAL, refs.terminalFrameId ?? refs.terminalSessionId)
  add(TraceNodeKind.BROWSER, refs.browserActionId ?? refs.browserStepId ?? refs.browserSessionId)
  add(TraceNodeKind.BACKGROUND, refs.backgroundJobId)
  const seen = new Set<string>()
  return Object.freeze(identities.filter((identity) => {
    if (seen.has(identity.key)) return false
    seen.add(identity.key)
    return true
  }))
}

export function eventNodeIdentity(eventId: string): TraceIdentity {
  const identity = traceIdentity(TraceNodeKind.EVENT, eventId)
  if (!identity) throw new TypeError(`Invalid canonical event identity: ${eventId}`)
  return identity
}

export function compareTraceAdmissions(
  left: TraceEventAdmission,
  right: TraceEventAdmission,
): number {
  return (
    left.event.sequence - right.event.sequence ||
    left.event.aggregateSequence - right.event.aggregateSequence ||
    left.event.committedAt.localeCompare(right.event.committedAt) ||
    left.event.eventId.localeCompare(right.event.eventId)
  )
}
