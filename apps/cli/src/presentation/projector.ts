import type { TaskProjection } from "@zyra/typed-api-client"
import type { IngressFrame } from "../api.ts"
import {
  ZYRA_UI_EVENT_SCHEMA,
  type UiFileChange,
  type UiPermissionRequest,
  type UiPermissionSnapshot,
  type UiTransportSnapshot,
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

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined
}

function content(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.flatMap((item) => typeof item === "string" && item.trim() ? [item.trim()] : [])
    : []
}

function displayPath(value: string): string {
  const normalized = value.replaceAll("\\", "/").replace(/^[A-Za-z]:\//, "").replace(/^\/+/, "")
  const parts = normalized.split("/").filter((part) => part && part !== "." && part !== "..")
  return parts.join("/") || "<workspace>"
}

function occurredAt(frame: IngressFrame): string | undefined {
  return text(frame.event.createdAt) ?? text(frame.event.committedAt)
}

function sortedFrames(frames: readonly IngressFrame[], taskId: string): IngressFrame[] {
  const seen = new Set<string>()
  return [...frames]
    .filter((frame) => frame.taskId === taskId)
    .sort((left, right) => left.sequence - right.sequence || left.eventId.localeCompare(right.eventId))
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

  for (const node of [...task.planNodes].sort((left, right) => left.nodeId.localeCompare(right.nodeId))) {
    if (node.nodeId === task.rootNodeId) continue
    const label = node.title.trim() || node.description.trim() || "执行任务"
    const base = {
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: `ui:activity:${node.nodeId}:${node.status}`,
      occurredAt: node.updatedAt ?? task.updatedAt,
      activityId: `activity:${node.nodeId}`,
      label,
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

  for (const frame of sortedFrames(input.frames ?? [], task.taskId)) {
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
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "tool.failed", toolCallId, message: `${toolName} 执行失败${text(inline.error_code) ? `（${text(inline.error_code)}）` : ""}` })
        break
      case "runtime.tool.cancelled":
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "tool.failed", toolCallId, message: `${toolName} 已取消` })
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
        push({ schema: ZYRA_UI_EVENT_SCHEMA, eventId: `ui:${frame.eventId}`, occurredAt: at, type: "subagent.updated", agentId, label: "协作代理", status })
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

  const finalAnswer = content(task.metadata.final_answer)
  if (task.status === "completed") {
    if (finalAnswer && !completedAssistantTexts.includes(finalAnswer)) {
      push({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `ui:assistant:${task.taskId}:canonical-final`,
        occurredAt: task.updatedAt,
        type: "assistant.message.completed",
        messageId: `message:assistant:${task.taskId}:final`,
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
  } else if (task.terminal || task.status === "failed") {
    push({
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: `ui:task:${task.taskId}:failed`,
      occurredAt: task.updatedAt,
      type: "task.failed",
      taskId: task.taskId,
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
