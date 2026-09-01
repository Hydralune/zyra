import type { TaskProjection } from "@zyra/typed-api-client"
import type { IngressFrame } from "../api.ts"
import {
  ZYRA_UI_EVENT_SCHEMA,
  type UiFileChange,
  type UiPlanChange,
  type UiPlanSnapshot,
  type UiPlanStepStatus,
  type UiPermissionRequest,
  type UiPermissionSnapshot,
  type UiToolOutputRef,
  type UiTransportSnapshot,
  type UiVerificationCheck,
  type ZyraUiEvent,
} from "./events.ts"

export interface ProductProjectionInput {
  task: TaskProjection
  frames?: readonly IngressFrame[]
  permissions?: readonly UiPermissionSnapshot[]
  transport?: UiTransportSnapshot
}

function object(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>>
    : {}
}

function text(value: unknown, maximum = 2_000): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim().slice(0, maximum) : undefined
}

function content(value: unknown, maximum = 1_000_000): string | undefined {
  return typeof value === "string" && value.trim() ? value.slice(0, maximum) : undefined
}

function productText(value: unknown, maximum: number): string | undefined {
  return text(value, maximum)?.replaceAll("runtime.", "runtime")
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.flatMap((item) => typeof item === "string" && item.trim() ? [item.trim().slice(0, 256)] : [])
    : []
}

function toolOutputRefs(value: unknown): readonly UiToolOutputRef[] | undefined {
  if (!Array.isArray(value)) return undefined
  const output: UiToolOutputRef[] = []
  const seen = new Set<string>()
  for (const item of value.slice(0, 16)) {
    const selected = object(item)
    const artifactId = text(selected.artifactId, 256)
    const stream = selected.stream === "stdout" || selected.stream === "stderr" ? selected.stream : undefined
    if (!artifactId || !stream || seen.has(artifactId)) continue
    seen.add(artifactId)
    output.push(Object.freeze({
      artifactId,
      stream,
      title: text(selected.title, 256) ?? `Tool ${stream}`,
      mediaType: text(selected.mediaType, 128) ?? "application/octet-stream",
      sizeBytes: artifactSize(selected.sizeBytes),
    }))
  }
  return Object.freeze(output)
}

