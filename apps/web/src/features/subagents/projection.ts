import type {
  CanonicalProjectionState,
  CausalEventProjection,
  WorkerProjection,
} from "../../state/contracts.ts"
import { buildSubagentHierarchy, hierarchyScopeViolations } from "./hierarchy.ts"
import {
  SUBAGENT_PANEL_SCHEMA,
  type BudgetMetric,
  type LateResultRecord,
  type SubagentAdmissionIssue,
  type SubagentBudgetProjection,
  type SubagentCandidate,
  type SubagentErrorProjection,
  type SubagentHeartbeatProjection,
  type SubagentLifecycle,
  type SubagentProjection,
  type SubagentProjectionOptions,
  type SubagentResultProjection,
  type SubagentRow,
  type SubagentScopeProjection,
} from "./contracts.ts"
import {
  candidateRecords,
  compareNumber,
  compareText,
  elapsedMs,
  fingerprint,
  firstBoolean,
  firstIdentity,
  firstIdentityList,
  firstInteger,
  firstNumber,
  firstRecord,
  firstText,
  firstTextList,
  firstTime,
  identityList,
  optionalIdentity,
  ratio,
  record,
  safeError,
  secretPaths,
  sortStable,
  text,
  textList,
  unique,
  type UnknownRecord,
} from "./value.ts"

const SUBAGENT_EVENT = /(?:^|[._:/-])(?:subagent|agent_child|agent-task|agent_task)(?:[._:/-]|$)/i
const RESULT_EVENT = /(?:result|yield|completed|succeeded|settled|output)/i
const FAILURE_EVENT = /(?:failed|failure|error|crash|timeout)/i
const CANCEL_EVENT = /(?:cancel|killed|kill|stopped|terminated)/i
const HEARTBEAT_EVENT = /(?:heartbeat|progress|pulse|liveness)/i
const RECONNECT_EVENT = /(?:reconnect|resum|redispatch|recovered)/i
const STEER_EVENT = /(?:steer|message|instruction|inbox|mailbox)/i

export function buildSubagentProjection(
  state: CanonicalProjectionState,
  taskId: string,
  options: SubagentProjectionOptions = {},
): SubagentProjection {
  const task = state.tasks[taskId]
  const runId = options.runId ?? task?.runId
  const sessionId = options.sessionId
  const nowMs = options.nowMs ?? Date.now()
  const connected = state.runtimes[taskId]?.connected ?? Boolean(task)
  const candidates = collectCandidates(state, taskId, runId, sessionId)
  const rejectedWorkerIds: string[] = []
  const globalIssues: SubagentAdmissionIssue[] = []
  const rows: SubagentRow[] = []
  for (const candidate of candidates) {
    const row = projectCandidate(candidate, state, {
      taskId,
      runId,
      sessionId,
      nowMs,
      connected,
      heartbeatIntervalMs: options.heartbeatIntervalMs ?? 15_000,
      heartbeatTimeoutMs: options.heartbeatTimeoutMs ?? 60_000,
    })
    if (!row.admitted && !options.includeRejected) {
      if (candidate.worker) rejectedWorkerIds.push(candidate.worker.id)
      globalIssues.push(...row.issues)
      continue
    }
    rows.push(row)
  }
  const limited = sortStable(rows, compareRows).slice(
    0,
    Math.max(1, Math.min(20_000, options.maximumRows ?? 5_000)),
  )
  const rootIdentities = unique([
    taskId,
    runId,
    sessionId,
    task?.workerIds?.[0],
    firstIdentity(task ? candidateRecords(task.attributes, task.metadata) : [], "owner_id", "ownerId"),
  ])
  let hierarchy = buildSubagentHierarchy(limited, rootIdentities)
  const hierarchyViolations = hierarchyScopeViolations(hierarchy)
  const adjusted = limited.map((row) => {
    const violations = hierarchyViolations[row.id] ?? []
    if (violations.length === 0) return row
    const issues: SubagentAdmissionIssue[] = [
      ...row.issues,
      ...violations.map((message) => ({
        code: `SUBAGENT_HIERARCHY_${message.toUpperCase().replace(/\s+/g, "_")}`,
        severity: "error" as const,
        message,
        childId: row.id,
        field: "parent/depth/lineage",
      })),
    ]
    return freezeRow({
      ...row,
      admitted: false,
      controlEligible: false,
      issues,
    })
  })
  if (adjusted.some((row, index) => row !== limited[index])) {
    hierarchy = buildSubagentHierarchy(adjusted, rootIdentities)
  }
  const rowById: Record<string, SubagentRow> = {}
  const sourceEventIds: string[] = []
  const lateResults: LateResultRecord[] = []
  const issues: SubagentAdmissionIssue[] = [...globalIssues]
  for (const row of adjusted) {
    rowById[row.id] = row
    sourceEventIds.push(...row.eventIds)
    lateResults.push(...row.lateResults)
    issues.push(...row.issues)
  }
  const activeIds = adjusted.filter((row) => !row.terminal).map((row) => row.id)
  const settledIds = adjusted.filter((row) => row.terminal).map((row) => row.id)
  const failedIds = adjusted
    .filter((row) => row.lifecycle === "failed" || row.crashed)
    .map((row) => row.id)
  const quarantinedIds = adjusted
    .filter((row) => row.quarantined || row.lateResults.length > 0)
    .map((row) => row.id)
  const ready =
    Boolean(task) &&
    connected &&
    issues.every((issue) => issue.severity !== "error") &&
    adjusted.every((row) => row.admitted)
  const projection = {
    schema: SUBAGENT_PANEL_SCHEMA,
    taskId,
    runId,
    sessionId,
    canonicalRevision: state.revision,
    connected,
    ready,
    rows: Object.freeze(adjusted),
    rowById: Object.freeze(rowById),
    hierarchy,
    activeIds: Object.freeze(activeIds),
    settledIds: Object.freeze(settledIds),
    failedIds: Object.freeze(failedIds),
    quarantinedIds: Object.freeze(quarantinedIds),
    lateResults: Object.freeze(
      sortStable(lateResults, (left, right) =>
        compareNumber(left.resultSequence, right.resultSequence) ||
        compareText(left.id, right.id)),
    ),
    issues: Object.freeze(sortStable(issues, compareIssues)),
    sourceEventIds: unique(sourceEventIds),
    rejectedWorkerIds: unique(rejectedWorkerIds),
  }
  return Object.freeze({
    ...projection,
    fingerprint: fingerprint([
      projection.taskId,
      projection.runId,
      projection.sessionId,
      projection.canonicalRevision,
      projection.connected,
      projection.rows.map((row) => row.fingerprint),
      projection.hierarchy.fingerprint,
      projection.issues,
    ]),
  })
}

