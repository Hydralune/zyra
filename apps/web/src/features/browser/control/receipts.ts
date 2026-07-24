import {
  BROWSER_CONTROL_SCHEMA,
  BrowserControlPhase,
  booleanValue,
  firstDefined,
  integerValue,
  isRecord,
  nestedRecord,
  optionalRecord,
  optionalString,
  stringArray,
  type BrowserControlActionValue,
  type BrowserControlReceipt,
  type BrowserControlRequest,
} from "../contracts.ts"

function allRecords(
  value: Readonly<Record<string, unknown>>,
): readonly Readonly<Record<string, unknown>>[] {
  const output: Readonly<Record<string, unknown>>[] = []
  const queue: unknown[] = [value]
  const seen = new WeakSet<object>()
  while (queue.length && output.length < 5_000) {
    const current = queue.shift()
    if (Array.isArray(current)) {
      for (const child of current.slice(0, 5_000)) queue.push(child)
      continue
    }
    if (!isRecord(current)) continue
    if (seen.has(current)) continue
    seen.add(current)
    output.push(current)
    for (const child of Object.values(current)) {
      if (child && typeof child === "object") queue.push(child)
    }
  }
  return Object.freeze(output)
}

function firstRecordWith(
  records: readonly Readonly<Record<string, unknown>>[],
  ...keys: readonly string[]
): Readonly<Record<string, unknown>> | undefined {
  return records.find((record) => keys.some((key) => record[key] !== undefined))
}

function receiptPhase(
  request: BrowserControlRequest,
  records: readonly Readonly<Record<string, unknown>>[],
  statusCode?: number,
): BrowserControlReceipt["phase"] {
  const values = records
    .flatMap((record) =>
      [
        record.status,
        record.phase,
        record.state,
        record.error,
        record.error_code,
        record.permission_effect,
      ],
    )
    .filter((value) => value !== undefined)
    .map((value) => String(value).toLowerCase())
  const text = values.join(" ")
  if (
    request.identity.sealed
    && (
      statusCode === 403
      || /sealed|human.intervention|operator.intervention/.test(text)
    )
  ) return BrowserControlPhase.DENIED
  if (
    statusCode === 403
    || /permission.denied|forbidden|rejected|denied/.test(text)
  ) return BrowserControlPhase.DENIED
  if (statusCode === 408 || /timed.out|timeout/.test(text)) {
    return BrowserControlPhase.TIMED_OUT
  }
  if (statusCode === 409 && /stale|generation|owner|revision/.test(text)) {
    return BrowserControlPhase.STALE
  }
  if (/cancelled|canceled|aborted/.test(text)) {
    return BrowserControlPhase.CANCELLED
  }
  if (
    statusCode !== undefined
    && statusCode >= 400
    || /failed|error|conflict|invalid/.test(text)
  ) return BrowserControlPhase.FAILED
  if (/observed|diagnosed|inspect/.test(text)) {
    return BrowserControlPhase.OBSERVED
  }
  if (/applied|completed|stopped|navigated|created/.test(text)) {
    return BrowserControlPhase.APPLIED
  }
  if (statusCode === 202 || /accepted|pending|waiting/.test(text)) {
    return BrowserControlPhase.ACCEPTED
  }
  if (statusCode !== undefined && statusCode >= 200 && statusCode < 300) {
    return request.action === "inspect"
      ? BrowserControlPhase.OBSERVED
      : BrowserControlPhase.APPLIED
  }
  return BrowserControlPhase.FAILED
}

function valuesFor(
  records: readonly Readonly<Record<string, unknown>>[],
  ...keys: readonly string[]
): readonly unknown[] {
  const output: unknown[] = []
  for (const record of records) {
    for (const key of keys) {
      if (record[key] !== undefined && record[key] !== null) {
        output.push(record[key])
      }
    }
  }
  return output
}

function collectedStrings(
  records: readonly Readonly<Record<string, unknown>>[],
  ...keys: readonly string[]
): readonly string[] {
  const output = new Set<string>()
  for (const value of valuesFor(records, ...keys)) {
    for (const item of stringArray(value)) output.add(item)
    if (!Array.isArray(value) && !isRecord(value)) {
      const text = String(value).trim()
      if (text) output.add(text)
    }
  }
  return Object.freeze([...output])
}

function firstString(
  records: readonly Readonly<Record<string, unknown>>[],
  ...keys: readonly string[]
): string | undefined {
  for (const record of records) {
    const value = firstDefined(record, ...keys)
    const normalized = optionalString(value, `$.${keys[0]}`, 64 * 1024)
    if (normalized) return normalized
  }
  return undefined
}

