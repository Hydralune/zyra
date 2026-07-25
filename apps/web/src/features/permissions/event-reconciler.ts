import {
  boundedPermissionText,
  comparePermissionTime,
  identifier,
  stablePermissionId,
} from "./canonical.ts"
import type {
  PermissionSourceSurface,
  PermissionTimelineEntry,
  PermissionWarningSeverity,
} from "./contracts.ts"
import { redactPermissionText } from "./redaction.ts"

export interface PermissionCanonicalEvent {
  eventId: string
  eventType: string
  taskId: string
  runId: string
  createdAt: string
  payload?: Record<string, unknown>
  summary?: string
  sessionId?: string
  nodeId?: string
  workerId?: string
  spanId?: string
  toolCallId?: string
  checkpointId?: string
  controlCommandId?: string
  correlationId?: string
  causationId?: string
  entityRefs?: readonly string[]
}

export interface PermissionEventBinding {
  taskId: string
  runId: string
}

export interface PermissionEventQuarantine {
  quarantineId: string
  eventId?: string
  code: string
  message: string
  createdAt: string
}

export interface PermissionEventReconcilerSnapshot {
  schema: "zyra.permission-event-reconciler/v1"
  entries: readonly PermissionTimelineEntry[]
  quarantines: readonly PermissionEventQuarantine[]
  admittedEventIds: readonly string[]
  admitted: number
  duplicate: number
  irrelevant: number
  rejected: number
  conflicting: number
  ownsPendingState: false
  rawArgumentsRetained: false
  revision: number
}

export class PermissionEventReconciler {
  readonly #entries = new Map<string, PermissionTimelineEntry>()
  readonly #eventDigests = new Map<string, string>()
  readonly #quarantines = new Map<string, PermissionEventQuarantine>()
  readonly #maximumEntries: number
  readonly #maximumQuarantines: number
  #admitted = 0
  #duplicate = 0
  #irrelevant = 0
  #rejected = 0
  #conflicting = 0
  #revision = 0

