export const ZYRA_UI_EVENT_SCHEMA = "zyra.ui-event/v2" as const

export type UiConnectionState = "connecting" | "connected" | "reconnecting" | "disconnected"
export type UiPermissionDecision = "allow" | "deny"
export type UiFileChangeKind = "created" | "modified" | "deleted" | "renamed"
export type UiSeverity = "info" | "warning" | "error"
export type UiFailureImpact = "local" | "task"

export interface UiToolOutputRef {
  artifactId: string
  stream: "stdout" | "stderr"
  title: string
  mediaType: string
  sizeBytes?: number
}

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

export interface UiUserInputOption {
  label: string
  description: string
}

export interface UiUserInputQuestion {
  id: string
  header: string
  question: string
  options: readonly UiUserInputOption[]
}

export interface UiUserInputRequest {
  requestId: string
  status: "pending" | "answered" | "cancelled" | "expired"
  revision: number
  questions: readonly UiUserInputQuestion[]
  answers?: Readonly<Record<string, { answers: readonly string[] }>>
  createdAt?: string
  updatedAt?: string
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

export interface UiContextUsage {
  activeChars: number
  activeLimitChars: number
  usedPercent: number
  remainingPercent: number
  compactNeeded: boolean
  pressure?: string
  inputTokens?: number
  outputTokens?: number
}

export type UiPlanStepStatus = "pending" | "running" | "completed" | "failed" | "cancelled" | "superseded"

export interface UiPlanStep {
  stepId: string
  label: string
  description?: string
  status: UiPlanStepStatus
  dependsOn: readonly string[]
  assignedAgentId?: string
}

export interface UiPlanChange {
  changeId: string
  kind: "requirement_change" | "failure_recovery"
  summary: string
  affectedStepIds: readonly string[]
  createdAt?: string
}

export interface UiPlanSnapshot {
  schema: "zyra.ui-plan/v1"
  revision: number
  revisionSource: "canonical_graph" | "compatibility"
  graphId?: string
  steps: readonly UiPlanStep[]
  changes: readonly UiPlanChange[]
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
  | (UiEventBase & { type: "activity.started"; activityId: string; label: string; category?: string; summary?: string; severity?: UiSeverity; impact?: UiFailureImpact })
  | (UiEventBase & { type: "activity.updated"; activityId: string; label: string; category?: string; summary?: string; severity?: UiSeverity; impact?: UiFailureImpact })
  | (UiEventBase & { type: "activity.completed"; activityId: string; label: string; outcome?: string; category?: string; summary?: string; severity?: UiSeverity; impact?: UiFailureImpact })
  | (UiEventBase & { type: "tool.started"; toolCallId: string; name: string; summary: string; durationMs?: number; artifactIds?: readonly string[]; outputRefs?: readonly UiToolOutputRef[] })
  | (UiEventBase & { type: "tool.updated"; toolCallId: string; name?: string; summary: string; durationMs?: number; artifactIds?: readonly string[]; outputRefs?: readonly UiToolOutputRef[] })
  | (UiEventBase & { type: "tool.completed"; toolCallId: string; name?: string; summary: string; durationMs?: number; artifactIds?: readonly string[]; outputRefs?: readonly UiToolOutputRef[] })
  | (UiEventBase & { type: "tool.failed"; toolCallId: string; name?: string; message: string; durationMs?: number; artifactIds?: readonly string[]; outputRefs?: readonly UiToolOutputRef[]; impact: UiFailureImpact; code?: string; retryable?: boolean; recovery?: string })
  | (UiEventBase & { type: "permission.requested"; request: UiPermissionRequest })
  | (UiEventBase & { type: "permission.resolved"; requestId: string; decision: string })
  | (UiEventBase & { type: "user_input.requested"; request: UiUserInputRequest })
  | (UiEventBase & { type: "user_input.resolved"; request: UiUserInputRequest })
  | (UiEventBase & { type: "workspace.changed"; changes: readonly UiFileChange[] })
  | (UiEventBase & { type: "workspace.diff"; lines: readonly string[]; truncated: boolean; source: "local_workspace" })
  | (UiEventBase & { type: "verification.updated"; verification: UiVerificationSummary })
  | (UiEventBase & { type: "context.updated"; context: UiContextUsage })
  | (UiEventBase & { type: "plan.updated"; plan: UiPlanSnapshot })
  | (UiEventBase & { type: "subagent.updated"; agentId: string; label: string; status: string; summary?: string; impact?: UiFailureImpact; code?: string; retryable?: boolean; recovery?: string })
  | (UiEventBase & { type: "task.issue"; issueId: string; severity: UiSeverity; message: string; code?: string; retryable?: boolean; recovery?: string; impact?: UiFailureImpact })
  | (UiEventBase & { type: "task.completed"; taskId: string; finalAnswer: string })
  | (UiEventBase & { type: "task.needs_revision"; taskId: string; message: string; recovery?: string })
  | (UiEventBase & { type: "task.failed"; taskId: string; status?: "failed" | "blocked" | "killed"; message: string; recovery?: string })
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
