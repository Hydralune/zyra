import type { IngressArtifactRef, IngressEvent, JsonObject } from "../events/ingress/index.ts"
import {
  EntityLifecycle,
  ProjectionDomain,
  ProjectionStatus,
  type ArtifactProjection,
  type CommandProjection,
  type DomainProjection,
  type MemoryProjection,
  type MutableProjectionState,
  type NodeProjection,
  type OverlayProjection,
  type PermissionProjection,
  type ProjectionDomainValue,
  type ProjectionEntity,
  type ProjectionEventContext,
  type RecoveryProjection,
  type SchedulerProjection,
  type SessionProjection,
  type TaskProjection,
  type ToolProjection,
  type WorkerProjection,
} from "./contracts.ts"
import {
  artifactIds,
  baseEntity,
  booleanValue,
  entityKey,
  eventTitle,
  failureId,
  firstBoolean,
  firstNumber,
  firstString,
  inferDomain,
  inferEntityId,
  lifecycleIsTerminal,
  numberValue,
  objectValue,
  orderedUnion,
  projectionPayload,
  recoveryId,
  stringArray,
  stringValue,
  uniqueStrings,
} from "./value.ts"

export interface ProjectionDispatchResult {
  domain: ProjectionDomainValue
  entityIds: readonly string[]
  primaryEntityId: string
}

export function projectEvent(
  context: ProjectionEventContext,
): ProjectionDispatchResult {
  const domain = inferDomain(context.event)
  const primary = inferEntityId(context.event, domain)
  const ids: string[] = []
  if (domain !== ProjectionDomain.TASK) ensureTaskProjection(context)
  ensureIdentityProjections(context, domain)
  switch (domain) {
    case ProjectionDomain.TASK:
      ids.push(projectTask(context, primary).id)
      break
    case ProjectionDomain.NODE:
      ids.push(projectNode(context, primary).id)
      break
    case ProjectionDomain.WORKER:
      ids.push(projectWorker(context, primary).id)
      break
    case ProjectionDomain.TOOL:
      ids.push(projectTool(context, primary).id)
      break
    case ProjectionDomain.ARTIFACT:
      ids.push(...projectArtifacts(context, primary).map((item) => item.id))
      break
    case ProjectionDomain.MEMORY:
      ids.push(projectMemory(context, primary).id)
      break
    case ProjectionDomain.SCHEDULER:
      ids.push(projectScheduler(context, primary).id)
      break
    case ProjectionDomain.RECOVERY:
      ids.push(projectRecovery(context, primary).id)
      break
    case ProjectionDomain.COMMAND:
      ids.push(projectCommand(context, primary).id)
      break
    case ProjectionDomain.PERMISSION:
      ids.push(projectPermission(context, primary).id)
      break
    case ProjectionDomain.OVERLAY:
      ids.push(projectOverlay(context, primary).id)
      break
    case ProjectionDomain.SESSION:
      ids.push(projectSession(context, primary).id)
      break
    default:
      ids.push(projectTask(context, context.event.identity.taskId).id)
  }
  if (context.event.artifactRefs.length > 0 && domain !== ProjectionDomain.ARTIFACT) {
    ids.push(...projectArtifacts(context).map((item) => item.id))
  }
  linkCrossDomainRelationships(context, domain, ids)
  reconcileTaskFromNodes(context)
  return {
    domain,
    entityIds: uniqueStrings(ids),
    primaryEntityId: primary,
  }
}

function ensureIdentityProjections(
  context: ProjectionEventContext,
  primaryDomain: ProjectionDomainValue,
): void {
  const event = context.event
  if (
    event.identity.sessionId &&
    primaryDomain !== ProjectionDomain.SESSION &&
    !context.state.sessions[event.identity.sessionId]
  ) {
    const base = baseEntity(
      event,
      ProjectionDomain.SESSION,
      event.identity.sessionId,
    )
    const session: SessionProjection = {
      ...base,
      lifecycle: EntityLifecycle.UNKNOWN,
      terminal: false,
      progress: undefined,
      domain: ProjectionDomain.SESSION,
      compactCount: 0,
      childSessionIds: [],
    }
    context.state.sessions[session.id] = session
    markEntity(context, session)
  }
  if (
    event.identity.nodeId &&
    primaryDomain !== ProjectionDomain.NODE &&
    !context.state.nodes[event.identity.nodeId]
  ) {
    const base = baseEntity(event, ProjectionDomain.NODE, event.identity.nodeId)
    const node: NodeProjection = {
      ...base,
      lifecycle: EntityLifecycle.UNKNOWN,
      terminal: false,
      progress: undefined,
      domain: ProjectionDomain.NODE,
      capabilityRefs: [],
      dependencyIds: [],
      childNodeIds: [],
    }
    context.state.nodes[node.id] = node
    markEntity(context, node)
  }
  if (
    event.identity.workerId &&
    primaryDomain !== ProjectionDomain.WORKER &&
    !context.state.workers[event.identity.workerId]
  ) {
    const base = baseEntity(
      event,
      ProjectionDomain.WORKER,
      event.identity.workerId,
    )
    const worker: WorkerProjection = {
      ...base,
      lifecycle: EntityLifecycle.UNKNOWN,
      terminal: false,
      progress: undefined,
      domain: ProjectionDomain.WORKER,
      capabilityRefs: [],
    }
    context.state.workers[worker.id] = worker
    markEntity(context, worker)
  }
}

