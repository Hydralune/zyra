import {
  RecoveryControlAction,
  type RecoveryControlActionValue,
  type RecoveryControlOwnerExpectation,
  type RecoveryControlTargetKind,
} from "./contracts.ts"

export interface RecoveryControlCommandSource {
  action: RecoveryControlActionValue
  taskId: string
  runId: string
  actorId: string
  reason: string
  instruction?: string
  targetKind: RecoveryControlTargetKind
  targetId?: string
  sealed: boolean
  timeoutMs: number
  maximumAttempts?: number
  owner: RecoveryControlOwnerExpectation
  metadata: Readonly<Record<string, string | number | boolean>>
}

export interface RecoveryControlCommand {
  name: string
  text: string
  arguments: Readonly<Record<string, unknown>>
}

function ownerArguments(
  owner: RecoveryControlOwnerExpectation,
): Record<string, unknown> {
  return {
    expected_task_id: owner.taskId,
    expected_run_id: owner.runId,
    expected_worker_id: owner.workerId,
    expected_lease_id: owner.leaseId,
    expected_attempt_id: owner.attemptId,
    expected_owner_revision: owner.ownerRevision,
    expected_graph_revision: owner.graphRevision,
    expected_revision: owner.sessionRevision,
    node_id: owner.nodeId,
    graph_id: owner.graphId,
    checkpoint_ref: owner.checkpointId,
    target_session_id: owner.sessionId,
  }
}

function compactArguments(
  value: Readonly<Record<string, unknown>>,
): Readonly<Record<string, unknown>> {
  const result: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(value)) {
    if (item === undefined || item === null || item === "") continue
    result[key] = item
  }
  return Object.freeze(result)
}

function commandText(name: string, raw: string): string {
  const value = raw.trim()
  return value ? `${name} ${value}` : name
}

function killCommand(source: RecoveryControlCommandSource): RecoveryControlCommand {
  const target =
    source.targetKind === "worker" ? "worker-and-task" : source.targetKind
  const raw = `${target} ${source.reason}`.trim()
  return Object.freeze({
    name: "/kill",
    text: commandText("/kill", raw),
    arguments: compactArguments({
      ...ownerArguments(source.owner),
      raw,
      reason: source.reason,
      target,
      target_id: source.targetId,
      old_fence_must_block_commit: true,
      timeout_ms: source.timeoutMs,
      control_source: "timeline",
      ...source.metadata,
    }),
  })
}

function steerCommand(source: RecoveryControlCommandSource): RecoveryControlCommand {
  const instruction = source.instruction || source.reason
  return Object.freeze({
    name: "/steer",
    text: commandText("/steer", instruction),
    arguments: compactArguments({
      ...ownerArguments(source.owner),
      raw: instruction,
      instruction,
      requirement: instruction,
      reason: source.reason,
      target: source.targetId,
      target_kind: source.targetKind,
      change_scope: "recovery_graph_route",
      requirement_change_is_fault: false,
      timeout_ms: source.timeoutMs,
      control_source: "timeline",
      ...source.metadata,
    }),
  })
}

function retryCommand(source: RecoveryControlCommandSource): RecoveryControlCommand {
  return Object.freeze({
    name: "/retry",
    text: commandText("/retry", source.reason),
    arguments: compactArguments({
      ...ownerArguments(source.owner),
      raw: source.reason,
      reason: source.reason,
      target: source.targetId,
      target_kind: source.targetKind,
      maximum_attempts: source.maximumAttempts ?? 1,
      bounded_retry: true,
      replay_committed_effects: false,
      timeout_ms: source.timeoutMs,
      control_source: "timeline",
      ...source.metadata,
    }),
  })
}

function reassignCommand(
  source: RecoveryControlCommandSource,
): RecoveryControlCommand {
  const raw = `${source.owner.workerId ?? ""} ${source.reason}`.trim()
  return Object.freeze({
    name: "/reassign",
    text: commandText("/reassign", raw),
    arguments: compactArguments({
      ...ownerArguments(source.owner),
      raw,
      reason: source.reason,
      target: source.targetId ?? source.owner.workerId,
      target_kind: "worker",
      exclude_worker_id: source.owner.workerId,
      fence_previous_lease: true,
      resume_from_checkpoint: Boolean(source.owner.checkpointId),
      timeout_ms: source.timeoutMs,
      control_source: "timeline",
      ...source.metadata,
    }),
  })
}

