import type {
  CausalEventProjection,
  MutationProjection,
} from "../../../state/contracts.ts"
import {
  BrowserStepPhase,
  booleanValue,
  firstDefined,
  integerValue,
  isRecord,
  optionalRecord,
  optionalString,
  stableDigest,
  stringArray,
  type BrowserAction,
  type BrowserActionResult,
  type BrowserDomSummary,
  type BrowserScope,
  type BrowserStep,
  type BrowserStepPhaseValue,
  type BrowserToolReceipt,
} from "../contracts.ts"
import {
  deepString,
  deepStrings,
  firstDeep,
  valuesDeep,
  type BrowserRecord,
  type BrowserSpanRecord,
} from "./reader.ts"

interface ActionDraft {
  actionId: string
  stepId: string
  browserSessionId: string
  workerRequestId: string
  name: string
  arguments: Record<string, unknown>
  argumentDigest?: string
  targetId?: string
  frameId?: string
  toolCallId?: string
  spanId?: string
  correlationId?: string
  causationId?: string
  permissionRequestId?: string
  permissionDecision?: string
  sequence: number
  startedAt?: string
  status: BrowserStepPhaseValue
  retryable: boolean
  sourceEventIds: Set<string>
  sourceRecordIds: Set<string>
}

interface ResultDraft {
  resultId: string
  actionId?: string
  stepId: string
  browserSessionId: string
  toolCallId?: string
  spanId?: string
  correlationId?: string
  causationId?: string
  ok: boolean
  status: BrowserStepPhaseValue
  summary: string
  extractedContent?: string
  errorCode?: string
  errorMessage?: string
  retryable: boolean
  artifactIds: Set<string>
  mutationIds: Set<string>
  sequence: number
  completedAt?: string
  sourceEventIds: Set<string>
  sourceRecordIds: Set<string>
}

interface StepDraft {
  stepId: string
  browserSessionId: string
  workerRequestId: string
  status: BrowserStepPhaseValue
  url?: string
  title?: string
  targetId?: string
  frameId?: string
  actionIds: Set<string>
  resultIds: Set<string>
  screenshotArtifactIds: Set<string>
  downloadArtifactIds: Set<string>
  toolReceiptIds: Set<string>
  mutationIds: Set<string>
  eventIds: Set<string>
  recordIds: Set<string>
  startSequence: number
  endSequence: number
  startedAt?: string
  completedAt?: string
  retryable: boolean
  partial: boolean
  errorCode?: string
  errorMessage?: string
}

export interface BrowserStepProjection {
  steps: readonly BrowserStep[]
  actions: readonly BrowserAction[]
  results: readonly BrowserActionResult[]
  toolReceipts: readonly BrowserToolReceipt[]
  actionById: ReadonlyMap<string, BrowserAction>
  resultById: ReadonlyMap<string, BrowserActionResult>
  stepById: ReadonlyMap<string, BrowserStep>
  actionIdByRecordId: ReadonlyMap<string, string>
  resultIdByRecordId: ReadonlyMap<string, string>
  stepIdByRecordId: ReadonlyMap<string, string>
  findings: readonly string[]
}

const ACTION_KIND = /(?:action|tool_call|command|navigate|click|input|scroll|extract|screenshot|download)/
const RESULT_KIND = /(?:result|outcome|observation|completed|failed|error|receipt)/
const STATE_KIND = /(?:state|snapshot|dom|accessibility|target|frame|page)/

function normalizedPhase(
  value: unknown,
  fallback: BrowserStepPhaseValue = BrowserStepPhase.PARTIAL,
): BrowserStepPhaseValue {
  const raw = String(value ?? "").trim().toLowerCase().replaceAll("-", "_")
  if (
    raw.includes("permission")
    || raw.includes("ask")
    || raw.includes("pending_approval")
  ) return BrowserStepPhase.WAITING_PERMISSION
  if (
    raw.includes("complete")
    || raw === "ok"
    || raw === "success"
    || raw === "succeeded"
    || raw === "applied"
    || raw === "observed"
  ) return BrowserStepPhase.COMPLETED
  if (
    raw.includes("fail")
    || raw.includes("error")
    || raw.includes("denied")
    || raw.includes("reject")
    || raw === "unhealthy"
  ) return BrowserStepPhase.FAILED
  if (
    raw.includes("cancel")
    || raw.includes("abort")
    || raw.includes("stop")
  ) return BrowserStepPhase.CANCELLED
  if (
    raw.includes("running")
    || raw.includes("started")
    || raw.includes("executing")
    || raw.includes("active")
  ) return BrowserStepPhase.RUNNING
  if (
    raw.includes("queued")
    || raw.includes("admitted")
    || raw.includes("requested")
  ) return BrowserStepPhase.QUEUED
  if (raw.includes("partial")) return BrowserStepPhase.PARTIAL
  return fallback
}

function phaseRank(phase: BrowserStepPhaseValue): number {
  switch (phase) {
    case BrowserStepPhase.FAILED:
      return 7
    case BrowserStepPhase.CANCELLED:
      return 6
    case BrowserStepPhase.WAITING_PERMISSION:
      return 5
    case BrowserStepPhase.COMPLETED:
      return 4
    case BrowserStepPhase.RUNNING:
      return 3
    case BrowserStepPhase.QUEUED:
      return 2
    default:
      return 1
  }
}

function mergePhase(
  current: BrowserStepPhaseValue,
  candidate: BrowserStepPhaseValue,
): BrowserStepPhaseValue {
  if (
    current === BrowserStepPhase.COMPLETED
    && candidate === BrowserStepPhase.RUNNING
  ) return current
  if (
    current === BrowserStepPhase.FAILED
    && candidate === BrowserStepPhase.COMPLETED
  ) return current
  return phaseRank(candidate) >= phaseRank(current) ? candidate : current
}