  constructor(options: {
    maximumEntries?: number
    maximumQuarantines?: number
  } = {}) {
    this.#maximumEntries = boundedCount(
      options.maximumEntries,
      32,
      20_000,
      2_000,
    )
    this.#maximumQuarantines = boundedCount(
      options.maximumQuarantines,
      8,
      2_000,
      128,
    )
  }

  ingest(
    events: readonly PermissionCanonicalEvent[],
    bindingValue: PermissionEventBinding,
  ): PermissionEventReconcilerSnapshot {
    const binding = {
      taskId: identifier(bindingValue.taskId, "permission event task id"),
      runId: identifier(bindingValue.runId, "permission event run id"),
    }
    let changed = false
    for (const event of events) {
      try {
        const outcome = this.#ingestOne(event, binding)
        changed ||= outcome
      } catch (error) {
        this.#rejected += 1
        this.#quarantine(
          event?.eventId,
          event?.createdAt,
          errorCode(error),
          errorMessage(error),
        )
        changed = true
      }
    }
    if (changed) {
      this.#revision += 1
      this.#prune()
    }
    return this.snapshot()
  }

  snapshot(): PermissionEventReconcilerSnapshot {
    return Object.freeze({
      schema: "zyra.permission-event-reconciler/v1",
      entries: Object.freeze(this.entries()),
      quarantines: Object.freeze(
        [...this.#quarantines.values()]
          .sort(
            (left, right) =>
              comparePermissionTime(left.createdAt, right.createdAt)
              || left.quarantineId.localeCompare(right.quarantineId),
          )
          .map((item) => Object.freeze({ ...item })),
      ),
      admittedEventIds: Object.freeze(
        [...this.#eventDigests.keys()].sort(),
      ),
      admitted: this.#admitted,
      duplicate: this.#duplicate,
      irrelevant: this.#irrelevant,
      rejected: this.#rejected,
      conflicting: this.#conflicting,
      ownsPendingState: false,
      rawArgumentsRetained: false,
      revision: this.#revision,
    })
  }

  entries(): PermissionTimelineEntry[] {
    return [...this.#entries.values()]
      .sort(
        (left, right) =>
          comparePermissionTime(left.createdAt, right.createdAt)
          || left.entryId.localeCompare(right.entryId),
      )
      .map((entry) =>
        Object.freeze({
          ...entry,
          refs: Object.freeze({ ...entry.refs }),
        }),
      )
  }

  clear(): void {
    this.#entries.clear()
    this.#eventDigests.clear()
    this.#quarantines.clear()
    this.#admitted = 0
    this.#duplicate = 0
    this.#irrelevant = 0
    this.#rejected = 0
    this.#conflicting = 0
    this.#revision += 1
  }

  #ingestOne(
    event: PermissionCanonicalEvent,
    binding: PermissionEventBinding,
  ): boolean {
    if (!event || typeof event !== "object") {
      throw eventError(
        "permission_event_invalid",
        "Canonical permission event must be an object.",
      )
    }
    const eventId = identifier(event.eventId, "permission event id")
    const eventTaskId = identifier(
      event.taskId,
      "permission event task id",
    )
    const eventRunId = identifier(event.runId, "permission event run id")
    if (
      eventTaskId !== binding.taskId
      || eventRunId !== binding.runId
    ) {
      throw eventError(
        "permission_event_binding_mismatch",
        `Event ${eventId} belongs to another task or run.`,
      )
    }
    const type = boundedPermissionText(event.eventType, {
      label: "permission event type",
      maximumBytes: 256,
      allowEmpty: false,
    })
    const candidates = candidateRecords(eventPayload(event))
    const identity = eventIdentity(candidates)
    if (!permissionRelevant(type, candidates, identity)) {
      this.#irrelevant += 1
      return false
    }
    const digest = stablePermissionId(
      "permission_event_digest",
      eventId,
      eventTaskId,
      eventRunId,
      type,
      event.createdAt,
      identity,
    )
    const priorDigest = this.#eventDigests.get(eventId)
    if (priorDigest) {
      if (priorDigest === digest) {
        this.#duplicate += 1
        return false
      }
      this.#conflicting += 1
      this.#quarantine(
        eventId,
        event.createdAt,
        "permission_event_identity_conflict",
        `Event ${eventId} was replayed with different permission identity.`,
      )
      return true
    }
    const entry = permissionTimelineFromEvent(
      event,
      type,
      candidates,
      identity,
    )
    this.#eventDigests.set(eventId, digest)
    this.#entries.set(entry.entryId, entry)
    this.#admitted += 1
    return true
  }

  #quarantine(
    eventIdValue: unknown,
    timestampValue: unknown,
    code: string,
    message: string,
  ): void {
    const eventId =
      typeof eventIdValue === "string" && eventIdValue.trim()
        ? eventIdValue.trim().slice(0, 512)
        : undefined
    const createdAt = safeTimestamp(timestampValue)
    const quarantineId = stablePermissionId(
      "permission_event_quarantine",
      eventId,
      code,
      message,
    )
    this.#quarantines.set(
      quarantineId,
      Object.freeze({
        quarantineId,
        eventId,
        code,
        message: message.slice(0, 2_000),
        createdAt,
      }),
    )
  }

  #prune(): void {
    while (this.#entries.size > this.#maximumEntries) {
      const oldest = this.entries()[0]
      if (!oldest) break
      this.#entries.delete(oldest.entryId)
      const eventId = oldest.refs.event_id
      if (eventId) this.#eventDigests.delete(eventId)
    }
    while (this.#quarantines.size > this.#maximumQuarantines) {
      const oldest = [...this.#quarantines.values()].sort(
        (left, right) =>
          comparePermissionTime(left.createdAt, right.createdAt)
          || left.quarantineId.localeCompare(right.quarantineId),
      )[0]
      if (!oldest) break
      this.#quarantines.delete(oldest.quarantineId)
    }
  }
}

function permissionTimelineFromEvent(
  event: PermissionCanonicalEvent,
  type: string,
  records: readonly Record<string, unknown>[],
  identity: Readonly<Record<string, string>>,
): PermissionTimelineEntry {
  const phase = eventPhase(type, records)
  const kind = eventKind(type, phase)
  const source = eventSurface(records)
  const detail = eventDetail(type, records, identity)
  const requestId = identity.request_id
  const responseId = identity.response_id
  const refs = Object.freeze({
    event_id: event.eventId,
    run_id: event.runId,
    task_id: event.taskId,
    ...(requestId ? { request_id: requestId } : {}),
    ...(responseId ? { response_id: responseId } : {}),
    ...(identity.tool_call_id
      ? { tool_call_id: identity.tool_call_id }
      : {}),
    ...(identity.permit_id ? { permit_id: identity.permit_id } : {}),
    ...(identity.arguments_digest
      ? { arguments_digest: identity.arguments_digest }
      : {}),
    ...(event.spanId ? { span_id: event.spanId } : {}),
  })
  return Object.freeze({
    entryId: `permission_event:${event.eventId}`,
    requestId,
    responseId,
    kind,
    phase,
    title: eventTitle(kind, phase, source),
    detail,
    createdAt: safeTimestamp(event.createdAt),
    severity: eventSeverity(kind, phase),
    sourceSurface: source,
    exactBinding: Boolean(
      identity.tool_call_id
      && identity.arguments_digest
      && identity.arguments_digest.length === 64,
    ),
    canonical: true,
    refs,
  })
}

