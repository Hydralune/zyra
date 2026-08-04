import { CONTRACT_NAMES, ACTIVE_TASK_STATUSES, TERMINAL_TASK_STATUSES } from "./constants.ts"
import { ResponseValidationError } from "./errors.ts"
import type { IdentityBinding } from "./identifiers.ts"
import {
  bindIdentities,
  bindingFromUnknown,
  normalizeIdentity,
  optionalIdentity,
  validateBinding,
} from "./identifiers.ts"
import {
  arrayBody,
  objectBody,
  optionalResponseRecord,
  optionalResponseString,
  responseArray,
  responseBoolean,
  responseInteger,
  responseNumber,
  responseRecord,
  responseString,
  type NormalizerRegistry,
} from "./response.ts"

export interface ApiHealth {
  status: string
  phase: string
  service: string
  apiVersion: string
  now?: string
  capabilities: string[]
  raw: Record<string, unknown>
}

export interface RuntimeReadiness {
  ready: boolean
  status: string
  apiVersion: string
  owners: Record<string, boolean>
  blockers: string[]
  raw: Record<string, unknown>
}

export interface ArtifactProjection {
  artifactId: string
  kind: string
  path?: string
  uri?: string
  mediaType?: string
  title?: string
  sizeBytes?: number
  createdAt?: string
  metadata: Record<string, unknown>
}

export interface PlanNodeProjection {
  nodeId: string
  title: string
  description: string
  status: string
  parentNodeId?: string
  assignedWorkerId?: string
  dependsOn: string[]
  artifactIds: string[]
  updatedAt?: string
  metadata: Record<string, unknown>
}

export interface TaskProjection {
  taskId: string
  runId: string
  sessionId?: string
  rootNodeId: string
  userGoal: string
  status: string
  createdAt: string
  updatedAt: string
  planNodes: PlanNodeProjection[]
  artifacts: ArtifactProjection[]
  metadata: Record<string, unknown>
  binding: IdentityBinding
  terminal: boolean
  active: boolean
}

export interface EventProjection {
  eventId: string
  runId: string
  taskId: string
  nodeId?: string
  spanId?: string
  checkpointId?: string
  toolId?: string
  artifactId?: string
  controlCommandId?: string
  eventType: string
  createdAt: string
  payload: Record<string, unknown>
  binding: IdentityBinding
}

export interface TaskListProjection {
  tasks: TaskProjection[]
  total: number
  cursor?: string
}

export interface SessionProjection {
  sessionId: string
  taskIds: string[]
  activeTaskIds: string[]
  latestTaskId: string
  resumeTaskId?: string
  resolution: "resolved" | "ambiguous"
  taskCount: number
  statuses: string[]
  active: boolean
  terminal: boolean
  createdAt?: string
  updatedAt?: string
}

export interface SessionListProjection {
  sessions: SessionProjection[]
  total: number
  cursor?: string
  stateOwner: "task_store_projection"
}

export interface TaskMutationProjection {
  task: TaskProjection
  events: EventProjection[]
  receipt: Record<string, unknown>
  controls: Record<string, unknown>
  raw: Record<string, unknown>
}

export interface ControlCommandProjection {
  task?: TaskProjection
  controlRequest: Record<string, unknown>
  command: Record<string, unknown>
  commandResult: Record<string, unknown>
  event?: Record<string, unknown>
  interventionCounted: boolean
  raw: Record<string, unknown>
}

export interface PermissionControlProjection {
  schema: string
  ok: boolean
  operation: string
  stateOwner: string
  sessionId?: string
  error?: string
  message?: string
  raw: Record<string, unknown>
}

function unknownRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