function actionPayloads(record: BrowserRecord): readonly Readonly<Record<string, unknown>>[] {
  const output: Readonly<Record<string, unknown>>[] = []
  const seen = new Set<string>()
  const accept = (value: unknown) => {
    if (!isRecord(value)) return
    const name = optionalString(
      firstDefined(
        value,
        "action",
        "action_name",
        "actionName",
        "tool_name",
        "toolName",
        "name",
      ),
      "$.action.name",
      512,
    )
    const actionId = optionalString(
      firstDefined(value, "action_id", "actionId", "tool_call_id", "toolCallId"),
      "$.action.action_id",
      512,
    )
    if (!name && !actionId) return
    const key = actionId ?? stableDigest(value)
    if (seen.has(key)) return
    seen.add(key)
    output.push(value)
  }
  accept(record.payload.action)
  accept(record.payload.browser_action)
  accept(record.payload.tool_call)
  accept(record.payload.command)
  for (const key of ["actions", "model_actions", "action_requests", "tool_calls"]) {
    const value = record.payload[key]
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 1_000)) accept(item)
    }
  }
  for (const candidate of valuesDeep(
    record.payload,
    ["action", "browser_action", "tool_call"],
    6,
    2_000,
  )) {
    if (Array.isArray(candidate)) {
      for (const item of candidate.slice(0, 1_000)) accept(item)
    } else {
      accept(candidate)
    }
  }
  if (output.length === 0 && ACTION_KIND.test(record.kind)) {
    accept(record.payload)
  }
  return Object.freeze(output)
}

function resultPayloads(record: BrowserRecord): readonly Readonly<Record<string, unknown>>[] {
  const output: Readonly<Record<string, unknown>>[] = []
  const seen = new Set<string>()
  const accept = (value: unknown) => {
    if (!isRecord(value)) return
    const status = firstDefined(
      value,
      "status",
      "phase",
      "ok",
      "success",
      "error",
      "error_code",
    )
    const resultId = optionalString(
      firstDefined(value, "result_id", "resultId", "receipt_id", "receiptId", "id"),
      "$.result.result_id",
      512,
    )
    if (status === undefined && !resultId) return
    const key = resultId ?? stableDigest(value)
    if (seen.has(key)) return
    seen.add(key)
    output.push(value)
  }
  accept(record.payload.result)
  accept(record.payload.browser_result)
  accept(record.payload.action_result)
  accept(record.payload.tool_result)
  accept(record.payload.receipt)
  for (const key of ["results", "action_results", "tool_results", "receipts"]) {
    const value = record.payload[key]
    if (Array.isArray(value)) {
      for (const item of value.slice(0, 1_000)) accept(item)
    }
  }
  for (const candidate of valuesDeep(
    record.payload,
    ["result", "browser_result", "action_result", "tool_result", "receipt"],
    6,
    2_000,
  )) {
    if (Array.isArray(candidate)) {
      for (const item of candidate.slice(0, 1_000)) accept(item)
    } else {
      accept(candidate)
    }
  }
  if (output.length === 0 && RESULT_KIND.test(record.kind)) {
    accept(record.payload)
  }
  return Object.freeze(output)
}

function actionName(
  candidate: Readonly<Record<string, unknown>>,
  record: BrowserRecord,
): string {
  const direct = optionalString(
    firstDefined(
      candidate,
      "action",
      "action_name",
      "actionName",
      "tool_name",
      "toolName",
      "name",
      "method",
    ),
    "$.action.name",
    512,
  )
  if (direct) return direct.toLowerCase().replaceAll("-", "_")
  const parts = record.kind.split("_").filter(Boolean)
  const ignored = new Set([
    "browser",
    "action",
    "requested",
    "started",
    "completed",
    "event",
    "record",
  ])
  return parts.filter((part) => !ignored.has(part)).join("_") || "browser_action"
}

function actionArguments(
  candidate: Readonly<Record<string, unknown>>,
): Record<string, unknown> {
  const direct =
    optionalRecord(
      firstDefined(
        candidate,
        "arguments",
        "args",
        "parameters",
        "params",
        "input",
      ),
    )
    ?? {}
  const output: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(direct).slice(0, 256)) {
    if (/(?:secret|token|password|authorization|cookie|credential)/i.test(key)) {
      output[key] = "[redacted]"
      continue
    }
    if (typeof value === "string") {
      output[key] = value.slice(0, 64 * 1024)
    } else if (
      value === null
      || typeof value === "number"
      || typeof value === "boolean"
    ) {
      output[key] = value
    } else if (Array.isArray(value)) {
      output[key] = value.slice(0, 1_000)
    } else if (isRecord(value)) {
      output[key] = Object.fromEntries(Object.entries(value).slice(0, 256))
    }
  }
  return output
}

function recordStepId(
  record: BrowserRecord,
  candidate?: Readonly<Record<string, unknown>>,
): string | undefined {
  const value = firstDefined(
    candidate ?? {},
    "browser_step_id",
    "browserStepId",
    "step_id",
    "stepId",
  ) ?? firstDeep(record.payload, [
    "browser_step_id",
    "browserStepId",
    "step_id",
    "stepId",
  ])
  return optionalString(value, "$.step_id", 512)
}

function actionIdentity(
  candidate: Readonly<Record<string, unknown>>,
  record: BrowserRecord,
  index: number,
): string {
  return (
    optionalString(
      firstDefined(
        candidate,
        "action_id",
        "actionId",
        "tool_call_id",
        "toolCallId",
        "id",
      ),
      "$.action.action_id",
      512,
    )
    ?? `browser-action:${stableDigest({
      session: record.scope.browserSessionId,
      worker: record.scope.workerRequestId,
      record: record.recordId,
      sequence: record.sequence,
      index,
      name: actionName(candidate, record),
      arguments: actionArguments(candidate),
    })}`
  )
}