function maximumInteger(
  records: readonly Readonly<Record<string, unknown>>[],
  keys: readonly string[],
  fallback = 0,
): number {
  let result = fallback
  for (const value of valuesFor(records, ...keys)) {
    const number = Number(value)
    if (Number.isSafeInteger(number) && number >= 0) {
      result = Math.max(result, number)
    }
  }
  return result
}

function errorDetails(
  phase: BrowserControlReceipt["phase"],
  records: readonly Readonly<Record<string, unknown>>[],
): { code?: string; message?: string; retryable: boolean } {
  if (
    phase === BrowserControlPhase.APPLIED
    || phase === BrowserControlPhase.OBSERVED
    || phase === BrowserControlPhase.ACCEPTED
  ) {
    return { retryable: false }
  }
  const code =
    firstString(
      records,
      "error_code",
      "errorCode",
      "code",
      "error",
    )
    ?? (
      phase === BrowserControlPhase.DENIED
        ? "browser_control_denied"
        : phase === BrowserControlPhase.TIMED_OUT
          ? "browser_control_timed_out"
          : phase === BrowserControlPhase.STALE
            ? "browser_control_stale"
            : phase === BrowserControlPhase.CANCELLED
              ? "browser_control_cancelled"
              : "browser_control_failed"
    )
  const message =
    firstString(
      records,
      "error_message",
      "errorMessage",
      "message",
      "reason",
      "summary",
    )
    ?? code
  const retryable =
    phase === BrowserControlPhase.TIMED_OUT
    || phase === BrowserControlPhase.STALE
    || records.some((record) =>
      booleanValue(firstDefined(record, "retryable", "can_retry")),
    )
  return { code, message, retryable }
}

function manualMutationApplied(
  request: BrowserControlRequest,
  records: readonly Readonly<Record<string, unknown>>[],
  mutationIds: readonly string[],
): boolean {
  if (request.identity.sealed) {
    if (records.some((record) =>
      booleanValue(
        firstDefined(
          record,
          "manual_mutation_applied",
          "manualMutationApplied",
          "state_mutated",
        ),
      ),
    )) return true
    const denialOnly = records.every((record) => {
      const status = String(
        firstDefined(record, "status", "phase", "state", "error_code") ?? "",
      ).toLowerCase()
      return !status || /deny|sealed|reject|forbidden|attempt/.test(status)
    })
    return mutationIds.length > 0 && !denialOnly
  }
  return mutationIds.length > 0
}

function approvalWaitEntered(
  records: readonly Readonly<Record<string, unknown>>[],
): boolean {
  return records.some((record) => {
    if (booleanValue(firstDefined(record, "approval_wait_entered", "awaiting_approval"))) {
      return true
    }
    const status = String(
      firstDefined(record, "status", "phase", "state") ?? "",
    ).toLowerCase()
    return /waiting.permission|pending.approval|human.approval/.test(status)
  })
}

function replayed(
  records: readonly Readonly<Record<string, unknown>>[],
): boolean {
  return records.some((record) =>
    booleanValue(
      firstDefined(
        record,
        "replayed",
        "idempotent_replay",
        "receipt_replayed",
      ),
    ),
  )
}

