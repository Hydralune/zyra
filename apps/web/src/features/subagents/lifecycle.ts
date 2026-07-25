import type {
  LateResultRecord,
  SubagentLifecycleIncident,
  SubagentProjection,
  SubagentRow,
} from "./contracts.ts"
import {
  compareNumber,
  compareText,
  fingerprint,
  sortStable,
  text,
  unique,
} from "./value.ts"

export class SubagentLifecycleMonitor {
  readonly #incidents = new Map<string, SubagentLifecycleIncident>()
  readonly #previous = new Map<string, SubagentRow>()
  readonly #maximum: number
  readonly #now: () => Date
  #enabled = true
  #disabledReason = "Subagent lifecycle monitor is disabled."

  constructor(options: {
    maximum?: number
    now?: () => Date
  } = {}) {
    this.#maximum = Math.max(16, Math.min(100_000, options.maximum ?? 1_000))
    this.#now = options.now ?? (() => new Date())
  }

  observe(projection: SubagentProjection): readonly SubagentLifecycleIncident[] {
    this.#assertEnabled()
    const observedAt = this.#now().toISOString()
    const present = new Set<string>()
    for (const row of projection.rows) {
      present.add(row.id)
      const previous = this.#previous.get(row.id)
      this.#observeHeartbeat(row, observedAt)
      this.#observeCrash(row, previous, observedAt)
      this.#observeReconnect(row, previous, observedAt)
      this.#observeBudget(row, observedAt)
      this.#observeLateResults(row, observedAt)
      this.#previous.set(row.id, row)
    }
    for (const id of [...this.#previous.keys()]) {
      if (!present.has(id)) this.#previous.delete(id)
    }
    this.#prune()
    return this.list()
  }

  list(input: {
    childId?: string
    unacknowledgedOnly?: boolean
    kinds?: readonly SubagentLifecycleIncident["kind"][]
  } = {}): readonly SubagentLifecycleIncident[] {
    this.#assertEnabled()
    const kinds = new Set(input.kinds ?? [])
    return sortStable(
      [...this.#incidents.values()]
        .filter((incident) => !input.childId || incident.childId === input.childId)
        .filter((incident) => !input.unacknowledgedOnly || !incident.acknowledged)
        .filter((incident) => kinds.size === 0 || kinds.has(incident.kind)),
      (left, right) =>
        compareText(right.lastObservedAt, left.lastObservedAt) ||
        compareNumber(severityRank(left.severity), severityRank(right.severity)) ||
        compareText(left.id, right.id),
    )
  }

  acknowledge(id: string): SubagentLifecycleIncident {
    this.#assertEnabled()
    const current = this.#incidents.get(id)
    if (!current) throw new Error(`Unknown subagent lifecycle incident: ${id}`)
    const next = Object.freeze({
      ...current,
      acknowledged: true,
    })
    this.#incidents.set(id, next)
    return next
  }

  acknowledgeChild(childId: string): number {
    this.#assertEnabled()
    let changed = 0
    for (const incident of this.#incidents.values()) {
      if (incident.childId !== childId || incident.acknowledged) continue
      this.#incidents.set(incident.id, Object.freeze({
        ...incident,
        acknowledged: true,
      }))
      changed += 1
    }
    return changed
  }

  clearAcknowledged(): number {
    this.#assertEnabled()
    let removed = 0
    for (const [id, incident] of this.#incidents) {
      if (!incident.acknowledged) continue
      this.#incidents.delete(id)
      removed += 1
    }
    return removed
  }

  disable(reason = "Subagent lifecycle monitor is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Subagent lifecycle monitor is disabled.")
    this.#previous.clear()
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#assertEnabled()
    this.#incidents.clear()
    this.#previous.clear()
  }

  #observeHeartbeat(row: SubagentRow, observedAt: string): void {
    if (row.terminal) return
    if (row.heartbeat.phase === "due") {
      this.#record({
        row,
        kind: "heartbeat_due",
        severity: "info",
        message: "Subagent heartbeat is due.",
        eventIds: row.heartbeat.sourceEventId ? [row.heartbeat.sourceEventId] : [],
        observedAt,
      })
      return
    }
    if (row.heartbeat.phase === "stale" || row.heartbeat.phase === "missing") {
      this.#record({
        row,
        kind: "heartbeat_stale",
        severity: "warning",
        message:
          row.heartbeat.phase === "missing"
            ? "Subagent has no admitted canonical heartbeat."
            : "Subagent heartbeat is stale.",
        eventIds: row.heartbeat.sourceEventId ? [row.heartbeat.sourceEventId] : [],
        observedAt,
      })
      return
    }
    if (row.heartbeat.phase === "timed_out") {
      this.#record({
        row,
        kind: "heartbeat_timeout",
        severity: "error",
        message: "Subagent heartbeat timed out and requires owner recovery.",
        eventIds: row.heartbeat.sourceEventId ? [row.heartbeat.sourceEventId] : [],
        observedAt,
      })
    }
  }

  #observeCrash(
    row: SubagentRow,
    previous: SubagentRow | undefined,
    observedAt: string,
  ): void {
    if (!row.crashed) return
    if (previous?.crashed && previous.attempt === row.attempt) return
    this.#record({
      row,
      kind: "crashed",
      severity: "error",
      message: row.error?.message ?? "Subagent crashed.",
      eventIds: unique([
        row.error?.eventId,
        row.heartbeat.sourceEventId,
      ]),
      observedAt,
    })
  }

  #observeReconnect(
    row: SubagentRow,
    previous: SubagentRow | undefined,
    observedAt: string,
  ): void {
    if (!previous) return
    const recovered =
      (
        previous.crashed ||
        previous.heartbeat.phase === "timed_out" ||
        previous.lifecycle === "failed"
      ) &&
      (
        row.lifecycle === "resuming" ||
        row.lifecycle === "recovering" ||
        row.lifecycle === "running"
      )
    const newAttempt = row.attempt > previous.attempt
    if (!recovered && !newAttempt) return
    this.#record({
      row,
      kind: "reconnected",
      severity: "info",
      message: `Subagent reconnected on attempt ${row.attempt}.`,
      eventIds: row.eventIds.filter((id) => !previous.eventIds.includes(id)),
      observedAt,
    })
  }

  #observeBudget(row: SubagentRow, observedAt: string): void {
    if (!row.budget.exceeded) return
    this.#record({
      row,
      kind: "budget_exceeded",
      severity: "error",
      message: row.budget.warnings.join("; ") || "Subagent budget exceeded.",
      eventIds: row.eventIds.slice(-5),
      observedAt,
    })
  }

  #observeLateResults(row: SubagentRow, observedAt: string): void {
    for (const late of row.lateResults) {
      this.#recordLate(row, late, observedAt)
    }
  }

  #recordLate(
    row: SubagentRow,
    late: LateResultRecord,
    observedAt: string,
  ): void {
    this.#record({
      row,
      kind: "late_result",
      severity: "error",
      message: late.reason,
      eventIds: [late.terminalEventId, late.resultEventId],
      observedAt,
      identity: late.id,
    })
  }

  #record(input: {
    row: SubagentRow
    kind: SubagentLifecycleIncident["kind"]
    severity: SubagentLifecycleIncident["severity"]
    message: string
    eventIds: readonly string[]
    observedAt: string
    identity?: string
  }): void {
    const id = fingerprint([
      input.row.id,
      input.row.attempt,
      input.kind,
      input.identity ?? "",
    ])
    const current = this.#incidents.get(id)
    if (current) {
      this.#incidents.set(id, Object.freeze({
        ...current,
        severity:
          severityRank(input.severity) < severityRank(current.severity)
            ? input.severity
            : current.severity,
        message: input.message,
        lastObservedAt: input.observedAt,
        occurrences: current.occurrences + 1,
        eventIds: unique([...current.eventIds, ...input.eventIds]),
      }))
      return
    }
    this.#incidents.set(id, Object.freeze({
      id,
      childId: input.row.id,
      attempt: input.row.attempt,
      kind: input.kind,
      severity: input.severity,
      message: input.message,
      firstObservedAt: input.observedAt,
      lastObservedAt: input.observedAt,
      occurrences: 1,
      eventIds: unique(input.eventIds),
      acknowledged: false,
    }))
  }

  #prune(): void {
    if (this.#incidents.size <= this.#maximum) return
    const ordered = this.list()
    const keep = new Set(ordered.slice(0, this.#maximum).map((incident) => incident.id))
    for (const id of this.#incidents.keys()) {
      if (!keep.has(id)) this.#incidents.delete(id)
    }
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function severityRank(
  severity: SubagentLifecycleIncident["severity"],
): number {
  if (severity === "error") return 0
  if (severity === "warning") return 1
  return 2
}

export class LateResultQuarantine {
  readonly #records = new Map<string, LateResultRecord>()
  readonly #maximum: number
  #enabled = true
  #disabledReason = "Late-result quarantine is disabled."

  constructor(maximum = 5_000) {
    this.#maximum = Math.max(16, Math.min(100_000, maximum))
  }

  replace(projection: SubagentProjection): readonly LateResultRecord[] {
    this.#assertEnabled()
    for (const late of projection.lateResults) this.#records.set(late.id, late)
    this.#prune()
    return this.list()
  }

  list(childId?: string): readonly LateResultRecord[] {
    this.#assertEnabled()
    return sortStable(
      [...this.#records.values()]
        .filter((record) => !childId || record.childId === childId),
      (left, right) =>
        compareNumber(right.resultSequence, left.resultSequence) ||
        compareText(left.id, right.id),
    )
  }

  require(id: string): LateResultRecord {
    this.#assertEnabled()
    const selected = this.#records.get(id)
    if (!selected) throw new Error(`Late-result quarantine record is absent: ${id}`)
    return selected
  }

  release(): never {
    this.#assertEnabled()
    throw new Error("Late subagent results are permanently fenced and cannot be released.")
  }

  clearSettled(childId?: string): number {
    this.#assertEnabled()
    let removed = 0
    for (const [id, record] of this.#records) {
      if (childId && record.childId !== childId) continue
      this.#records.delete(id)
      removed += 1
    }
    return removed
  }

  disable(reason = "Late-result quarantine is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Late-result quarantine is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  #prune(): void {
    if (this.#records.size <= this.#maximum) return
    const keep = new Set(this.list().slice(0, this.#maximum).map((record) => record.id))
    for (const id of this.#records.keys()) {
      if (!keep.has(id)) this.#records.delete(id)
    }
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export class SubagentReconnectLedger {
  readonly #frames: Array<{
    generation: number
    connected: boolean
    viewerOpen: boolean
    projectionRevision: number
    fingerprint: string
    capturedAt: string
  }> = []
  readonly #maximum: number
  #enabled = true

  constructor(maximum = 256) {
    this.#maximum = Math.max(8, Math.min(10_000, maximum))
  }

  capture(input: {
    generation: number
    connected: boolean
    viewerOpen: boolean
    projection?: SubagentProjection
    capturedAt?: string
  }) {
    if (!this.#enabled) throw new Error("Subagent reconnect ledger is disabled.")
    const frame = Object.freeze({
      generation: input.generation,
      connected: input.connected,
      viewerOpen: input.viewerOpen,
      projectionRevision: input.projection?.canonicalRevision ?? 0,
      fingerprint: input.projection?.fingerprint ?? "",
      capturedAt: input.capturedAt ?? new Date().toISOString(),
    })
    const previous = this.#frames.at(-1)
    if (
      previous &&
      frame.generation < previous.generation
    ) {
      throw new Error("Subagent reconnect generation regressed.")
    }
    this.#frames.push(frame)
    if (this.#frames.length > this.#maximum) {
      this.#frames.splice(0, this.#frames.length - this.#maximum)
    }
    return frame
  }

  settlement() {
    if (!this.#enabled) throw new Error("Subagent reconnect ledger is disabled.")
    const current = this.#frames.at(-1)
    const previous = this.#frames.at(-2)
    if (!current || !previous) return undefined
    const projectionAdvanced =
      current.projectionRevision >= previous.projectionRevision &&
      (
        current.fingerprint !== previous.fingerprint ||
        current.projectionRevision === previous.projectionRevision
      )
    return Object.freeze({
      fromGeneration: previous.generation,
      toGeneration: current.generation,
      reconnected: !previous.connected && current.connected,
      reopened: !previous.viewerOpen && current.viewerOpen,
      projectionAdvanced,
      safe:
        current.generation >= previous.generation &&
        projectionAdvanced,
      capturedAt: current.capturedAt,
    })
  }

  disable(): void {
    this.#enabled = false
  }

  enable(): void {
    this.#enabled = true
  }
}