function inferredStepId(
  record: BrowserRecord,
  candidate: Readonly<Record<string, unknown>>,
  actionId?: string,
): string {
  const explicit = recordStepId(record, candidate)
  if (explicit) return explicit
  const spanId = optionalString(
    firstDefined(candidate, "span_id", "spanId")
      ?? firstDeep(record.payload, ["span_id", "spanId"]),
    "$.span_id",
    512,
  )
  if (spanId) return `browser-step:span:${spanId}`
  const toolCallId = optionalString(
    firstDefined(candidate, "tool_call_id", "toolCallId")
      ?? firstDeep(record.payload, ["tool_call_id", "toolCallId"]),
    "$.tool_call_id",
    512,
  )
  if (toolCallId) return `browser-step:tool:${toolCallId}`
  const correlationId = optionalString(
    firstDefined(candidate, "correlation_id", "correlationId")
      ?? firstDeep(record.payload, ["correlation_id", "correlationId"]),
    "$.correlation_id",
    512,
  )
  if (correlationId) {
    return `browser-step:correlation:${correlationId}:${Math.floor(record.sequence / 16)}`
  }
  if (actionId) return `browser-step:action:${actionId}`
  return `browser-step:record:${record.recordId}`
}

function phaseFromCandidate(
  candidate: Readonly<Record<string, unknown>>,
  record: BrowserRecord,
  fallback: BrowserStepPhaseValue,
): BrowserStepPhaseValue {
  const error = firstDefined(candidate, "error", "error_code", "errorCode")
  if (error) return BrowserStepPhase.FAILED
  if (
    firstDefined(candidate, "ok", "success") === true
    && RESULT_KIND.test(record.kind)
  ) return BrowserStepPhase.COMPLETED
  return normalizedPhase(
    firstDefined(candidate, "status", "phase", "state") ?? record.kind,
    fallback,
  )
}

function eventMatchesScope(
  event: CausalEventProjection,
  scope: BrowserScope,
): boolean {
  return (
    event.taskId === scope.taskId
    && event.runId === scope.runId
    && (!event.sessionId
      || event.sessionId === scope.browserSessionId
      || event.sessionId === scope.canonicalSessionId)
  )
}

function relatedEvents(
  scope: BrowserScope,
  events: readonly CausalEventProjection[],
  input: {
    toolCallId?: string
    spanId?: string
    correlationId?: string
    causationId?: string
    actionId?: string
    record?: BrowserRecord
  },
): CausalEventProjection[] {
  const output: CausalEventProjection[] = []
  for (const event of events) {
    if (!eventMatchesScope(event, scope)) continue
    const related =
      Boolean(input.toolCallId && event.toolCallId === input.toolCallId)
      || Boolean(input.spanId && event.spanId === input.spanId)
      || Boolean(input.spanId && event.parentSpanId === input.spanId)
      || Boolean(
        input.correlationId && event.correlationId === input.correlationId,
      )
      || Boolean(input.causationId && event.causationId === input.causationId)
      || Boolean(
        input.actionId
        && event.entityRefs.some((ref) => ref.includes(input.actionId!)),
      )
      || Boolean(
        input.record
        && event.sequence >= input.record.sequence - 2
        && event.sequence <= input.record.sequence + 4
        && /browser|tool|artifact|permission|control/i.test(event.eventType),
      )
    if (related) output.push(event)
  }
  return output.sort(
    (left, right) =>
      left.sequence - right.sequence
      || left.eventId.localeCompare(right.eventId),
  )
}

function actionDraft(
  candidate: Readonly<Record<string, unknown>>,
  record: BrowserRecord,
  index: number,
  events: readonly CausalEventProjection[],
): ActionDraft {
  const actionId = actionIdentity(candidate, record, index)
  const stepId = inferredStepId(record, candidate, actionId)
  const toolCallId = optionalString(
    firstDefined(candidate, "tool_call_id", "toolCallId")
      ?? firstDeep(record.payload, ["tool_call_id", "toolCallId"]),
    "$.action.tool_call_id",
    512,
  )
  const spanId = optionalString(
    firstDefined(candidate, "span_id", "spanId")
      ?? firstDeep(record.payload, ["span_id", "spanId"]),
    "$.action.span_id",
    512,
  )
  const correlationId = optionalString(
    firstDefined(candidate, "correlation_id", "correlationId")
      ?? firstDeep(record.payload, ["correlation_id", "correlationId"]),
    "$.action.correlation_id",
    512,
  )
  const causationId = optionalString(
    firstDefined(candidate, "causation_id", "causationId")
      ?? firstDeep(record.payload, ["causation_id", "causationId"]),
    "$.action.causation_id",
    512,
  )
  const related = relatedEvents(record.scope, events, {
    toolCallId,
    spanId,
    correlationId,
    causationId,
    actionId,
    record,
  })
  const args = actionArguments(candidate)
  return {
    actionId,
    stepId,
    browserSessionId: record.scope.browserSessionId,
    workerRequestId: record.scope.workerRequestId,
    name: actionName(candidate, record),
    arguments: args,
    argumentDigest:
      optionalString(
        firstDefined(candidate, "argument_digest", "arguments_digest", "input_digest"),
        "$.action.argument_digest",
        512,
      ) ?? stableDigest(args),
    targetId: optionalString(
      firstDefined(candidate, "target_id", "targetId", "tab_id"),
      "$.action.target_id",
      512,
    ),
    frameId: optionalString(
      firstDefined(candidate, "frame_id", "frameId"),
      "$.action.frame_id",
      512,
    ),
    toolCallId,
    spanId,
    correlationId,
    causationId,
    permissionRequestId: optionalString(
      firstDefined(
        candidate,
        "permission_request_id",
        "permissionRequestId",
      ),
      "$.action.permission_request_id",
      512,
    ),
    permissionDecision: optionalString(
      firstDefined(
        candidate,
        "permission_decision",
        "permissionDecision",
        "permission_effect",
      ),
      "$.action.permission_decision",
      128,
    ),
    sequence: record.sequence,
    startedAt:
      optionalString(
        firstDefined(candidate, "started_at", "startedAt", "created_at"),
        "$.action.started_at",
        512,
      ) ?? record.createdAt,
    status: phaseFromCandidate(candidate, record, BrowserStepPhase.RUNNING),
    retryable: booleanValue(
      firstDefined(candidate, "retryable", "can_retry"),
      true,
    ),
    sourceEventIds: new Set(related.map((event) => event.eventId)),
    sourceRecordIds: new Set([record.recordId]),
  }
}

