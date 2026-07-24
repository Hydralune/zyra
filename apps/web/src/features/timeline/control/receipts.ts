import {
  RecoveryControlPhase,
  type NormalizedRecoveryControlRequest,
  type RecoveryControlObservation,
  type RecoveryControlOwnerEvidence,
  type RecoveryControlPhaseValue,
  type RecoveryControlReceipt,
} from "./contracts.ts"

function record(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? Object.freeze({ ...(value as Record<string, unknown>) })
    : Object.freeze({})
}

function records(value: unknown): readonly Readonly<Record<string, unknown>>[] {
  if (!Array.isArray(value)) return Object.freeze([])
  return Object.freeze(value.map(record).filter((item) => Object.keys(item).length))
}

function stringValue(
  value: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): string | undefined {
  for (const key of keys) {
    const candidate = value[key]
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim()
    }
    if (
      (typeof candidate === "number" || typeof candidate === "boolean") &&
      String(candidate)
    ) {
      return String(candidate)
    }
  }
  return undefined
}

function numberValue(
  value: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): number | undefined {
  for (const key of keys) {
    const candidate = Number(value[key])
    if (Number.isFinite(candidate)) return candidate
  }
  return undefined
}

function booleanValue(
  value: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): boolean | undefined {
  for (const key of keys) {
    const candidate = value[key]
    if (typeof candidate === "boolean") return candidate
    if (candidate === 1 || candidate === "true") return true
    if (candidate === 0 || candidate === "false") return false
  }
  return undefined
}

function strings(value: unknown): readonly string[] {
  if (!Array.isArray(value)) return Object.freeze([])
  return Object.freeze(
    [...new Set(value.map(String).map((item) => item.trim()).filter(Boolean))],
  )
}

function nested(
  value: Readonly<Record<string, unknown>>,
  ...path: readonly string[]
): Readonly<Record<string, unknown>> {
  let current = value
  for (const key of path) current = record(current[key])
  return current
}

function firstRecord(
  ...values: readonly unknown[]
): Readonly<Record<string, unknown>> {
  for (const value of values) {
    const candidate = record(value)
    if (Object.keys(candidate).length) return candidate
  }
  return Object.freeze({})
}

function statusTokens(
  response: Readonly<Record<string, unknown>>,
): readonly string[] {
  const commandResult = record(response.command_result)
  const result = record(commandResult.result)
  const data = record(commandResult.data)
  const error = firstRecord(
    commandResult.error,
    result.error,
    data.error,
    response.error,
  )
  const values = [
    stringValue(response, "status", "phase", "state"),
    stringValue(commandResult, "status", "phase", "state", "runtime_status"),
    stringValue(result, "status", "phase", "state"),
    stringValue(data, "status", "phase", "state"),
    stringValue(error, "code", "message", "error_code"),
    stringValue(response, "error", "message"),
  ]
  return Object.freeze(
    values
      .filter((value): value is string => Boolean(value))
      .flatMap((value) => value.toLocaleLowerCase().split(/[^a-z0-9_-]+/))
      .filter(Boolean),
  )
}

function containsAny(
  tokens: readonly string[],
  expected: readonly string[],
): boolean {
  return tokens.some((token) =>
    expected.some(
      (candidate) =>
        token === candidate ||
        token.startsWith(`${candidate}_`) ||
        token.startsWith(`${candidate}-`) ||
        token.includes(candidate),
    ),
  )
}