export function normalizePermissionControl(
  value: unknown,
): PermissionControlProjection {
  const body = objectBody(value, "permission control response")
  const schema = responseString(
    body.schema ?? "zyra.permission-api.v2",
    "permission.schema",
  )
  if (
    schema !== "zyra.permission-api.v2"
    && schema !== "zyra.permission-api/v1"
  ) {
    throw new ResponseValidationError(
      "Permission control response schema is unsupported.",
      { schema },
    )
  }
  const ok = responseBoolean(body.ok, "permission.ok")
  const operation = responseString(
    body.operation ?? (ok ? "permission.unknown" : "permission.error"),
    "permission.operation",
  )
  const stateOwner = responseString(
    body.state_owner
      ?? body.stateOwner
      ?? (ok ? "typescript.PermissionCoordinator" : "unknown"),
    "permission.state_owner",
  )
  return {
    schema,
    ok,
    operation,
    stateOwner,
    sessionId: optionalResponseString(
      body.session_id ?? body.sessionId,
      "permission.session_id",
    ),
    error: optionalResponseString(body.error, "permission.error"),
    message: optionalResponseString(body.message, "permission.message"),
    raw: { ...body },
  }
}

function optionalNumber(value: unknown, label: string): number | undefined {
  if (value === undefined || value === null) return undefined
  return responseNumber(value, label)
}

function isoTimestamp(value: unknown, label: string, options: { required?: boolean } = {}): string {
  if ((value === undefined || value === null || value === "") && options.required === false) return ""
  const timestamp = responseString(value, label)
  if (Number.isNaN(Date.parse(timestamp))) throw new ResponseValidationError(`${label} must be ISO-8601.`, { value })
  return timestamp
}

function optionalTimestamp(value: unknown, label: string): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return isoTimestamp(value, label)
}

function statusValue(value: unknown, label: string): string {
  const status = responseString(value, label).trim().toLowerCase()
  if (!/^[a-z][a-z0-9_.-]{1,63}$/.test(status)) {
    throw new ResponseValidationError(`${label} contains an invalid status.`, { status })
  }
  return status
}

function stringArray(value: unknown, label: string): string[] {
  if (value === undefined || value === null) return []
  return responseArray(value, label, (entry, index) => responseString(entry, `${label}[${index}]`))
}

function identifierArray(kind: Parameters<typeof normalizeIdentity>[0], value: unknown, label: string): string[] {
  if (value === undefined || value === null) return []
  return responseArray(value, label, (entry) => normalizeIdentity(kind, entry))
}

export function normalizeArtifact(value: unknown, index = 0): ArtifactProjection {
  const item = responseRecord(value, `artifact[${index}]`)
  const artifactId = normalizeIdentity("artifact", item.artifact_id ?? item.artifactId)
  const kind = responseString(item.kind ?? item.artifact_kind ?? "artifact", `artifact[${index}].kind`)
  return {
    artifactId,
    kind,
    path: optionalResponseString(item.path, `artifact[${index}].path`),
    uri: optionalResponseString(item.uri, `artifact[${index}].uri`),
    mediaType: optionalResponseString(item.media_type ?? item.mediaType, `artifact[${index}].media_type`),
    title: optionalResponseString(item.title ?? item.name, `artifact[${index}].title`),
    sizeBytes: optionalNumber(item.size_bytes ?? item.sizeBytes, `artifact[${index}].size_bytes`),
    createdAt: optionalTimestamp(item.created_at ?? item.createdAt, `artifact[${index}].created_at`),
    metadata: { ...unknownRecord(item.metadata) },
  }
}