function ensureTaskProjection(context: ProjectionEventContext): TaskProjection {
  const taskId = context.event.identity.taskId
  const existing = context.state.tasks[taskId]
  if (existing) return existing
  const base = baseEntity(context.event, ProjectionDomain.TASK, taskId)
  const task: TaskProjection = {
    ...base,
    domain: ProjectionDomain.TASK,
    activeNodeIds: context.event.identity.nodeId
      ? [context.event.identity.nodeId]
      : [],
    workerIds: context.event.identity.workerId
      ? [context.event.identity.workerId]
      : [],
    sessionIds: context.event.identity.sessionId
      ? [context.event.identity.sessionId]
      : [],
    artifactIds: context.event.artifactRefs.map((item) => item.artifactId),
    pendingPermissionIds: [],
    commandIds: context.event.identity.controlCommandId
      ? [context.event.identity.controlCommandId]
      : [],
    recoveryIds: [],
    lastCheckpointId: context.event.identity.checkpointId,
  }
  context.state.tasks[taskId] = task
  markEntity(context, task)
  return task
}

export function projectTask(
  context: ProjectionEventContext,
  id: string,
): TaskProjection {
  const existing = context.state.tasks[id]
  const base = baseEntity(context.event, ProjectionDomain.TASK, id, existing)
  const inline = projectionPayload(context.event, ProjectionDomain.TASK)
  const task: TaskProjection = {
    ...base,
    domain: ProjectionDomain.TASK,
    activeNodeIds: orderedUnion(
      existing?.activeNodeIds ?? [],
      uniqueStrings([
        context.event.identity.nodeId,
        ...stringArray(inline.node_ids),
        ...stringArray(inline.active_node_ids),
      ]),
    ),
    workerIds: orderedUnion(
      existing?.workerIds ?? [],
      uniqueStrings([
        context.event.identity.workerId,
        ...stringArray(inline.worker_ids),
      ]),
    ),
    sessionIds: orderedUnion(
      existing?.sessionIds ?? [],
      uniqueStrings([
        context.event.identity.sessionId,
        ...stringArray(inline.session_ids),
      ]),
    ),
    artifactIds: artifactIds(
      context.event.artifactRefs,
      existing?.artifactIds,
    ),
    pendingPermissionIds: orderedUnion(
      existing?.pendingPermissionIds ?? [],
      stringArray(inline.pending_permission_ids),
    ),
    commandIds: orderedUnion(
      existing?.commandIds ?? [],
      uniqueStrings([
        context.event.identity.controlCommandId,
        ...stringArray(inline.command_ids),
      ]),
    ),
    recoveryIds: orderedUnion(
      existing?.recoveryIds ?? [],
      uniqueStrings([
        recoveryId(context.event),
        ...stringArray(inline.recovery_ids),
      ]),
    ),
    lastCheckpointId:
      context.event.identity.checkpointId ??
      firstString(inline, "checkpoint_id", "checkpointId") ??
      existing?.lastCheckpointId,
  }
  context.state.tasks[id] = task
  markEntity(context, task)
  return task
}

export function projectNode(
  context: ProjectionEventContext,
  id: string,
): NodeProjection {
  const existing = context.state.nodes[id]
  const base = baseEntity(context.event, ProjectionDomain.NODE, id, existing)
  const inline = projectionPayload(context.event, ProjectionDomain.NODE)
  const node: NodeProjection = {
    ...base,
    domain: ProjectionDomain.NODE,
    role:
      firstString(inline, "role", "node_role", "nodeRole") ??
      firstString(context.event.metadata, "role", "node_role", "nodeRole") ??
      existing?.role,
    capabilityRefs: orderedUnion(
      existing?.capabilityRefs ?? [],
      stringArray(
        inline.capability_refs ??
          inline.capabilityRefs ??
          context.event.metadata.capability_refs,
      ),
    ),
    dependencyIds: orderedUnion(
      existing?.dependencyIds ?? [],
      stringArray(
        inline.dependency_ids ??
          inline.dependencies ??
          inline.predecessor_ids,
      ),
    ),
    childNodeIds: orderedUnion(
      existing?.childNodeIds ?? [],
      stringArray(inline.child_node_ids ?? inline.childNodeIds),
    ),
    placementId:
      firstString(inline, "placement_id", "placementId") ??
      existing?.placementId,
    routeId:
      firstString(inline, "route_id", "routeId") ?? existing?.routeId,
  }
  context.state.nodes[id] = node
  markEntity(context, node)
  return node
}