export function parseBrowserControlReceipt(
  value: unknown,
  request: BrowserControlRequest,
  options: { statusCode?: number; completedAt?: number } = {},
): BrowserControlReceipt {
  if (!isRecord(value)) {
    throw new TypeError("Browser control receipt must be an object.")
  }
  const raw = Object.freeze({ ...value })
  const records = allRecords(raw)
  const phase = receiptPhase(request, records, options.statusCode)
  const eventIds = new Set(
    collectedStrings(
      records,
      "event_ids",
      "eventIds",
      "committed_event_ids",
    ),
  )
  for (const event of valuesFor(records, "event", "event_record")) {
    if (!isRecord(event)) continue
    const id = optionalString(
      firstDefined(event, "event_id", "eventId", "id"),
      "$.event.event_id",
      512,
    )
    if (id) eventIds.add(id)
  }
  const mutationIds = collectedStrings(
    records,
    "mutation_ids",
    "mutationIds",
    "mutation_id",
  )
  const artifactIds = collectedStrings(
    records,
    "artifact_ids",
    "artifactIds",
    "artifact_id",
  )
  const details = errorDetails(phase, records)
  const sealed = request.identity.sealed
  const interventionCounted =
    records.some((record) =>
      booleanValue(
        firstDefined(
          record,
          "intervention_counted",
          "operator_intervention_counted",
        ),
      ),
    )
    || (
      sealed
      && phase === BrowserControlPhase.DENIED
      && maximumInteger(
        records,
        [
          "operator_intervention_attempt_count",
          "operatorInterventionAttemptCount",
        ],
      ) > 0
    )
  const humanInterventionCount = maximumInteger(
    records,
    ["human_intervention_count", "humanInterventionCount"],
    0,
  )
  return Object.freeze({
    schema: BROWSER_CONTROL_SCHEMA,
    commandId: request.commandId,
    requestId: request.requestId,
    idempotencyKey: request.idempotencyKey,
    action: request.action,
    phase,
    taskId: request.identity.taskId,
    runId: request.identity.runId,
    browserSessionId: request.identity.browserSessionId,
    workerRequestId:
      firstString(records, "worker_request_id", "workerRequestId")
      ?? request.identity.workerRequestId,
    actionId:
      firstString(records, "action_id", "actionId")
      ?? request.identity.actionId,
    statusCode: options.statusCode,
    eventIds: Object.freeze([...eventIds]),
    mutationIds: Object.freeze([...new Set(mutationIds)]),
    artifactIds: Object.freeze([...new Set(artifactIds)]),
    permissionRequestId: firstString(
      records,
      "permission_request_id",
      "permissionRequestId",
    ),
    lifecycleReceiptId: firstString(
      records,
      "browser_lifecycle_receipt_id",
      "lifecycle_receipt_id",
      "lifecycleReceiptId",
    ),
    errorCode: details.code,
    errorMessage: details.message,
    retryable: details.retryable,
    replayed: replayed(records),
    sealed,
    interventionCounted,
    humanInterventionCount,
    manualMutationApplied: manualMutationApplied(
      request,
      records,
      mutationIds,
    ),
    approvalWaitEntered: approvalWaitEntered(records),
    automaticRecoveryAction: firstString(
      records,
      "automatic_recovery_action",
      "automaticRecoveryAction",
      "recovery_action",
    ),
    submittedAt: request.createdAt,
    completedAt: options.completedAt ?? Date.now(),
    raw,
  })
}

export interface BrowserSealedReceiptAssessment {
  valid: boolean
  violations: readonly string[]
  operatorAttemptRecorded: boolean
  noManualMutation: boolean
  noHumanWait: boolean
  humanInterventionCountZero: boolean
  recoveryOrFailClosed: boolean
}

export function assessSealedBrowserReceipt(
  receipt: BrowserControlReceipt,
): BrowserSealedReceiptAssessment {
  if (!receipt.sealed) {
    return Object.freeze({
      valid: true,
      violations: Object.freeze([]),
      operatorAttemptRecorded: false,
      noManualMutation: true,
      noHumanWait: true,
      humanInterventionCountZero: true,
      recoveryOrFailClosed: true,
    })
  }
  const violations: string[] = []
  const operatorAttemptRecorded =
    receipt.interventionCounted
    || allRecords(receipt.raw).some((record) =>
      maximumInteger(
        [record],
        [
          "operator_intervention_attempt_count",
          "operatorInterventionAttemptCount",
        ],
      ) > 0,
    )
  const noManualMutation = !receipt.manualMutationApplied
  const noHumanWait = !receipt.approvalWaitEntered
  const humanInterventionCountZero = receipt.humanInterventionCount === 0
  const recoveryOrFailClosed =
    Boolean(receipt.automaticRecoveryAction)
    || receipt.phase === BrowserControlPhase.DENIED
    || receipt.phase === BrowserControlPhase.FAILED
  if (receipt.phase !== BrowserControlPhase.DENIED) {
    violations.push("sealed browser control was not denied")
  }
  if (!operatorAttemptRecorded) {
    violations.push("sealed browser control attempt was not canonically counted")
  }
  if (!noManualMutation) {
    violations.push("sealed browser control produced a manual mutation")
  }
  if (!noHumanWait) {
    violations.push("sealed browser control entered a human approval wait")
  }
  if (!humanInterventionCountZero) {
    violations.push("sealed browser control changed human_intervention_count")
  }
  if (!recoveryOrFailClosed) {
    violations.push("sealed browser control neither failed closed nor recovered automatically")
  }
  return Object.freeze({
    valid: violations.length === 0,
    violations: Object.freeze(violations),
    operatorAttemptRecorded,
    noManualMutation,
    noHumanWait,
    humanInterventionCountZero,
    recoveryOrFailClosed,
  })
}

export function browserReceiptTerminal(
  receipt: BrowserControlReceipt,
): boolean {
  switch (receipt.phase) {
    case BrowserControlPhase.APPLIED:
    case BrowserControlPhase.OBSERVED:
    case BrowserControlPhase.DENIED:
    case BrowserControlPhase.FAILED:
    case BrowserControlPhase.TIMED_OUT:
    case BrowserControlPhase.STALE:
    case BrowserControlPhase.CANCELLED:
      return true
    default:
      return false
  }
}