export function phaseFromControlResponse(
  responseValue: unknown,
): RecoveryControlPhaseValue {
  const response = record(responseValue)
  const commandResult = record(response.command_result)
  const result = record(commandResult.result)
  const data = record(commandResult.data)
  const error = firstRecord(
    commandResult.error,
    result.error,
    data.error,
    response.error,
  )
  const tokens = statusTokens(response)
  const ok =
    booleanValue(commandResult, "ok", "success", "accepted") ??
    booleanValue(result, "ok", "success", "accepted") ??
    booleanValue(data, "ok", "success", "accepted") ??
    booleanValue(response, "ok", "success", "accepted")
  const errorCode =
    stringValue(error, "code", "error_code") ??
    stringValue(commandResult, "error_code") ??
    stringValue(response, "error_code", "error")
  if (
    containsAny(tokens, [
      "denied",
      "forbidden",
      "permission_denied",
      "sealed",
      "not_authorized",
    ]) ||
    /permission|sealed|forbidden|denied/i.test(errorCode ?? "")
  ) {
    return RecoveryControlPhase.DENIED
  }
  if (
    containsAny(tokens, [
      "queued",
      "pending",
      "waiting",
      "created",
      "delivered",
      "running",
      "dispatched",
    ])
  ) {
    return RecoveryControlPhase.PENDING
  }
  if (
    containsAny(tokens, [
      "failed",
      "rejected",
      "conflict",
      "invalid",
      "unavailable",
      "cancelled",
      "canceled",
    ]) ||
    ok === false ||
    Boolean(errorCode)
  ) {
    return RecoveryControlPhase.FAILED
  }
  if (
    containsAny(tokens, [
      "applied",
      "succeeded",
      "completed",
      "committed",
      "success",
    ]) ||
    ok === true
  ) {
    return RecoveryControlPhase.APPLIED
  }
  return RecoveryControlPhase.PENDING
}

function ownerEvidenceEntry(
  value: Readonly<Record<string, unknown>>,
  fallbackOwner = "",
): RecoveryControlOwnerEvidence | undefined {
  if (!Object.keys(value).length) return undefined
  const canonicalRef = record(value.canonical_ref)
  const effect = record(value.effect)
  const cancellation = record(effect.cancellation)
  const after = firstRecord(value.after, effect.after, cancellation.after)
  const before = firstRecord(value.before, effect.before, cancellation.before)
  const worker = firstRecord(
    value.worker,
    effect.worker,
    after.worker,
    after.worker_pool,
  )
  const lease = firstRecord(
    value.lease,
    effect.lease,
    cancellation.lease,
    after.lease,
  )
  const binding = firstRecord(value.binding, effect.binding, after.binding)
  const owner =
    stringValue(value, "owner", "canonical_owner", "physical_owner") ??
    stringValue(effect, "owner", "canonical_owner", "physical_owner") ??
    fallbackOwner
  const operation =
    stringValue(value, "operation", "action", "kind") ??
    stringValue(effect, "operation", "action", "kind")
  const receiptId =
    stringValue(value, "receipt_id", "id", "command_id") ??
    stringValue(canonicalRef, "receipt_id", "lease_id", "event_id")
  const accepted =
    booleanValue(value, "accepted", "ok", "success") ??
    booleanValue(effect, "accepted", "ok", "success")
  const changed =
    booleanValue(value, "changed") ??
    booleanValue(effect, "changed") ??
    booleanValue(cancellation, "changed")
  const evidence: RecoveryControlOwnerEvidence = {
    owner: owner || "unknown-owner",
    operation,
    receiptId,
    accepted,
    changed,
    workerId:
      stringValue(value, "worker_id") ??
      stringValue(worker, "worker_id", "id") ??
      stringValue(binding, "worker_id") ??
      stringValue(canonicalRef, "worker_id"),
    leaseId:
      stringValue(value, "lease_id", "worker_lease_id") ??
      stringValue(lease, "lease_id", "id") ??
      stringValue(binding, "lease_id") ??
      stringValue(canonicalRef, "lease_id", "worker_lease_id"),
    attemptId:
      stringValue(value, "attempt_id") ??
      stringValue(binding, "attempt_id") ??
      stringValue(canonicalRef, "attempt_id"),
    nodeId:
      stringValue(value, "node_id") ??
      stringValue(canonicalRef, "node_id"),
    graphId:
      stringValue(value, "graph_id") ??
      stringValue(canonicalRef, "graph_id", "state_id"),
    checkpointId:
      stringValue(value, "checkpoint_id", "checkpoint_ref") ??
      stringValue(canonicalRef, "checkpoint_id", "checkpoint_ref"),
    revision:
      numberValue(value, "revision", "commit_revision", "version") ??
      numberValue(canonicalRef, "revision", "commit_revision", "version"),
    before,
    after,
    raw: value,
  }
  return Object.freeze(evidence)
}