export function projectWorker(
  context: ProjectionEventContext,
  id: string,
): WorkerProjection {
  const existing = context.state.workers[id]
  const base = baseEntity(context.event, ProjectionDomain.WORKER, id, existing)
  const inline = projectionPayload(context.event, ProjectionDomain.WORKER)
  const worker: WorkerProjection = {
    ...base,
    domain: ProjectionDomain.WORKER,
    role:
      firstString(inline, "role", "worker_role", "workerRole") ??
      existing?.role,
    capabilityRefs: orderedUnion(
      existing?.capabilityRefs ?? [],
      stringArray(inline.capability_refs ?? inline.capabilityRefs),
    ),
    leaseId:
      firstString(inline, "lease_id", "leaseId") ?? existing?.leaseId,
    routeId:
      firstString(inline, "route_id", "routeId") ?? existing?.routeId,
    placementId:
      firstString(inline, "placement_id", "placementId") ??
      existing?.placementId,
    activeToolCallId:
      context.event.identity.toolCallId ??
      firstString(inline, "active_tool_call_id", "activeToolCallId") ??
      existing?.activeToolCallId,
    health: firstString(inline, "health", "health_status") ?? existing?.health,
  }
  context.state.workers[id] = worker
  markEntity(context, worker)
  return worker
}

export function projectTool(
  context: ProjectionEventContext,
  id: string,
): ToolProjection {
  const existing = context.state.tools[id]
  const base = baseEntity(context.event, ProjectionDomain.TOOL, id, existing)
  const inline = projectionPayload(context.event, ProjectionDomain.TOOL)
  const resultRefs = context.event.artifactRefs.map((item) => item.artifactId)
  const duration =
    firstNumber(inline, "duration_ms", "durationMs", "elapsed_ms") ??
    existing?.durationMs
  const tool: ToolProjection = {
    ...base,
    domain: ProjectionDomain.TOOL,
    toolName:
      firstString(inline, "tool_name", "toolName", "name") ??
      firstString(context.event.metadata, "tool_name", "toolName") ??
      existing?.toolName,
    inputDigest:
      firstString(inline, "input_digest", "inputDigest", "args_digest") ??
      existing?.inputDigest,
    resultDigest:
      firstString(inline, "result_digest", "resultDigest", "output_digest") ??
      context.event.artifactRefs[0]?.digest ??
      existing?.resultDigest,
    resultArtifactIds: orderedUnion(
      existing?.resultArtifactIds ?? [],
      resultRefs,
    ),
    permissionId:
      firstString(inline, "permission_id", "permissionId", "request_id") ??
      existing?.permissionId,
    durationMs: duration,
    errorCode:
      firstString(inline, "error_code", "errorCode", "code") ??
      existing?.errorCode,
  }
  context.state.tools[id] = tool
  markEntity(context, tool)
  return tool
}

export function projectArtifacts(
  context: ProjectionEventContext,
  fallbackId?: string,
): ArtifactProjection[] {
  const refs =
    context.event.artifactRefs.length > 0
      ? context.event.artifactRefs
      : fallbackId
        ? [syntheticArtifactRef(context.event, fallbackId)]
        : []
  const result: ArtifactProjection[] = []
  for (const ref of refs) {
    const existing = context.state.artifacts[ref.artifactId]
    const base = baseEntity(
      context.event,
      ProjectionDomain.ARTIFACT,
      ref.artifactId,
      existing,
    )
    const version =
      firstNumber(context.event.inline, "artifact_version", "version") ??
      (existing?.version ?? 0) + 1
    const artifact: ArtifactProjection = {
      ...base,
      title: ref.title || eventTitle(context.event, existing?.title ?? ref.artifactId),
      domain: ProjectionDomain.ARTIFACT,
      digest: ref.digest || existing?.digest || context.event.contentDigest,
      mediaType: ref.mediaType || existing?.mediaType || "application/octet-stream",
      sizeBytes: ref.sizeBytes || existing?.sizeBytes || 0,
      uri: ref.uri ?? existing?.uri,
      producerEventId: context.event.eventId,
      producerToolCallId:
        context.event.identity.toolCallId ?? existing?.producerToolCallId,
      version,
      deleted:
        Boolean(context.event.tombstoneTargetId) ||
        base.lifecycle === EntityLifecycle.DELETED,
    }
    context.state.artifacts[artifact.id] = artifact
    markEntity(context, artifact)
    result.push(artifact)
  }
  return result
}

