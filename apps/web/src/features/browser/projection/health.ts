import type {
  CausalEventProjection,
  RecoveryProjection,
} from "../../../state/contracts.ts"
import {
  booleanValue,
  firstDefined,
  integerValue,
  isRecord,
  optionalRecord,
  optionalString,
  stringArray,
  type BrowserHealth,
  type BrowserScope,
} from "../contracts.ts"
import {
  firstDeep,
  valuesDeep,
  type BrowserReadResult,
  type BrowserRecord,
} from "./reader.ts"

const HEALTH_RANK: Readonly<Record<BrowserHealth["phase"], number>> =
  Object.freeze({
    unknown: 0,
    healthy: 1,
    reconnecting: 2,
    degraded: 3,
    unhealthy: 4,
    stopped: 5,
    crashed: 6,
  })

interface HealthDraft {
  phase: BrowserHealth["phase"]
  crashed: boolean
  reconnecting: boolean
  reconnectCount: number
  processEpoch?: number
  lastHeartbeatAt?: string
  lastFailureId?: string
  recoveryIds: Set<string>
  signalIds: Set<string>
  reasons: Set<string>
  sourceRecordIds: Set<string>
  sequence: number
}

function normalizeHealth(value: unknown): BrowserHealth["phase"] {
  const raw = String(value ?? "").trim().toLowerCase().replaceAll("-", "_")
  if (
    raw.includes("crash")
    || raw.includes("terminated_unexpected")
    || raw.includes("process_exit")
  ) return "crashed"
  if (raw.includes("reconnect") || raw.includes("recovering")) {
    return "reconnecting"
  }
  if (
    raw.includes("unhealthy")
    || raw.includes("failed")
    || raw.includes("error")
    || raw.includes("timeout")
  ) return "unhealthy"
  if (raw.includes("degraded") || raw.includes("warning") || raw.includes("stale")) {
    return "degraded"
  }
  if (
    raw.includes("stopped")
    || raw.includes("cancelled")
    || raw.includes("closed")
  ) return "stopped"
  if (
    raw.includes("healthy")
    || raw.includes("running")
    || raw.includes("ready")
    || raw.includes("attached")
  ) return "healthy"
  return "unknown"
}

function mergeHealth(
  current: BrowserHealth["phase"],
  candidate: BrowserHealth["phase"],
  laterRecovery = false,
): BrowserHealth["phase"] {
  if (
    laterRecovery
    && candidate === "healthy"
    && (current === "crashed" || current === "unhealthy")
  ) return "healthy"
  return HEALTH_RANK[candidate] >= HEALTH_RANK[current]
    ? candidate
    : current
}

function newDraft(): HealthDraft {
  return {
    phase: "unknown",
    crashed: false,
    reconnecting: false,
    reconnectCount: 0,
    recoveryIds: new Set(),
    signalIds: new Set(),
    reasons: new Set(),
    sourceRecordIds: new Set(),
    sequence: 0,
  }
}

function healthRecords(
  scope: BrowserScope,
  records: readonly BrowserRecord[],
): BrowserRecord[] {
  return records
    .filter(
      (record) =>
        record.scope.browserSessionId === scope.browserSessionId
        && record.scope.workerRequestId === scope.workerRequestId
        && /watchdog|health|crash|disconnect|reconnect|recover|heartbeat|process|stop|lifecycle/.test(
          record.kind,
        ),
    )
    .sort(
      (left, right) =>
        left.sequence - right.sequence
        || left.recordId.localeCompare(right.recordId),
    )
}

function reasonText(value: unknown): string | undefined {
  if (typeof value === "string") return value.trim().slice(0, 8_192) || undefined
  if (!isRecord(value)) return undefined
  return optionalString(
    firstDefined(
      value,
      "reason",
      "message",
      "error",
      "error_message",
      "summary",
      "status",
    ),
    "$.health.reason",
    8_192,
  )
}

