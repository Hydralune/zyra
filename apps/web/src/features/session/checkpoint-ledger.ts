import type { CausalEventProjection } from "../../state/contracts.ts"
import type { CheckpointRow, CompactPreview, SessionConsoleProjection } from "./projection.ts"
import {
  bounded,
  compareNumber,
  compareText,
  fingerprint,
  integer,
  record,
  sortStable,
  text,
  truth,
  unique,
} from "./value.ts"

export interface CheckpointIdentity {
  checkpointId: string
  taskId: string
  runId: string
  sessionId: string
  workflowSignature?: string
  graphSignature?: string
  topologySignature?: string
  compactBoundaryId?: string
  revision: number
}

export interface ResumeIntent {
  intentId: string
  identity: CheckpointIdentity
  expectedTaskId: string
  expectedRunId: string
  expectedSessionId: string
  expectedRevision: number
  compactFirst: boolean
  requestedAt: string
  requestedBy: string
  sealed: boolean
}

export interface ResumeAdmission {
  allowed: boolean
  code:
    | "allow"
    | "checkpoint-missing"
    | "task-mismatch"
    | "run-mismatch"
    | "session-mismatch"
    | "revision-stale"
    | "pending-writes"
    | "checkpoint-stale"
    | "checkpoint-conflict"
    | "signature-mismatch"
    | "disconnected"
    | "projection-not-ready"
  reason: string
  checkpoint?: CheckpointRow
  intent: ResumeIntent
  warnings: readonly string[]
}

export interface CompactReceiptInput {
  commandId: string
  requestId: string
  taskId: string
  runId: string
  sessionId: string
  previewFingerprint: string
  revisionBefore: number
  revisionAfter?: number
  checkpointId?: string
  compactId?: string
  compactEpoch?: number
  retainedSourceIds?: readonly string[]
  droppedSourceIds?: readonly string[]
  restoredSourceIds?: readonly string[]
  eventIds?: readonly string[]
  phase: string
  durable: boolean
  executed: boolean
  replayed?: boolean
  error?: string
}

export interface CompactSettlement {
  id: string
  phase: "accepted" | "waiting-projection" | "committed" | "rejected" | "conflict"
  commandId: string
  requestId: string
  previewFingerprint: string
  previewMatched: boolean
  revisionAdvanced: boolean
  epochAdvanced: boolean
  checkpointObserved: boolean
  eventsObserved: boolean
  retainedMatched: boolean
  droppedMatched: boolean
  restoredMatched: boolean
  missingEventIds: readonly string[]
  warnings: readonly string[]
  error?: string
}

export interface EpochRecord {
  sessionId: string
  epoch: number
  boundaryId?: string
  checkpointId?: string
  revision: number
  tokens: number
  source: string
  eventIds: readonly string[]
  createdAt: string
}

export interface EpochDelta {
  from?: EpochRecord
  to: EpochRecord
  tokenDelta: number
  revisionDelta: number
  epochDelta: number
  boundaryChanged: boolean
  checkpointChanged: boolean
  sourceChanged: boolean
  eventIdsAdded: readonly string[]
  eventIdsRemoved: readonly string[]
}

export class CheckpointLedger {
  readonly #entries = new Map<string, CheckpointRow>()
  readonly #history = new Map<string, CheckpointRow[]>()
  #revision = 0
  #enabled = true
  #disabledReason = "Checkpoint ledger is disabled."

  replace(projection: SessionConsoleProjection): void {
    this.#assertEnabled()
    const next = new Map<string, CheckpointRow>()
    for (const checkpoint of projection.checkpoints) {
      const existing = this.#entries.get(checkpoint.id)
      next.set(checkpoint.id, checkpoint)
      if (!existing || checkpointChanged(existing, checkpoint)) {
        const history = this.#history.get(checkpoint.id) ?? []
        history.push(checkpoint)
        this.#history.set(checkpoint.id, history.slice(-100))
      }
    }
    this.#entries.clear()
    for (const [id, checkpoint] of next) this.#entries.set(id, checkpoint)
    this.#revision += 1
  }

