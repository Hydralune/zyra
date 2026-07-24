import {
  createIdempotencyKey,
  normalizeIdempotencyKey,
} from "../../../../../../packages/core/typed-api-client/src/index.ts"
import {
  RecoveryControlAction,
  RecoveryControlError,
  type NormalizedRecoveryControlRequest,
  type RecoveryControlActionValue,
  type RecoveryControlOwnerExpectation,
  type RecoveryControlRequest,
  type RecoveryControlTargetKind,
} from "./contracts.ts"
import { buildRecoveryControlCommand } from "./commands.ts"

const IDENTITY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,511}$/
const ACTIONS = new Set<RecoveryControlActionValue>(
  Object.values(RecoveryControlAction),
)
const TARGETS = new Set<RecoveryControlTargetKind>([
  "task",
  "worker",
  "node",
  "recovery",
  "checkpoint",
  "session",
])
const encoder = new TextEncoder()

export interface RecoveryControlValidationOptions {
  now?: () => number
  requestId: () => string
  commandId: () => string
  defaultTimeoutMs: number
  maximumTimeoutMs: number
}

function byteLength(value: string): number {
  return encoder.encode(value).byteLength
}

function boundedText(
  value: unknown,
  label: string,
  options: {
    maximumBytes: number
    required?: boolean
    collapseWhitespace?: boolean
  },
): string {
  let result = String(value ?? "").trim()
  if (options.collapseWhitespace) result = result.replace(/\s+/g, " ")
  if (options.required !== false && !result) {
    throw new RecoveryControlError(
      "control_validation_empty",
      `${label} must not be empty.`,
      { details: { label } },
    )
  }
  const size = byteLength(result)
  if (size > options.maximumBytes) {
    throw new RecoveryControlError(
      "control_validation_too_large",
      `${label} exceeds ${options.maximumBytes} bytes.`,
      {
        details: {
          label,
          size,
          maximum: options.maximumBytes,
        },
      },
    )
  }
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(result)) {
    throw new RecoveryControlError(
      "control_validation_control_character",
      `${label} contains unsupported control characters.`,
      { details: { label } },
    )
  }
  return result
}

function identity(
  value: unknown,
  label: string,
  options: { required?: boolean } = {},
): string | undefined {
  const result = boundedText(value, label, {
    maximumBytes: 512,
    required: options.required,
  })
  if (!result && options.required === false) return undefined
  if (!IDENTITY_PATTERN.test(result)) {
    throw new RecoveryControlError(
      "control_validation_identity",
      `${label} is not a valid canonical identity.`,
      { details: { label } },
    )
  }
  return result
}

function integer(
  value: unknown,
  label: string,
  options: {
    minimum?: number
    maximum?: number
    required?: boolean
  } = {},
): number | undefined {
  if (value === undefined || value === null || value === "") {
    if (options.required) {
      throw new RecoveryControlError(
        "control_validation_integer",
        `${label} is required.`,
        { details: { label } },
      )
    }
    return undefined
  }
  const result = Number(value)
  const minimum = options.minimum ?? 0
  const maximum = options.maximum ?? Number.MAX_SAFE_INTEGER
  if (
    !Number.isSafeInteger(result) ||
    result < minimum ||
    result > maximum
  ) {
    throw new RecoveryControlError(
      "control_validation_integer",
      `${label} must be a safe integer between ${minimum} and ${maximum}.`,
      {
        details: {
          label,
          minimum,
          maximum,
        },
      },
    )
  }
  return result
}

function finiteTimeout(
  value: unknown,
  defaultTimeoutMs: number,
  maximumTimeoutMs: number,
): number {
  if (value === undefined || value === null || value === "") {
    return Math.max(250, Math.min(maximumTimeoutMs, defaultTimeoutMs))
  }
  const result = Number(value)
  if (!Number.isFinite(result)) {
    throw new RecoveryControlError(
      "control_timeout_invalid",
      "Control timeout must be finite.",
    )
  }
  return Math.max(250, Math.min(maximumTimeoutMs, Math.floor(result)))
}

function booleanMetadata(
  value: unknown,
): value is string | number | boolean {
  return (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  )
}

function metadata(
  value: Readonly<Record<string, string | number | boolean>> | undefined,
): Readonly<Record<string, string | number | boolean>> {
  const result: Record<string, string | number | boolean> = {}
  for (const [rawKey, rawValue] of Object.entries(value ?? {})) {
    const key = boundedText(rawKey, "Metadata key", {
      maximumBytes: 128,
      collapseWhitespace: true,
    })
    if (!/^[A-Za-z0-9_.:\-]+$/.test(key)) {
      throw new RecoveryControlError(
        "control_metadata_key_invalid",
        `Metadata key ${key} contains unsupported characters.`,
      )
    }
    if (!booleanMetadata(rawValue)) {
      throw new RecoveryControlError(
        "control_metadata_value_invalid",
        `Metadata value ${key} must be a scalar.`,
      )
    }
    if (typeof rawValue === "number" && !Number.isFinite(rawValue)) {
      throw new RecoveryControlError(
        "control_metadata_number_invalid",
        `Metadata value ${key} must be finite.`,
      )
    }
    const normalized =
      typeof rawValue === "string"
        ? boundedText(rawValue, `Metadata ${key}`, {
            maximumBytes: 2048,
            required: false,
          })
        : rawValue
    result[key] = normalized
  }
  return Object.freeze(result)
}

