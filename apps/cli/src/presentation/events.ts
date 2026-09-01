export const ZYRA_UI_EVENT_SCHEMA = "zyra.ui-event/v2" as const

export type UiConnectionState = "connecting" | "connected" | "reconnecting" | "disconnected"
export type UiPermissionDecision = "allow" | "deny"
export type UiFileChangeKind = "created" | "modified" | "deleted" | "renamed"
export type UiSeverity = "info" | "warning" | "error"

export interface UiPermissionRequest {
  requestId: string
  action: string
  target?: string
  reason?: string
  risk?: string
  expiresAt?: string
  scope?: string
  decisions: readonly UiPermissionDecision[]
}

export interface UiFileChange {
  path: string
  kind: UiFileChangeKind
  previousPath?: string
}

export interface UiVerificationSummary {
  status: "passed" | "failed" | "not_run"
  label: string
  details: readonly string[]
  checks: readonly UiVerificationCheck[]
  commandEvidence: "recorded" | "not_recorded"
}

export interface UiVerificationCheck {
  source: "final_verifier" | "delivery_verifier" | "completion_gate" | "command"
  name: string
  status: "passed" | "failed" | "skipped" | "not_run"
  summary?: string
  command?: string
  exitCode?: number
}

interface UiEventBase {
  schema: typeof ZYRA_UI_EVENT_SCHEMA
  eventId: string
  occurredAt?: string
}

export type ZyraUiEvent =
  | (UiEventBase & { type: "session.started"; sessionId: string; taskId?: string })
  | (UiEventBase & { type: "user.message"; messageId: string; text: string })
  | (UiEventBase & { type: "assistant.message.started"; messageId: string })
  | (UiEventBase & { type: "assistant.message.delta"; messageId: string; text: string })
  | (UiEventBase & {
      type: "assistant.message.completed"
      messageId: string
      text: string
      source: "stream" | "canonical_final_answer"
    })
  | (UiEventBase & { type: "activity.started"; activityId: string; label: string; category?: string; summary?: string; severity?: UiSeverity })
  | (UiEventBase & { type: "activity.updated"; activityId: string; label: string; category?: string; summary?: string; severity?: UiSeverity })
  | (UiEventBase & { type: "activity.completed"; activityId: string; label: string; outcome?: string; category?: string; summary?: string; severity?: UiSeverity })
  | (UiEventBase & { type: "tool.started"; toolCallId: string; name: string; summary: string; durationMs?: number; artifactIds?: readonly string[] })
  | (UiEventBase & { type: "tool.updated"; toolCallId: string; name?: string; summary: string; durationMs?: number; artifactIds?: readonly string[] })
  | (UiEventBase & { type: "tool.completed"; toolCallId: string; name?: string; summary: string; durationMs?: number; artifactIds?: readonly string[] })
  | (UiEventBase & { type: "tool.failed"; toolCallId: string; name?: string; message: string; durationMs?: number; artifactIds?: readonly string[] })
  | (UiEventBase & { type: "permission.requested"; request: UiPermissionRequest })
  | (UiEventBase & { type: "permission.resolved"; requestId: string; decision: string })
  | (UiEventBase & { type: "workspace.changed"; changes: readonly UiFileChange[] })
  | (UiEventBase & { type: "workspace.diff"; lines: readonly string[]; truncated: boolean; source: "local_workspace" })
  | (UiEventBase & { type: "verification.updated"; verification: UiVerificationSummary })
  | (UiEventBase & { type: "subagent.updated"; agentId: string; label: string; status: string; summary?: string })
  | (UiEventBase & { type: "task.issue"; issueId: string; severity: UiSeverity; message: string; code?: string; retryable?: boolean; recovery?: string })
  | (UiEventBase & { type: "task.completed"; taskId: string; finalAnswer: string })
  | (UiEventBase & { type: "task.failed"; taskId: string; message: string; recovery?: string })
  | (UiEventBase & { type: "task.cancelled"; taskId: string; message: string })
  | (UiEventBase & { type: "transport.reconnecting"; attempt: number })
  | (UiEventBase & { type: "transport.recovered" })

export interface UiPermissionSnapshot {
  requestId: string
  status: string
  prompt?: string
  reason?: string
  toolName?: string
  operation?: string
  target?: string
  risk?: string
  expiresAt?: string
  scope?: string
  selectable?: boolean
}

export interface UiTransportSnapshot {
  state: UiConnectionState
  attempt?: number
}