function collectCandidates(
  state: CanonicalProjectionState,
  taskId: string,
  runId?: string,
  sessionId?: string,
): readonly SubagentCandidate[] {
  const events = taskEvents(state, taskId)
    .filter((event) => !runId || event.runId === runId)
    .filter((event) => !sessionId || !event.sessionId || event.sessionId === sessionId)
  const eventsByChild = new Map<string, CausalEventProjection[]>()
  const workerByChild = new Map<string, WorkerProjection>()
  for (const worker of Object.values(state.workers)) {
    if (worker.taskId !== taskId) continue
    if (runId && worker.runId !== runId) continue
    if (sessionId && worker.sessionId && worker.sessionId !== sessionId) continue
    const sources = workerSources(worker)
    const explicit = explicitChildId(sources)
    const evidenceEvents = events.filter((event) =>
      event.workerId === worker.id ||
      event.entityRefs.includes(`worker:${worker.id}`) ||
      (explicit ? eventReferencesChild(event, explicit) : false))
    if (!isSubagentWorker(worker, sources, evidenceEvents)) continue
    const childId = explicit ?? worker.id
    workerByChild.set(childId, chooseWorker(workerByChild.get(childId), worker))
    appendEvents(eventsByChild, childId, evidenceEvents)
  }
  for (const event of events) {
    if (!isSubagentEvent(event)) continue
    const childId = eventChildId(event)
    if (!childId) continue
    appendEvents(eventsByChild, childId, [event])
  }
  const ids = new Set([...workerByChild.keys(), ...eventsByChild.keys()])
  return Object.freeze(
    sortStable(ids, compareText).map((childId) => ({
      childId,
      worker: workerByChild.get(childId),
      events: Object.freeze(
        sortStable(
          eventsByChild.get(childId) ?? [],
          (left, right) =>
            compareNumber(left.sequence, right.sequence) ||
            compareNumber(left.aggregateSequence, right.aggregateSequence) ||
            compareText(left.eventId, right.eventId),
        ),
      ),
    })),
  )
}

function appendEvents(
  index: Map<string, CausalEventProjection[]>,
  childId: string,
  events: readonly CausalEventProjection[],
): void {
  const current = index.get(childId) ?? []
  const byId = new Map(current.map((event) => [event.eventId, event]))
  for (const event of events) byId.set(event.eventId, event)
  index.set(childId, [...byId.values()])
}

function taskEvents(
  state: CanonicalProjectionState,
  taskId: string,
): readonly CausalEventProjection[] {
  return Object.freeze(
    (state.causality.byTask[taskId] ?? [])
      .map((eventId) => state.causality.byEvent[eventId])
      .filter((event): event is CausalEventProjection => Boolean(event)),
  )
}

function workerSources(worker: WorkerProjection): readonly UnknownRecord[] {
  const direct = candidateRecords(worker.attributes, worker.metadata)
  return Object.freeze([
    ...direct,
    firstRecord(direct, "subagent", "agent_task", "agentTask", "child_run", "childRun"),
    firstRecord(direct, "scope", "agent_scope", "agentScope"),
    firstRecord(direct, "budget", "agent_budget", "agentBudget"),
    firstRecord(direct, "result", "agent_result", "agentResult"),
    firstRecord(direct, "error", "failure"),
  ].filter((source) => Object.keys(source).length > 0))
}

function eventSources(event: CausalEventProjection): readonly UnknownRecord[] {
  const base = record(event as unknown)
  return candidateRecords(base)
}

function explicitChildId(
  sources: readonly UnknownRecord[],
): string | undefined {
  return firstIdentity(
    sources,
    "subagent_id",
    "subagentId",
    "agent_task_id",
    "agentTaskId",
    "child_task_id",
    "childTaskId",
    "child_run_id",
    "childRunId",
  )
}

function isSubagentWorker(
  worker: WorkerProjection,
  sources: readonly UnknownRecord[],
  events: readonly CausalEventProjection[],
): boolean {
  if (explicitChildId(sources)) return true
  if (
    firstBoolean(sources, "is_subagent", "isSubagent", "child_agent", "childAgent")
  ) {
    return true
  }
  if (
    worker.role &&
    /^(?:subagent|child-agent|child_agent|agent-child|agent_task)(?:$|[.:/_-])/i.test(worker.role)
  ) {
    return true
  }
  return events.some(isSubagentEvent)
}