function syntheticArtifactRef(
  event: IngressEvent,
  artifactId: string,
): IngressArtifactRef {
  return {
    artifactId,
    digest:
      firstString(event.inline, "digest", "artifact_digest") ??
      event.contentDigest,
    mediaType:
      firstString(event.inline, "media_type", "mediaType") ??
      "application/octet-stream",
    sizeBytes: firstNumber(event.inline, "size_bytes", "sizeBytes") ?? 0,
    title: eventTitle(event, artifactId),
    uri: firstString(event.inline, "uri", "url"),
  }
}

export function projectMemory(
  context: ProjectionEventContext,
  id: string,
): MemoryProjection {
  const existing = context.state.memories[id]
  const base = baseEntity(context.event, ProjectionDomain.MEMORY, id, existing)
  const inline = projectionPayload(context.event, ProjectionDomain.MEMORY)
  const memory: MemoryProjection = {
    ...base,
    domain: ProjectionDomain.MEMORY,
    memoryKind:
      firstString(inline, "memory_kind", "memoryKind", "kind", "type") ??
      existing?.memoryKind,
    namespace:
      firstString(inline, "namespace", "memory_namespace") ??
      existing?.namespace,
    key: firstString(inline, "key", "memory_key") ?? existing?.key,
    score: firstNumber(inline, "score", "relevance", "confidence") ?? existing?.score,
    tokenCount:
      firstNumber(inline, "token_count", "tokenCount", "tokens") ??
      existing?.tokenCount,
    sourceArtifactIds: orderedUnion(
      existing?.sourceArtifactIds ?? [],
      uniqueStrings([
        ...context.event.artifactRefs.map((item) => item.artifactId),
        ...stringArray(inline.source_artifact_ids),
      ]),
    ),
  }
  context.state.memories[id] = memory
  markEntity(context, memory)
  return memory
}

export function projectScheduler(
  context: ProjectionEventContext,
  id: string,
): SchedulerProjection {
  const existing = context.state.schedulers[id]
  const base = baseEntity(
    context.event,
    ProjectionDomain.SCHEDULER,
    id,
    existing,
  )
  const inline = projectionPayload(context.event, ProjectionDomain.SCHEDULER)
  const scheduler: SchedulerProjection = {
    ...base,
    domain: ProjectionDomain.SCHEDULER,
    routeId:
      firstString(inline, "route_id", "routeId") ?? existing?.routeId,
    placementId:
      firstString(inline, "placement_id", "placementId") ??
      existing?.placementId,
    modelId:
      firstString(inline, "model_id", "modelId", "model") ?? existing?.modelId,
    providerId:
      firstString(inline, "provider_id", "providerId", "provider") ??
      existing?.providerId,
    resourceClass:
      firstString(inline, "resource_class", "resourceClass") ??
      existing?.resourceClass,
    privacyClass:
      firstString(inline, "privacy_class", "privacyClass") ??
      existing?.privacyClass,
    costEstimate:
      firstNumber(inline, "cost_estimate", "costEstimate", "estimated_cost") ??
      existing?.costEstimate,
    latencyEstimateMs:
      firstNumber(
        inline,
        "latency_estimate_ms",
        "latencyEstimateMs",
        "estimated_latency_ms",
      ) ?? existing?.latencyEstimateMs,
    candidateIds: orderedUnion(
      existing?.candidateIds ?? [],
      stringArray(inline.candidate_ids ?? inline.candidates),
    ),
  }
  context.state.schedulers[id] = scheduler
  markEntity(context, scheduler)
  return scheduler
}