function applyHealthRecord(draft: HealthDraft, record: BrowserRecord): void {
  const payload = record.payload
  const rawPhase =
    firstDeep(payload, ["health_status", "status", "phase", "state"])
    ?? record.kind
  const candidate = normalizeHealth(rawPhase)
  const laterRecovery =
    /reconnect|recover|restart|reattach/.test(record.kind)
    && record.sequence >= draft.sequence
  draft.phase = mergeHealth(draft.phase, candidate, laterRecovery)
  draft.crashed =
    candidate === "crashed"
    || booleanValue(firstDeep(payload, ["crashed", "process_crashed"]))
    || (draft.crashed && candidate !== "healthy")
  draft.reconnecting =
    candidate === "reconnecting"
    || booleanValue(firstDeep(payload, ["reconnecting", "restart_pending"]))
  const reconnectCount = Number(
    firstDeep(payload, [
      "reconnect_count",
      "restart_count",
      "recovery_attempt",
      "attempt",
    ]),
  )
  if (Number.isSafeInteger(reconnectCount) && reconnectCount >= 0) {
    draft.reconnectCount = Math.max(draft.reconnectCount, reconnectCount)
  } else if (/reconnect|restart|reattach/.test(record.kind)) {
    draft.reconnectCount += 1
  }
  const processEpoch = Number(
    firstDeep(payload, ["process_epoch", "runtime_process_epoch", "epoch"]),
  )
  if (Number.isSafeInteger(processEpoch) && processEpoch >= 0) {
    draft.processEpoch = Math.max(draft.processEpoch ?? 0, processEpoch)
  }
  const heartbeat = optionalString(
    firstDeep(payload, [
      "last_heartbeat_at",
      "heartbeat_at",
      "observed_at",
      "created_at",
    ]),
    "$.health.last_heartbeat_at",
    512,
  )
  if (heartbeat) draft.lastHeartbeatAt = heartbeat
  const failureId = optionalString(
    firstDeep(payload, ["failure_id", "failureId", "crash_id"]),
    "$.health.failure_id",
    512,
  )
  if (failureId) draft.lastFailureId = failureId
  for (const recoveryId of stringArray(
    firstDeep(payload, ["recovery_ids", "recovery_id", "recoveryId"]),
  )) draft.recoveryIds.add(recoveryId)
  for (const signalId of stringArray(
    firstDeep(payload, ["signal_ids", "signal_id", "signalId"]),
  )) draft.signalIds.add(signalId)
  for (const candidateReason of valuesDeep(
    payload,
    ["reason", "message", "error", "error_message", "summary"],
    5,
    128,
  )) {
    const reason = reasonText(candidateReason)
    if (reason) draft.reasons.add(reason)
  }
  draft.sourceRecordIds.add(record.recordId)
  draft.sequence = Math.max(draft.sequence, record.sequence)
}

function scopeProjection(
  projection: Readonly<Record<string, unknown>>,
): Readonly<Record<string, unknown>> | undefined {
  return optionalRecord(projection.scope)
}

function projectionBelongs(
  scope: BrowserScope,
  projection: Readonly<Record<string, unknown>>,
): boolean {
  const projectedScope = scopeProjection(projection)
  if (!projectedScope) return false
  const sessionId = String(
    firstDefined(projectedScope, "browser_session_id", "browserSessionId") ?? "",
  )
  const workerRequestId = String(
    firstDefined(projectedScope, "worker_request_id", "workerRequestId") ?? "",
  )
  return (
    sessionId === scope.browserSessionId
    && (!workerRequestId || workerRequestId === scope.workerRequestId)
  )
}

function applyHealthProjection(
  draft: HealthDraft,
  projection: Readonly<Record<string, unknown>>,
): void {
  const aggregate =
    optionalRecord(projection.aggregate)
    ?? optionalRecord(projection.crash_detector)
    ?? projection
  const candidate = normalizeHealth(
    firstDefined(aggregate, "status", "phase", "health"),
  )
  draft.phase = mergeHealth(draft.phase, candidate)
  draft.crashed ||= candidate === "crashed"
  draft.reconnecting ||= candidate === "reconnecting"
  for (const signalId of stringArray(
    firstDefined(aggregate, "signal_ids", "signalIds"),
  )) draft.signalIds.add(signalId)
  for (const recoveryId of stringArray(
    firstDefined(aggregate, "recovery_input_ids", "recovery_ids"),
  )) draft.recoveryIds.add(recoveryId)
  const reason = reasonText(aggregate)
  if (reason && candidate !== "unknown") draft.reasons.add(reason)
}

function applyCanonicalEvents(
  draft: HealthDraft,
  scope: BrowserScope,
  events: readonly CausalEventProjection[],
): void {
  for (const event of events) {
    if (
      event.taskId !== scope.taskId
      || event.runId !== scope.runId
      || !/browser|watchdog|fault|recovery/i.test(event.eventType)
    ) continue
    if (
      event.sessionId
      && event.sessionId !== scope.browserSessionId
      && event.sessionId !== scope.canonicalSessionId
    ) continue
    const candidate = normalizeHealth(`${event.eventType} ${event.summary}`)
    if (candidate !== "unknown") {
      draft.phase = mergeHealth(
        draft.phase,
        candidate,
        /recover|restart|reconnect/i.test(event.eventType),
      )
    }
    if (candidate === "crashed") {
      draft.crashed = true
      draft.lastFailureId = event.failureId ?? draft.lastFailureId
    }
    if (candidate === "reconnecting") draft.reconnecting = true
    if (event.recoveryId) draft.recoveryIds.add(event.recoveryId)
    draft.sequence = Math.max(draft.sequence, event.sequence)
  }
}

