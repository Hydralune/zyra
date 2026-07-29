import type {
  LoopXControlState,
  LoopXTodo,
} from "../../../api/loopx-api.ts"

export interface LoopXWorkbenchView {
  readonly lifecycle: LoopXControlState["lifecycle"]
  readonly goalId: string
  readonly objectiveRef: string
  readonly todos: readonly LoopXTodo[]
  readonly quota: Readonly<Record<string, unknown>>
  readonly sync: LoopXControlState["sync"]
  readonly continuationAllowed: boolean
  readonly canonicalTaskStatus: string
  readonly workerLeaseStatus: string
  readonly executionBudget: Readonly<Record<string, unknown>>
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  return (
    value && typeof value === "object" && !Array.isArray(value)
      ? value as Readonly<Record<string, unknown>>
      : {}
  )
}

export function loopxWorkbenchView(
  state: LoopXControlState,
): LoopXWorkbenchView {
  const canonical = record(state.canonical_state)
  const task = record(canonical.task)
  const lease = record(canonical.worker_lease)
  return Object.freeze({
    lifecycle: state.lifecycle,
    goalId: state.goal_id,
    objectiveRef: String(state.private_state.goal.objective_ref ?? ""),
    todos: state.private_state.todos,
    quota: state.private_state.quota,
    sync: state.sync,
    continuationAllowed: state.continuation.allowed,
    canonicalTaskStatus: String(task.status ?? "unknown"),
    workerLeaseStatus: String(lease.status ?? "none"),
    executionBudget: record(canonical.execution_budget),
  })
}