function mergeAction(left: ActionDraft, right: ActionDraft): ActionDraft {
  if (left.browserSessionId !== right.browserSessionId) return left
  return {
    actionId: left.actionId,
    stepId: left.stepId || right.stepId,
    browserSessionId: left.browserSessionId,
    workerRequestId: left.workerRequestId || right.workerRequestId,
    name: left.name === "browser_action" ? right.name : left.name,
    arguments:
      Object.keys(right.arguments).length >= Object.keys(left.arguments).length
        ? { ...right.arguments }
        : { ...left.arguments },
    argumentDigest: right.argumentDigest ?? left.argumentDigest,
    targetId: right.targetId ?? left.targetId,
    frameId: right.frameId ?? left.frameId,
    toolCallId: right.toolCallId ?? left.toolCallId,
    spanId: right.spanId ?? left.spanId,
    correlationId: right.correlationId ?? left.correlationId,
    causationId: right.causationId ?? left.causationId,
    permissionRequestId:
      right.permissionRequestId ?? left.permissionRequestId,
    permissionDecision:
      right.permissionDecision ?? left.permissionDecision,
    sequence: Math.min(left.sequence, right.sequence),
    startedAt: left.startedAt ?? right.startedAt,
    status: mergePhase(left.status, right.status),
    retryable: left.retryable && right.retryable,
    sourceEventIds: new Set([
      ...left.sourceEventIds,
      ...right.sourceEventIds,
    ]),
    sourceRecordIds: new Set([
      ...left.sourceRecordIds,
      ...right.sourceRecordIds,
    ]),
  }
}

function resultIdentity(
  candidate: Readonly<Record<string, unknown>>,
  record: BrowserRecord,
  index: number,
): string {
  return (
    optionalString(
      firstDefined(
        candidate,
        "result_id",
        "resultId",
        "receipt_id",
        "receiptId",
        "id",
      ),
      "$.result.result_id",
      512,
    )
    ?? `browser-result:${stableDigest({
      session: record.scope.browserSessionId,
      record: record.recordId,
      sequence: record.sequence,
      index,
      status: candidate.status,
      error: candidate.error_code ?? candidate.error,
    })}`
  )
}

function resultDraft(
  candidate: Readonly<Record<string, unknown>>,
  record: BrowserRecord,
  index: number,
  events: readonly CausalEventProjection[],
  actionByTool: ReadonlyMap<string, string>,
): ResultDraft {
  const resultId = resultIdentity(candidate, record, index)
  const toolCallId = optionalString(
    firstDefined(candidate, "tool_call_id", "toolCallId")
      ?? firstDeep(record.payload, ["tool_call_id", "toolCallId"]),
    "$.result.tool_call_id",
    512,
  )
  const spanId = optionalString(
    firstDefined(candidate, "span_id", "spanId")
      ?? firstDeep(record.payload, ["span_id", "spanId"]),
    "$.result.span_id",
    512,
  )
  const correlationId = optionalString(
    firstDefined(candidate, "correlation_id", "correlationId")
      ?? firstDeep(record.payload, ["correlation_id", "correlationId"]),
    "$.result.correlation_id",
    512,
  )
  const causationId = optionalString(
    firstDefined(candidate, "causation_id", "causationId")
      ?? firstDeep(record.payload, ["causation_id", "causationId"]),
    "$.result.causation_id",
    512,
  )
  const actionId =
    optionalString(
      firstDefined(candidate, "action_id", "actionId"),
      "$.result.action_id",
      512,
    )
    ?? (toolCallId ? actionByTool.get(toolCallId) : undefined)
  const stepId =
    recordStepId(record, candidate)
    ?? (actionId ? `browser-step:action:${actionId}` : undefined)
    ?? inferredStepId(record, candidate, actionId)
  const related = relatedEvents(record.scope, events, {
    toolCallId,
    spanId,
    correlationId,
    causationId,
    actionId,
    record,
  })
  const errorCode = optionalString(
    firstDefined(candidate, "error_code", "errorCode", "code"),
    "$.result.error_code",
    512,
  )
  const errorMessage = optionalString(
    firstDefined(candidate, "error_message", "errorMessage", "error"),
    "$.result.error_message",
    64 * 1024,
  )
  const phase = phaseFromCandidate(
    candidate,
    record,
    errorCode || errorMessage
      ? BrowserStepPhase.FAILED
      : BrowserStepPhase.COMPLETED,
  )
  const okValue = firstDefined(candidate, "ok", "success")
  const ok =
    typeof okValue === "boolean"
      ? okValue
      : phase === BrowserStepPhase.COMPLETED
  const artifactIds = new Set<string>([
    ...stringArray(
      firstDefined(candidate, "artifact_ids", "artifactIds", "result_artifacts"),
    ),
    ...related.flatMap((event) => event.artifactIds),
  ])
  const mutationIds = new Set(
    related.map((event) => event.mutationId).filter(Boolean),
  )
  const summary =
    optionalString(
      firstDefined(
        candidate,
        "summary",
        "message",
        "long_term_memory",
        "extracted_content",
        "result",
      ),
      "$.result.summary",
      64 * 1024,
    )
    ?? errorMessage
    ?? (ok ? "Browser action completed." : "Browser action failed.")
  return {
    resultId,
    actionId,
    stepId,
    browserSessionId: record.scope.browserSessionId,
    toolCallId,
    spanId,
    correlationId,
    causationId,
    ok,
    status: phase,
    summary,
    extractedContent: optionalString(
      firstDefined(candidate, "extracted_content", "extractedContent", "content"),
      "$.result.extracted_content",
      256 * 1024,
    ),
    errorCode,
    errorMessage,
    retryable: booleanValue(
      firstDefined(candidate, "retryable", "can_retry"),
      phase === BrowserStepPhase.FAILED,
    ),
    artifactIds,
    mutationIds,
    sequence: record.sequence,
    completedAt:
      optionalString(
        firstDefined(
          candidate,
          "completed_at",
          "completedAt",
          "created_at",
        ),
        "$.result.completed_at",
        512,
      ) ?? record.createdAt,
    sourceEventIds: new Set(related.map((event) => event.eventId)),
    sourceRecordIds: new Set([record.recordId]),
  }
}