export function projectRecovery(
  context: ProjectionEventContext,
  id: string,
): RecoveryProjection {
  const existing = context.state.recoveries[id]
  const base = baseEntity(
    context.event,
    ProjectionDomain.RECOVERY,
    id,
    existing,
  )
  const inline = projectionPayload(context.event, ProjectionDomain.RECOVERY)
  const recovery: RecoveryProjection = {
    ...base,
    domain: ProjectionDomain.RECOVERY,
    failureId: failureId(context.event) ?? existing?.failureId,
    planId:
      firstString(inline, "plan_id", "planId", "recovery_plan_id") ??
      existing?.planId,
    attempt:
      firstNumber(inline, "attempt", "attempt_number", "retry_count") ??
      (existing?.attempt ?? 0) +
        (/retry|attempt/.test(context.event.eventType.toLowerCase()) ? 1 : 0),
    strategy:
      firstString(inline, "strategy", "recovery_strategy") ??
      existing?.strategy,
    previousWorkerId:
      firstString(inline, "previous_worker_id", "previousWorkerId") ??
      existing?.previousWorkerId,
    replacementWorkerId:
      firstString(inline, "replacement_worker_id", "replacementWorkerId") ??
      existing?.replacementWorkerId,
    resumedCheckpointId:
      firstString(inline, "resumed_checkpoint_id", "resumedCheckpointId") ??
      context.event.identity.checkpointId ??
      existing?.resumedCheckpointId,
    reason:
      firstString(inline, "reason", "failure_reason", "recovery_reason") ??
      existing?.reason,
  }
  context.state.recoveries[id] = recovery
  markEntity(context, recovery)
  return recovery
}

export function projectCommand(
  context: ProjectionEventContext,
  id: string,
): CommandProjection {
  const existing = context.state.commands[id]
  const base = baseEntity(context.event, ProjectionDomain.COMMAND, id, existing)
  const inline = projectionPayload(context.event, ProjectionDomain.COMMAND)
  const priority = normalizePriority(
    firstString(inline, "priority", "queue_priority") ?? existing?.priority,
  )
  const command: CommandProjection = {
    ...base,
    domain: ProjectionDomain.COMMAND,
    commandName:
      firstString(inline, "command_name", "commandName", "name", "command") ??
      existing?.commandName,
    mode: firstString(inline, "mode", "command_mode") ?? existing?.mode,
    scope: firstString(inline, "scope", "command_scope") ?? existing?.scope,
    priority,
    queuePosition:
      firstNumber(inline, "queue_position", "queuePosition", "position") ??
      existing?.queuePosition,
    argsDigest:
      firstString(inline, "args_digest", "argsDigest", "input_digest") ??
      existing?.argsDigest,
    receiptId:
      firstString(inline, "receipt_id", "receiptId") ?? existing?.receiptId,
    errorCode:
      firstString(inline, "error_code", "errorCode", "code") ??
      existing?.errorCode,
  }
  context.state.commands[id] = command
  reorderCommandQueue(context.state, command.taskId)
  markEntity(context, command)
  return command
}

function normalizePriority(
  value: string | undefined,
): "now" | "next" | "later" {
  const normalized = String(value ?? "next").trim().toLowerCase()
  if (normalized === "now" || normalized === "urgent" || normalized === "high") {
    return "now"
  }
  if (normalized === "later" || normalized === "low" || normalized === "background") {
    return "later"
  }
  return "next"
}

function reorderCommandQueue(state: MutableProjectionState, taskId: string): void {
  const weight = { now: 0, next: 1, later: 2 } as const
  const queued = Object.values(state.commands)
    .filter((item) => item.taskId === taskId)
    .filter((item) =>
      ([
        EntityLifecycle.QUEUED,
        EntityLifecycle.ADMITTED,
        EntityLifecycle.UNKNOWN,
      ] as readonly string[]).includes(item.lifecycle),
    )
    .sort(
      (left, right) =>
        weight[left.priority] - weight[right.priority] ||
        left.sequence - right.sequence ||
        left.id.localeCompare(right.id),
    )
  queued.forEach((item, index) => {
    if (item.queuePosition === index) return
    state.commands[item.id] = { ...item, queuePosition: index }
  })
}

export function projectPermission(
  context: ProjectionEventContext,
  id: string,
): PermissionProjection {
  const existing = context.state.permissions[id]
  const base = baseEntity(
    context.event,
    ProjectionDomain.PERMISSION,
    id,
    existing,
  )
  const inline = projectionPayload(context.event, ProjectionDomain.PERMISSION)
  const decision = normalizeDecision(
    firstString(inline, "decision", "response", "result"),
  )
  const resolved =
    decision === "allow" ||
    decision === "deny" ||
    ([
      EntityLifecycle.COMPLETED,
      EntityLifecycle.REJECTED,
      EntityLifecycle.EXPIRED,
      EntityLifecycle.CANCELLED,
    ] as readonly string[]).includes(base.lifecycle)
  const permission: PermissionProjection = {
    ...base,
    lifecycle:
      base.lifecycle === EntityLifecycle.UNKNOWN
        ? resolved
          ? decision === "deny"
            ? EntityLifecycle.REJECTED
            : EntityLifecycle.COMPLETED
          : EntityLifecycle.WAITING_POLICY
        : base.lifecycle,
    domain: ProjectionDomain.PERMISSION,
    requestId:
      firstString(inline, "request_id", "requestId", "permission_id") ?? id,
    permissionKind:
      firstString(inline, "permission_kind", "permissionKind", "kind", "type") ??
      existing?.permissionKind,
    toolName:
      firstString(inline, "tool_name", "toolName", "tool") ??
      existing?.toolName,
    argsDigest:
      firstString(inline, "args_digest", "argsDigest", "input_digest") ??
      existing?.argsDigest,
    policyRevision:
      firstString(inline, "policy_revision", "policyRevision") ??
      existing?.policyRevision,
    ownerId:
      firstString(inline, "owner_id", "ownerId", "approver_id") ??
      existing?.ownerId,
    decision: decision ?? existing?.decision,
    expiresAt:
      firstString(inline, "expires_at", "expiresAt") ?? existing?.expiresAt,
    resolvedAt: resolved ? context.event.committedAt : existing?.resolvedAt,
    reason:
      firstString(inline, "reason", "decision_reason") ?? existing?.reason,
  }
  context.state.permissions[id] = permission
  markEntity(context, permission)
  return permission
}