function applyRecoveries(
  draft: HealthDraft,
  scope: BrowserScope,
  recoveries: readonly RecoveryProjection[],
): void {
  for (const recovery of recoveries) {
    if (recovery.taskId !== scope.taskId) continue
    if (
      recovery.sessionId
      && recovery.sessionId !== scope.browserSessionId
      && recovery.sessionId !== scope.canonicalSessionId
    ) continue
    draft.recoveryIds.add(recovery.id)
    if (recovery.failureId) draft.lastFailureId = recovery.failureId
    if (recovery.reason) draft.reasons.add(recovery.reason)
    if (
      recovery.lifecycle === "recovering"
      || recovery.lifecycle === "running"
    ) {
      draft.phase = mergeHealth(draft.phase, "reconnecting", true)
      draft.reconnecting = true
    }
    if (recovery.lifecycle === "completed") {
      draft.phase = "healthy"
      draft.crashed = false
      draft.reconnecting = false
    }
    draft.sequence = Math.max(draft.sequence, recovery.sequence)
  }
}

export function projectBrowserHealth(
  scope: BrowserScope,
  read: BrowserReadResult,
  events: readonly CausalEventProjection[],
  recoveries: readonly RecoveryProjection[] = [],
): BrowserHealth {
  const draft = newDraft()
  for (const record of healthRecords(scope, read.records)) {
    applyHealthRecord(draft, record)
  }
  for (const projection of read.health) {
    if (projectionBelongs(scope, projection)) {
      applyHealthProjection(draft, projection)
    }
  }
  applyCanonicalEvents(draft, scope, events)
  applyRecoveries(draft, scope, recoveries)
  if (draft.phase === "unknown" && read.records.some(
    (record) =>
      record.scope.browserSessionId === scope.browserSessionId
      && record.scope.workerRequestId === scope.workerRequestId,
  )) draft.phase = "healthy"
  if (draft.phase === "healthy") {
    draft.crashed = false
    draft.reconnecting = false
  }
  return Object.freeze({
    browserSessionId: scope.browserSessionId,
    phase: draft.phase,
    crashed: draft.crashed,
    reconnecting: draft.reconnecting,
    reconnectCount: draft.reconnectCount,
    processEpoch: draft.processEpoch,
    lastHeartbeatAt: draft.lastHeartbeatAt,
    lastFailureId: draft.lastFailureId,
    recoveryIds: Object.freeze([...draft.recoveryIds]),
    signalIds: Object.freeze([...draft.signalIds]),
    reasons: Object.freeze([...draft.reasons]),
    sourceRecordIds: Object.freeze([...draft.sourceRecordIds]),
    sequence: draft.sequence,
  })
}

export function browserHealthLabel(health: BrowserHealth): string {
  if (health.phase === "healthy" && health.reconnectCount > 0) {
    return `healthy after ${health.reconnectCount} reconnect${
      health.reconnectCount === 1 ? "" : "s"
    }`
  }
  if (health.phase === "crashed" && health.lastFailureId) {
    return `crashed · ${health.lastFailureId}`
  }
  if (health.phase === "reconnecting") {
    return `reconnecting · attempt ${Math.max(1, health.reconnectCount)}`
  }
  return health.phase
}

export function browserHealthCanControl(
  health: BrowserHealth,
  action: "navigate" | "stop" | "retry" | "inspect",
): { allowed: boolean; reason?: string } {
  if (action === "inspect") return { allowed: true }
  if (action === "stop") {
    if (health.phase === "stopped") {
      return { allowed: false, reason: "Browser session is already stopped." }
    }
    return { allowed: true }
  }
  if (health.phase === "crashed" && action !== "retry") {
    return {
      allowed: false,
      reason: "Crashed browser session requires bounded retry or inspection.",
    }
  }
  if (health.phase === "reconnecting") {
    return {
      allowed: false,
      reason: "Browser session is reconciling a reconnect generation.",
    }
  }
  if (health.phase === "stopped" && action === "navigate") {
    return {
      allowed: false,
      reason: "Stopped browser session must be restarted by its runtime owner.",
    }
  }
  return { allowed: true }
}
