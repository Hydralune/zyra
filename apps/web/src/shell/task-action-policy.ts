import type { TaskProjection } from "../../../../packages/core/typed-api-client/src/index.ts"

export type TaskAction = "refresh" | "cancel" | "resume"

export interface TaskActionDecision {
  action: TaskAction
  allowed: boolean
  reason: string
  destructive: boolean
}

const CANCELLABLE = new Set([
  "pending",
  "queued",
  "dispatched",
  "running",
  "active",
  "paused",
  "blocked",
])

const RESUMABLE = new Set([
  "failed",
  "cancelled",
  "interrupted",
  "paused",
  "blocked",
  "completed",
  "succeeded",
])

export function taskActionDecision(
  task: TaskProjection,
  action: TaskAction,
  options: { lifecycleBusy?: boolean; transportEnabled?: boolean } = {},
): TaskActionDecision {
  if (options.transportEnabled === false) {
    return {
      action,
      allowed: false,
      reason: "Typed API transport is unavailable.",
      destructive: action === "cancel",
    }
  }
  if (options.lifecycleBusy) {
    return {
      action,
      allowed: false,
      reason: "Another task lifecycle operation is in flight.",
      destructive: action === "cancel",
    }
  }
  if (action === "refresh") {
    return {
      action,
      allowed: true,
      reason: "Refresh the task projection from the backend.",
      destructive: false,
    }
  }
  if (action === "cancel") {
    const allowed = task.active || CANCELLABLE.has(task.status)
    return {
      action,
      allowed,
      reason: allowed
        ? "Submit a cancellation request to the backend."
        : `Task status ${task.status} is not cancellable.`,
      destructive: true,
    }
  }
  const allowed = task.terminal || RESUMABLE.has(task.status)
  return {
    action,
    allowed,
    reason: allowed
      ? "Resume from the task's durable backend state."
      : `Task status ${task.status} is not resumable.`,
    destructive: false,
  }
}

export function taskActionSet(
  task: TaskProjection,
  options: { lifecycleBusy?: boolean; transportEnabled?: boolean } = {},
): Record<TaskAction, TaskActionDecision> {
  return {
    refresh: taskActionDecision(task, "refresh", options),
    cancel: taskActionDecision(task, "cancel", options),
    resume: taskActionDecision(task, "resume", options),
  }
}

export function assertTaskAction(
  task: TaskProjection,
  action: TaskAction,
  options: { lifecycleBusy?: boolean; transportEnabled?: boolean } = {},
): TaskActionDecision {
  const decision = taskActionDecision(task, action, options)
  if (!decision.allowed) throw new TypeError(decision.reason)
  return decision
}