function mergeResult(left: ResultDraft, right: ResultDraft): ResultDraft {
  if (left.browserSessionId !== right.browserSessionId) return left
  const candidate =
    right.summary.length >= left.summary.length ? right : left
  return {
    resultId: left.resultId,
    actionId: right.actionId ?? left.actionId,
    stepId: right.stepId || left.stepId,
    browserSessionId: left.browserSessionId,
    toolCallId: right.toolCallId ?? left.toolCallId,
    spanId: right.spanId ?? left.spanId,
    correlationId: right.correlationId ?? left.correlationId,
    causationId: right.causationId ?? left.causationId,
    ok: left.ok && right.ok,
    status: mergePhase(left.status, right.status),
    summary: candidate.summary,
    extractedContent:
      right.extractedContent ?? left.extractedContent,
    errorCode: right.errorCode ?? left.errorCode,
    errorMessage: right.errorMessage ?? left.errorMessage,
    retryable: left.retryable || right.retryable,
    artifactIds: new Set([...left.artifactIds, ...right.artifactIds]),
    mutationIds: new Set([...left.mutationIds, ...right.mutationIds]),
    sequence: Math.max(left.sequence, right.sequence),
    completedAt: right.completedAt ?? left.completedAt,
    sourceEventIds: new Set([
      ...left.sourceEventIds,
      ...right.sourceEventIds,
    ]),
    sourceRecordIds: new Set([
      ...left.sourceRecordIds,
      ...right.sourceRecordIds,
    ]),
  }
}

function finalizeAction(draft: ActionDraft): BrowserAction {
  return Object.freeze({
    actionId: draft.actionId,
    stepId: draft.stepId,
    browserSessionId: draft.browserSessionId,
    workerRequestId: draft.workerRequestId,
    name: draft.name,
    arguments: Object.freeze({ ...draft.arguments }),
    argumentDigest: draft.argumentDigest,
    targetId: draft.targetId,
    frameId: draft.frameId,
    toolCallId: draft.toolCallId,
    spanId: draft.spanId,
    correlationId: draft.correlationId,
    causationId: draft.causationId,
    permissionRequestId: draft.permissionRequestId,
    permissionDecision: draft.permissionDecision,
    sequence: draft.sequence,
    startedAt: draft.startedAt,
    status: draft.status,
    retryable: draft.retryable,
    sourceEventIds: Object.freeze([...draft.sourceEventIds]),
    sourceRecordIds: Object.freeze([...draft.sourceRecordIds]),
  })
}

function finalizeResult(draft: ResultDraft): BrowserActionResult {
  return Object.freeze({
    resultId: draft.resultId,
    actionId: draft.actionId,
    stepId: draft.stepId,
    browserSessionId: draft.browserSessionId,
    toolCallId: draft.toolCallId,
    spanId: draft.spanId,
    correlationId: draft.correlationId,
    causationId: draft.causationId,
    ok: draft.ok,
    status: draft.status,
    summary: draft.summary,
    extractedContent: draft.extractedContent,
    errorCode: draft.errorCode,
    errorMessage: draft.errorMessage,
    retryable: draft.retryable,
    artifactIds: Object.freeze([...draft.artifactIds]),
    mutationIds: Object.freeze([...draft.mutationIds]),
    sequence: draft.sequence,
    completedAt: draft.completedAt,
    sourceEventIds: Object.freeze([...draft.sourceEventIds]),
    sourceRecordIds: Object.freeze([...draft.sourceRecordIds]),
  })
}

function actionToolMap(actions: ReadonlyMap<string, ActionDraft>): Map<string, string> {
  const output = new Map<string, string>()
  for (const action of actions.values()) {
    if (action.toolCallId) output.set(action.toolCallId, action.actionId)
  }
  return output
}