function isSubagentEvent(event: CausalEventProjection): boolean {
  if (SUBAGENT_EVENT.test(event.eventType)) return true
  if (event.entityRefs.some((ref) => /^subagent:|^agent-task:|^agent_task:/.test(ref))) {
    return true
  }
  return Boolean(eventChildId(event))
}

function eventChildId(
  event: CausalEventProjection,
): string | undefined {
  for (const ref of event.entityRefs) {
    const match = /^(?:subagent|agent-task|agent_task|child-run|child_run):(.+)$/.exec(ref)
    const selected = optionalIdentity(match?.[1])
    if (selected) return selected
  }
  return firstIdentity(
    eventSources(event),
    "subagent_id",
    "subagentId",
    "agent_task_id",
    "agentTaskId",
    "child_id",
    "childId",
  ) ?? (SUBAGENT_EVENT.test(event.eventType) ? optionalIdentity(event.workerId) : undefined)
}

function eventReferencesChild(
  event: CausalEventProjection,
  childId: string,
): boolean {
  return (
    eventChildId(event) === childId ||
    event.entityRefs.some((ref) => ref.endsWith(`:${childId}`))
  )
}

function chooseWorker(
  current: WorkerProjection | undefined,
  next: WorkerProjection,
): WorkerProjection {
  if (!current) return next
  if (next.revision !== current.revision) {
    return next.revision > current.revision ? next : current
  }
  return next.sequence > current.sequence ? next : current
}

function projectCandidate(
  candidate: SubagentCandidate,
  state: CanonicalProjectionState,
  context: {
    taskId: string
    runId?: string
    sessionId?: string
    nowMs: number
    connected: boolean
    heartbeatIntervalMs: number
    heartbeatTimeoutMs: number
  },
): SubagentRow {
  const worker = candidate.worker
  const latestEvent = candidate.events.at(-1)
  const sources = Object.freeze([
    ...(worker ? workerSources(worker) : []),
    ...candidate.events.slice(-20).reverse().flatMap(eventSources),
  ])
  const issues = admissionIssues(candidate, sources, context)
  const childId = candidate.childId
  const parentId = firstIdentity(
    sources,
    "parent_agent_id",
    "parentAgentId",
    "parent_task_id",
    "parentTaskId",
    "parent_run_id",
    "parentRunId",
    "parent_worker_id",
    "parentWorkerId",
    "parent_id",
    "parentId",
  ) ?? context.taskId
  const taskId = worker?.taskId ?? latestEvent?.taskId ?? context.taskId
  const runId = firstIdentity(sources, "run_id", "runId") ??
    worker?.runId ??
    latestEvent?.runId ??
    context.runId ??
    ""
  const sessionId = firstIdentity(
    sources,
    "child_session_id",
    "childSessionId",
    "session_id",
    "sessionId",
  ) ?? worker?.sessionId ?? latestEvent?.sessionId
  const parentTaskId = firstIdentity(sources, "parent_task_id", "parentTaskId")
  const parentRunId = firstIdentity(sources, "parent_run_id", "parentRunId")
  const parentSessionId = firstIdentity(sources, "parent_session_id", "parentSessionId")
  const ownerId = firstIdentity(
    sources,
    "owner_id",
    "ownerId",
    "runtime_owner_id",
    "runtimeOwnerId",
    "custodian_id",
    "custodianId",
  )
  const depth = firstInteger(
    sources,
    parentId === context.taskId ? 1 : 0,
    0,
    128,
    "depth",
    "agent_depth",
    "agentDepth",
    "scope_depth",
    "scopeDepth",
  )
  const attempt = firstInteger(
    sources,
    inferAttempt(candidate.events),
    0,
    1_000_000,
    "attempt",
    "attempt_number",
    "attemptNumber",
    "generation",
  )
  const revision = Math.max(
    worker?.revision ?? 0,
    firstInteger(
      sources,
      0,
      0,
      Number.MAX_SAFE_INTEGER,
      "revision",
      "agent_revision",
      "agentRevision",
    ),
  )
  const lifecycle = inferLifecycle(worker, candidate.events, sources)
  const terminal = terminalLifecycle(lifecycle) || Boolean(worker?.terminal)
  const sequence = Math.max(
    worker?.sequence ?? 0,
    ...candidate.events.map((event) => event.sequence),
  )
  const createdAt =
    firstTime(sources, "created_at", "createdAt", "started_at", "startedAt") ??
    worker?.createdAt ??
    candidate.events[0]?.createdAt ??
    new Date(context.nowMs).toISOString()
  const updatedAt =
    firstTime(sources, "updated_at", "updatedAt", "finished_at", "finishedAt") ??
    worker?.updatedAt ??
    latestEvent?.committedAt ??
    createdAt
  const scope = buildScope(sources, depth)
  const childCount = firstInteger(
    sources,
    firstIdentityList(sources, "child_ids", "childIds").length,
    0,
    100_000,
    "child_count",
    "childCount",
    "children_used",
    "childrenUsed",
  )
  const budget = buildBudget(sources, depth, childCount, createdAt, updatedAt, context.nowMs)
  const heartbeat = buildHeartbeat(
    sources,
    candidate.events,
    terminal,
    context.connected,
    context.nowMs,
    context.heartbeatIntervalMs,
    context.heartbeatTimeoutMs,
  )
  const result = buildResult(sources, candidate.events, lifecycle)
  const error = buildError(sources, candidate.events, lifecycle, heartbeat)
  const terminalEvent = findTerminalEvent(candidate.events, lifecycle)
  const lateResults = terminalEvent
    ? buildLateResults(childId, attempt, terminalEvent, candidate.events)
    : Object.freeze([])
  const crashed =
    lifecycle === "failed" &&
    (
      error?.crashed ||
      heartbeat.phase === "timed_out" ||
      candidate.events.some((event) => /crash/i.test(event.eventType))
    )
  const reconnected =
    attempt > 0 ||
    candidate.events.some((event) => RECONNECT_EVENT.test(event.eventType))
  const quarantined =
    lateResults.length > 0 ||
    firstBoolean(sources, "quarantined", "late_result_quarantined") === true
  if (!scope.bounded) {
    issues.push({
      code: "SUBAGENT_SCOPE_UNBOUNDED",
      severity: "error",
      message: "Canonical scope digest and permission mode are required.",
      childId,
      field: "scope",
    })
  }
  if (secretPaths(worker?.attributes).length > 0 || secretPaths(worker?.metadata).length > 0) {
    issues.push({
      code: "SUBAGENT_SECRET_MATERIAL_REDACTED",
      severity: "error",
      message: "Secret-like fields are present in the canonical worker projection.",
      childId,
      field: "attributes/metadata",
    })
  }
  if (budget.exceeded) {
    issues.push({
      code: "SUBAGENT_BUDGET_EXCEEDED",
      severity: terminal ? "warning" : "error",
      message: budget.warnings.join("; "),
      childId,
      field: "budget",
    })
  }
  if (heartbeat.phase === "timed_out" && !terminal) {
    issues.push({
      code: "SUBAGENT_HEARTBEAT_TIMEOUT",
      severity: "error",
      message: "The canonical heartbeat exceeded its timeout window.",
      childId,
      field: "heartbeat",
    })
  }
  const admitted = issues.every((issue) => issue.severity !== "error")
  const controlEligible =
    admitted &&
    context.connected &&
    !terminal &&
    !quarantined &&
    Boolean(ownerId) &&
    revision >= 0 &&
    scope.bounded
  const row = {
    id: childId,
    taskId,
    runId,
    sessionId,
    parentId,
    parentTaskId,
    parentRunId,
    parentSessionId,
    workerId: worker?.id ?? latestEvent?.workerId,
    ownerId,
    routeId: worker?.routeId ?? firstIdentity(sources, "route_id", "routeId"),
    placementId: worker?.placementId ?? firstIdentity(sources, "placement_id", "placementId"),
    leaseId: worker?.leaseId ?? firstIdentity(sources, "lease_id", "leaseId"),
    checkpointId: worker?.checkpointId ??
      firstIdentity(sources, "checkpoint_id", "checkpointId", "resume_checkpoint_id"),
    definitionName: firstText(sources, "definition_name", "definitionName", "agent_name", "agentName"),
    definitionDigest: firstText(sources, "definition_digest", "definitionDigest"),
    contextDigest: firstText(sources, "context_digest", "contextDigest"),
    promptDigest: firstText(sources, "prompt_digest", "promptDigest"),
    idempotencyKey: firstText(sources, "idempotency_key", "idempotencyKey"),
    executionMode: firstText(sources, "execution_mode", "executionMode"),
    lifecycle,
    depth,
    attempt,
    sequence,
    revision,
    createdAt,
    updatedAt,
    terminal,
    admitted,
    controlEligible,
    reconnected,
    crashed,
    quarantined,
    connected: context.connected,
    childIds: firstIdentityList(sources, "child_ids", "childIds"),
    scope,
    budget,
    heartbeat,
    result,
    error,
    lateResults,
    eventIds: Object.freeze(candidate.events.map((event) => event.eventId)),
    artifactIds: unique([
      ...(worker?.artifactIds ?? []),
      ...candidate.events.flatMap((event) => event.artifactIds),
      ...(result?.artifactIds ?? []),
    ]),
    issues: Object.freeze(sortStable(issues, compareIssues)),
  }
  return freezeRow(row)
}