function resumeCommand(source: RecoveryControlCommandSource): RecoveryControlCommand {
  const checkpointId = source.owner.checkpointId as string
  const sessionId = source.owner.sessionId as string
  return Object.freeze({
    name: "/resume",
    text: commandText("/resume", sessionId),
    arguments: compactArguments({
      ...ownerArguments(source.owner),
      raw: sessionId,
      target: sessionId,
      target_session_id: sessionId,
      checkpoint_ref: checkpointId,
      candidate_step_ids: [],
      compact_first: false,
      rebind_worker: Boolean(source.owner.workerId),
      rebind_graph: Boolean(source.owner.graphId),
      exact_resume: true,
      timeout_ms: source.timeoutMs,
      control_source: "timeline",
      ...source.metadata,
    }),
  })
}

export function buildRecoveryControlCommand(
  source: RecoveryControlCommandSource,
): RecoveryControlCommand {
  switch (source.action) {
    case RecoveryControlAction.KILL:
      return killCommand(source)
    case RecoveryControlAction.STEER:
      return steerCommand(source)
    case RecoveryControlAction.RETRY:
      return retryCommand(source)
    case RecoveryControlAction.REASSIGN:
      return reassignCommand(source)
    case RecoveryControlAction.RESUME:
      return resumeCommand(source)
  }
}

export function commandNameForRecoveryAction(
  action: RecoveryControlActionValue,
): string {
  switch (action) {
    case RecoveryControlAction.KILL:
      return "/kill"
    case RecoveryControlAction.STEER:
      return "/steer"
    case RecoveryControlAction.RETRY:
      return "/retry"
    case RecoveryControlAction.REASSIGN:
      return "/reassign"
    case RecoveryControlAction.RESUME:
      return "/resume"
  }
}

export function controlActionForCommand(
  command: string,
): RecoveryControlActionValue | undefined {
  const name = command.trim().toLocaleLowerCase().split(/\s+/, 1)[0]
  if (name === "/kill") return RecoveryControlAction.KILL
  if (name === "/steer") return RecoveryControlAction.STEER
  if (name === "/retry") return RecoveryControlAction.RETRY
  if (name === "/reassign") return RecoveryControlAction.REASSIGN
  if (name === "/resume") return RecoveryControlAction.RESUME
  return undefined
}

export function recoveryActionRequiresWorker(
  action: RecoveryControlActionValue,
): boolean {
  return (
    action === RecoveryControlAction.KILL ||
    action === RecoveryControlAction.REASSIGN
  )
}

export function recoveryActionDestructive(
  action: RecoveryControlActionValue,
): boolean {
  return action === RecoveryControlAction.KILL
}

export function recoveryActionLabel(
  action: RecoveryControlActionValue,
): string {
  switch (action) {
    case RecoveryControlAction.KILL:
      return "Kill and fence"
    case RecoveryControlAction.STEER:
      return "Steer route"
    case RecoveryControlAction.RETRY:
      return "Retry once"
    case RecoveryControlAction.REASSIGN:
      return "Reassign worker"
    case RecoveryControlAction.RESUME:
      return "Resume checkpoint"
  }
}

export function recoveryActionDescription(
  action: RecoveryControlActionValue,
): string {
  switch (action) {
    case RecoveryControlAction.KILL:
      return "Cancel the current task attempt, fence its lease, and stop the selected worker."
    case RecoveryControlAction.STEER:
      return "Commit an operator instruction through the recovery policy and graph route owner."
    case RecoveryControlAction.RETRY:
      return "Request one bounded retry through RecoveryDecisionRuntime and the canonical continuation."
    case RecoveryControlAction.REASSIGN:
      return "Fence the current lease and acquire a successor worker through the scheduler owner."
    case RecoveryControlAction.RESUME:
      return "Resume an exact canonical checkpoint for the selected session."
  }
}