function candidateRecords(
  payload: Record<string, unknown>,
): Record<string, unknown>[] {
  const records: Record<string, unknown>[] = []
  const queue: Array<{ value: unknown; depth: number }> = [
    { value: payload, depth: 0 },
  ]
  let visited = 0
  while (queue.length && visited < 256) {
    const selected = queue.shift()!
    visited += 1
    if (
      !selected.value
      || typeof selected.value !== "object"
      || Array.isArray(selected.value)
    ) {
      continue
    }
    const record = selected.value as Record<string, unknown>
    records.push(record)
    if (selected.depth >= 4) continue
    for (const [key, value] of Object.entries(record)) {
      if (
        value
        && typeof value === "object"
        && !Array.isArray(value)
        && !secretKey(key)
      ) {
        queue.push({ value, depth: selected.depth + 1 })
      }
    }
  }
  return records
}

function eventPayload(
  event: PermissionCanonicalEvent,
): Record<string, unknown> {
  const requestRef = (event.entityRefs ?? [])
    .map((value) => String(value))
    .find((value) => /permission|approval|request/i.test(value))
  const requestId = requestRef
    ?.replace(/^(?:permission|approval|request)[:/]/i, "")
    .trim()
  return {
    ...(event.payload ?? {}),
    event_projection: {
      request_id:
        requestId && safeIdentity(requestId) ? requestId : undefined,
      tool_call_id: event.toolCallId,
      session_id: event.sessionId,
      node_id: event.nodeId,
      worker_id: event.workerId,
      checkpoint_id: event.checkpointId,
      control_command_id: event.controlCommandId,
      correlation_id: event.correlationId,
      causation_id: event.causationId,
      summary: event.summary,
    },
  }
}

function eventIdentity(
  records: readonly Record<string, unknown>[],
): Readonly<Record<string, string>> {
  const result: Record<string, string> = {}
  for (const [target, keys] of Object.entries({
    request_id: [
      "permission_request_id",
      "continuation_request_id",
      "request_id",
    ],
    response_id: ["permission_response_id", "response_id"],
    tool_call_id: ["tool_call_id", "tool_use_id"],
    permit_id: ["permit_id"],
    arguments_digest: [
      "final_arguments_digest",
      "arguments_digest",
    ],
    decision_id: ["permission_decision_id", "decision_id"],
  })) {
    for (const record of records) {
      const found = keys
        .map((key) => record[key])
        .find((value) => safeIdentity(value))
      if (found) {
        const rendered = String(found).trim()
        if (
          target === "arguments_digest"
          && !/^[0-9a-f]{64}$/i.test(rendered)
        ) {
          continue
        }
        result[target] = rendered
        break
      }
    }
  }
  return Object.freeze(result)
}

function permissionRelevant(
  type: string,
  records: readonly Record<string, unknown>[],
  identity: Readonly<Record<string, string>>,
): boolean {
  if (/permission|approval|permit/i.test(type)) return true
  if (identity.request_id || identity.permit_id) return true
  return records.some(
    (record) =>
      record.intervention_counted === true
      || record.operator_intervention_counted === true
      || record.permission_effect !== undefined,
  )
}

function eventPhase(
  type: string,
  records: readonly Record<string, unknown>[],
): string {
  const explicit = findText(records, [
    "permission_phase",
    "response_phase",
    "status",
    "phase",
    "effect",
    "decision",
  ])
  if (explicit) return explicit.toLowerCase().replace(/\s+/g, "_")
  if (/expir|timeout/i.test(type)) return "default_deny"
  if (/deliver/i.test(type)) return "delivered"
  if (/resolv|respond/i.test(type)) return "responded"
  if (/permit/i.test(type)) return "issued"
  if (/restore|resume/i.test(type)) return "restored"
  if (/den|reject|forbid/i.test(type)) return "denied"
  if (/allow|approv/i.test(type)) return "allowed"
  return "observed"
}

function eventKind(
  type: string,
  phase: string,
): PermissionTimelineEntry["kind"] {
  if (/expir|timeout/i.test(type + phase)) return "timeout"
  if (/permit/i.test(type)) return "permit"
  if (/restore|resume|compact/i.test(type)) return "restore"
  if (/deliver/i.test(type)) return "delivery"
  if (/intervention|control/i.test(type)) return "intervention"
  if (/decision|evaluat|policy/i.test(type)) return "decision"
  if (/respond|resolv|approv|denied/i.test(type + phase)) return "response"
  if (/error|fail|reject/i.test(type + phase)) return "error"
  return "request"
}