function spanRelations(
  spans: readonly BrowserSpanRecord[],
  actions: Map<string, ActionDraft>,
  results: Map<string, ResultDraft>,
): void {
  const actionByTool = actionToolMap(actions)
  for (const span of spans) {
    let action: ActionDraft | undefined
    if (span.actionId) action = actions.get(span.actionId)
    if (!action && span.toolCallId) {
      const actionId = actionByTool.get(span.toolCallId)
      if (actionId) action = actions.get(actionId)
    }
    if (!action) {
      action = [...actions.values()].find(
        (candidate) =>
          candidate.browserSessionId === span.scope.browserSessionId
          && candidate.sequence >= span.startSequence - 2
          && candidate.sequence <= span.endSequence + 2,
      )
    }
    if (action) {
      action.spanId ??= span.spanId
      action.toolCallId ??= span.toolCallId
      action.correlationId ??= span.correlationId
      action.causationId ??= span.causationId
      for (const eventId of span.eventIds) action.sourceEventIds.add(eventId)
      for (const recordId of span.recordIds) action.sourceRecordIds.add(recordId)
      action.status = mergePhase(action.status, normalizedPhase(span.status))
    }
    const result = [...results.values()].find(
      (candidate) =>
        candidate.browserSessionId === span.scope.browserSessionId
        && (
          candidate.spanId === span.spanId
          || Boolean(span.toolCallId && candidate.toolCallId === span.toolCallId)
          || (
            candidate.sequence >= span.startSequence
            && candidate.sequence <= span.endSequence + 4
          )
        ),
    )
    if (result) {
      result.spanId ??= span.spanId
      result.toolCallId ??= span.toolCallId
      result.correlationId ??= span.correlationId
      result.causationId ??= span.causationId
      result.actionId ??= action?.actionId
      for (const eventId of span.eventIds) result.sourceEventIds.add(eventId)
      for (const recordId of span.recordIds) result.sourceRecordIds.add(recordId)
    }
  }
}

function associateResults(
  actions: ReadonlyMap<string, ActionDraft>,
  results: Map<string, ResultDraft>,
): string[] {
  const findings: string[] = []
  const actionByTool = actionToolMap(actions)
  const sortedActions = [...actions.values()].sort(
    (left, right) => left.sequence - right.sequence,
  )
  for (const result of results.values()) {
    let action = result.actionId ? actions.get(result.actionId) : undefined
    if (!action && result.toolCallId) {
      const actionId = actionByTool.get(result.toolCallId)
      if (actionId) action = actions.get(actionId)
    }
    if (!action && result.spanId) {
      action = sortedActions.find(
        (candidate) =>
          candidate.browserSessionId === result.browserSessionId
          && candidate.spanId === result.spanId,
      )
    }
    if (!action && result.correlationId) {
      action = [...sortedActions]
        .reverse()
        .find(
          (candidate) =>
            candidate.browserSessionId === result.browserSessionId
            && candidate.correlationId === result.correlationId
            && candidate.sequence <= result.sequence,
        )
    }
    if (!action) {
      action = [...sortedActions]
        .reverse()
        .find(
          (candidate) =>
            candidate.browserSessionId === result.browserSessionId
            && candidate.sequence <= result.sequence
            && result.sequence - candidate.sequence <= 8,
        )
    }
    if (!action) {
      findings.push(`browser_result_without_action:${result.resultId}`)
      continue
    }
    result.actionId = action.actionId
    result.stepId = action.stepId
    action.status = mergePhase(action.status, result.status)
    action.retryable = result.retryable
    for (const eventId of result.sourceEventIds) action.sourceEventIds.add(eventId)
  }
  return findings
}

function stepFor(
  drafts: Map<string, StepDraft>,
  stepId: string,
  scope: BrowserScope,
  sequence: number,
): StepDraft {
  const existing = drafts.get(stepId)
  if (existing) return existing
  const created: StepDraft = {
    stepId,
    browserSessionId: scope.browserSessionId,
    workerRequestId: scope.workerRequestId,
    status: BrowserStepPhase.PARTIAL,
    actionIds: new Set(),
    resultIds: new Set(),
    screenshotArtifactIds: new Set(),
    downloadArtifactIds: new Set(),
    toolReceiptIds: new Set(),
    mutationIds: new Set(),
    eventIds: new Set(),
    recordIds: new Set(),
    startSequence: sequence,
    endSequence: sequence,
    retryable: false,
    partial: true,
  }
  drafts.set(stepId, created)
  return created
}

function buildStepDrafts(
  scope: BrowserScope,
  records: readonly BrowserRecord[],
  actions: readonly BrowserAction[],
  results: readonly BrowserActionResult[],
  domSummaries: readonly BrowserDomSummary[],
): Map<string, StepDraft> {
  const drafts = new Map<string, StepDraft>()
  for (const action of actions) {
    const step = stepFor(drafts, action.stepId, scope, action.sequence)
    step.actionIds.add(action.actionId)
    step.status = mergePhase(step.status, action.status)
    step.targetId ??= action.targetId
    step.frameId ??= action.frameId
    step.startSequence = Math.min(step.startSequence, action.sequence)
    step.endSequence = Math.max(step.endSequence, action.sequence)
    step.startedAt ??= action.startedAt
    step.retryable ||= action.retryable
    for (const eventId of action.sourceEventIds) step.eventIds.add(eventId)
    for (const recordId of action.sourceRecordIds) step.recordIds.add(recordId)
  }
  for (const result of results) {
    const step = stepFor(drafts, result.stepId, scope, result.sequence)
    step.resultIds.add(result.resultId)
    step.status = mergePhase(step.status, result.status)
    step.endSequence = Math.max(step.endSequence, result.sequence)
    step.completedAt = result.completedAt ?? step.completedAt
    step.retryable ||= result.retryable
    step.errorCode ??= result.errorCode
    step.errorMessage ??= result.errorMessage
    for (const artifactId of result.artifactIds) {
      step.screenshotArtifactIds.add(artifactId)
    }
    for (const mutationId of result.mutationIds) step.mutationIds.add(mutationId)
    for (const eventId of result.sourceEventIds) step.eventIds.add(eventId)
    for (const recordId of result.sourceRecordIds) step.recordIds.add(recordId)
  }
  for (const summary of domSummaries) {
    const candidate =
      summary.stepId
      ?? [...drafts.values()]
        .filter((step) => step.startSequence <= summary.sequence)
        .sort((left, right) => right.endSequence - left.endSequence)[0]?.stepId
    if (!candidate) continue
    const step = drafts.get(candidate)
    if (!step) continue
    step.url = summary.url ?? step.url
    step.title = summary.title ?? step.title
    step.targetId ??= summary.targetId
    step.frameId ??= summary.frameId
    step.endSequence = Math.max(step.endSequence, summary.sequence)
    for (const recordId of summary.sourceRecordIds) step.recordIds.add(recordId)
  }
  for (const record of records) {
    if (!STATE_KIND.test(record.kind)) continue
    const explicit = recordStepId(record)
    const candidate =
      explicit
        ? drafts.get(explicit)
        : [...drafts.values()]
          .filter((step) => step.startSequence <= record.sequence)
          .sort((left, right) => right.endSequence - left.endSequence)[0]
    if (!candidate) continue
    candidate.recordIds.add(record.recordId)
    candidate.endSequence = Math.max(candidate.endSequence, record.sequence)
    candidate.url =
      safeRecordUrl(record)
      ?? candidate.url
    candidate.title =
      deepString(record.payload, ["title", "page_title"], 16 * 1024)
      ?? candidate.title
    candidate.targetId ??= optionalString(
      firstDeep(record.payload, ["target_id", "targetId", "tab_id"]),
      "$.step.target_id",
      512,
    )
    candidate.frameId ??= optionalString(
      firstDeep(record.payload, ["frame_id", "frameId"]),
      "$.step.frame_id",
      512,
    )
  }
  return drafts
}