function admissionIssues(
  candidate: SubagentCandidate,
  sources: readonly UnknownRecord[],
  context: {
    taskId: string
    runId?: string
    sessionId?: string
  },
): SubagentAdmissionIssue[] {
  const issues: SubagentAdmissionIssue[] = []
  const childId = candidate.childId
  const worker = candidate.worker
  const explicitIds = unique(
    sources.flatMap((source) => [
      optionalIdentity(source.subagent_id),
      optionalIdentity(source.subagentId),
      optionalIdentity(source.agent_task_id),
      optionalIdentity(source.agentTaskId),
      optionalIdentity(source.child_task_id),
      optionalIdentity(source.childTaskId),
    ]),
  )
  if (explicitIds.length > 1) {
    issues.push({
      code: "SUBAGENT_IDENTITY_CONFLICT",
      severity: "error",
      message: `Conflicting canonical child identities: ${explicitIds.join(", ")}.`,
      childId,
      field: "child_id",
    })
  }
  if (worker && worker.taskId !== context.taskId) {
    issues.push({
      code: "SUBAGENT_TASK_SCOPE_CONFLICT",
      severity: "error",
      message: "Worker task identity differs from the selected canonical task.",
      childId,
      field: "task_id",
    })
  }
  if (worker && context.runId && worker.runId !== context.runId) {
    issues.push({
      code: "SUBAGENT_RUN_SCOPE_CONFLICT",
      severity: "error",
      message: "Worker run identity differs from the selected canonical run.",
      childId,
      field: "run_id",
    })
  }
  if (
    worker &&
    context.sessionId &&
    worker.sessionId &&
    worker.sessionId !== context.sessionId
  ) {
    issues.push({
      code: "SUBAGENT_SESSION_SCOPE_CONFLICT",
      severity: "error",
      message: "Worker session identity differs from the selected canonical session.",
      childId,
      field: "session_id",
    })
  }
  const eventScopes = candidate.events.filter((event) =>
    event.taskId !== context.taskId ||
    (context.runId && event.runId !== context.runId) ||
    (context.sessionId && event.sessionId && event.sessionId !== context.sessionId))
  if (eventScopes.length > 0) {
    issues.push({
      code: "SUBAGENT_EVENT_SCOPE_CONFLICT",
      severity: "error",
      message: "One or more subagent events belong to another exact scope.",
      childId,
      eventId: eventScopes[0]?.eventId,
      field: "event.identity",
    })
  }
  if (!worker && candidate.events.length === 0) {
    issues.push({
      code: "SUBAGENT_CANONICAL_EVIDENCE_MISSING",
      severity: "error",
      message: "No canonical worker or event evidence exists.",
      childId,
    })
  }
  const owner = firstIdentity(sources, "owner_id", "ownerId", "runtime_owner_id", "runtimeOwnerId")
  if (!owner) {
    issues.push({
      code: "SUBAGENT_OWNER_MISSING",
      severity: "error",
      message: "Canonical runtime owner identity is absent; controls remain disabled.",
      childId,
      field: "owner_id",
    })
  }
  return issues
}