function collectOwnerEvidence(
  response: Readonly<Record<string, unknown>>,
): readonly RecoveryControlOwnerEvidence[] {
  const commandResult = record(response.command_result)
  const result = record(commandResult.result)
  const data = record(commandResult.data)
  const candidates: Array<{
    value: Readonly<Record<string, unknown>>
    owner: string
  }> = []
  const add = (value: unknown, owner = "") => {
    const candidate = record(value)
    if (Object.keys(candidate).length) candidates.push({ value: candidate, owner })
  }
  add(data, stringValue(commandResult, "canonical_owner") ?? "")
  add(result, stringValue(commandResult, "canonical_owner") ?? "")
  add(response.event, "CanonicalEventStore")
  for (const control of records(data.controls)) add(control, "WorkerControlRuntime")
  for (const control of records(data.owner_receipts)) add(control)
  for (const control of records(result.owner_receipts)) add(control)
  const recovery = firstRecord(data.recovery, result.recovery, response.recovery)
  add(recovery.plan, "RecoveryDecisionRuntime")
  const execution = record(recovery.execution)
  add(execution.outcome, "RecoveryActionRuntime")
  for (const receipt of records(execution.receipts)) add(receipt)
  const taskRecovery = firstRecord(data.task_recovery, response.task_recovery)
  add(taskRecovery.last_mutation, "TaskState")
  const unique = new Map<string, RecoveryControlOwnerEvidence>()
  for (const candidate of candidates) {
    const evidence = ownerEvidenceEntry(candidate.value, candidate.owner)
    if (!evidence) continue
    const key = [
      evidence.owner,
      evidence.operation ?? "",
      evidence.receiptId ?? "",
      evidence.workerId ?? "",
      evidence.leaseId ?? "",
      evidence.checkpointId ?? "",
      evidence.revision ?? "",
    ].join("|")
    unique.set(key, evidence)
  }
  return Object.freeze([...unique.values()])
}

function collectEventIds(
  response: Readonly<Record<string, unknown>>,
): readonly string[] {
  const commandResult = record(response.command_result)
  const result = record(commandResult.result)
  const data = record(commandResult.data)
  const event = record(response.event)
  const controlEvent = firstRecord(data.control_event, result.control_event)
  const recovery = firstRecord(data.recovery, result.recovery)
  const receipts = records(recovery.event_receipts)
  return Object.freeze(
    [
      ...strings(commandResult.event_ids),
      ...strings(commandResult.observed_event_ids),
      ...strings(result.event_ids),
      ...strings(result.observed_event_ids),
      ...strings(data.event_ids),
      ...strings(data.observed_event_ids),
      ...strings(data.worker_event_ids),
      stringValue(event, "event_id", "id"),
      stringValue(controlEvent, "event_id", "id"),
      ...receipts.map((item) => stringValue(item, "event_id", "id")),
    ].filter((value): value is string => Boolean(value)),
  )
}

function responseSummary(
  response: Readonly<Record<string, unknown>>,
  request: NormalizedRecoveryControlRequest,
  phase: RecoveryControlPhaseValue,
): string {
  const commandResult = record(response.command_result)
  const result = record(commandResult.result)
  const data = record(commandResult.data)
  const error = firstRecord(
    commandResult.error,
    result.error,
    data.error,
    response.error,
  )
  const explicit =
    stringValue(commandResult, "summary", "display_text", "message") ??
    stringValue(result, "summary", "display_text", "message") ??
    stringValue(data, "summary", "display_text", "message") ??
    stringValue(response, "summary", "message") ??
    stringValue(error, "message")
  if (explicit) return explicit
  if (phase === RecoveryControlPhase.APPLIED) {
    return `${request.commandName} applied by the canonical runtime.`
  }
  if (phase === RecoveryControlPhase.DENIED) {
    return `${request.commandName} denied by the canonical permission policy.`
  }
  if (phase === RecoveryControlPhase.FAILED) {
    return `${request.commandName} failed before a canonical effect was observed.`
  }
  return `${request.commandName} is pending canonical evidence.`
}