export function normalizePlanNode(value: unknown, key?: string, index = 0): PlanNodeProjection {
  const item = responseRecord(value, `plan_node[${index}]`)
  const nodeId = responseString(item.node_id ?? item.nodeId ?? key, `plan_node[${index}].node_id`)
  if (!/^node[_:-][A-Za-z0-9][A-Za-z0-9._:-]{2,240}$/.test(nodeId)) {
    throw new ResponseValidationError(`plan_node[${index}].node_id is invalid.`, { node_id: nodeId })
  }
  const artifactValues = item.artifact_refs ?? item.artifacts ?? []
  const artifactIds = Array.isArray(artifactValues)
    ? artifactValues.map((artifact, artifactIndex) => {
        if (typeof artifact === "string") return normalizeIdentity("artifact", artifact)
        const record = responseRecord(artifact, `plan_node[${index}].artifact_refs[${artifactIndex}]`)
        return normalizeIdentity("artifact", record.artifact_id ?? record.artifactId)
      })
    : []
  return {
    nodeId,
    title: responseString(item.title ?? "", `plan_node[${index}].title`, { allowEmpty: true }),
    description: responseString(item.description ?? "", `plan_node[${index}].description`, { allowEmpty: true }),
    status: statusValue(item.status ?? "pending", `plan_node[${index}].status`),
    parentNodeId: optionalResponseString(item.parent_node_id ?? item.parentNodeId, `plan_node[${index}].parent_node_id`),
    assignedWorkerId: optionalResponseString(
      item.assigned_worker_id ?? item.assignedWorkerId,
      `plan_node[${index}].assigned_worker_id`,
    ),
    dependsOn: stringArray(item.depends_on ?? item.dependsOn, `plan_node[${index}].depends_on`),
    artifactIds,
    updatedAt: optionalTimestamp(item.updated_at ?? item.updatedAt, `plan_node[${index}].updated_at`),
    metadata: { ...unknownRecord(item.metadata) },
  }
}

function normalizePlanNodes(value: unknown): PlanNodeProjection[] {
  if (value === undefined || value === null) return []
  if (Array.isArray(value)) return value.map((entry, index) => normalizePlanNode(entry, undefined, index))
  const items = responseRecord(value, "task.plan_nodes")
  return Object.entries(items)
    .map(([key, entry], index) => normalizePlanNode(entry, key, index))
    .sort((left, right) => left.nodeId.localeCompare(right.nodeId))
}

export function normalizeTask(value: unknown, index = 0): TaskProjection {
  const item = responseRecord(value, `task[${index}]`)
  const taskId = normalizeIdentity("task", item.task_id ?? item.taskId)
  const runId = normalizeIdentity("run", item.run_id ?? item.runId)
  const metadata = { ...unknownRecord(item.metadata) }
  const sessionId = optionalIdentity(
    "session",
    item.session_id ??
      item.sessionId ??
      metadata.query_session_id ??
      metadata.session_id,
  )
  const rootNodeId = responseString(item.root_node_id ?? item.rootNodeId, `task[${index}].root_node_id`)
  if (!/^node[_:-][A-Za-z0-9][A-Za-z0-9._:-]{2,240}$/.test(rootNodeId)) {
    throw new ResponseValidationError(`task[${index}].root_node_id is invalid.`, { root_node_id: rootNodeId })
  }
  const status = statusValue(item.status ?? "pending", `task[${index}].status`)
  const planNodes = normalizePlanNodes(item.plan_nodes ?? item.planNodes)
  if (planNodes.length && !planNodes.some((node) => node.nodeId === rootNodeId)) {
    throw new ResponseValidationError("Task root node is absent from plan nodes.", {
      task_id: taskId,
      root_node_id: rootNodeId,
    })
  }
  const artifacts = responseArray(item.artifacts ?? [], `task[${index}].artifacts`, normalizeArtifact)
  const binding = validateBinding({ taskId, runId, sessionId })
  return {
    taskId,
    runId,
    sessionId,
    rootNodeId,
    userGoal: responseString(item.user_goal ?? item.userGoal ?? "", `task[${index}].user_goal`, { allowEmpty: true }),
    status,
    createdAt: isoTimestamp(item.created_at ?? item.createdAt, `task[${index}].created_at`),
    updatedAt: isoTimestamp(item.updated_at ?? item.updatedAt, `task[${index}].updated_at`),
    planNodes,
    artifacts,
    metadata,
    binding,
    terminal: TERMINAL_TASK_STATUSES.has(status),
    active: ACTIVE_TASK_STATUSES.has(status),
  }
}