function safeRecordUrl(record: BrowserRecord): string | undefined {
  const value = firstDeep(record.payload, ["url", "current_url", "page_url"])
  if (typeof value !== "string") return undefined
  return value.trim().slice(0, 16 * 1024) || undefined
}

function finalizeSteps(
  drafts: ReadonlyMap<string, StepDraft>,
): BrowserStep[] {
  const sorted = [...drafts.values()].sort(
    (left, right) =>
      left.startSequence - right.startSequence
      || left.stepId.localeCompare(right.stepId),
  )
  return sorted.map((draft, index) => {
    const partial =
      draft.actionIds.size === 0
      || draft.resultIds.size === 0
      || draft.status === BrowserStepPhase.PARTIAL
      || draft.status === BrowserStepPhase.RUNNING
      || draft.status === BrowserStepPhase.WAITING_PERMISSION
    return Object.freeze({
      stepId: draft.stepId,
      browserSessionId: draft.browserSessionId,
      workerRequestId: draft.workerRequestId,
      index,
      status: draft.status,
      url: draft.url,
      title: draft.title,
      targetId: draft.targetId,
      frameId: draft.frameId,
      actionIds: Object.freeze([...draft.actionIds]),
      resultIds: Object.freeze([...draft.resultIds]),
      screenshotArtifactIds: Object.freeze([...draft.screenshotArtifactIds]),
      downloadArtifactIds: Object.freeze([...draft.downloadArtifactIds]),
      toolReceiptIds: Object.freeze([...draft.toolReceiptIds]),
      mutationIds: Object.freeze([...draft.mutationIds]),
      eventIds: Object.freeze([...draft.eventIds]),
      recordIds: Object.freeze([...draft.recordIds]),
      startSequence: draft.startSequence,
      endSequence: draft.endSequence,
      startedAt: draft.startedAt,
      completedAt: draft.completedAt,
      retryable: draft.retryable,
      partial,
      errorCode: draft.errorCode,
      errorMessage: draft.errorMessage,
    })
  })
}

function toolReceipts(
  scope: BrowserScope,
  steps: readonly BrowserStep[],
  actions: readonly BrowserAction[],
  results: readonly BrowserActionResult[],
): BrowserToolReceipt[] {
  const stepById = new Map(steps.map((step) => [step.stepId, step]))
  const actionById = new Map(actions.map((action) => [action.actionId, action]))
  const receipts: BrowserToolReceipt[] = []
  const seen = new Set<string>()
  for (const result of results) {
    const action = result.actionId ? actionById.get(result.actionId) : undefined
    const step = stepById.get(result.stepId)
    const receiptId = `browser-tool-receipt:${stableDigest({
      result: result.resultId,
      tool: result.toolCallId ?? action?.toolCallId,
      events: result.sourceEventIds,
    })}`
    if (seen.has(receiptId)) continue
    seen.add(receiptId)
    receipts.push(
      Object.freeze({
        receiptId,
        browserSessionId: scope.browserSessionId,
        workerRequestId: action?.workerRequestId ?? scope.workerRequestId,
        stepId: result.stepId,
        actionId: result.actionId,
        toolCallId: result.toolCallId ?? action?.toolCallId,
        spanId: result.spanId ?? action?.spanId,
        correlationId: result.correlationId ?? action?.correlationId,
        causationId: result.causationId ?? action?.causationId,
        mutationIds: result.mutationIds,
        artifactIds: result.artifactIds,
        permissionRequestId: action?.permissionRequestId,
        status: result.status,
        errorCode: result.errorCode,
        summary: result.summary,
        sequence: result.sequence,
        sourceEventIds: Object.freeze([
          ...new Set([
            ...result.sourceEventIds,
            ...(action?.sourceEventIds ?? []),
            ...(step?.eventIds ?? []),
          ]),
        ]),
        sourceRecordIds: Object.freeze([
          ...new Set([
            ...result.sourceRecordIds,
            ...(action?.sourceRecordIds ?? []),
            ...(step?.recordIds ?? []),
          ]),
        ]),
      }),
    )
  }
  return receipts.sort(
    (left, right) =>
      left.sequence - right.sequence
      || left.receiptId.localeCompare(right.receiptId),
  )
}