function normalizeDecision(
  value: string | undefined,
): "allow" | "deny" | "ask" | undefined {
  const normalized = String(value ?? "").trim().toLowerCase()
  if (["allow", "allowed", "approve", "approved", "accept", "yes"].includes(normalized)) {
    return "allow"
  }
  if (["deny", "denied", "reject", "rejected", "no"].includes(normalized)) {
    return "deny"
  }
  if (["ask", "pending", "requested"].includes(normalized)) return "ask"
  return undefined
}

export function projectOverlay(
  context: ProjectionEventContext,
  id: string,
): OverlayProjection {
  const existing = context.state.overlays[id]
  const base = baseEntity(context.event, ProjectionDomain.OVERLAY, id, existing)
  const inline = projectionPayload(context.event, ProjectionDomain.OVERLAY)
  const closed =
    firstBoolean(inline, false, "closed", "dismissed") ||
    ([
      EntityLifecycle.COMPLETED,
      EntityLifecycle.CANCELLED,
      EntityLifecycle.DELETED,
      EntityLifecycle.REJECTED,
    ] as readonly string[]).includes(base.lifecycle)
  const kind =
    firstString(inline, "overlay_kind", "overlayKind", "kind", "type") ??
    existing?.overlayKind
  const overlay: OverlayProjection = {
    ...base,
    domain: ProjectionDomain.OVERLAY,
    overlayKind: kind,
    modal:
      firstValueModal(inline, kind) ??
      existing?.modal ??
      kind !== "autocomplete",
    commandId:
      firstString(inline, "command_id", "commandId") ??
      context.event.identity.controlCommandId ??
      existing?.commandId,
    permissionId:
      firstString(inline, "permission_id", "permissionId", "request_id") ??
      existing?.permissionId,
    openedAt: existing?.openedAt ?? context.event.committedAt,
    closedAt: closed ? context.event.committedAt : existing?.closedAt,
  }
  context.state.overlays[id] = overlay
  markEntity(context, overlay)
  return overlay
}

function firstValueModal(inline: Readonly<JsonObject>, kind?: string): boolean | undefined {
  if (Object.prototype.hasOwnProperty.call(inline, "modal")) {
    return booleanValue(inline.modal)
  }
  if (kind) return kind !== "autocomplete" && kind !== "suggestion"
  return undefined
}

export function projectSession(
  context: ProjectionEventContext,
  id: string,
): SessionProjection {
  const existing = context.state.sessions[id]
  const base = baseEntity(context.event, ProjectionDomain.SESSION, id, existing)
  const inline = projectionPayload(context.event, ProjectionDomain.SESSION)
  const parent =
    firstString(inline, "parent_session_id", "parentSessionId") ??
    existing?.parentSessionId
  const compactIncrement = /compact/.test(context.event.eventType.toLowerCase())
    ? 1
    : 0
  const session: SessionProjection = {
    ...base,
    domain: ProjectionDomain.SESSION,
    parentSessionId: parent,
    contextTokens:
      firstNumber(inline, "context_tokens", "contextTokens", "token_count") ??
      existing?.contextTokens,
    contextLimit:
      firstNumber(inline, "context_limit", "contextLimit", "token_limit") ??
      existing?.contextLimit,
    compactCount:
      firstNumber(inline, "compact_count", "compactCount") ??
      (existing?.compactCount ?? 0) + compactIncrement,
    restoredCheckpointId:
      firstString(
        inline,
        "restored_checkpoint_id",
        "restoredCheckpointId",
        "checkpoint_id",
      ) ??
      context.event.identity.checkpointId ??
      existing?.restoredCheckpointId,
    childSessionIds: orderedUnion(
      existing?.childSessionIds ?? [],
      stringArray(inline.child_session_ids ?? inline.childSessionIds),
    ),
  }
  context.state.sessions[id] = session
  if (parent) {
    const parentSession = context.state.sessions[parent]
    if (parentSession) {
      context.state.sessions[parent] = {
        ...parentSession,
        childSessionIds: orderedUnion(parentSession.childSessionIds, [id]),
      }
      markEntity(context, context.state.sessions[parent]!)
    }
  }
  markEntity(context, session)
  return session
}