function ownerExpectation(
  value: RecoveryControlOwnerExpectation,
  taskId: string,
  runId: string,
): RecoveryControlOwnerExpectation {
  const ownerTaskId = identity(
    value?.taskId ?? taskId,
    "Expected task id",
  ) as string
  const ownerRunId = identity(
    value?.runId ?? runId,
    "Expected run id",
  ) as string
  if (ownerTaskId !== taskId || ownerRunId !== runId) {
    throw new RecoveryControlError(
      "control_owner_scope_mismatch",
      "Expected owner crosses the requested task/run scope.",
      {
        details: {
          taskId,
          runId,
          ownerTaskId,
          ownerRunId,
        },
      },
    )
  }
  return Object.freeze({
    taskId,
    runId,
    sessionId: identity(value?.sessionId, "Session id", { required: false }),
    workerId: identity(value?.workerId, "Worker id", { required: false }),
    leaseId: identity(value?.leaseId, "Lease id", { required: false }),
    attemptId: identity(value?.attemptId, "Attempt id", { required: false }),
    nodeId: identity(value?.nodeId, "Node id", { required: false }),
    graphId: identity(value?.graphId, "Graph id", { required: false }),
    checkpointId: identity(value?.checkpointId, "Checkpoint id", {
      required: false,
    }),
    ownerRevision: integer(value?.ownerRevision, "Owner revision"),
    graphRevision: integer(value?.graphRevision, "Graph revision"),
    sessionRevision: integer(value?.sessionRevision, "Session revision"),
  })
}

function targetKind(
  action: RecoveryControlActionValue,
  value: RecoveryControlTargetKind | undefined,
): RecoveryControlTargetKind {
  const inferred: RecoveryControlTargetKind =
    action === RecoveryControlAction.KILL
      ? "worker"
      : action === RecoveryControlAction.STEER
        ? "node"
        : action === RecoveryControlAction.REASSIGN
          ? "worker"
          : action === RecoveryControlAction.RESUME
            ? "checkpoint"
            : "recovery"
  const result = value ?? inferred
  if (!TARGETS.has(result)) {
    throw new RecoveryControlError(
      "control_target_kind_invalid",
      `Unsupported control target kind ${String(result)}.`,
    )
  }
  return result
}

function validateActionOwner(
  action: RecoveryControlActionValue,
  owner: RecoveryControlOwnerExpectation,
  target: RecoveryControlTargetKind,
): void {
  if (
    (action === RecoveryControlAction.KILL ||
      action === RecoveryControlAction.REASSIGN) &&
    (!owner.workerId || !owner.leaseId)
  ) {
    throw new RecoveryControlError(
      "control_worker_owner_required",
      `${action} requires the currently observed worker and lease identities.`,
    )
  }
  if (action === RecoveryControlAction.STEER && !owner.nodeId) {
    throw new RecoveryControlError(
      "control_node_owner_required",
      "steer requires a canonical node identity.",
    )
  }
  if (
    action === RecoveryControlAction.RESUME &&
    (!owner.checkpointId || !owner.sessionId)
  ) {
    throw new RecoveryControlError(
      "control_checkpoint_owner_required",
      "resume requires exact checkpoint and session identities.",
    )
  }
  if (target === "worker" && !owner.workerId) {
    throw new RecoveryControlError(
      "control_target_owner_required",
      "Worker target requires a worker identity.",
    )
  }
}

function targetIdentity(
  target: RecoveryControlTargetKind,
  explicit: string | undefined,
  owner: RecoveryControlOwnerExpectation,
): string | undefined {
  const value =
    explicit ??
    (target === "task"
      ? owner.taskId
      : target === "worker"
        ? owner.workerId
        : target === "node"
          ? owner.nodeId
          : target === "checkpoint"
            ? owner.checkpointId
            : target === "session"
              ? owner.sessionId
              : undefined)
  return identity(value, "Target id", { required: false })
}

