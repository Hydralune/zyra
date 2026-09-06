import {
  COMMAND_RECEIPT_PROTOCOL,
  type CommandErrorReceipt,
  type CommandName,
  type CommandReceipt,
  type CommandReceiptPhase,
  type CommandTransportRequest,
  type CommandUsageReceipt,
} from "./contracts.ts"

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function numberValue(value: unknown, fallback = 0): number {
  const parsed = typeof value === "number" ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function booleanValue(value: unknown): boolean {
  if (typeof value === "boolean") return value
  if (typeof value === "number") return value !== 0
  return ["true", "yes", "1"].includes(String(value).toLowerCase())
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map(stringValue)
    .filter(Boolean)
}

function phaseValue(value: unknown): CommandReceiptPhase {
  const normalized = String(value ?? "").trim().toLowerCase()
  if (["received"].includes(normalized)) return "received"
  if (["validated", "admitted"].includes(normalized)) return "validated"
  if (["queued", "reserved"].includes(normalized)) return "queued"
  if (["dispatched", "running", "started"].includes(normalized)) return "running"
  if (["succeeded", "success", "completed", "committed", "applied"].includes(normalized)) {
    return "applied"
  }
  if (["cancelled", "canceled"].includes(normalized)) return "cancelled"
  if (["expired", "timeout", "timed_out"].includes(normalized)) return "expired"
  return "rejected"
}

function commandName(value: unknown, fallback: CommandName): CommandName {
  const normalized = String(value ?? "").trim().toLowerCase()
  const allowed = new Set<CommandName>([
    "/status",
    "/graph",
    "/trace",
    "/artifacts",
    "/permissions",
    "/btw",
    "/inject",
    "/change",
    "/verify",
    "/eval",
    "/doctor",
  ])
  return allowed.has(normalized as CommandName)
    ? normalized as CommandName
    : fallback
}

function errorReceipt(value: unknown): CommandErrorReceipt | undefined {
  const source = record(value)
  const code = stringValue(source.code)
  const message = stringValue(source.message)
  if (!code && !message) return undefined
  return {
    code: code || "command_rejected",
    message: message || "Command was rejected.",
    retryable: booleanValue(source.retryable),
    details: Object.freeze(record(source.details)),
  }
}

function usageReceipt(value: unknown): CommandUsageReceipt | undefined {
  const source = record(value)
  if (!Object.keys(source).length) return undefined
  const inputTokens = numberValue(
    source.input_tokens ?? source.inputTokens,
  )
  const outputTokens = numberValue(
    source.output_tokens ?? source.outputTokens,
  )
  const cachedTokens = numberValue(
    source.cached_tokens ?? source.cachedTokens,
  )
  const costUsd = numberValue(source.cost_usd ?? source.costUsd, Number.NaN)
  const durationMs = numberValue(
    source.duration_ms ?? source.durationMs,
    Number.NaN,
  )
  return {
    inputTokens,
    outputTokens,
    cachedTokens,
    costUsd: Number.isFinite(costUsd) ? costUsd : undefined,
    durationMs: Number.isFinite(durationMs) ? durationMs : undefined,
  }
}

export function admitCommandReceipt(
  rawValue: unknown,
  request: CommandTransportRequest,
): CommandReceipt {
  const raw = record(rawValue)
  const requestRaw = record(
    raw.control_request ?? raw.controlRequest,
  )
  const result = record(
    raw.command_result ?? raw.commandResult,
  )
  const nestedResult = record(result.result)
  const data = record(
    nestedResult.data ?? result.data,
  )
  const metadata = record(result.metadata)
  const event = record(raw.event)
  const error = errorReceipt(result.error ?? (raw.ok === false ? {
    code: raw.error,
    message: raw.message,
    details: raw.detail,
  } : undefined))
  const requestId = stringValue(
    result.request_id ??
    result.requestId ??
    requestRaw.request_id ??
    requestRaw.requestId,
  ) || request.requestId
  const commandId = stringValue(
    result.command_id ??
    result.commandId ??
    requestRaw.command_id ??
    requestRaw.commandId,
  ) || request.commandId
  if (requestId !== request.requestId) {
    throw new TypeError(
      `Command receipt request mismatch: expected ${request.requestId}, received ${requestId}.`,
    )
  }
  if (commandId !== request.commandId) {
    throw new TypeError(
      `Command receipt identity mismatch: expected ${request.commandId}, received ${commandId}.`,
    )
  }
  const expectedName = request.text.split(/\s+/, 1)[0] as CommandName
  const name = commandName(
    result.name ??
    requestRaw.canonical_name ??
    requestRaw.canonicalName,
    expectedName,
  )
  if (name !== expectedName) {
    throw new TypeError(
      `Command receipt name mismatch: expected ${expectedName}, received ${name}.`,
    )
  }
  const phase = phaseValue(result.status ?? metadata.status)
  const queue = record(
    nestedResult.data && record(nestedResult.data).queue,
  )
  const queueId = stringValue(
    nestedResult.followup_queue_id ??
    nestedResult.followupQueueId ??
    result.queue_id ??
    result.queueId ??
    queue.queue_id ??
    queue.queueId,
  )
  const eventIds = new Set<string>([
    ...strings(result.event_ids ?? result.eventIds),
    ...strings(data.event_ids ?? data.eventIds),
  ])
  const eventId = stringValue(event.event_id ?? event.eventId)
  if (eventId) eventIds.add(eventId)
  const summary = stringValue(
    result.summary ??
    nestedResult.display_text ??
    nestedResult.displayText,
  )
  const displayText = stringValue(
    nestedResult.display_text ??
    nestedResult.displayText ??
    result.summary,
  )
  const createdAt = stringValue(
    requestRaw.created_at ??
    requestRaw.createdAt,
  ) || new Date().toISOString()
  const finishedAt = stringValue(
    result.finished_at ??
    result.finishedAt,
  )
  const durable = metadata.durable === undefined
    ? true
    : booleanValue(metadata.durable)
  const executed = metadata.executed === undefined
    ? phase === "applied"
    : booleanValue(metadata.executed)
  if (phase === "applied" && error) {
    throw new TypeError(
      "Applied command receipt cannot contain an error.",
    )
  }
  if (phase === "queued" && !queueId) {
    throw new TypeError(
      "Queued command receipt is missing a backend queue identity.",
    )
  }
  if (
    name === "/btw" &&
    phase === "applied" &&
    booleanValue(
      data.tools_used ??
      data.tool_used ??
      metadata.tools_used,
    )
  ) {
    throw new TypeError(
      "Side-question receipt reports forbidden tool use.",
    )
  }
  return Object.freeze({
    schema: COMMAND_RECEIPT_PROTOCOL,
    requestId,
    commandId,
    queueId: queueId || undefined,
    taskId: request.taskId,
    runId: request.runId,
    sessionId: request.sessionId,
    name,
    scope: stringValue(
      request.arguments.scope ??
      request.arguments.target ??
      request.arguments.node,
    ) || "task",
    mode: request.deliveryMode,
    priority: request.priority,
    idempotencyKey: request.idempotencyKey,
    retryOf:
      stringValue(
        requestRaw.retry_of_request_id ??
        requestRaw.retryOfRequestId ??
        record(requestRaw.metadata).retry_of_request_id,
      ) ||
      request.retryOf ||
      undefined,
    phase,
    summary,
    displayText,
    data: Object.freeze(data),
    error,
    usage: usageReceipt(nestedResult.usage ?? result.usage),
    eventIds: Object.freeze([...eventIds]),
    checkpointRef: stringValue(
      nestedResult.checkpoint_ref ??
      nestedResult.checkpointRef,
    ) || undefined,
    revisionBefore:
      result.revision_before === undefined ?
        undefined
      : numberValue(result.revision_before),
    revisionAfter:
      result.revision_after === undefined ?
        undefined
      : numberValue(result.revision_after),
    replayed:
      booleanValue(raw.receipt_replayed) ||
      booleanValue(metadata.replayed),
    durable,
    executed,
    interventionCounted: booleanValue(
      result.intervention_counted ??
      raw.intervention_counted,
    ),
    humanInterventionCount: numberValue(
      result.human_intervention_count ??
      raw.human_intervention_count,
    ),
    operatorInterventionAttemptCount: numberValue(
      result.operator_intervention_attempt_count ??
      raw.operator_intervention_attempt_count,
    ),
    createdAt,
    finishedAt: finishedAt || undefined,
    raw: Object.freeze(raw),
  })
}

export function commandReceiptTerminal(receipt: CommandReceipt): boolean {
  return [
    "applied",
    "rejected",
    "expired",
    "cancelled",
  ].includes(receipt.phase)
}

export function commandReceiptRetryable(receipt: CommandReceipt): boolean {
  if (receipt.phase === "expired") return true
  if (receipt.phase === "cancelled") return true
  return receipt.phase === "rejected" && receipt.error?.retryable === true
}

export function assertSideQuestionReceipt(
  receipt: CommandReceipt,
): CommandReceipt {
  if (receipt.name !== "/btw") {
    throw new TypeError("Receipt is not a side-question receipt.")
  }
  if (receipt.phase === "applied") {
    const data = receipt.data
    if (
      booleanValue(data.tools_used) ||
      booleanValue(data.main_session_mutated) ||
      booleanValue(data.message_appended)
    ) {
      throw new TypeError(
        "Side-question receipt violates isolation.",
      )
    }
  }
  return receipt
}