function linkCrossDomainRelationships(
  context: ProjectionEventContext,
  domain: ProjectionDomainValue,
  ids: readonly string[],
): void {
  const event = context.event
  const task = context.state.tasks[event.identity.taskId]
  if (!task) return
  let next = task
  if (
    event.identity.nodeId &&
    !next.activeNodeIds.includes(event.identity.nodeId)
  ) {
    next = {
      ...next,
      activeNodeIds: orderedUnion(next.activeNodeIds, [event.identity.nodeId]),
    }
  }
  if (
    event.identity.workerId &&
    !next.workerIds.includes(event.identity.workerId)
  ) {
    next = {
      ...next,
      workerIds: orderedUnion(next.workerIds, [event.identity.workerId]),
    }
  }
  if (
    event.identity.sessionId &&
    !next.sessionIds.includes(event.identity.sessionId)
  ) {
    next = {
      ...next,
      sessionIds: orderedUnion(next.sessionIds, [event.identity.sessionId]),
    }
  }
  if (
    event.identity.checkpointId &&
    event.identity.checkpointId !== next.lastCheckpointId
  ) {
    next = { ...next, lastCheckpointId: event.identity.checkpointId }
  }
  if (domain === ProjectionDomain.ARTIFACT) {
    const additions = ids.filter((id) => !next.artifactIds.includes(id))
    if (additions.length > 0) {
      next = {
        ...next,
        artifactIds: orderedUnion(next.artifactIds, additions),
      }
    }
  }
  if (domain === ProjectionDomain.COMMAND) {
    const additions = ids.filter((id) => !next.commandIds.includes(id))
    if (additions.length > 0) {
      next = { ...next, commandIds: orderedUnion(next.commandIds, additions) }
    }
  }
  if (domain === ProjectionDomain.RECOVERY) {
    const additions = ids.filter((id) => !next.recoveryIds.includes(id))
    if (additions.length > 0) {
      next = {
        ...next,
        recoveryIds: orderedUnion(next.recoveryIds, additions),
      }
    }
  }
  if (domain === ProjectionDomain.PERMISSION) {
    const pending = ids.filter((id) => {
      const permission = context.state.permissions[id]
      return permission && !permission.resolvedAt
    })
    const resolved = ids.filter((id) => !pending.includes(id))
    const permissionIds = orderedUnion(
      next.pendingPermissionIds.filter((id) => !resolved.includes(id)),
      pending,
    )
    if (!sameStrings(permissionIds, next.pendingPermissionIds)) {
      next = { ...next, pendingPermissionIds: permissionIds }
    }
  }
  if (next !== task) {
    const touched: TaskProjection = {
      ...next,
      revision: task.revision + 1,
      sequence: Math.max(task.sequence, event.globalSequence),
      aggregateSequence: Math.max(
        task.aggregateSequence,
        event.aggregateSequence,
      ),
      updatedAt: event.committedAt,
      lastEventId: event.eventId,
      correlationId: event.correlationId,
      causationId: event.causationId ?? task.causationId,
      effective: event.effective || task.effective,
    }
    context.state.tasks[task.id] = touched
    markEntity(context, touched)
  }
}

function sameStrings(
  left: readonly string[],
  right: readonly string[],
): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  )
}

function reconcileTaskFromNodes(context: ProjectionEventContext): void {
  const taskId = context.event.identity.taskId
  const task = context.state.tasks[taskId]
  if (!task || task.activeNodeIds.length === 0) return
  const nodes = task.activeNodeIds.map((id) => context.state.nodes[id])
  if (
    nodes.some((node) => !node) ||
    nodes.some((node) => !lifecycleIsTerminal(node!.lifecycle))
  ) {
    return
  }
  const lifecycles = new Set(nodes.map((node) => node!.lifecycle))
  const lifecycle = aggregateTerminalLifecycle(lifecycles)
  if (task.terminal && task.lifecycle === lifecycle) return
  const event = context.event
  const reconciled: TaskProjection = {
    ...task,
    lifecycle,
    status: ProjectionStatus.FINAL,
    revision: task.revision + 1,
    sequence: Math.max(task.sequence, event.globalSequence),
    aggregateSequence: Math.max(
      task.aggregateSequence,
      event.aggregateSequence,
    ),
    updatedAt: event.committedAt,
    lastEventId: event.eventId,
    correlationId: event.correlationId,
    causationId: event.causationId ?? task.causationId,
    summary: event.summary || task.summary,
    progress: lifecycle === EntityLifecycle.COMPLETED ? 1 : task.progress,
    terminal: true,
    effective: true,
  }
  context.state.tasks[taskId] = reconciled
  markEntity(context, reconciled)
}