export function normalizeEvent(value: unknown, index = 0, expected: IdentityBinding = {}): EventProjection {
  const item = responseRecord(value, `event[${index}]`)
  const eventId = normalizeIdentity("event", item.event_id ?? item.eventId)
  const runId = normalizeIdentity("run", item.run_id ?? item.runId ?? expected.runId)
  const taskId = normalizeIdentity("task", item.task_id ?? item.taskId ?? expected.taskId)
  const payload = { ...unknownRecord(item.payload) }
  const inferred = bindingFromUnknown({
    ...payload,
    ...item,
    event_id: eventId,
    run_id: runId,
    task_id: taskId,
  })
  const binding = bindIdentities(expected, inferred)
  return {
    eventId,
    runId,
    taskId,
    nodeId: optionalResponseString(item.node_id ?? item.nodeId, `event[${index}].node_id`),
    spanId: binding.spanId,
    checkpointId: binding.checkpointId,
    toolId: binding.toolId,
    artifactId: binding.artifactId,
    controlCommandId: binding.controlCommandId,
    eventType: responseString(item.event_type ?? item.eventType, `event[${index}].event_type`),
    createdAt: isoTimestamp(item.created_at ?? item.createdAt, `event[${index}].created_at`),
    payload,
    binding,
  }
}

function taskEnvelope(value: unknown): Record<string, unknown> {
  const body = objectBody(value, "task response")
  const task = body.task
  if (!task) throw new ResponseValidationError("Task response omitted task.")
  return responseRecord(task, "task response.task")
}

export function normalizeHealth(value: unknown): ApiHealth {
  const body = objectBody(value, "health response")
  const status = responseString(body.status ?? "ok", "health.status")
  const capabilitiesValue = body.capabilities
  const capabilities = Array.isArray(capabilitiesValue)
    ? stringArray(capabilitiesValue, "health.capabilities")
    : capabilitiesValue && typeof capabilitiesValue === "object"
      ? Object.entries(capabilitiesValue as Record<string, unknown>)
          .filter(([, enabled]) => enabled === true)
          .map(([name]) => name)
          .sort()
      : []
  return {
    status,
    phase: responseString(body.phase ?? body.stage ?? "runtime", "health.phase"),
    service: responseString(body.service ?? "zyra-api", "health.service"),
    apiVersion: responseString(body.api_version ?? body.apiVersion ?? "1.0", "health.api_version"),
    now: optionalTimestamp(body.now ?? body.timestamp, "health.now"),
    capabilities,
    raw: { ...body },
  }
}

export function normalizeReadiness(value: unknown): RuntimeReadiness {
  const body = objectBody(value, "runtime readiness response")
  const ownersValue = optionalResponseRecord(body.owners, "runtime readiness.owners") ?? {}
  const owners: Record<string, boolean> = {}
  for (const [name, ready] of Object.entries(ownersValue)) {
    owners[name] = responseBoolean(ready, `runtime readiness.owners.${name}`)
  }
  const blockers = stringArray(body.blockers ?? [], "runtime readiness.blockers")
  const ready =
    body.ready === undefined
      ? Object.values(owners).every(Boolean) && blockers.length === 0
      : responseBoolean(body.ready, "runtime readiness.ready")
  return {
    ready,
    status: statusValue(body.status ?? (ready ? "ready" : "blocked"), "runtime readiness.status"),
    apiVersion: responseString(body.api_version ?? body.apiVersion ?? "1.0", "runtime readiness.api_version"),
    owners,
    blockers,
    raw: { ...body },
  }
}

export function normalizeTaskDetail(value: unknown): TaskProjection {
  return normalizeTask(taskEnvelope(value))
}