function findingsFor(
  actions: readonly BrowserAction[],
  results: readonly BrowserActionResult[],
  steps: readonly BrowserStep[],
): string[] {
  const findings: string[] = []
  const resultsByAction = new Map<string, BrowserActionResult[]>()
  for (const result of results) {
    if (!result.actionId) continue
    const values = resultsByAction.get(result.actionId) ?? []
    values.push(result)
    resultsByAction.set(result.actionId, values)
  }
  for (const action of actions) {
    if (!resultsByAction.has(action.actionId)) {
      findings.push(`browser_action_without_result:${action.actionId}`)
    }
    if (!action.sourceEventIds.length) {
      findings.push(`browser_action_without_canonical_event:${action.actionId}`)
    }
  }
  const actionIds = new Set(actions.map((action) => action.actionId))
  for (const result of results) {
    if (!result.actionId || !actionIds.has(result.actionId)) {
      findings.push(`browser_result_without_action:${result.resultId}`)
    }
    if (!result.sourceEventIds.length) {
      findings.push(`browser_result_without_canonical_event:${result.resultId}`)
    }
  }
  for (const step of steps) {
    if (step.partial) findings.push(`browser_step_partial:${step.stepId}`)
    if (step.startSequence > step.endSequence) {
      findings.push(`browser_step_sequence_invalid:${step.stepId}`)
    }
  }
  return [...new Set(findings)]
}

export function projectBrowserSteps(
  scope: BrowserScope,
  records: readonly BrowserRecord[],
  spans: readonly BrowserSpanRecord[],
  canonicalEvents: readonly CausalEventProjection[],
  domSummaries: readonly BrowserDomSummary[],
): BrowserStepProjection {
  const scopedRecords = records.filter(
    (record) =>
      record.scope.browserSessionId === scope.browserSessionId
      && record.scope.workerRequestId === scope.workerRequestId,
  )
  const scopedSpans = spans.filter(
    (span) =>
      span.scope.browserSessionId === scope.browserSessionId
      && span.scope.workerRequestId === scope.workerRequestId,
  )
  const actions = new Map<string, ActionDraft>()
  for (const record of scopedRecords) {
    for (const [index, candidate] of actionPayloads(record).entries()) {
      const draft = actionDraft(candidate, record, index, canonicalEvents)
      const existing = actions.get(draft.actionId)
      actions.set(
        draft.actionId,
        existing ? mergeAction(existing, draft) : draft,
      )
    }
  }
  const actionByTool = actionToolMap(actions)
  const results = new Map<string, ResultDraft>()
  for (const record of scopedRecords) {
    for (const [index, candidate] of resultPayloads(record).entries()) {
      const draft = resultDraft(
        candidate,
        record,
        index,
        canonicalEvents,
        actionByTool,
      )
      const existing = results.get(draft.resultId)
      results.set(
        draft.resultId,
        existing ? mergeResult(existing, draft) : draft,
      )
    }
  }
  spanRelations(scopedSpans, actions, results)
  const associationFindings = associateResults(actions, results)
  const finalizedActions = [...actions.values()]
    .map(finalizeAction)
    .sort(
      (left, right) =>
        left.sequence - right.sequence
        || left.actionId.localeCompare(right.actionId),
    )
  const finalizedResults = [...results.values()]
    .map(finalizeResult)
    .sort(
      (left, right) =>
        left.sequence - right.sequence
        || left.resultId.localeCompare(right.resultId),
    )
  const stepDrafts = buildStepDrafts(
    scope,
    scopedRecords,
    finalizedActions,
    finalizedResults,
    domSummaries,
  )
  const finalizedSteps = finalizeSteps(stepDrafts)
  const receipts = toolReceipts(
    scope,
    finalizedSteps,
    finalizedActions,
    finalizedResults,
  )
  for (const receipt of receipts) {
    const step = stepDrafts.get(receipt.stepId ?? "")
    if (step) step.toolReceiptIds.add(receipt.receiptId)
  }
  const refreshedSteps = finalizeSteps(stepDrafts)
  const findings = [
    ...associationFindings,
    ...findingsFor(finalizedActions, finalizedResults, refreshedSteps),
  ]
  return Object.freeze({
    steps: Object.freeze(refreshedSteps),
    actions: Object.freeze(finalizedActions),
    results: Object.freeze(finalizedResults),
    toolReceipts: Object.freeze(receipts),
    actionById: new Map(finalizedActions.map((item) => [item.actionId, item])),
    resultById: new Map(finalizedResults.map((item) => [item.resultId, item])),
    stepById: new Map(refreshedSteps.map((item) => [item.stepId, item])),
    actionIdByRecordId: new Map(
      finalizedActions.flatMap((action) =>
        action.sourceRecordIds.map((recordId) => [recordId, action.actionId] as const),
      ),
    ),
    resultIdByRecordId: new Map(
      finalizedResults.flatMap((result) =>
        result.sourceRecordIds.map((recordId) => [recordId, result.resultId] as const),
      ),
    ),
    stepIdByRecordId: new Map(
      refreshedSteps.flatMap((step) =>
        step.recordIds.map((recordId) => [recordId, step.stepId] as const),
      ),
    ),
    findings: Object.freeze([...new Set(findings)]),
  })
}

export function retryActionPayload(
  action: BrowserAction,
): Readonly<Record<string, unknown>> {
  return Object.freeze({
    action: action.name,
    arguments: Object.freeze({ ...action.arguments }),
    retry_of_action_id: action.actionId,
    expected_argument_digest: action.argumentDigest,
  })
}