function requestFingerprint(
  request: Omit<
    NormalizedRecoveryControlRequest,
    "requestDigest" | "idempotencyKey"
  >,
): string {
  const value = JSON.stringify({
    action: request.action,
    taskId: request.taskId,
    runId: request.runId,
    actorId: request.actorId,
    reason: request.reason,
    instruction: request.instruction,
    targetKind: request.targetKind,
    targetId: request.targetId,
    sealed: request.sealed,
    maximumAttempts: request.maximumAttempts,
    owner: request.owner,
    commandName: request.commandName,
    commandText: request.commandText,
    commandArguments: request.commandArguments,
    metadata: request.metadata,
  })
  let first = 0x811c9dc5
  let second = 0x9e3779b9
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index)
    first ^= code
    first = Math.imul(first, 0x01000193)
    second ^= code + index
    second = Math.imul(second, 0x85ebca6b)
    second ^= second >>> 13
  }
  return `fnv128:${(first >>> 0).toString(16).padStart(8, "0")}${(
    second >>> 0
  )
    .toString(16)
    .padStart(8, "0")}:${byteLength(value)}`
}

export function normalizeRecoveryControlRequest(
  request: RecoveryControlRequest,
  options: RecoveryControlValidationOptions,
): NormalizedRecoveryControlRequest {
  if (!ACTIONS.has(request.action)) {
    throw new RecoveryControlError(
      "control_action_invalid",
      `Unsupported recovery control action ${String(request.action)}.`,
    )
  }
  const taskId = identity(request.taskId, "Task id") as string
  const runId = identity(request.runId, "Run id") as string
  const actorId = identity(request.actorId, "Actor id") as string
  const reason = boundedText(request.reason, "Control reason", {
    maximumBytes: 8192,
    collapseWhitespace: true,
  })
  const instruction = request.instruction
    ? boundedText(request.instruction, "Control instruction", {
        maximumBytes: 32 * 1024,
      })
    : undefined
  const owner = ownerExpectation(request.owner, taskId, runId)
  const target = targetKind(request.action, request.targetKind)
  validateActionOwner(request.action, owner, target)
  const targetId = targetIdentity(target, request.targetId, owner)
  const maximumAttempts = integer(
    request.maximumAttempts,
    "Maximum attempts",
    { minimum: 1, maximum: 8 },
  )
  const timeoutMs = finiteTimeout(
    request.timeoutMs,
    options.defaultTimeoutMs,
    options.maximumTimeoutMs,
  )
  const requestId = identity(options.requestId(), "Request id") as string
  const commandId = identity(options.commandId(), "Command id") as string
  const normalizedAt = (options.now ?? Date.now)()
  if (!Number.isFinite(normalizedAt) || normalizedAt < 0) {
    throw new RecoveryControlError(
      "control_clock_invalid",
      "Control clock returned an invalid timestamp.",
    )
  }
  const normalizedMetadata = metadata(request.metadata)
  const command = buildRecoveryControlCommand({
    action: request.action,
    taskId,
    runId,
    actorId,
    reason,
    instruction,
    targetKind: target,
    targetId,
    sealed: request.sealed === true,
    timeoutMs,
    maximumAttempts,
    owner,
    metadata: normalizedMetadata,
  })
  const partial = Object.freeze({
    action: request.action,
    taskId,
    runId,
    actorId,
    reason,
    instruction,
    targetKind: target,
    targetId,
    sealed: request.sealed === true,
    timeoutMs,
    maximumAttempts,
    owner,
    metadata: normalizedMetadata,
    requestId,
    commandId,
    commandName: command.name,
    commandText: command.text,
    commandArguments: command.arguments,
    normalizedAt,
  })
  const requestDigest = requestFingerprint(partial)
  const idempotencyKey = normalizeIdempotencyKey(
    createIdempotencyKey(
      "task.control-command",
      {
        taskId,
        runId,
        requestId,
        controlCommandId: commandId,
        sessionId: owner.sessionId,
      },
      {
        request_digest: requestDigest,
        action: request.action,
        command: command.text,
        arguments: command.arguments,
        sealed: request.sealed === true,
      },
    ),
  )
  return Object.freeze({
    ...partial,
    requestDigest,
    idempotencyKey,
  })
}

export function validateRecoveryControlScope(
  request: NormalizedRecoveryControlRequest,
  taskId: string,
  runId: string,
): void {
  if (request.taskId !== taskId || request.runId !== runId) {
    throw new RecoveryControlError(
      "control_runtime_scope_mismatch",
      "Control request does not belong to this runtime task/run.",
      {
        details: {
          requestTaskId: request.taskId,
          requestRunId: request.runId,
          runtimeTaskId: taskId,
          runtimeRunId: runId,
        },
      },
    )
  }
  if (
    request.owner.taskId !== taskId ||
    request.owner.runId !== runId
  ) {
    throw new RecoveryControlError(
      "control_owner_scope_mismatch",
      "Control owner does not belong to this runtime task/run.",
    )
  }
}

export function controlRequestEquivalent(
  left: NormalizedRecoveryControlRequest,
  right: NormalizedRecoveryControlRequest,
): boolean {
  return (
    left.requestDigest === right.requestDigest &&
    left.taskId === right.taskId &&
    left.runId === right.runId &&
    left.action === right.action
  )
}
