import type { TaskApi } from "../../api/task-api.ts"
import {
  freshPermissionId,
  identifier,
  safePermissionClone,
} from "./canonical.ts"
import type {
  PermissionInterventionRecord,
  PermissionProductMode,
} from "./contracts.ts"

export interface PermissionInterventionInput {
  taskId: string
  runId: string
  sessionId: string
  requestId?: string
  action: PermissionInterventionRecord["action"]
  actorId: string
  productMode: PermissionProductMode
  reason?: string
  signal?: AbortSignal
}

export class PermissionInterventionLedger {
  readonly #taskApi: Pick<TaskApi, "controlCommand">
  readonly #records = new Map<string, PermissionInterventionRecord>()
  readonly #now: () => Date
  readonly #maximum: number

  constructor(
    taskApi: Pick<TaskApi, "controlCommand">,
    options: { now?: () => Date; maximum?: number } = {},
  ) {
    this.#taskApi = taskApi
    this.#now = options.now ?? (() => new Date())
    this.#maximum = Math.min(
      10_000,
      Math.max(16, Math.floor(options.maximum ?? 256)),
    )
  }

  async record(
    input: PermissionInterventionInput,
  ): Promise<PermissionInterventionRecord> {
    const taskId = identifier(input.taskId, "permission task id")
    const runId = identifier(input.runId, "permission run id")
    const sessionId = identifier(input.sessionId, "permission session id")
    const requestId = input.requestId
      ? identifier(input.requestId, "permission request id")
      : undefined
    const interventionId = freshPermissionId(
      "permission_intervention",
      this.#now(),
    )
    const actorId = boundedActor(input.actorId)
    if (input.productMode !== "sealed") {
      return this.#remember({
        interventionId,
        requestId,
        action: input.action,
        actorId,
        productMode: input.productMode,
        rejected: false,
        counted: false,
        reasonCode: "interactive_permission_response",
        reason:
          input.reason
          ?? "Interactive permission decisions are submitted to the canonical permission owner.",
        recordedAt: this.#now().toISOString(),
      })
    }

    const commandRequestId = freshPermissionId(
      "request_sealed_permission_intervention",
      this.#now(),
    )
    const commandId = freshPermissionId(
      "control_sealed_permission_intervention",
      this.#now(),
    )
    const text =
      input.action === "retry"
        ? `/retry sealed permission intervention ${interventionId}`
        : `/steer sealed permission ${input.action} intervention ${interventionId}`
    const reason =
      input.reason
      ?? `Sealed autonomous policy rejected manual permission ${input.action}.`
    let canonicalReceipt: Readonly<Record<string, unknown>> | undefined
    let counted = false
    let reasonCode = "sealed_permission_intervention_denied"
    try {
      canonicalReceipt = await this.#taskApi.controlCommand({
        taskId,
        runId,
        sessionId,
        text,
        arguments: {
          permission_intervention_id: interventionId,
          permission_request_id: requestId,
          attempted_permission_action: input.action,
          operator_reason: reason,
          no_human_wait: true,
          manual_mutation_applied: false,
        },
        requestId: commandRequestId,
        commandId,
        actorId,
        sealed: true,
        priority: "next",
        deliveryMode: "enqueue",
        idempotencyKey: interventionId,
        signal: input.signal,
      })
      counted = interventionCounted(canonicalReceipt)
      reasonCode = receiptReasonCode(canonicalReceipt) ?? reasonCode
    } catch (error) {
      canonicalReceipt = errorReceipt(error)
      counted = interventionCounted(canonicalReceipt)
      reasonCode = errorCode(error) ?? reasonCode
    }
    return this.#remember({
      interventionId,
      requestId,
      action: input.action,
      actorId,
      productMode: "sealed",
      rejected: true,
      counted,
      reasonCode,
      reason,
      recordedAt: this.#now().toISOString(),
      canonicalReceipt,
    })
  }

  list(): PermissionInterventionRecord[] {
    return [...this.#records.values()]
      .sort(
        (left, right) =>
          left.recordedAt.localeCompare(right.recordedAt)
          || left.interventionId.localeCompare(right.interventionId),
      )
      .map(freezeRecord)
  }

  clear(): void {
    this.#records.clear()
  }

  #remember(
    value: PermissionInterventionRecord,
  ): PermissionInterventionRecord {
    const record = freezeRecord(value)
    this.#records.set(record.interventionId, record)
    while (this.#records.size > this.#maximum) {
      const oldest = this.list()[0]
      if (!oldest) break
      this.#records.delete(oldest.interventionId)
    }
    return freezeRecord(record)
  }
}

function interventionCounted(
  receipt: Readonly<Record<string, unknown>> | undefined,
): boolean {
  if (!receipt) return false
  if (
    receipt.intervention_counted === true
    || receipt.operator_intervention_counted === true
  ) {
    return true
  }
  const command = asRecord(receipt.command_result)
  const data = asRecord(command.data)
  return (
    command.intervention_counted === true
    || data.intervention_counted === true
    || Number(
      receipt.operator_intervention_attempt_count
      ?? data.operator_intervention_attempt_count
      ?? 0,
    ) > 0
  )
}

function receiptReasonCode(
  receipt: Readonly<Record<string, unknown>>,
): string | undefined {
  const result = asRecord(receipt.command_result)
  const error = asRecord(result.error)
  return optionalCode(
    error.code
      ?? result.error_code
      ?? receipt.error
      ?? receipt.code,
  )
}

function errorReceipt(
  error: unknown,
): Readonly<Record<string, unknown>> {
  if (!error || typeof error !== "object") {
    return Object.freeze({
      error: "sealed_permission_intervention_failed",
      message: String(error),
    })
  }
  const value = error as Record<string, unknown>
  const detail = asRecord(value.detail)
  return Object.freeze(
    safePermissionClone({
      ...detail,
      error: value.code ?? detail.error ?? "sealed_permission_intervention_failed",
      message: value.message ?? detail.message ?? "Sealed intervention failed closed.",
      transport_rejected: true,
    }),
  )
}

function errorCode(error: unknown): string | undefined {
  if (!error || typeof error !== "object") return undefined
  return optionalCode((error as Record<string, unknown>).code)
}

function optionalCode(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined
  const rendered = value.trim()
  return rendered && rendered.length <= 256 ? rendered : undefined
}

function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

function boundedActor(value: unknown): string {
  const actor = typeof value === "string" ? value.trim() : ""
  if (
    !actor
    || /[\u0000\r\n]/.test(actor)
    || new TextEncoder().encode(actor).byteLength > 256
  ) {
    throw new TypeError("Permission intervention actor is invalid.")
  }
  return actor
}

function freezeRecord(
  value: PermissionInterventionRecord,
): PermissionInterventionRecord {
  return Object.freeze(safePermissionClone(value))
}