function aggregateTerminalLifecycle(
  lifecycles: ReadonlySet<string>,
): TaskProjection["lifecycle"] {
  if (lifecycles.has(EntityLifecycle.FAILED)) return EntityLifecycle.FAILED
  if (lifecycles.has(EntityLifecycle.CANCELLED)) {
    return EntityLifecycle.CANCELLED
  }
  if (lifecycles.has(EntityLifecycle.REJECTED)) {
    return EntityLifecycle.REJECTED
  }
  if (lifecycles.has(EntityLifecycle.EXPIRED)) return EntityLifecycle.EXPIRED
  if (lifecycles.has(EntityLifecycle.DELETED)) return EntityLifecycle.DELETED
  return EntityLifecycle.COMPLETED
}

export function removeProjectedEntity(
  context: ProjectionEventContext,
  domain: ProjectionDomainValue,
  id: string,
): DomainProjection | undefined {
  const table = projectionTable(context.state, domain)
  const entity = table[id] as DomainProjection | undefined
  if (!entity) return undefined
  const tombstoned = {
    ...entity,
    lifecycle: EntityLifecycle.DELETED,
    status: ProjectionStatus.TOMBSTONED,
    revision: entity.revision + 1,
    sequence: Math.max(entity.sequence, context.event.globalSequence),
    aggregateSequence: Math.max(
      entity.aggregateSequence,
      context.event.aggregateSequence,
    ),
    updatedAt: context.event.committedAt,
    lastEventId: context.event.eventId,
    terminal: true,
    effective: context.event.effective,
  } as DomainProjection
  ;(table as Record<string, DomainProjection>)[id] = tombstoned
  unlinkEntity(context, tombstoned)
  markEntity(context, tombstoned)
  return tombstoned
}

function unlinkEntity(
  context: ProjectionEventContext,
  entity: DomainProjection,
): void {
  const task = context.state.tasks[entity.taskId]
  if (!task) return
  let next = task
  switch (entity.domain) {
    case ProjectionDomain.NODE:
      next = {
        ...next,
        activeNodeIds: next.activeNodeIds.filter((id) => id !== entity.id),
      }
      break
    case ProjectionDomain.WORKER:
      next = {
        ...next,
        workerIds: next.workerIds.filter((id) => id !== entity.id),
      }
      break
    case ProjectionDomain.ARTIFACT:
      next = {
        ...next,
        artifactIds: next.artifactIds.filter((id) => id !== entity.id),
      }
      break
    case ProjectionDomain.PERMISSION:
      next = {
        ...next,
        pendingPermissionIds: next.pendingPermissionIds.filter(
          (id) => id !== entity.id,
        ),
      }
      break
    case ProjectionDomain.COMMAND:
      next = {
        ...next,
        commandIds: next.commandIds.filter((id) => id !== entity.id),
      }
      break
    case ProjectionDomain.RECOVERY:
      next = {
        ...next,
        recoveryIds: next.recoveryIds.filter((id) => id !== entity.id),
      }
      break
    case ProjectionDomain.SESSION:
      next = {
        ...next,
        sessionIds: next.sessionIds.filter((id) => id !== entity.id),
      }
      break
  }
  if (next !== task) {
    context.state.tasks[task.id] = next
    markEntity(context, next)
  }
}

export function projectionTable(
  state: MutableProjectionState,
  domain: ProjectionDomainValue,
): Record<string, ProjectionEntity> {
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
    case ProjectionDomain.EVENT:
      return {}
  }
}

export function markEntity(
  context: ProjectionEventContext,
  entity: ProjectionEntity,
): void {
  context.changes.taskIds.add(entity.taskId)
  context.changes.domains.add(entity.domain)
  context.changes.entityKeys.add(entityKey(entity.domain, entity.id))
}

export function domainFromTombstone(event: IngressEvent): ProjectionDomainValue {
  const declared =
    firstString(event.inline, "target_domain", "targetDomain", "domain") ??
    firstString(event.metadata, "target_domain", "targetDomain", "domain")
  if (declared) {
    const normalized = declared.toLowerCase()
    for (const domain of Object.values(ProjectionDomain)) {
      if (normalized === domain) return domain
    }
  }
  return inferDomain(event)
}