function buildScope(
  sources: readonly UnknownRecord[],
  depth: number,
): SubagentScopeProjection {
  const scope = [
    firstRecord(sources, "scope", "agent_scope", "agentScope"),
    ...sources,
  ].filter((source) => Object.keys(source).length > 0)
  const parentTools = firstTextList(scope, "parent_tools", "parentTools")
  const childTools = firstTextList(scope, "child_tools", "childTools", "allowed_tools", "allowedTools")
  const deniedTools = firstTextList(scope, "denied_tools", "deniedTools")
  const lineage = firstIdentityList(scope, "lineage", "ancestry", "ancestor_ids", "ancestorIds")
  const permissionMode = firstText(scope, "permission_mode", "permissionMode")
  const digest = firstText(scope, "scope_digest", "scopeDigest", "digest")
  const maxDepth = firstNumber(scope, "max_depth", "maxDepth")
  const bounded =
    Boolean(digest) &&
    Boolean(permissionMode) &&
    (maxDepth === undefined || depth <= maxDepth)
  return Object.freeze({
    digest,
    permissionCeilingDigest: firstText(
      scope,
      "permission_ceiling_digest",
      "permissionCeilingDigest",
    ),
    toolCatalogDigest: firstText(scope, "tool_catalog_digest", "toolCatalogDigest"),
    permissionMode,
    parentTools,
    childTools,
    deniedTools,
    skills: firstTextList(scope, "skills", "skill_names", "skillNames"),
    mcpServers: firstTextList(scope, "mcp_servers", "mcpServers"),
    memoryScope: firstText(scope, "memory_scope", "memoryScope"),
    isolation: firstText(scope, "isolation", "isolation_mode", "isolationMode"),
    workspaceId: firstIdentity(scope, "workspace_id", "workspaceId"),
    worktreeId: firstIdentity(scope, "worktree_id", "worktreeId"),
    remoteId: firstIdentity(scope, "remote_id", "remoteId"),
    lineage,
    cycleKey: firstText(scope, "cycle_key", "cycleKey"),
    allowKill: firstBoolean(scope, "allow_kill", "allowKill", "kill_allowed") !== false,
    allowSteer: firstBoolean(scope, "allow_steer", "allowSteer", "steer_allowed") !== false,
    bounded,
  })
}