function eventSurface(
  records: readonly Record<string, unknown>[],
): PermissionSourceSurface {
  const material = [
    findText(records, ["source_surface", "namespace"]),
    findText(records, ["tool_name", "tool"]),
    findText(records, ["operation", "event_source"]),
  ]
    .join(" ")
    .toLowerCase()
  if (/browser|playwright|navigate|page/.test(material)) return "browser"
  if (/terminal|shell|command|pty/.test(material)) return "terminal"
  if (/\bmcp\b|server/.test(material)) return "mcp"
  if (/plugin|hook/.test(material)) return "plugin"
  if (/skill/.test(material)) return "skill"
  if (/agent|subagent/.test(material)) return "agent"
  if (/control/.test(material)) return "command"
  return "tool"
}

function eventDetail(
  type: string,
  records: readonly Record<string, unknown>[],
  identity: Readonly<Record<string, string>>,
): string {
  const narrative = findText(records, [
    "permission_summary",
    "summary",
    "reason",
    "reason_code",
    "message",
    "error_message",
  ])
  if (narrative) {
    return redactPermissionText(narrative, {
      kind: "text",
      maximumBytes: 2_000,
    }).value
  }
  const refs = [
    identity.request_id
      ? `request ${short(identity.request_id)}`
      : "",
    identity.tool_call_id
      ? `tool ${short(identity.tool_call_id)}`
      : "",
    identity.permit_id
      ? `permit ${short(identity.permit_id)}`
      : "",
  ].filter(Boolean)
  return refs.length
    ? `${type} · ${refs.join(" · ")}`
    : `${type} projected without raw permission arguments.`
}

function eventTitle(
  kind: PermissionTimelineEntry["kind"],
  phase: string,
  source: PermissionSourceSurface,
): string {
  const label = {
    request: "Permission request",
    delivery: "Approval delivery",
    response: "Permission response",
    decision: "Policy decision",
    permit: "Exact-call permit",
    timeout: "Permission timeout",
    restore: "Permission restore",
    intervention: "Operator intervention",
    error: "Permission error",
  }[kind]
  return `${label} · ${phase} · ${source}`
}

function eventSeverity(
  kind: PermissionTimelineEntry["kind"],
  phase: string,
): PermissionWarningSeverity {
  if (
    kind === "error"
    || /forged|mismatch|invalid|fail/i.test(phase)
  ) {
    return "danger"
  }
  if (
    kind === "timeout"
    || kind === "intervention"
    || /den|reject|expir|cancel/i.test(phase)
  ) {
    return "warning"
  }
  return "info"
}

function findText(
  records: readonly Record<string, unknown>[],
  keys: readonly string[],
): string {
  for (const record of records) {
    for (const key of keys) {
      const value = record[key]
      if (typeof value !== "string") continue
      const rendered = value.trim()
      if (
        rendered
        && !secretKey(key)
        && new TextEncoder().encode(rendered).byteLength <= 8_192
      ) {
        return rendered
      }
    }
  }
  return ""
}

function safeIdentity(value: unknown): boolean {
  return (
    typeof value === "string"
    && /^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$/.test(value.trim())
  )
}

function secretKey(value: string): boolean {
  return /token|secret|password|authorization|cookie|credential|api[_-]?key/i.test(
    value,
  )
}

function safeTimestamp(value: unknown): string {
  if (typeof value === "string" && Number.isFinite(Date.parse(value))) {
    return new Date(Date.parse(value)).toISOString()
  }
  return new Date(0).toISOString()
}

function short(value: string): string {
  return value.length > 24
    ? `${value.slice(0, 14)}…${value.slice(-7)}`
    : value
}

function boundedCount(
  value: number | undefined,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  if (!Number.isFinite(value)) return fallback
  return Math.min(maximum, Math.max(minimum, Math.floor(value!)))
}

function errorCode(error: unknown): string {
  if (!error || typeof error !== "object") return "permission_event_rejected"
  const code = (error as Record<string, unknown>).code
  return typeof code === "string" && code.trim()
    ? code.trim().slice(0, 256)
    : "permission_event_rejected"
}

function errorMessage(error: unknown): string {
  return (
    error instanceof Error
      ? error.message
      : String(error || "Permission event was rejected.")
  ).slice(0, 2_000)
}

function eventError(code: string, message: string): Error {
  return Object.assign(new Error(message), {
    name: "PermissionEventReconcilerError",
    code,
  })
}