export function normalizeTaskList(value: unknown): TaskListProjection {
  const body = objectBody(value, "task list response")
  const tasks = responseArray(body.tasks ?? [], "task list.tasks", normalizeTask)
  const total =
    body.total === undefined
      ? tasks.length
      : responseInteger(body.total, "task list.total")
  if (total < tasks.length) {
    throw new ResponseValidationError("Task list total is smaller than returned task count.", {
      total,
      returned: tasks.length,
    })
  }
  return {
    tasks,
    total,
    cursor: optionalResponseString(body.cursor ?? body.next_cursor, "task list.cursor"),
  }
}

export function normalizeSession(value: unknown, index = 0): SessionProjection {
  const item = responseRecord(value, `session[${index}]`)
  const resolution = responseString(item.resolution, `session[${index}].resolution`)
  if (resolution !== "resolved" && resolution !== "ambiguous") {
    throw new ResponseValidationError("Session resolution is unsupported.", { resolution })
  }
  const sessionId = normalizeIdentity("session", item.session_id ?? item.sessionId)
  const taskIds = identifierArray("task", item.task_ids ?? item.taskIds, `session[${index}].task_ids`)
  const activeTaskIds = identifierArray(
    "task",
    item.active_task_ids ?? item.activeTaskIds,
    `session[${index}].active_task_ids`,
  )
  const latestTaskId = normalizeIdentity("task", item.latest_task_id ?? item.latestTaskId)
  const resumeTaskId = optionalIdentity("task", item.resume_task_id ?? item.resumeTaskId)
  if (resolution === "resolved" && !resumeTaskId) {
    throw new ResponseValidationError("Resolved session omitted resume task identity.", { session_id: sessionId })
  }
  if (resolution === "ambiguous" && resumeTaskId) {
    throw new ResponseValidationError("Ambiguous session exposed a resume task identity.", { session_id: sessionId })
  }
  if (!taskIds.includes(latestTaskId) || (resumeTaskId && !taskIds.includes(resumeTaskId))) {
    throw new ResponseValidationError("Session task identities are inconsistent.", { session_id: sessionId })
  }
  return {
    sessionId,
    taskIds,
    activeTaskIds,
    latestTaskId,
    resumeTaskId,
    resolution,
    taskCount: responseInteger(item.task_count ?? item.taskCount, `session[${index}].task_count`),
    statuses: stringArray(item.statuses, `session[${index}].statuses`),
    active: responseBoolean(item.active, `session[${index}].active`),
    terminal: responseBoolean(item.terminal, `session[${index}].terminal`),
    createdAt: optionalTimestamp(item.created_at ?? item.createdAt, `session[${index}].created_at`),
    updatedAt: optionalTimestamp(item.updated_at ?? item.updatedAt, `session[${index}].updated_at`),
  }
}

export function normalizeSessionList(value: unknown): SessionListProjection {
  const body = objectBody(value, "session list response")
  const stateOwner = responseString(body.state_owner ?? body.stateOwner, "session list.state_owner")
  if (stateOwner !== "task_store_projection") {
    throw new ResponseValidationError("Session list owner is not task-backed.", { state_owner: stateOwner })
  }
  const sessions = responseArray(body.sessions ?? [], "session list.sessions", normalizeSession)
  const total = responseInteger(body.total ?? sessions.length, "session list.total")
  if (total < sessions.length) {
    throw new ResponseValidationError("Session list total is smaller than returned session count.", { total })
  }
  return {
    sessions,
    total,
    cursor: optionalResponseString(body.cursor, "session list.cursor"),
    stateOwner,
  }
}

export function normalizeSessionDetail(value: unknown): SessionProjection {
  const body = objectBody(value, "session detail response")
  const stateOwner = responseString(body.state_owner ?? body.stateOwner, "session detail.state_owner")
  if (stateOwner !== "task_store_projection") {
    throw new ResponseValidationError("Session detail owner is not task-backed.", { state_owner: stateOwner })
  }
  return normalizeSession(body.session)
}