function buildBudget(
  sources: readonly UnknownRecord[],
  depth: number,
  childCount: number,
  createdAt: string,
  updatedAt: string,
  nowMs: number,
): SubagentBudgetProjection {
  const budget = [
    firstRecord(sources, "budget", "agent_budget", "agentBudget"),
    firstRecord(sources, "usage", "agent_usage", "agentUsage"),
    ...sources,
  ].filter((source) => Object.keys(source).length > 0)
  const maxTurns = firstInteger(budget, 0, 0, 10_000_000, "max_turns", "maxTurns")
  const turnsUsed = firstInteger(budget, 0, 0, 10_000_000, "turns_used", "turnsUsed", "turns")
  const maxToolCalls = firstInteger(budget, 0, 0, 10_000_000, "max_tool_calls", "maxToolCalls")
  const toolCallsUsed = firstInteger(
    budget,
    0,
    0,
    10_000_000,
    "tool_calls_used",
    "toolCallsUsed",
    "tool_calls",
  )
  const maxInputTokens = firstInteger(
    budget,
    0,
    0,
    Number.MAX_SAFE_INTEGER,
    "max_input_tokens",
    "maxInputTokens",
  )
  const inputTokensUsed = firstInteger(
    budget,
    0,
    0,
    Number.MAX_SAFE_INTEGER,
    "input_tokens_used",
    "inputTokensUsed",
    "input_tokens",
  )
  const maxOutputTokens = firstInteger(
    budget,
    0,
    0,
    Number.MAX_SAFE_INTEGER,
    "max_output_tokens",
    "maxOutputTokens",
  )
  const outputTokensUsed = firstInteger(
    budget,
    0,
    0,
    Number.MAX_SAFE_INTEGER,
    "output_tokens_used",
    "outputTokensUsed",
    "output_tokens",
  )
  const maxResultChars = firstInteger(
    budget,
    0,
    0,
    Number.MAX_SAFE_INTEGER,
    "max_result_chars",
    "maxResultChars",
  )
  const resultCharsUsed = firstInteger(
    budget,
    0,
    0,
    Number.MAX_SAFE_INTEGER,
    "result_chars_used",
    "resultCharsUsed",
    "result_chars",
  )
  const maxWallTimeMs = firstInteger(
    budget,
    0,
    0,
    Number.MAX_SAFE_INTEGER,
    "max_wall_time_ms",
    "maxWallTimeMs",
  )
  const observedEnd = Date.parse(updatedAt)
  const wallTimeMs = firstInteger(
    budget,
    Math.max(0, (Number.isFinite(observedEnd) ? observedEnd : nowMs) - Date.parse(createdAt)),
    0,
    Number.MAX_SAFE_INTEGER,
    "wall_time_ms",
    "wallTimeMs",
    "elapsed_ms",
  )
  const maxChildren = firstInteger(budget, 0, 0, 100_000, "max_children", "maxChildren")
  const maxDepth = firstInteger(budget, 0, 0, 128, "max_depth", "maxDepth")
  const metrics: BudgetMetric[] = [
    budgetMetric("turns", turnsUsed, maxTurns),
    budgetMetric("tool_calls", toolCallsUsed, maxToolCalls),
    budgetMetric("input_tokens", inputTokensUsed, maxInputTokens),
    budgetMetric("output_tokens", outputTokensUsed, maxOutputTokens),
    budgetMetric("result_chars", resultCharsUsed, maxResultChars),
    budgetMetric("wall_time_ms", wallTimeMs, maxWallTimeMs),
    budgetMetric("children", childCount, maxChildren),
    budgetMetric("depth", depth, maxDepth),
  ]
  const applicable = metrics.filter((metric) => metric.limit > 0)
  const highestRatio = Math.max(0, ...applicable.map((metric) => metric.ratio))
  const exceeded = applicable.some((metric) => metric.exceeded)
  const warnings = applicable
    .filter((metric) => metric.exceeded || metric.nearLimit)
    .map((metric) =>
      metric.exceeded
        ? `${metric.key} budget exceeded (${metric.used}/${metric.limit})`
        : `${metric.key} budget near limit (${metric.used}/${metric.limit})`)
  return Object.freeze({
    metrics: Object.freeze(metrics),
    maxTurns,
    turnsUsed,
    maxToolCalls,
    toolCallsUsed,
    maxInputTokens,
    inputTokensUsed,
    maxOutputTokens,
    outputTokensUsed,
    maxResultChars,
    resultCharsUsed,
    maxWallTimeMs,
    wallTimeMs,
    maxChildren,
    childCount,
    maxDepth,
    depth,
    highestRatio,
    exceeded,
    warnings: Object.freeze(warnings),
  })
}

function budgetMetric(
  key: BudgetMetric["key"],
  used: number,
  limit: number,
): BudgetMetric {
  const selectedRatio = ratio(used, limit)
  return Object.freeze({
    key,
    used,
    limit,
    remaining: limit > 0 ? Math.max(0, limit - used) : 0,
    ratio: selectedRatio,
    exceeded: limit > 0 && used > limit,
    nearLimit: limit > 0 && selectedRatio >= 0.8 && used <= limit,
  })
}

function buildHeartbeat(
  sources: readonly UnknownRecord[],
  events: readonly CausalEventProjection[],
  terminal: boolean,
  connected: boolean,
  nowMs: number,
  defaultIntervalMs: number,
  defaultTimeoutMs: number,
): SubagentHeartbeatProjection {
  const heartbeatEvents = events.filter((event) => HEARTBEAT_EVENT.test(event.eventType))
  const latest = heartbeatEvents.at(-1)
  const lastAt = firstTime(
    sources,
    "last_heartbeat_at",
    "lastHeartbeatAt",
    "heartbeat_at",
    "heartbeatAt",
  ) ?? latest?.committedAt
  const expectedEveryMs = firstInteger(
    sources,
    defaultIntervalMs,
    100,
    86_400_000,
    "heartbeat_interval_ms",
    "heartbeatIntervalMs",
    "expected_heartbeat_ms",
  )
  const timeoutMs = firstInteger(
    sources,
    Math.max(defaultTimeoutMs, expectedEveryMs * 2),
    expectedEveryMs + 1,
    604_800_000,
    "heartbeat_timeout_ms",
    "heartbeatTimeoutMs",
    "heartbeat_ttl_ms",
  )
  const ageMs = elapsedMs(lastAt, nowMs)
  let phase: SubagentHeartbeatProjection["phase"]
  if (terminal) phase = "terminal"
  else if (!lastAt || ageMs === undefined) phase = "missing"
  else if (ageMs >= timeoutMs) phase = "timed_out"
  else if (ageMs >= Math.max(expectedEveryMs * 2, timeoutMs * 0.75)) phase = "stale"
  else if (ageMs >= expectedEveryMs) phase = "due"
  else phase = "healthy"
  return Object.freeze({
    phase,
    lastAt,
    expectedEveryMs,
    timeoutMs,
    ageMs,
    dueAt: lastAt
      ? new Date(Date.parse(lastAt) + expectedEveryMs).toISOString()
      : undefined,
    timeoutAt: lastAt
      ? new Date(Date.parse(lastAt) + timeoutMs).toISOString()
      : undefined,
    sequence: latest?.sequence ??
      firstNumber(sources, "heartbeat_sequence", "heartbeatSequence"),
    sourceEventId: latest?.eventId,
    connected,
  })
}