function errorFields(
  response: Readonly<Record<string, unknown>>,
): { code?: string; message?: string } {
  const commandResult = record(response.command_result)
  const result = record(commandResult.result)
  const data = record(commandResult.data)
  const error = firstRecord(
    commandResult.error,
    result.error,
    data.error,
    response.error,
  )
  return {
    code:
      stringValue(error, "code", "error_code") ??
      stringValue(commandResult, "error_code") ??
      stringValue(result, "error_code") ??
      stringValue(data, "error_code") ??
      stringValue(response, "error_code", "error"),
    message:
      stringValue(error, "message", "error_message") ??
      stringValue(commandResult, "error_message") ??
      stringValue(result, "error_message") ??
      stringValue(data, "error_message") ??
      stringValue(response, "message"),
  }
}

function observedRevision(
  response: Readonly<Record<string, unknown>>,
  evidence: readonly RecoveryControlOwnerEvidence[],
): number | undefined {
  const commandResult = record(response.command_result)
  const result = record(commandResult.result)
  const data = record(commandResult.data)
  const revisions = [
    numberValue(commandResult, "revision_after", "revision", "commit_revision"),
    numberValue(result, "revision_after", "revision", "commit_revision"),
    numberValue(data, "revision_after", "revision", "commit_revision"),
    numberValue(response, "revision_after", "revision", "commit_revision"),
    ...evidence.map((item) => item.revision),
  ].filter((value): value is number => value !== undefined)
  return revisions.length ? Math.max(...revisions) : undefined
}

export function controlObservationFromResponse(
  request: NormalizedRecoveryControlRequest,
  responseValue: unknown,
  now: number,
): RecoveryControlObservation {
  const response = record(responseValue)
  const phase = phaseFromControlResponse(response)
  const ownerEvidence = collectOwnerEvidence(response)
  const eventIds = collectEventIds(response)
  const revision = observedRevision(response, ownerEvidence)
  const conflict = ownerEvidence.find(
    (item) =>
      item.accepted === false ||
      (request.owner.workerId &&
        item.workerId &&
        item.workerId !== request.owner.workerId &&
        request.action !== "reassign"),
  )
  return Object.freeze({
    phase: conflict ? RecoveryControlPhase.FAILED : phase,
    observedAt: now,
    observedRevision: revision,
    eventIds,
    rowKeys: Object.freeze([]),
    ownerEvidence,
    summary: conflict
      ? `Canonical owner evidence conflicts with ${request.commandName}.`
      : responseSummary(response, request, phase),
    source:
      phase === RecoveryControlPhase.DENIED && request.sealed
        ? "sealed-policy"
        : "response",
    terminal:
      phase === RecoveryControlPhase.APPLIED ||
      phase === RecoveryControlPhase.DENIED ||
      phase === RecoveryControlPhase.FAILED,
    consistent: !conflict,
    conflictReason: conflict
      ? `${conflict.owner}:${conflict.operation ?? "unknown"} rejected or changed ownership`
      : undefined,
  })
}

export function initialControlReceipt(
  request: NormalizedRecoveryControlRequest,
  now: number,
): RecoveryControlReceipt {
  return Object.freeze({
    id: `control-receipt:${request.commandId}`,
    action: request.action,
    phase: RecoveryControlPhase.SUBMITTING,
    taskId: request.taskId,
    runId: request.runId,
    actorId: request.actorId,
    targetKind: request.targetKind,
    targetId: request.targetId,
    requestId: request.requestId,
    commandId: request.commandId,
    commandName: request.commandName,
    commandText: request.commandText,
    requestDigest: request.requestDigest,
    idempotencyKey: request.idempotencyKey,
    expectedOwner: request.owner,
    submittedAt: now,
    updatedAt: now,
    deadlineAt: now + request.timeoutMs,
    attempt: 1,
    summary: `Submitting ${request.commandName} to the canonical control runtime.`,
    denied: false,
    sealed: request.sealed,
    interventionCounted: false,
    noHumanWait: request.sealed,
    replayed: false,
    timedOut: false,
    detached: false,
    observedEventIds: Object.freeze([]),
    observedRowKeys: Object.freeze([]),
    ownerEvidence: Object.freeze([]),
    observations: Object.freeze([]),
    metadata: Object.freeze({
      targetKind: request.targetKind,
      timeoutMs: request.timeoutMs,
      controlSource: "timeline",
      ...(request.metadata ?? {}),
    }),
  })
}