export function normalizeTaskEvents(value: unknown): EventProjection[] {
  const body = objectBody(value, "task events response")
  return responseArray(body.events ?? [], "task events.events", normalizeEvent)
}

export function normalizeEventIngressEnvelope(value: unknown): Record<string, unknown> {
  const body = objectBody(value, "event ingress response")
  return { ...body }
}

export function normalizeTaskMutation(value: unknown): TaskMutationProjection {
  const body = objectBody(value, "task mutation response")
  const task = normalizeTask(body.task)
  const events = responseArray(
    body.events ?? [],
    "task mutation.events",
    (entry, index) => normalizeEvent(entry, index, task.binding),
  )
  for (const event of events) {
    if (event.taskId !== task.taskId || event.runId !== task.runId) {
      throw new ResponseValidationError("Task mutation returned a cross-task event.", {
        task_id: task.taskId,
        run_id: task.runId,
        event_id: event.eventId,
        event_task_id: event.taskId,
        event_run_id: event.runId,
      })
    }
  }
  const receipt = optionalResponseRecord(body.receipt, "task mutation.receipt") ?? {}
  const controls: Record<string, unknown> = {}
  for (const key of [
    "backend_dispatch_control",
    "worker_pool_control",
    "cancelled_subagents",
    "cancelled_physical_children",
    "subagent_cancel_errors",
    "memory_curator",
  ]) {
    if (body[key] !== undefined) controls[key] = body[key]
  }
  return {
    task,
    events,
    receipt,
    controls,
    raw: { ...body },
  }
}

export function normalizeControlCommand(value: unknown): ControlCommandProjection {
  const body = objectBody(value, "control command response")
  const task = body.task === undefined ? undefined : normalizeTask(body.task)
  const controlRequest =
    optionalResponseRecord(body.control_request, "control command.control_request") ?? {}
  const command =
    optionalResponseRecord(body.command, "control command.command") ?? {}
  const commandResult =
    optionalResponseRecord(body.command_result, "control command.command_result") ?? {}
  const event = optionalResponseRecord(body.event, "control command.event")
  const interventionCounted =
    body.intervention_counted === true ||
    commandResult.intervention_counted === true ||
    Boolean(
      commandResult.data &&
      typeof commandResult.data === "object" &&
      !Array.isArray(commandResult.data) &&
      (commandResult.data as Record<string, unknown>).intervention_counted === true
    )
  return {
    task,
    controlRequest,
    command,
    commandResult,
    event,
    interventionCounted,
    raw: { ...body },
  }
}