function buildResult(
  sources: readonly UnknownRecord[],
  events: readonly CausalEventProjection[],
  lifecycle: SubagentLifecycle,
): SubagentResultProjection | undefined {
  const resultSources = [
    firstRecord(sources, "result", "agent_result", "agentResult", "output"),
    ...sources,
  ].filter((source) => Object.keys(source).length > 0)
  const event = [...events].reverse().find((entry) =>
    RESULT_EVENT.test(entry.eventType) && !FAILURE_EVENT.test(entry.eventType))
  const digest = firstText(
    resultSources,
    "result_digest",
    "resultDigest",
    "output_digest",
    "outputDigest",
    "digest",
  )
  const summary = safeError(
    firstText(
      resultSources,
      "result_summary",
      "resultSummary",
      "summary",
      "message",
    ) ?? event?.summary,
  )
  const artifactIds = unique([
    ...firstIdentityList(resultSources, "artifact_ids", "artifactIds", "artifacts"),
    ...(event?.artifactIds ?? []),
  ])
  if (!digest && !summary && artifactIds.length === 0 && lifecycle !== "completed") {
    return undefined
  }
  const usageRecord = firstRecord(resultSources, "usage", "token_usage", "tokenUsage")
  const usage: Record<string, number> = {}
  for (const key of [
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cost_usd",
    "duration_ms",
    "tool_calls",
    "turns",
  ]) {
    const selected = firstNumber([usageRecord, ...resultSources], key, camel(key))
    if (selected !== undefined) usage[key] = selected
  }
  return Object.freeze({
    digest,
    summary,
    artifactIds,
    usage: Object.freeze(usage),
    committedEventId: event?.eventId,
    committedAt: event?.committedAt,
  })
}

function buildError(
  sources: readonly UnknownRecord[],
  events: readonly CausalEventProjection[],
  lifecycle: SubagentLifecycle,
  heartbeat: SubagentHeartbeatProjection,
): SubagentErrorProjection | undefined {
  const errorSources = [
    firstRecord(sources, "error", "failure", "last_error", "lastError"),
    ...sources,
  ].filter((source) => Object.keys(source).length > 0)
  const event = [...events].reverse().find((entry) => FAILURE_EVENT.test(entry.eventType))
  const code = firstText(
    errorSources,
    "error_code",
    "errorCode",
    "failure_code",
    "failureCode",
    "code",
  ) ?? (heartbeat.phase === "timed_out" ? "heartbeat_timeout" : undefined)
  const message = safeError(
    firstText(
      errorSources,
      "error_message",
      "errorMessage",
      "failure_reason",
      "failureReason",
      "message",
      "reason",
      "error",
    ) ?? event?.summary,
  )
  if (!code && !message && lifecycle !== "failed") return undefined
  return Object.freeze({
    code,
    message,
    failureId: event?.failureId ??
      firstIdentity(errorSources, "failure_id", "failureId"),
    eventId: event?.eventId,
    retryable: firstBoolean(errorSources, "retryable", "can_retry", "canRetry"),
    crashed:
      heartbeat.phase === "timed_out" ||
      /(?:crash|panic|process_exit|worker_lost|heartbeat_timeout)/i.test(
        `${code ?? ""} ${message ?? ""} ${event?.eventType ?? ""}`,
      ),
  })
}

function findTerminalEvent(
  events: readonly CausalEventProjection[],
  lifecycle: SubagentLifecycle,
): CausalEventProjection | undefined {
  const matcher =
    lifecycle === "completed"
      ? RESULT_EVENT
      : lifecycle === "failed"
        ? FAILURE_EVENT
        : lifecycle === "cancelled" || lifecycle === "killed"
          ? CANCEL_EVENT
          : /(?:completed|failed|cancel|killed|terminated)/i
  return [...events].reverse().find((event) => event.terminal || matcher.test(event.eventType))
}

function buildLateResults(
  childId: string,
  attempt: number,
  terminalEvent: CausalEventProjection,
  events: readonly CausalEventProjection[],
): readonly LateResultRecord[] {
  const result: LateResultRecord[] = []
  for (const event of events) {
    if (event.sequence <= terminalEvent.sequence) continue
    if (!RESULT_EVENT.test(event.eventType) || FAILURE_EVENT.test(event.eventType)) continue
    result.push(Object.freeze({
      id: fingerprint([
        childId,
        attempt,
        terminalEvent.eventId,
        event.eventId,
      ]),
      childId,
      attempt,
      terminalEventId: terminalEvent.eventId,
      resultEventId: event.eventId,
      terminalSequence: terminalEvent.sequence,
      resultSequence: event.sequence,
      resultDigest: firstText(eventSources(event), "result_digest", "resultDigest", "digest"),
      artifactIds: Object.freeze([...event.artifactIds]),
      reason: "Result arrived after the terminal fence and cannot commit.",
      quarantinedAt: event.committedAt,
      releasable: false,
    }))
  }
  return Object.freeze(result)
}

function inferLifecycle(
  worker: WorkerProjection | undefined,
  events: readonly CausalEventProjection[],
  sources: readonly UnknownRecord[],
): SubagentLifecycle {
  const declared = firstText(
    sources,
    "status",
    "lifecycle",
    "phase",
    "task_status",
    "taskStatus",
    "agent_status",
    "agentStatus",
  )
  const normalized = normalizeLifecycle(declared)
  if (normalized !== "unknown") return normalized
  const latest = events.at(-1)
  if (latest) {
    const fromEvent = normalizeLifecycle(latest.eventType)
    if (fromEvent !== "unknown") return fromEvent
    if (latest.terminal) {
      if (FAILURE_EVENT.test(latest.eventType)) return "failed"
      if (CANCEL_EVENT.test(latest.eventType)) return "cancelled"
      if (RESULT_EVENT.test(latest.eventType)) return "completed"
    }
  }
  return normalizeLifecycle(worker?.lifecycle)
}