export function receiptFromControlResponse(
  request: NormalizedRecoveryControlRequest,
  previous: RecoveryControlReceipt,
  responseValue: unknown,
  now: number,
): RecoveryControlReceipt {
  const response = record(responseValue)
  const observation = controlObservationFromResponse(request, response, now)
  const commandResult = record(response.command_result)
  const data = record(commandResult.data)
  const interventionCounted =
    booleanValue(response, "intervention_counted") ??
    booleanValue(commandResult, "intervention_counted") ??
    booleanValue(data, "intervention_counted") ??
    (observation.phase === RecoveryControlPhase.DENIED && request.sealed)
  const error = errorFields(response)
  return Object.freeze({
    ...previous,
    phase: observation.phase,
    updatedAt: now,
    summary: observation.summary,
    denied: observation.phase === RecoveryControlPhase.DENIED,
    interventionCounted,
    operatorInterventionAttemptCount:
      numberValue(response, "operator_intervention_attempt_count") ??
      numberValue(commandResult, "operator_intervention_attempt_count") ??
      numberValue(data, "operator_intervention_attempt_count"),
    humanInterventionCount:
      numberValue(response, "human_intervention_count") ??
      numberValue(commandResult, "human_intervention_count") ??
      numberValue(data, "human_intervention_count"),
    noHumanWait:
      request.sealed ||
      booleanValue(data, "no_human_wait") === true ||
      observation.phase !== RecoveryControlPhase.PENDING,
    replayed:
      booleanValue(response, "receipt_replayed", "replayed") ??
      booleanValue(commandResult, "replayed") ??
      booleanValue(data, "replayed") ??
      false,
    errorCode: error.code,
    errorMessage: error.message,
    observedRevision: observation.observedRevision,
    observedEventIds: observation.eventIds,
    observedRowKeys: observation.rowKeys,
    ownerEvidence: observation.ownerEvidence,
    observations: Object.freeze([
      ...previous.observations,
      observation,
    ]),
    response,
  })
}

export function failedControlReceipt(
  previous: RecoveryControlReceipt,
  error: unknown,
  now: number,
): RecoveryControlReceipt {
  const value =
    error instanceof Error
      ? error
      : new Error(typeof error === "string" ? error : JSON.stringify(error))
  const code =
    "code" in value && typeof (value as { code?: unknown }).code === "string"
      ? String((value as { code: string }).code)
      : "control_transport_failed"
  const denied = /permission|denied|forbidden|sealed/i.test(
    `${code} ${value.message}`,
  )
  const observation: RecoveryControlObservation = Object.freeze({
    phase: denied
      ? RecoveryControlPhase.DENIED
      : RecoveryControlPhase.FAILED,
    observedAt: now,
    eventIds: Object.freeze([]),
    rowKeys: Object.freeze([]),
    ownerEvidence: Object.freeze([]),
    summary: denied
      ? `${previous.commandName} was denied by the backend policy owner.`
      : `${previous.commandName} failed before a canonical receipt was returned.`,
    source: "transport",
    terminal: true,
    consistent: true,
  })
  return Object.freeze({
    ...previous,
    phase: observation.phase,
    updatedAt: now,
    summary: observation.summary,
    denied,
    interventionCounted: denied && previous.sealed,
    noHumanWait: previous.sealed || denied,
    errorCode: code,
    errorMessage: value.message,
    observations: Object.freeze([
      ...previous.observations,
      observation,
    ]),
  })
}

export function timedOutControlReceipt(
  previous: RecoveryControlReceipt,
  now: number,
): RecoveryControlReceipt {
  const observation: RecoveryControlObservation = Object.freeze({
    phase: RecoveryControlPhase.TIMED_OUT,
    observedAt: now,
    eventIds: Object.freeze([]),
    rowKeys: Object.freeze([]),
    ownerEvidence: Object.freeze([]),
    summary:
      `${previous.commandName} response timed out; canonical receipt ` +
      "observation continues and the durable request will not be replayed.",
    source: "timeout",
    terminal: false,
    consistent: true,
  })
  return Object.freeze({
    ...previous,
    phase: RecoveryControlPhase.TIMED_OUT,
    updatedAt: now,
    timedOut: true,
    summary: observation.summary,
    errorCode: "control_response_timeout",
    errorMessage:
      "Transport deadline expired while canonical outcome remains unknown.",
    observations: Object.freeze([
      ...previous.observations,
      observation,
    ]),
  })
}