  upsert(checkpoint: CheckpointRow): void {
    this.#assertEnabled()
    const existing = this.#entries.get(checkpoint.id)
    if (existing && !checkpointChanged(existing, checkpoint)) return
    this.#entries.set(checkpoint.id, checkpoint)
    const history = this.#history.get(checkpoint.id) ?? []
    history.push(checkpoint)
    this.#history.set(checkpoint.id, history.slice(-100))
    this.#revision += 1
  }

  get(checkpointId: string): CheckpointRow | undefined {
    this.#assertEnabled()
    return this.#entries.get(checkpointId)
  }

  list(sessionId?: string): CheckpointRow[] {
    this.#assertEnabled()
    return sortStable(
      [...this.#entries.values()].filter((entry) => !sessionId || entry.sessionId === sessionId),
      (left, right) =>
        compareNumber(right.sequence, left.sequence) ||
        compareText(left.id, right.id),
    )
  }

  history(checkpointId: string): readonly CheckpointRow[] {
    this.#assertEnabled()
    return Object.freeze([...(this.#history.get(checkpointId) ?? [])])
  }

  admit(intent: ResumeIntent, projection: SessionConsoleProjection): ResumeAdmission {
    this.#assertEnabled()
    const checkpoint = this.#entries.get(intent.identity.checkpointId)
    const warnings: string[] = []
    if (!projection.connected) return admission(intent, "disconnected", "Canonical projection is disconnected.", checkpoint)
    if (!projection.ready) return admission(intent, "projection-not-ready", "Canonical projection snapshot is incomplete.", checkpoint)
    if (!checkpoint) return admission(intent, "checkpoint-missing", "Checkpoint is absent from canonical projection.")
    if (checkpoint.taskId !== intent.expectedTaskId) {
      return admission(intent, "task-mismatch", "Checkpoint task identity differs.", checkpoint)
    }
    if (checkpoint.runId !== intent.expectedRunId) {
      return admission(intent, "run-mismatch", "Checkpoint run identity differs.", checkpoint)
    }
    if (checkpoint.sessionId !== intent.expectedSessionId) {
      return admission(intent, "session-mismatch", "Checkpoint session identity differs.", checkpoint)
    }
    if (projection.revision !== intent.expectedRevision) {
      return admission(intent, "revision-stale", "Canonical projection revision advanced.", checkpoint)
    }
    if (checkpoint.disposition === "conflict") {
      return admission(intent, "checkpoint-conflict", checkpoint.conflictReason ?? "Checkpoint conflicts.", checkpoint)
    }
    if (checkpoint.disposition === "stale") {
      return admission(intent, "checkpoint-stale", checkpoint.staleReason ?? "Checkpoint is stale.", checkpoint)
    }
    if (checkpoint.pendingWrites > checkpoint.committedWrites) {
      return admission(intent, "pending-writes", "Checkpoint has uncommitted pending writes.", checkpoint)
    }
    const signatures = signatureWarnings(intent.identity, checkpoint)
    if (signatures.length) {
      return {
        ...admission(intent, "signature-mismatch", signatures[0]!, checkpoint),
        warnings: Object.freeze(signatures),
      }
    }
    if (checkpoint.disposition === "pending") warnings.push("checkpoint disposition is pending")
    if (!checkpoint.correlationId) warnings.push("checkpoint has no correlation identity")
    if (!checkpoint.workflowSignature) warnings.push("workflow signature unavailable")
    if (!checkpoint.graphSignature) warnings.push("graph signature unavailable")
    if (!checkpoint.topologySignature) warnings.push("topology signature unavailable")
    return Object.freeze({
      allowed: true,
      code: "allow",
      reason: "Exact checkpoint identity is eligible for canonical resume.",
      checkpoint,
      intent,
      warnings: Object.freeze(warnings),
    })
  }

  dispositionCounts(): Readonly<Record<string, number>> {
    this.#assertEnabled()
    const counts: Record<string, number> = {}
    for (const checkpoint of this.#entries.values()) {
      counts[checkpoint.disposition] = (counts[checkpoint.disposition] ?? 0) + 1
    }
    return Object.freeze(counts)
  }

  pendingWriteCount(sessionId?: string): number {
    this.#assertEnabled()
    return this.list(sessionId).reduce((total, checkpoint) => total + checkpoint.pendingWrites, 0)
  }

  committedWriteCount(sessionId?: string): number {
    this.#assertEnabled()
    return this.list(sessionId).reduce((total, checkpoint) => total + checkpoint.committedWrites, 0)
  }

  conflictIds(sessionId?: string): string[] {
    this.#assertEnabled()
    return this.list(sessionId)
      .filter((checkpoint) => checkpoint.disposition === "conflict")
      .map((checkpoint) => checkpoint.id)
  }

  staleIds(sessionId?: string): string[] {
    this.#assertEnabled()
    return this.list(sessionId)
      .filter((checkpoint) => checkpoint.disposition === "stale")
      .map((checkpoint) => checkpoint.id)
  }

  exactResumeIds(sessionId?: string): string[] {
    this.#assertEnabled()
    return this.list(sessionId)
      .filter((checkpoint) => checkpoint.exactResumeEligible)
      .map((checkpoint) => checkpoint.id)
  }

  audit(): Readonly<Record<string, unknown>> {
    this.#assertEnabled()
    const all = this.list()
    return Object.freeze({
      revision: this.#revision,
      checkpointCount: all.length,
      sessionCount: new Set(all.map((entry) => entry.sessionId)).size,
      dispositionCounts: this.dispositionCounts(),
      pendingWrites: this.pendingWriteCount(),
      committedWrites: this.committedWriteCount(),
      conflicts: this.conflictIds(),
      stale: this.staleIds(),
      exactResume: this.exactResumeIds(),
      historyEntries: [...this.#history.values()].reduce((total, entries) => total + entries.length, 0),
    })
  }

  disable(reason = "Checkpoint ledger is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Checkpoint ledger is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#assertEnabled()
    this.#entries.clear()
    this.#history.clear()
    this.#revision += 1
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export class ContextEpochLedger {
  readonly #records = new Map<string, EpochRecord[]>()
  #enabled = true
  #disabledReason = "Context epoch ledger is disabled."

  append(recordValue: EpochRecord): EpochDelta {
    this.#assertEnabled()
    const normalized = normalizeEpoch(recordValue)
    const records = this.#records.get(normalized.sessionId) ?? []
    const previous = records.at(-1)
    if (previous) {
      if (normalized.epoch < previous.epoch) {
        throw new Error("Context epoch cannot move backwards.")
      }
      if (normalized.epoch === previous.epoch && normalized.revision < previous.revision) {
        throw new Error("Context epoch revision cannot move backwards.")
      }
      if (
        normalized.epoch === previous.epoch &&
        normalized.revision === previous.revision &&
        epochFingerprint(normalized) !== epochFingerprint(previous)
      ) {
        throw new Error("Context epoch payload conflicts at the same revision.")
      }
      if (epochFingerprint(normalized) === epochFingerprint(previous)) {
        return epochDelta(previous, normalized)
      }
    }
    records.push(normalized)
    this.#records.set(normalized.sessionId, records.slice(-1000))
    return epochDelta(previous, normalized)
  }

  latest(sessionId: string): EpochRecord | undefined {
    this.#assertEnabled()
    return this.#records.get(sessionId)?.at(-1)
  }

  at(sessionId: string, epoch: number): EpochRecord | undefined {
    this.#assertEnabled()
    return [...(this.#records.get(sessionId) ?? [])]
      .reverse()
      .find((entry) => entry.epoch === epoch)
  }

  list(sessionId: string): readonly EpochRecord[] {
    this.#assertEnabled()
    return Object.freeze([...(this.#records.get(sessionId) ?? [])])
  }

  deltas(sessionId: string): readonly EpochDelta[] {
    this.#assertEnabled()
    const records = this.#records.get(sessionId) ?? []
    return Object.freeze(records.map((entry, index) => epochDelta(records[index - 1], entry)))
  }

  compactCount(sessionId: string): number {
    this.#assertEnabled()
    return this.deltas(sessionId).filter((delta) => delta.epochDelta > 0).length
  }

  tokenSavings(sessionId: string): number {
    this.#assertEnabled()
    return this.deltas(sessionId)
      .filter((delta) => delta.tokenDelta < 0)
      .reduce((total, delta) => total - delta.tokenDelta, 0)
  }

  restoredSources(sessionId: string): readonly string[] {
    this.#assertEnabled()
    return Object.freeze(unique(
      this.list(sessionId)
        .map((entry) => entry.source)
        .filter((source) => /restore|resume|snapshot/i.test(source)),
    ))
  }

  rewind(sessionId: string, epoch: number): EpochRecord {
    this.#assertEnabled()
    const records = this.#records.get(sessionId) ?? []
    const targetIndex = records.findIndex((entry) => entry.epoch === epoch)
    if (targetIndex < 0) throw new Error("Requested context epoch is unavailable.")
    const target = records[targetIndex]!
    this.#records.set(sessionId, records.slice(0, targetIndex + 1))
    return target
  }

  disable(reason = "Context epoch ledger is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Context epoch ledger is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(sessionId?: string): void {
    this.#assertEnabled()
    if (sessionId) this.#records.delete(sessionId)
    else this.#records.clear()
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export class CompactReceiptReconciler {
  readonly #settlements = new Map<string, CompactSettlement>()
  readonly #inputs = new Map<string, CompactReceiptInput>()
  #enabled = true
  #disabledReason = "Compact receipt reconciler is disabled."

  admit(input: CompactReceiptInput, preview: CompactPreview): CompactSettlement {
    this.#assertEnabled()
    const id = compactSettlementId(input)
    const existingInput = this.#inputs.get(id)
    if (existingInput && fingerprint([existingInput]) !== fingerprint([input])) {
      const conflict = settlement(input, {
        phase: "conflict",
        previewMatched: false,
        error: "Compact receipt identity was reused with another payload.",
      })
      this.#settlements.set(id, conflict)
      return conflict
    }
    this.#inputs.set(id, input)
    if (input.previewFingerprint !== preview.fingerprint) {
      const conflict = settlement(input, {
        phase: "conflict",
        previewMatched: false,
        error: "Compact receipt does not match the accepted preview.",
      })
      this.#settlements.set(id, conflict)
      return conflict
    }
    if (input.phase === "rejected" || input.error) {
      const rejected = settlement(input, {
        phase: "rejected",
        previewMatched: true,
        error: input.error ?? "Compact command was rejected.",
      })
      this.#settlements.set(id, rejected)
      return rejected
    }
    if (!input.durable || !input.executed) {
      const rejected = settlement(input, {
        phase: "rejected",
        previewMatched: true,
        error: "Compact receipt is not durable and executed.",
      })
      this.#settlements.set(id, rejected)
      return rejected
    }
    const accepted = settlement(input, {
      phase: "accepted",
      previewMatched: true,
      retainedMatched: sameSet(input.retainedSourceIds, preview.retainedSourceIds),
      droppedMatched: sameSet(input.droppedSourceIds, preview.droppedSourceIds),
      restoredMatched: sameSet(input.restoredSourceIds, preview.restoredSourceIds),
    })
    this.#settlements.set(id, accepted)
    return accepted
  }

  reconcile(
    input: CompactReceiptInput,
    preview: CompactPreview,
    projection: SessionConsoleProjection,
    events: Readonly<Record<string, CausalEventProjection>>,
  ): CompactSettlement {
    this.#assertEnabled()
    const admitted = this.admit(input, preview)
    if (admitted.phase === "conflict" || admitted.phase === "rejected") return admitted
    const missingEventIds = (input.eventIds ?? [])
      .filter((eventId) => !events[eventId])
    const revisionAdvanced =
      projection.revision > input.revisionBefore &&
      (input.revisionAfter === undefined || projection.revision >= input.revisionAfter)
    const checkpointObserved =
      !input.checkpointId ||
      projection.checkpoints.some((checkpoint) => checkpoint.id === input.checkpointId)
    const eventsObserved = missingEventIds.length === 0
    const epochAdvanced =
      input.compactEpoch === undefined ||
      (projection.context?.compactEpoch ?? -1) >= input.compactEpoch
    const warnings: string[] = []
    if (!admitted.retainedMatched) warnings.push("retained references differ from preview")
    if (!admitted.droppedMatched) warnings.push("dropped references differ from preview")
    if (!admitted.restoredMatched) warnings.push("restored references differ from preview")
    if (!revisionAdvanced) warnings.push("canonical revision has not advanced")
    if (!epochAdvanced) warnings.push("compact epoch has not advanced")
    if (!checkpointObserved) warnings.push("compact checkpoint is not projected")
    if (!eventsObserved) warnings.push("compact receipt events are not fully projected")
    const phase =
      revisionAdvanced &&
      epochAdvanced &&
      checkpointObserved &&
      eventsObserved &&
      admitted.retainedMatched &&
      admitted.droppedMatched &&
      admitted.restoredMatched
        ? "committed"
        : "waiting-projection"
    const updated: CompactSettlement = Object.freeze({
      ...admitted,
      phase,
      revisionAdvanced,
      epochAdvanced,
      checkpointObserved,
      eventsObserved,
      missingEventIds: Object.freeze(missingEventIds),
      warnings: Object.freeze(warnings),
    })
    this.#settlements.set(updated.id, updated)
    return updated
  }

  get(commandId: string, requestId: string): CompactSettlement | undefined {
    this.#assertEnabled()
    return this.#settlements.get(fingerprint([commandId, requestId]))
  }

  list(): readonly CompactSettlement[] {
    this.#assertEnabled()
    return Object.freeze([...this.#settlements.values()])
  }

  pending(): readonly CompactSettlement[] {
    this.#assertEnabled()
    return Object.freeze(this.list().filter((entry) =>
      entry.phase === "accepted" || entry.phase === "waiting-projection"))
  }

  conflicts(): readonly CompactSettlement[] {
    this.#assertEnabled()
    return Object.freeze(this.list().filter((entry) => entry.phase === "conflict"))
  }

  disable(reason = "Compact receipt reconciler is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Compact receipt reconciler is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#assertEnabled()
    this.#settlements.clear()
    this.#inputs.clear()
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export function createResumeIntent(input: {
  checkpoint: CheckpointRow
  projection: SessionConsoleProjection
  compactFirst?: boolean
  requestedAt?: string
  requestedBy?: string
  sealed?: boolean
}): ResumeIntent {
  const checkpoint = input.checkpoint
  const requestedAt = input.requestedAt ?? new Date().toISOString()
  const identity: CheckpointIdentity = Object.freeze({
    checkpointId: checkpoint.id,
    taskId: checkpoint.taskId,
    runId: checkpoint.runId,
    sessionId: checkpoint.sessionId,
    workflowSignature: checkpoint.workflowSignature,
    graphSignature: checkpoint.graphSignature,
    topologySignature: checkpoint.topologySignature,
    compactBoundaryId: checkpoint.compactBoundaryId,
    revision: input.projection.revision,
  })
  return Object.freeze({
    intentId: fingerprint([
      identity,
      input.projection.revision,
      input.compactFirst === true,
      requestedAt,
      input.requestedBy,
    ]),
    identity,
    expectedTaskId: input.projection.taskId,
    expectedRunId: checkpoint.runId,
    expectedSessionId: input.projection.activeSessionId ?? checkpoint.sessionId,
    expectedRevision: input.projection.revision,
    compactFirst: input.compactFirst === true,
    requestedAt,
    requestedBy: text(input.requestedBy, "console-operator"),
    sealed: input.sealed === true,
  })
}

function admission(
  intent: ResumeIntent,
  code: ResumeAdmission["code"],
  reason: string,
  checkpoint?: CheckpointRow,
): ResumeAdmission {
  return Object.freeze({
    allowed: false,
    code,
    reason,
    checkpoint,
    intent,
    warnings: Object.freeze([]),
  })
}

function signatureWarnings(
  identity: CheckpointIdentity,
  checkpoint: CheckpointRow,
): string[] {
  const warnings: string[] = []
  if (
    identity.workflowSignature &&
    checkpoint.workflowSignature &&
    identity.workflowSignature !== checkpoint.workflowSignature
  ) warnings.push("workflow signature differs")
  if (
    identity.graphSignature &&
    checkpoint.graphSignature &&
    identity.graphSignature !== checkpoint.graphSignature
  ) warnings.push("graph signature differs")
  if (
    identity.topologySignature &&
    checkpoint.topologySignature &&
    identity.topologySignature !== checkpoint.topologySignature
  ) warnings.push("topology signature differs")
  if (
    identity.compactBoundaryId &&
    checkpoint.compactBoundaryId &&
    identity.compactBoundaryId !== checkpoint.compactBoundaryId
  ) warnings.push("compact boundary differs")
  return warnings
}

function normalizeEpoch(value: EpochRecord): EpochRecord {
  if (!value.sessionId) throw new TypeError("Context epoch requires session identity.")
  if (!Number.isSafeInteger(value.epoch) || value.epoch < 0) {
    throw new TypeError("Context epoch must be a non-negative safe integer.")
  }
  if (!Number.isSafeInteger(value.revision) || value.revision < 0) {
    throw new TypeError("Context epoch revision must be a non-negative safe integer.")
  }
  return Object.freeze({
    ...value,
    tokens: Math.max(0, integer(value.tokens)),
    source: text(value.source, "canonical-projection"),
    eventIds: Object.freeze(unique(value.eventIds)),
  })
}

function epochDelta(from: EpochRecord | undefined, to: EpochRecord): EpochDelta {
  const fromEvents = new Set(from?.eventIds ?? [])
  const toEvents = new Set(to.eventIds)
  return Object.freeze({
    from,
    to,
    tokenDelta: to.tokens - (from?.tokens ?? 0),
    revisionDelta: to.revision - (from?.revision ?? 0),
    epochDelta: to.epoch - (from?.epoch ?? 0),
    boundaryChanged: from?.boundaryId !== to.boundaryId,
    checkpointChanged: from?.checkpointId !== to.checkpointId,
    sourceChanged: from?.source !== to.source,
    eventIdsAdded: Object.freeze(to.eventIds.filter((eventId) => !fromEvents.has(eventId))),
    eventIdsRemoved: Object.freeze((from?.eventIds ?? []).filter((eventId) => !toEvents.has(eventId))),
  })
}

function epochFingerprint(value: EpochRecord): string {
  return fingerprint([
    value.sessionId,
    value.epoch,
    value.boundaryId,
    value.checkpointId,
    value.revision,
    value.tokens,
    value.source,
    value.eventIds,
  ])
}

function compactSettlementId(input: CompactReceiptInput): string {
  return fingerprint([input.commandId, input.requestId])
}

function settlement(
  input: CompactReceiptInput,
  patch: Partial<CompactSettlement>,
): CompactSettlement {
  return Object.freeze({
    id: compactSettlementId(input),
    phase: "accepted",
    commandId: input.commandId,
    requestId: input.requestId,
    previewFingerprint: input.previewFingerprint,
    previewMatched: true,
    revisionAdvanced: false,
    epochAdvanced: false,
    checkpointObserved: false,
    eventsObserved: false,
    retainedMatched: true,
    droppedMatched: true,
    restoredMatched: true,
    missingEventIds: Object.freeze([]),
    warnings: Object.freeze([]),
    ...patch,
  })
}

function sameSet(left: readonly string[] | undefined, right: readonly string[]): boolean {
  if (left === undefined) return true
  const leftSet = new Set(left)
  const rightSet = new Set(right)
  if (leftSet.size !== rightSet.size) return false
  for (const value of leftSet) if (!rightSet.has(value)) return false
  return true
}

function checkpointChanged(left: CheckpointRow, right: CheckpointRow): boolean {
  return fingerprint([
    left.id,
    left.disposition,
    left.pendingWrites,
    left.committedWrites,
    left.sequence,
    left.staleReason,
    left.conflictReason,
  ]) !== fingerprint([
    right.id,
    right.disposition,
    right.pendingWrites,
    right.committedWrites,
    right.sequence,
    right.staleReason,
    right.conflictReason,
  ])
}