function normalizeLifecycle(value: unknown): SubagentLifecycle {
  const selected = text(value).toLowerCase().replace(/[\s.:/-]+/g, "_")
  if (!selected) return "unknown"
  if (/kill|terminated|force_stop/.test(selected)) return "killed"
  if (/cancel|stopped/.test(selected)) return "cancelled"
  if (/reject|denied/.test(selected)) return "rejected"
  if (/fail|error|crash|timeout|lost/.test(selected)) return "failed"
  if (/complete|success|succeeded|done|settled/.test(selected)) return "completed"
  if (/recover/.test(selected)) return "recovering"
  if (/resum|reconnect|redispatch/.test(selected)) return "resuming"
  if (/pause/.test(selected)) return "paused"
  if (/wait|blocked/.test(selected)) return "waiting"
  if (/running|progress|heartbeat|started/.test(selected)) return "running"
  if (/starting|spawn/.test(selected)) return "starting"
  if (/dispatch/.test(selected)) return "dispatched"
  if (/ready|admitted/.test(selected)) return "ready"
  if (/validat/.test(selected)) return "validating"
  if (/creat|queued|pending/.test(selected)) return "created"
  return "unknown"
}

function terminalLifecycle(value: SubagentLifecycle): boolean {
  return ["completed", "failed", "cancelled", "killed", "rejected"].includes(value)
}

function inferAttempt(
  events: readonly CausalEventProjection[],
): number {
  return events.some((event) => RECONNECT_EVENT.test(event.eventType)) ? 1 : 0
}

function compareRows(
  left: SubagentRow,
  right: SubagentRow,
): number {
  const rank = (row: SubagentRow) => {
    if (row.lifecycle === "running") return 0
    if (["starting", "dispatched", "ready"].includes(row.lifecycle)) return 1
    if (["waiting", "paused", "recovering", "resuming"].includes(row.lifecycle)) return 2
    if (row.lifecycle === "failed") return 4
    if (row.lifecycle === "cancelled" || row.lifecycle === "killed") return 5
    if (row.lifecycle === "completed") return 6
    return 3
  }
  return (
    compareNumber(rank(left), rank(right)) ||
    compareNumber(left.depth, right.depth) ||
    compareNumber(right.sequence, left.sequence) ||
    compareText(left.id, right.id)
  )
}

function compareIssues(
  left: SubagentAdmissionIssue,
  right: SubagentAdmissionIssue,
): number {
  const rank = { error: 0, warning: 1, info: 2 }
  return (
    compareNumber(rank[left.severity], rank[right.severity]) ||
    compareText(left.childId, right.childId) ||
    compareText(left.code, right.code) ||
    compareText(left.eventId, right.eventId)
  )
}

function freezeRow(
  value: Omit<SubagentRow, "fingerprint"> | SubagentRow,
): SubagentRow {
  const selected = {
    ...value,
    childIds: Object.freeze([...value.childIds]),
    lateResults: Object.freeze([...value.lateResults]),
    eventIds: Object.freeze([...value.eventIds]),
    artifactIds: Object.freeze([...value.artifactIds]),
    issues: Object.freeze([...value.issues]),
  }
  return Object.freeze({
    ...selected,
    fingerprint: fingerprint([
      selected.id,
      selected.taskId,
      selected.runId,
      selected.sessionId,
      selected.parentId,
      selected.ownerId,
      selected.lifecycle,
      selected.depth,
      selected.attempt,
      selected.revision,
      selected.sequence,
      selected.checkpointId,
      selected.scope,
      selected.budget,
      selected.heartbeat,
      selected.result,
      selected.error,
      selected.lateResults,
      selected.issues,
    ]),
  })
}

function camel(value: string): string {
  return value.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase())
}

export function subagentEvents(
  state: CanonicalProjectionState,
  childId: string,
): readonly CausalEventProjection[] {
  return Object.freeze(
    Object.values(state.causality.byEvent)
      .filter((event) => eventReferencesChild(event, childId))
      .sort((left, right) =>
        compareNumber(left.sequence, right.sequence) ||
        compareText(left.eventId, right.eventId)),
  )
}

export function steeringEvidence(
  state: CanonicalProjectionState,
  childId: string,
  afterSequence: number,
  commandId?: string,
): readonly CausalEventProjection[] {
  return Object.freeze(
    subagentEvents(state, childId)
      .filter((event) => event.sequence > afterSequence)
      .filter((event) => STEER_EVENT.test(event.eventType))
      .filter((event) =>
        !commandId ||
        event.controlCommandId === commandId ||
        state.causality.byControlCommand[commandId]?.includes(event.eventId))
      .sort((left, right) =>
        compareNumber(left.sequence, right.sequence) ||
        compareText(left.eventId, right.eventId)),
  )
}

export function terminalControlEvidence(
  state: CanonicalProjectionState,
  childId: string,
  afterSequence: number,
  commandId?: string,
): readonly CausalEventProjection[] {
  return Object.freeze(
    subagentEvents(state, childId)
      .filter((event) => event.sequence > afterSequence)
      .filter((event) => CANCEL_EVENT.test(event.eventType) || event.terminal)
      .filter((event) =>
        !commandId ||
        event.controlCommandId === commandId ||
        state.causality.byControlCommand[commandId]?.includes(event.eventId))
      .sort((left, right) =>
        compareNumber(left.sequence, right.sequence) ||
        compareText(left.eventId, right.eventId)),
  )
}

export function subagentProjectionContainsSecrets(
  projection: SubagentProjection,
): readonly string[] {
  return secretPaths(projection)
}