export function registerCoreNormalizers(registry: NormalizerRegistry): void {
  registry.register(CONTRACT_NAMES.health, normalizeHealth)
  registry.register(CONTRACT_NAMES.readiness, normalizeReadiness)
  registry.register(CONTRACT_NAMES.taskList, normalizeTaskList)
  registry.register(CONTRACT_NAMES.taskDetail, normalizeTaskDetail)
  registry.register(CONTRACT_NAMES.sessionList, normalizeSessionList)
  registry.register(CONTRACT_NAMES.sessionDetail, normalizeSessionDetail)
  registry.register(CONTRACT_NAMES.taskEvents, normalizeTaskEvents)
  registry.register(CONTRACT_NAMES.taskEventIngressCapabilities, normalizeEventIngressEnvelope)
  registry.register(CONTRACT_NAMES.taskEventIngressSnapshot, normalizeEventIngressEnvelope)
  registry.register(CONTRACT_NAMES.taskEventIngressDelta, normalizeEventIngressEnvelope)
  registry.register(CONTRACT_NAMES.taskEventIngressSse, normalizeEventIngressEnvelope)
  registry.register(CONTRACT_NAMES.taskMutation, normalizeTaskMutation)
  registry.register(CONTRACT_NAMES.taskControlCommand, normalizeControlCommand)
  registry.register(
    CONTRACT_NAMES.taskLoopxState,
    (value) => ({ ...responseRecord(value, "LoopX control state response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskLoopxCommand,
    (value) => ({ ...responseRecord(value, "LoopX control result response") }),
  )
  registry.register(
    CONTRACT_NAMES.policyEvidence,
    (value) => ({ ...responseRecord(value, "policy evidence response") }),
  )
  registry.register(
    CONTRACT_NAMES.policyMetricSpecs,
    (value) => ({ ...responseRecord(value, "policy metric registry response") }),
  )
  registry.register(
    CONTRACT_NAMES.policyMetricReport,
    (value) => ({ ...responseRecord(value, "policy metric report response") }),
  )
  registry.register(CONTRACT_NAMES.taskCommandQueue, normalizeEventIngressEnvelope)
  registry.register(CONTRACT_NAMES.taskCommandCancel, normalizeEventIngressEnvelope)
  registry.register(CONTRACT_NAMES.permissionControl, normalizePermissionControl)
  registry.register(
    CONTRACT_NAMES.taskArtifactCatalog,
    (value) => ({ ...responseRecord(value, "artifact catalog response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskArtifactMetadata,
    (value) => ({ ...responseRecord(value, "artifact metadata response") }),
  )
  // Metadata and ranged content intentionally share zyra.artifact-read.v2.
  // One contract owns one normalizer; both endpoints receive the same bounded
  // record projection without attempting a duplicate registry mutation.
  registry.register(
    CONTRACT_NAMES.taskArtifactReceipts,
    (value) => ({ ...responseRecord(value, "artifact receipts response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskDiffReviewManifest,
    (value) => ({ ...responseRecord(value, "diff review manifest response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskDiffReviewPage,
    (value) => ({ ...responseRecord(value, "diff review page response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskDiffReviewFileContent,
    (value) => ({ ...responseRecord(value, "diff review file content response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskDiffReviewComment,
    (value) => ({ ...responseRecord(value, "diff review comment response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskDiffReviewTransaction,
    (value) => ({ ...responseRecord(value, "diff review transaction response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskTerminalList,
    (value) => ({ ...responseRecord(value, "terminal list response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskTerminalSession,
    (value) => ({ ...responseRecord(value, "terminal session response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskTerminalReceipt,
    (value) => ({ ...responseRecord(value, "terminal mutation response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskBrowserObservability,
    (value) => ({ ...responseRecord(value, "browser observability response") }),
  )
  registry.register(
    CONTRACT_NAMES.taskBrowserControl,
    (value) => ({ ...responseRecord(value, "browser control response") }),
  )
  registry.register(
    CONTRACT_NAMES.scenarioRegistry,
    (value) => ({ ...responseRecord(value, "scenario registry response") }),
  )
  registry.register(
    CONTRACT_NAMES.scenarioRun,
    (value) => ({ ...responseRecord(value, "scenario run response") }),
  )
  registry.register(
    CONTRACT_NAMES.scenarioEvidence,
    (value) => ({ ...responseRecord(value, "scenario evidence response") }),
  )
  registry.register(
    CONTRACT_NAMES.scenarioMutation,
    (value) => ({ ...responseRecord(value, "scenario mutation response") }),
  )
  registry.register(
    CONTRACT_NAMES.experimentRegistry,
    (value) => ({ ...responseRecord(value, "experiment registry response") }),
  )
  registry.register(
    CONTRACT_NAMES.experimentRun,
    (value) => ({ ...responseRecord(value, "experiment run response") }),
  )
  registry.register(
    CONTRACT_NAMES.experimentReport,
    (value) => ({ ...responseRecord(value, "experiment report response") }),
  )
  registry.register(
    CONTRACT_NAMES.experimentSamples,
    (value) => ({ ...responseRecord(value, "experiment sample response") }),
  )
  registry.register(
    CONTRACT_NAMES.experimentBundle,
    (value) => ({ ...responseRecord(value, "experiment bundle response") }),
  )
  registry.register(
    CONTRACT_NAMES.experimentSource,
    (value) => ({ ...responseRecord(value, "experiment source response") }),
  )
  registry.register(
    CONTRACT_NAMES.experimentRequirements,
    (value) => ({ ...responseRecord(value, "experiment requirement response") }),
  )
  registry.register(
    CONTRACT_NAMES.experimentMutation,
    (value) => ({ ...responseRecord(value, "experiment mutation response") }),
  )
}

export function assertTaskBinding(task: TaskProjection, expected: IdentityBinding): TaskProjection {
  const combined = bindIdentities(expected, task.binding)
  return { ...task, binding: combined }
}

export function taskToWire(task: TaskProjection): Record<string, unknown> {
  return {
    task_id: task.taskId,
    run_id: task.runId,
    session_id: task.sessionId,
    root_node_id: task.rootNodeId,
    user_goal: task.userGoal,
    status: task.status,
    created_at: task.createdAt,
    updated_at: task.updatedAt,
    plan_nodes: Object.fromEntries(
      task.planNodes.map((node) => [
        node.nodeId,
        {
          node_id: node.nodeId,
          title: node.title,
          description: node.description,
          status: node.status,
          parent_node_id: node.parentNodeId,
          assigned_worker_id: node.assignedWorkerId,
          depends_on: [...node.dependsOn],
          artifact_ids: [...node.artifactIds],
          updated_at: node.updatedAt,
          metadata: { ...node.metadata },
        },
      ]),
    ),
    artifacts: task.artifacts.map((artifact) => ({
      artifact_id: artifact.artifactId,
      kind: artifact.kind,
      path: artifact.path,
      uri: artifact.uri,
      media_type: artifact.mediaType,
      title: artifact.title,
      size_bytes: artifact.sizeBytes,
      created_at: artifact.createdAt,
      metadata: { ...artifact.metadata },
    })),
    metadata: { ...task.metadata },
  }
}

export function eventToWire(event: EventProjection): Record<string, unknown> {
  return {
    event_id: event.eventId,
    run_id: event.runId,
    task_id: event.taskId,
    node_id: event.nodeId,
    span_id: event.spanId,
    checkpoint_id: event.checkpointId,
    tool_id: event.toolId,
    artifact_id: event.artifactId,
    control_command_id: event.controlCommandId,
    event_type: event.eventType,
    created_at: event.createdAt,
    payload: { ...event.payload },
  }
}

export function taskStatusCounts(tasks: readonly TaskProjection[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const task of tasks) counts[task.status] = (counts[task.status] ?? 0) + 1
  return counts
}

export function latestTaskUpdate(tasks: readonly TaskProjection[]): string | undefined {
  let latest: string | undefined
  let latestTime = Number.NEGATIVE_INFINITY
  for (const task of tasks) {
    const timestamp = Date.parse(task.updatedAt)
    if (timestamp > latestTime) {
      latest = task.updatedAt
      latestTime = timestamp
    }
  }
  return latest
}

export function causalEventOrder(events: readonly EventProjection[]): EventProjection[] {
  return [...events].sort((left, right) => {
    const time = Date.parse(left.createdAt) - Date.parse(right.createdAt)
    if (time !== 0) return time
    return left.eventId.localeCompare(right.eventId)
  })
}

export function verifyEventOrder(events: readonly EventProjection[]): void {
  const ordered = causalEventOrder(events)
  for (let index = 0; index < events.length; index += 1) {
    if (events[index]?.eventId !== ordered[index]?.eventId) {
      throw new ResponseValidationError("Task events are not in deterministic causal order.", {
        index,
        actual_event_id: events[index]?.eventId,
        expected_event_id: ordered[index]?.eventId,
      })
    }
  }
}