function displayPath(value: string): string {
  const normalized = value.replaceAll("\\", "/").replace(/^[A-Za-z]:\//, "").replace(/^\/+/, "")
  const parts = normalized.split("/").filter((part) => part && part !== "." && part !== "..")
  return parts.join("/") || "<workspace>"
}

function occurredAt(frame: IngressFrame): string | undefined {
  return text(frame.event.createdAt) ?? text(frame.event.committedAt)
}

function severity(value: unknown): "info" | "warning" | "error" {
  return value === "warning" || value === "error" ? value : "info"
}

function failureImpact(value: unknown, fallback: "local" | "task"): "local" | "task" {
  return value === "local" || value === "task" ? value : fallback
}

function safeInteger(value: unknown): number | undefined {
  return Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : undefined
}

function artifactSize(value: unknown): number | undefined {
  const selected = safeInteger(value)
  return selected !== undefined && selected <= 1_000_000_000_000 ? selected : undefined
}

function planStepStatus(value: string): UiPlanStepStatus {
  if (["running", "active", "executing", "in_progress"].includes(value)) return "running"
  if (["completed", "succeeded", "success"].includes(value)) return "completed"
  if (["failed", "error"].includes(value)) return "failed"
  if (["cancelled", "canceled", "killed"].includes(value)) return "cancelled"
  if (["superseded", "replaced"].includes(value)) return "superseded"
  return "pending"
}

function planChanges(task: TaskProjection): readonly UiPlanChange[] {
  const changes: UiPlanChange[] = []
  const append = (value: unknown, kind: UiPlanChange["kind"], index: number) => {
    const item = object(value)
    const eventId = text(item.event_id ?? item.eventId, 256)
    const changeNodeId = text(item.replan_node_id ?? item.recovery_node_id, 256)
    const changeId = eventId ?? changeNodeId ?? `${kind}:${index}`
    const affected = stringList(item.affected_node_ids)
    for (const candidate of [item.target_node_id, item.replan_node_id, item.recovery_node_id]) {
      const nodeId = text(candidate, 256)
      if (nodeId && !affected.includes(nodeId)) affected.push(nodeId)
    }
    changes.push(Object.freeze({
      changeId,
      kind,
      summary: text(item.text, 2_000) ?? (kind === "requirement_change" ? "需求变化触发计划修订" : "局部失败触发恢复计划"),
      affectedStepIds: Object.freeze(affected.slice(0, 64)),
      createdAt: text(item.created_at ?? item.createdAt, 128),
    }))
  }
  const requirements = task.metadata.requirement_changes
  if (Array.isArray(requirements)) requirements.slice(-256).forEach((item, index) => append(item, "requirement_change", index))
  const failures = task.metadata.failure_injections
  if (Array.isArray(failures)) failures.slice(-256).forEach((item, index) => append(item, "failure_recovery", index))
  changes.sort((left, right) => (left.createdAt ?? "").localeCompare(right.createdAt ?? "") || left.changeId.localeCompare(right.changeId))
  return Object.freeze(changes.slice(-512))
}

function planSnapshot(task: TaskProjection): UiPlanSnapshot {
  const graph = object(task.metadata.dynamic_graph_ref)
  const canonicalRevision = safeInteger(graph.revision)
  const stageOrder = stringList(task.metadata.stage_order)
  const order = new Map(stageOrder.map((nodeId, index) => [nodeId, index]))
  const steps = task.planNodes
    .filter((node) => node.nodeId !== task.rootNodeId)
    .sort((left, right) => {
      const leftOrder = order.get(left.nodeId) ?? Number.MAX_SAFE_INTEGER
      const rightOrder = order.get(right.nodeId) ?? Number.MAX_SAFE_INTEGER
      return leftOrder - rightOrder || left.nodeId.localeCompare(right.nodeId)
    })
    .slice(0, 2_000)
    .map((node) => Object.freeze({
      stepId: node.nodeId,
      label: productText(node.title, 256) ?? productText(node.description, 256) ?? "执行任务",
      description: productText(node.description, 2_000),
      status: planStepStatus(node.status),
      dependsOn: Object.freeze(node.dependsOn.slice(0, 64)),
      assignedAgentId: text(node.assignedWorkerId, 256),
    }))
  return Object.freeze({
    schema: "zyra.ui-plan/v1",
    revision: canonicalRevision && canonicalRevision > 0 ? canonicalRevision : 1,
    revisionSource: canonicalRevision && canonicalRevision > 0 ? "canonical_graph" : "compatibility",
    graphId: text(graph.graph_id ?? graph.graphId, 256),
    steps: Object.freeze(steps),
    changes: planChanges(task),
  })
}

function productPresentationEvents(frame: IngressFrame): ZyraUiEvent[] | undefined {
  const presentation = object(frame.presentation)
  if (presentation.schema !== "zyra.product-presentation/v1") return undefined
  const kind = text(presentation.kind, 64)
  const phase = text(presentation.phase, 64) ?? "updated"
  const identity = text(presentation.identity, 256)
  const label = text(presentation.label, 256)
  if (!kind || !identity || !label) return []
  const at = occurredAt(frame)
  const summary = text(presentation.summary, 2_000)
  const base = { schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at } as const
  if (kind === "activity") {
    const fields = {
      ...base,
      activityId: `activity:${identity}`,
      label,
      category: text(presentation.category),
      summary,
      severity: severity(presentation.severity),
      impact: presentation.impact === undefined ? undefined : failureImpact(presentation.impact, "local"),
    }
    if (["completed", "failed", "cancelled", "replaced"].includes(phase)) {
      return [{ ...fields, type: "activity.completed", outcome: phase }]
    }
    if (["started", "running", "dispatched"].includes(phase)) return [{ ...fields, type: "activity.started" }]
    return [{ ...fields, type: "activity.updated" }]
  }
  if (kind === "worker") {
    const impact = presentation.impact === undefined ? undefined : failureImpact(presentation.impact, "local")
    const code = text(presentation.code)
    const recovery = text(presentation.recovery)
    return [{
      ...base,
      type: "subagent.updated",
      agentId: identity,
      label,
      status: phase,
      summary,
      ...(impact === undefined ? {} : { impact }),
      ...(code === undefined ? {} : { code }),
      ...(presentation.retryable === true ? { retryable: true } : {}),
      ...(recovery === undefined ? {} : { recovery }),
    }]
  }
  if (kind === "tool") {
    const durationMs = safeInteger(presentation.durationMs)
    const artifactIds = Object.freeze(stringList(presentation.artifactIds).slice(0, 32))
    const outputRefs = toolOutputRefs(presentation.outputArtifacts)
    if (phase === "started") {
      return [{ ...base, type: "tool.started", toolCallId: identity, name: label, summary: summary ?? `正在运行 ${label}`, durationMs, artifactIds, outputRefs }]
    }
    if (phase === "failed" || phase === "cancelled") {
      const code = text(presentation.code)
      const recovery = text(presentation.recovery)
      return [{
        ...base,
        type: "tool.failed",
        toolCallId: identity,
        name: label,
        message: summary ?? `${label}${phase === "failed" ? "执行失败" : "已取消"}`,
        durationMs,
        artifactIds,
        outputRefs,
        impact: failureImpact(presentation.impact, "local"),
        ...(code === undefined ? {} : { code }),
        ...(presentation.retryable === true ? { retryable: true } : {}),
        ...(recovery === undefined ? {} : { recovery }),
      }]
    }
    if (phase === "completed") {
      return [{ ...base, type: "tool.completed", toolCallId: identity, name: label, summary: summary ?? `${label} 已完成`, durationMs, artifactIds, outputRefs }]
    }
    return [{ ...base, type: "tool.updated", toolCallId: identity, name: label, summary: summary ?? `${label} 正在运行`, durationMs, artifactIds, outputRefs }]
  }
  if (kind === "issue") {
    return [{
      ...base,
      type: "task.issue",
      issueId: identity,
      severity: severity(presentation.severity),
      message: label,
      code: text(presentation.code),
      retryable: presentation.retryable === true,
      recovery: text(presentation.recovery),
      impact: failureImpact(presentation.impact, "task"),
    }]
  }
  return []
}

interface AssistantPresentation {
  phase: "started" | "delta" | "completed"
  messageId: string
  text?: string
}

function assistantPresentation(frame: IngressFrame): AssistantPresentation | undefined {
  const presentation = object(frame.presentation)
  if (presentation.schema !== "zyra.product-presentation/v1" || presentation.kind !== "assistant") return undefined
  const phase = presentation.phase === "started" || presentation.phase === "delta" || presentation.phase === "completed"
    ? presentation.phase
    : undefined
  const messageId = text(presentation.identity, 256)
  if (!phase || !messageId) return undefined
  return { phase, messageId: `message:assistant:${messageId}`, text: content(presentation.text) }
}

function sortedFrames(frames: readonly IngressFrame[], taskId: string): IngressFrame[] {
  const seen = new Set<string>()
  return [...frames]
    .filter((frame) => frame.taskId === taskId)
    .sort((left, right) => {
      const leftLive = left.liveSequence
      const rightLive = right.liveSequence
      if (leftLive === undefined && rightLive !== undefined) return -1
      if (leftLive !== undefined && rightLive === undefined) return 1
      if (leftLive !== undefined && rightLive !== undefined) return leftLive - rightLive
      return left.sequence - right.sequence || left.eventId.localeCompare(right.eventId)
    })
    .filter((frame) => {
      if (seen.has(frame.eventId)) return false
      seen.add(frame.eventId)
      return true
    })
}

function explicitAssistantText(frame: IngressFrame): string | undefined {
  const inline = object(frame.event.inline)
  const presentation = object(frame.event.presentation)
  if (presentation.role === "assistant") {
    const value = content(presentation.text) ?? content(presentation.delta)
    if (value) return value
  }
  return content(inline.presentation_text) ?? content(inline.text) ?? content(inline.delta)
}

function permissionId(inline: Readonly<Record<string, unknown>>, fallback: string): string {
  return text(inline.permission_id)
    ?? text(inline.request_id)
    ?? text(inline.requestId)
    ?? fallback
}

function permissionFromFrame(frame: IngressFrame): UiPermissionRequest {
  const inline = object(frame.event.inline)
  const action = text(inline.action)
    ?? text(inline.operation)
    ?? text(inline.tool_name)
    ?? "受保护操作"
  return Object.freeze({
    requestId: permissionId(inline, frame.eventId),
    action,
    target: text(inline.target),
    reason: text(inline.reason),
    risk: text(inline.risk) ?? text(inline.risk_level),
    expiresAt: text(inline.expires_at),
    scope: text(inline.scope),
    decisions: Object.freeze(["allow", "deny"] as const),
  })
}

function permissionFromSnapshot(snapshot: UiPermissionSnapshot): UiPermissionRequest {
  return Object.freeze({
    requestId: snapshot.requestId,
    action: text(snapshot.prompt)
      ?? text(snapshot.operation)
      ?? text(snapshot.toolName)
      ?? "受保护操作",
    target: text(snapshot.target),
    reason: text(snapshot.reason),
    risk: text(snapshot.risk),
    expiresAt: text(snapshot.expiresAt),
    scope: text(snapshot.scope),
    decisions: Object.freeze(["allow", "deny"] as const),
  })
}

function workspaceChanges(task: TaskProjection): readonly UiFileChange[] {
  const delivery = object(task.metadata.delivery)
  if (delivery.schema !== "zyra.task-workspace-delivery/v1") return Object.freeze([])
  const changes = new Map<string, UiFileChange>()
  const add = (kind: UiFileChange["kind"], values: unknown) => {
    for (const value of stringList(values)) {
      const path = displayPath(value)
      changes.set(path, Object.freeze({ path, kind }))
    }
  }
  add("modified", delivery.changed_paths)
  add("modified", delivery.modified_paths)
  add("created", delivery.created_paths)
  add("deleted", delivery.deleted_paths)
  return Object.freeze([...changes.values()].sort((left, right) => left.path.localeCompare(right.path)))
}

function taskFailure(task: TaskProjection): string {
  const outcome = object(task.metadata.canonical_task_outcome)
  const diagnostics = Array.isArray(outcome.diagnostics) ? outcome.diagnostics.map(object) : []
  return text(task.metadata.failure_reason)
    ?? text(task.metadata.error)
    ?? diagnostics.map((item) => text(item.message)).find(Boolean)
    ?? "任务未能完成。"
}

function verificationSummary(task: TaskProjection): {
  status: "passed" | "failed" | "not_run"
  label: string
  details: readonly string[]
  checks: readonly UiVerificationCheck[]
  commandEvidence: "recorded" | "not_recorded"
} | undefined {
  if (!task.terminal && !["completed", "failed", "blocked", "cancelled", "killed"].includes(task.status)) return undefined
  const outcome = object(task.metadata.canonical_task_outcome)
  const verification = object(outcome.verification)
  const finalVerifier = object(verification.final_verifier)
  const completionGate = object(verification.completion_gate)
  const failedConditions = stringList(completionGate.failed_conditions).slice(0, 128)
  const checks: UiVerificationCheck[] = []
  const addChecks = (value: unknown, source: UiVerificationCheck["source"]) => {
    if (!Array.isArray(value)) return
    for (const item of value.slice(0, 512)) {
      const selected = object(item)
      const name = text(selected.name, 256)
      const status = text(selected.status, 32)
      if (!name || !["passed", "failed", "skipped", "not_run"].includes(status ?? "")) continue
      checks.push(Object.freeze({
        source,
        name,
        status: status as UiVerificationCheck["status"],
        summary: text(selected.summary, 2_000),
      }))
    }
  }
  addChecks(finalVerifier.checks, "final_verifier")
  addChecks(object(verification.delivery_verifier).checks, "delivery_verifier")
  for (const name of failedConditions) checks.push(Object.freeze({ source: "completion_gate", name, status: "failed" }))
  const commandEvidence = object(verification.command_evidence)
  const receipts = Array.isArray(commandEvidence.receipts) ? commandEvidence.receipts.slice(0, 256) : []
  let admittedCommandReceipts = 0
  for (const item of receipts) {
    const receipt = object(item)
    const command = content(receipt.command, 4_096)
    const status = text(receipt.status, 32)
    if (!command || !["passed", "failed", "skipped", "not_run"].includes(status ?? "")) continue
    admittedCommandReceipts += 1
    checks.push(Object.freeze({
      source: "command",
      name: text(receipt.label, 256) ?? command.slice(0, 256),
      command,
      status: status as UiVerificationCheck["status"],
      summary: text(receipt.summary, 2_000),
      exitCode: Number.isSafeInteger(receipt.exit_code) ? Number(receipt.exit_code) : undefined,
    }))
  }
  const commandEvidenceStatus = admittedCommandReceipts > 0 ? "recorded" as const : "not_recorded" as const
  const failedDetails = [
    ...failedConditions,
    ...checks.filter((item) => item.status === "failed" && item.source !== "completion_gate").map((item) => `${item.source}: ${item.name}`),
  ].slice(0, 10)
  if (finalVerifier.passed === false || completionGate.hard_conditions_passed === false) {
    return Object.freeze({ status: "failed", label: "最终验证未通过", details: Object.freeze(failedDetails), checks: Object.freeze(checks), commandEvidence: commandEvidenceStatus })
  }
  if (finalVerifier.passed === true && completionGate.hard_conditions_passed === true) {
    return Object.freeze({ status: "passed", label: "最终验证通过", details: Object.freeze([]), checks: Object.freeze(checks), commandEvidence: commandEvidenceStatus })
  }
  return Object.freeze({ status: "not_run", label: "未记录最终验证", details: Object.freeze([]), checks: Object.freeze(checks), commandEvidence: commandEvidenceStatus })
}

export function projectProductEvents(input: ProductProjectionInput): readonly ZyraUiEvent[] {
  const { task } = input
  const events: ZyraUiEvent[] = []
  const push = (event: ZyraUiEvent) => events.push(Object.freeze(event))
  const sessionId = task.sessionId ?? `session:${task.taskId}`

  push({
    schema: ZYRA_UI_EVENT_SCHEMA,
    eventId: `ui:session:${sessionId}`,
    occurredAt: task.createdAt,
    type: "session.started",
    sessionId,
    taskId: task.taskId,
  })
  if (task.userGoal.trim()) {
    push({
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: `ui:user:${task.taskId}:goal`,
      occurredAt: task.createdAt,
      type: "user.message",
      messageId: `message:user:${task.taskId}:goal`,
      text: task.userGoal,
    })
  }

  if (task.planNodes.some((node) => node.nodeId !== task.rootNodeId)) {
    push({
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: `ui:plan:${task.taskId}:${task.updatedAt}`,
      occurredAt: task.updatedAt,
      type: "plan.updated",
      plan: planSnapshot(task),
    })
  }

  for (const node of [...task.planNodes].sort((left, right) => left.nodeId.localeCompare(right.nodeId))) {
    if (node.nodeId === task.rootNodeId) continue
    const label = node.title.trim() || node.description.trim() || "执行任务"
    const base = {
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: `ui:activity:${node.nodeId}:${node.status}`,
      occurredAt: node.updatedAt ?? task.updatedAt,
      activityId: `activity:${node.nodeId}`,
      label,
      category: "plan",
    } as const
    if (["completed", "succeeded", "cancelled", "failed"].includes(node.status)) {
      push({ ...base, type: "activity.completed", outcome: node.status })
    } else if (["running", "active", "executing", "in_progress"].includes(node.status)) {
      push({ ...base, type: "activity.started" })
    } else {
      push({ ...base, type: "activity.updated" })
    }
  }

  const permissionSnapshots = [...(input.permissions ?? [])]
    .sort((left, right) => left.requestId.localeCompare(right.requestId))
  const canonicalPermissionIds = new Set(permissionSnapshots.map((item) => item.requestId))
  const streamBuffers = new Map<string, string>()
  const completedAssistantTexts: string[] = []
  let lastCompletedAssistantMessageId: string | undefined

  for (const frame of sortedFrames(input.frames ?? [], task.taskId)) {
    const assistant = assistantPresentation(frame)
    if (assistant !== undefined) {
      const base = { schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: occurredAt(frame) } as const
      if (assistant.phase === "started") {
        streamBuffers.set(assistant.messageId, "")
        push({ ...base, type: "assistant.message.started", messageId: assistant.messageId })
      } else if (assistant.phase === "delta") {
        if (assistant.text) {
          streamBuffers.set(assistant.messageId, `${streamBuffers.get(assistant.messageId) ?? ""}${assistant.text}`)
          push({ ...base, type: "assistant.message.delta", messageId: assistant.messageId, text: assistant.text })
        }
      } else {
        const completed = assistant.text ?? streamBuffers.get(assistant.messageId)
        if (completed) {
          completedAssistantTexts.push(completed)
          lastCompletedAssistantMessageId = assistant.messageId
          push({ ...base, type: "assistant.message.completed", messageId: assistant.messageId, text: completed, source: "stream" })
        }
      }
      continue
    }
    const presentationEvents = productPresentationEvents(frame)
    if (presentationEvents !== undefined) {
      for (const event of presentationEvents) push(event)
      continue
    }
    const inline = object(frame.event.inline)
    const at = occurredAt(frame)
    const streamId = text(inline.stream_id) ?? `stream:${task.taskId}`
    const messageId = `message:assistant:${streamId}`
    const toolCallId = text(inline.tool_call_id)
      ?? text(object(frame.event.identity).toolCallId)
      ?? frame.eventId
    const toolName = text(inline.tool_name) ?? "工具"

    switch (frame.eventType) {
      case "runtime.text.started":
        streamBuffers.set(streamId, "")
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "assistant.message.started", messageId })
        break
      case "runtime.text.delta": { // Current physical runs expose only byte count/digest; absent text is intentionally ignored.
        const delta = explicitAssistantText(frame)
        if (!delta) break
        streamBuffers.set(streamId, `${streamBuffers.get(streamId) ?? ""}${delta}`)
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "assistant.message.delta", messageId, text: delta })
        break
      }
      case "runtime.text.ended": { // Never reconstruct text from a digest or summary.
        const completed = explicitAssistantText(frame) ?? streamBuffers.get(streamId)
        if (!completed) break
        completedAssistantTexts.push(completed)
        lastCompletedAssistantMessageId = messageId
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "assistant.message.completed", messageId, text: completed, source: "stream" })
        break
      }
      case "runtime.tool.called":
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "tool.started", toolCallId, name: toolName, summary: `正在运行 ${toolName}` })
        break
      case "runtime.tool.progress":
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "tool.updated", toolCallId, summary: typeof inline.percent === "number" ? `${toolName} ${Math.max(0, Math.min(100, inline.percent))}%` : `${toolName} 正在运行` })
        break
      case "runtime.tool.succeeded":
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "tool.completed", toolCallId, summary: `${toolName} 已完成` })
        break
      case "runtime.tool.failed":
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "tool.failed", toolCallId, message: `${toolName} 执行失败${text(inline.error_code) ? `（${text(inline.error_code)}）` : ""}`, impact: "local", code: text(inline.error_code) })
        break
      case "runtime.tool.cancelled":
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "tool.failed", toolCallId, message: `${toolName} 已取消`, impact: "local" })
        break
      case "runtime.permission.requested":
      case "runtime.permission.pending": { // Canonical permission custody snapshot wins when available.
        const request = permissionFromFrame(frame)
        if (!canonicalPermissionIds.has(request.requestId)) {
          push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "permission.requested", request })
        }
        break
      }
      case "runtime.permission.allowed":
      case "runtime.permission.denied": { // Internal grant-consumed events are deliberately not product decisions.
        const requestId = permissionId(inline, frame.eventId)
        if (!canonicalPermissionIds.has(requestId)) {
          push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "permission.resolved", requestId, decision: frame.eventType.endsWith("allowed") ? "allow" : "deny" })
        }
        break
      }
      case "runtime.subagent.created":
      case "runtime.subagent.dispatched":
      case "runtime.subagent.progress":
      case "runtime.subagent.completed":
      case "runtime.subagent.failed":
      case "runtime.subagent.cancelled": { // Product state receives a stable aggregate, never topology details.
        const agentId = text(inline.subagent_id) ?? frame.eventId
        const status = frame.eventType.split(".").at(-1) ?? "updated"
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "subagent.updated", agentId, label: "协作代理", status, impact: status === "failed" || status === "cancelled" ? "local" : undefined })
        break
      }
      default:
        // node/lease/route/audit/artifact/system-message noise is developer-only.
        break
    }
  }

  for (const permission of permissionSnapshots) {
    const status = permission.status.trim().toLowerCase()
    const terminal = ["resolved", "allowed", "denied", "cancelled", "expired", "aborted"].includes(status)
    if (terminal) {
      push({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `ui:permission:${permission.requestId}:${status}`,
        type: "permission.resolved",
        requestId: permission.requestId,
        decision: status,
      })
    } else if (permission.selectable !== false) {
      push({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `ui:permission:${permission.requestId}:requested`,
        type: "permission.requested",
        request: permissionFromSnapshot(permission),
      })
    }
  }

  const changes = workspaceChanges(task)
  if (changes.length) {
    push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:workspace:${task.taskId}:${task.updatedAt}`, occurredAt: task.updatedAt, type: "workspace.changed", changes })
  }


  const verification = verificationSummary(task)
  if (verification) {
    push({
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: `ui:verification:${task.taskId}:${task.updatedAt}`,
      occurredAt: task.updatedAt,
      type: "verification.updated",
      verification,
    })
  }

  const finalAnswer = content(task.metadata.final_answer)
  if (task.status === "completed") {
    if (finalAnswer && !completedAssistantTexts.includes(finalAnswer)) {
      push({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `ui:assistant:${task.taskId}:canonical-final`,
        occurredAt: task.updatedAt,
        type: "assistant.message.completed",
        messageId: lastCompletedAssistantMessageId ?? `message:assistant:${task.taskId}:final`,
        text: finalAnswer,
        source: "canonical_final_answer",
      })
    }
    push({
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: `ui:task:${task.taskId}:completed`,
      occurredAt: task.updatedAt,
      type: "task.completed",
      taskId: task.taskId,
      finalAnswer: finalAnswer ?? "",
    })
  } else if (task.status === "cancelled") {
    push({
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: `ui:task:${task.taskId}:cancelled`,
      occurredAt: task.updatedAt,
      type: "task.cancelled",
      taskId: task.taskId,
      message: "任务已取消。",
    })
  } else if (task.terminal || ["failed", "blocked", "killed"].includes(task.status)) {
    push({
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: `ui:task:${task.taskId}:failed`,
      occurredAt: task.updatedAt,
      type: "task.failed",
      taskId: task.taskId,
      status: task.status === "blocked" || task.status === "killed" ? task.status : "failed",
      message: taskFailure(task),
      recovery: `可运行 zyra resume ${task.taskId} 查看可恢复状态。`,
    })
  }

  if (input.transport?.state === "reconnecting") {
    push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:transport:reconnecting:${input.transport.attempt ?? 1}`, type: "transport.reconnecting", attempt: Math.max(1, input.transport.attempt ?? 1) })
  } else if (input.transport?.state === "connected") {
    push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: "ui:transport:recovered", type: "transport.recovered" })
  }

  return Object.freeze(events)
}
