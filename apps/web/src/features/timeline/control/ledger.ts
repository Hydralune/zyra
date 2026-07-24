import {
  RecoveryControlPhase,
  activeControlPhase,
  terminalControlPhase,
  type RecoveryControlLedgerSnapshot,
  type RecoveryControlObservation,
  type RecoveryControlPhaseValue,
  type RecoveryControlRaceResult,
  type RecoveryControlReceipt,
} from "./contracts.ts"

const PHASE_RANK: Readonly<Record<RecoveryControlPhaseValue, number>> =
  Object.freeze({
    [RecoveryControlPhase.VALIDATING]: 10,
    [RecoveryControlPhase.SUBMITTING]: 20,
    [RecoveryControlPhase.PENDING]: 30,
    [RecoveryControlPhase.TIMED_OUT]: 35,
    [RecoveryControlPhase.DENIED]: 70,
    [RecoveryControlPhase.FAILED]: 70,
    [RecoveryControlPhase.CANCELLED]: 75,
    [RecoveryControlPhase.SUPERSEDED]: 80,
    [RecoveryControlPhase.APPLIED]: 90,
  })

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function unique(values: Iterable<string>): readonly string[] {
  return freeze([...new Set([...values].filter(Boolean))])
}

function evidenceKey(
  value: RecoveryControlReceipt["ownerEvidence"][number],
): string {
  return [
    value.owner,
    value.operation ?? "",
    value.receiptId ?? "",
    value.workerId ?? "",
    value.leaseId ?? "",
    value.attemptId ?? "",
    value.nodeId ?? "",
    value.graphId ?? "",
    value.checkpointId ?? "",
    value.revision ?? "",
  ].join("|")
}

function mergeEvidence(
  left: RecoveryControlReceipt["ownerEvidence"],
  right: RecoveryControlReceipt["ownerEvidence"],
): RecoveryControlReceipt["ownerEvidence"] {
  const values = new Map(
    left.map((value) => [evidenceKey(value), value] as const),
  )
  for (const value of right) values.set(evidenceKey(value), value)
  return freeze(values.values())
}

function observationKey(value: RecoveryControlObservation): string {
  return [
    value.source,
    value.phase,
    value.observedRevision ?? "",
    value.observedAt,
    value.eventIds.join(","),
    value.conflictReason ?? "",
  ].join("|")
}

function mergeObservations(
  left: readonly RecoveryControlObservation[],
  right: readonly RecoveryControlObservation[],
  maximum: number,
): readonly RecoveryControlObservation[] {
  const values = new Map(
    left.map((value) => [observationKey(value), value] as const),
  )
  for (const value of right) values.set(observationKey(value), value)
  return freeze(
    [...values.values()]
      .sort(
        (a, b) =>
          a.observedAt - b.observedAt ||
          (a.observedRevision ?? -1) - (b.observedRevision ?? -1),
      )
      .slice(-maximum),
  )
}

function terminalConflict(
  current: RecoveryControlReceipt,
  incoming: RecoveryControlReceipt,
): string | undefined {
  if (!terminalControlPhase(current.phase)) return undefined
  if (!terminalControlPhase(incoming.phase)) return undefined
  if (current.phase === incoming.phase) return undefined
  if (
    current.phase === RecoveryControlPhase.APPLIED &&
    incoming.phase !== RecoveryControlPhase.APPLIED
  ) {
    return "applied receipt cannot be replaced by a weaker terminal phase"
  }
  if (
    incoming.phase === RecoveryControlPhase.APPLIED &&
    current.phase !== RecoveryControlPhase.APPLIED
  ) {
    const currentRevision = current.observedRevision ?? -1
    const incomingRevision = incoming.observedRevision ?? -1
    if (incomingRevision < currentRevision) {
      return "late applied receipt has an older canonical revision"
    }
    return undefined
  }
  const currentRevision = current.observedRevision ?? -1
  const incomingRevision = incoming.observedRevision ?? -1
  if (incomingRevision <= currentRevision) {
    return "conflicting terminal receipt is not newer"
  }
  return undefined
}

function ownerConflict(
  current: RecoveryControlReceipt,
  incoming: RecoveryControlReceipt,
): string | undefined {
  if (current.taskId !== incoming.taskId || current.runId !== incoming.runId) {
    return "receipt crossed task/run ownership"
  }
  if (
    current.commandId !== incoming.commandId ||
    current.requestId !== incoming.requestId
  ) {
    return "receipt identity changed"
  }
  if (
    current.requestDigest !== incoming.requestDigest ||
    current.idempotencyKey !== incoming.idempotencyKey
  ) {
    return "receipt reused identity with different request content"
  }
  const expected = current.expectedOwner
  for (const evidence of incoming.ownerEvidence) {
    if (
      expected.workerId &&
      evidence.workerId &&
      expected.workerId !== evidence.workerId &&
      current.action !== "reassign"
    ) {
      return "receipt came from an unexpected worker owner"
    }
    if (
      expected.leaseId &&
      evidence.leaseId &&
      expected.leaseId !== evidence.leaseId &&
      current.action !== "reassign"
    ) {
      return "receipt came from an unexpected worker lease"
    }
    if (
      expected.checkpointId &&
      evidence.checkpointId &&
      expected.checkpointId !== evidence.checkpointId
    ) {
      return "receipt came from an unexpected checkpoint"
    }
  }
  return undefined
}

function preferredPhase(
  current: RecoveryControlReceipt,
  incoming: RecoveryControlReceipt,
): RecoveryControlPhaseValue {
  const currentRank = PHASE_RANK[current.phase]
  const incomingRank = PHASE_RANK[incoming.phase]
  if (incomingRank > currentRank) return incoming.phase
  if (incomingRank < currentRank) return current.phase
  const currentRevision = current.observedRevision ?? -1
  const incomingRevision = incoming.observedRevision ?? -1
  if (incomingRevision > currentRevision) return incoming.phase
  if (incoming.updatedAt > current.updatedAt) return incoming.phase
  return current.phase
}

function appendConflict(
  receipt: RecoveryControlReceipt,
  reason: string,
  now: number,
  maximumObservations: number,
): RecoveryControlReceipt {
  const observation: RecoveryControlObservation = Object.freeze({
    phase: receipt.phase,
    observedAt: now,
    observedRevision: receipt.observedRevision,
    eventIds: Object.freeze([]),
    rowKeys: Object.freeze([]),
    ownerEvidence: Object.freeze([]),
    summary: `Ignored conflicting control receipt: ${reason}.`,
    source: "projection",
    terminal: terminalControlPhase(receipt.phase),
    consistent: false,
    conflictReason: reason,
  })
  return Object.freeze({
    ...receipt,
    updatedAt: Math.max(receipt.updatedAt, now),
    observations: mergeObservations(
      receipt.observations,
      [observation],
      maximumObservations,
    ),
    metadata: Object.freeze({
      ...receipt.metadata,
      conflictingObservation: true,
      conflictReason: reason,
    }),
  })
}

function mergeReceipt(
  current: RecoveryControlReceipt,
  incoming: RecoveryControlReceipt,
  maximumObservations: number,
): RecoveryControlReceipt {
  const conflict =
    ownerConflict(current, incoming) ?? terminalConflict(current, incoming)
  if (conflict) {
    return appendConflict(
      current,
      conflict,
      Math.max(current.updatedAt, incoming.updatedAt),
      maximumObservations,
    )
  }
  const phase = preferredPhase(current, incoming)
  const incomingPreferred =
    phase === incoming.phase &&
    (PHASE_RANK[incoming.phase] > PHASE_RANK[current.phase] ||
      (incoming.observedRevision ?? -1) >=
        (current.observedRevision ?? -1))
  const selected = incomingPreferred ? incoming : current
  return Object.freeze({
    ...selected,
    phase,
    updatedAt: Math.max(current.updatedAt, incoming.updatedAt),
    deadlineAt: Math.max(current.deadlineAt, incoming.deadlineAt),
    attempt: Math.max(current.attempt, incoming.attempt),
    denied: current.denied || incoming.denied,
    interventionCounted:
      current.interventionCounted || incoming.interventionCounted,
    operatorInterventionAttemptCount:
      incoming.operatorInterventionAttemptCount ??
      current.operatorInterventionAttemptCount,
    humanInterventionCount:
      incoming.humanInterventionCount ?? current.humanInterventionCount,
    noHumanWait: current.noHumanWait || incoming.noHumanWait,
    replayed: current.replayed || incoming.replayed,
    timedOut: current.timedOut || incoming.timedOut,
    detached: current.detached || incoming.detached,
    observedRevision: Math.max(
      current.observedRevision ?? -1,
      incoming.observedRevision ?? -1,
    ) < 0
      ? undefined
      : Math.max(
          current.observedRevision ?? -1,
          incoming.observedRevision ?? -1,
        ),
    observedEventIds: unique([
      ...current.observedEventIds,
      ...incoming.observedEventIds,
    ]),
    observedRowKeys: unique([
      ...current.observedRowKeys,
      ...incoming.observedRowKeys,
    ]),
    ownerEvidence: mergeEvidence(
      current.ownerEvidence,
      incoming.ownerEvidence,
    ),
    observations: mergeObservations(
      current.observations,
      incoming.observations,
      maximumObservations,
    ),
    response: incoming.response ?? current.response,
    metadata: Object.freeze({
      ...current.metadata,
      ...incoming.metadata,
    }),
  })
}

function receiptSort(
  left: RecoveryControlReceipt,
  right: RecoveryControlReceipt,
): number {
  return (
    right.updatedAt - left.updatedAt ||
    right.submittedAt - left.submittedAt ||
    left.commandId.localeCompare(right.commandId)
  )
}

export interface RecoveryControlLedgerOptions {
  maximumReceipts?: number
  maximumObservations?: number
}

export class RecoveryControlLedger {
  readonly maximumReceipts: number
  readonly maximumObservations: number
  #byId = new Map<string, RecoveryControlReceipt>()
  #byCommand = new Map<string, string>()
  #byRequest = new Map<string, string>()
  #byIdempotency = new Map<string, string>()
  #revision = 0
  #snapshot?: RecoveryControlLedgerSnapshot

  constructor(options: RecoveryControlLedgerOptions = {}) {
    this.maximumReceipts = Math.max(
      10,
      Math.floor(options.maximumReceipts ?? 500),
    )
    this.maximumObservations = Math.max(
      4,
      Math.floor(options.maximumObservations ?? 64),
    )
  }

  get revision(): number {
    return this.#revision
  }

  receipt(id: string): RecoveryControlReceipt | undefined {
    return this.#byId.get(id)
  }

  receiptForCommand(commandId: string): RecoveryControlReceipt | undefined {
    const id = this.#byCommand.get(commandId)
    return id ? this.#byId.get(id) : undefined
  }

  receiptForRequest(requestId: string): RecoveryControlReceipt | undefined {
    const id = this.#byRequest.get(requestId)
    return id ? this.#byId.get(id) : undefined
  }

  receiptForIdempotency(
    idempotencyKey: string,
  ): RecoveryControlReceipt | undefined {
    const id = this.#byIdempotency.get(idempotencyKey)
    return id ? this.#byId.get(id) : undefined
  }

  remember(incoming: RecoveryControlReceipt): RecoveryControlReceipt {
    const byId = this.#byId.get(incoming.id)
    const byCommand = this.receiptForCommand(incoming.commandId)
    const byRequest = this.receiptForRequest(incoming.requestId)
    const byIdempotency = this.receiptForIdempotency(
      incoming.idempotencyKey,
    )
    const existing = byId ?? byCommand ?? byRequest ?? byIdempotency
    if (
      existing &&
      existing.requestDigest !== incoming.requestDigest
    ) {
      const conflicted = appendConflict(
        existing,
        "idempotency or command identity was reused with different content",
        incoming.updatedAt,
        this.maximumObservations,
      )
      this.#write(conflicted)
      return conflicted
    }
    const next = existing
      ? mergeReceipt(existing, incoming, this.maximumObservations)
      : incoming
    this.#write(next)
    this.#prune()
    return next
  }

  observe(
    receiptId: string,
    observation: RecoveryControlObservation,
  ): RecoveryControlReceipt | undefined {
    const current = this.#byId.get(receiptId)
    if (!current) return undefined
    const incoming: RecoveryControlReceipt = Object.freeze({
      ...current,
      phase: observation.phase,
      updatedAt: observation.observedAt,
      summary: observation.summary || current.summary,
      denied:
        current.denied ||
        observation.phase === RecoveryControlPhase.DENIED,
      noHumanWait:
        current.noHumanWait ||
        observation.phase !== RecoveryControlPhase.PENDING,
      observedRevision:
        observation.observedRevision ?? current.observedRevision,
      observedEventIds: unique([
        ...current.observedEventIds,
        ...observation.eventIds,
      ]),
      observedRowKeys: unique([
        ...current.observedRowKeys,
        ...observation.rowKeys,
      ]),
      ownerEvidence: mergeEvidence(
        current.ownerEvidence,
        observation.ownerEvidence,
      ),
      observations: mergeObservations(
        current.observations,
        [observation],
        this.maximumObservations,
      ),
    })
    return this.remember(incoming)
  }

  markDetached(receiptId: string, now: number): RecoveryControlReceipt | undefined {
    const current = this.#byId.get(receiptId)
    if (!current || current.detached) return current
    return this.remember(
      Object.freeze({
        ...current,
        detached: true,
        updatedAt: Math.max(now, current.updatedAt),
        metadata: Object.freeze({
          ...current.metadata,
          viewerDetached: true,
        }),
      }),
    )
  }

  supersede(
    receiptId: string,
    winner: RecoveryControlReceipt,
    now: number,
  ): RecoveryControlReceipt | undefined {
    const current = this.#byId.get(receiptId)
    if (!current || terminalControlPhase(current.phase)) return current
    const observation: RecoveryControlObservation = Object.freeze({
      phase: RecoveryControlPhase.SUPERSEDED,
      observedAt: now,
      observedRevision: winner.observedRevision,
      eventIds: winner.observedEventIds,
      rowKeys: winner.observedRowKeys,
      ownerEvidence: winner.ownerEvidence,
      summary: `${current.commandName} superseded by ${winner.commandId}.`,
      source: "projection",
      terminal: true,
      consistent: true,
    })
    return this.observe(current.id, observation)
  }

  race(
    receipt: RecoveryControlReceipt,
    now: number,
  ): RecoveryControlRaceResult {
    const sameOwner = this.snapshot().inFlight.filter(
      (candidate) =>
        candidate.id !== receipt.id &&
        candidate.taskId === receipt.taskId &&
        candidate.runId === receipt.runId &&
        (candidate.expectedOwner.leaseId
          ? candidate.expectedOwner.leaseId ===
            receipt.expectedOwner.leaseId
          : candidate.targetId === receipt.targetId),
    )
    const superseded: RecoveryControlReceipt[] = []
    const pending: RecoveryControlReceipt[] = []
    for (const candidate of sameOwner) {
      if (
        terminalControlPhase(receipt.phase) &&
        PHASE_RANK[receipt.phase] > PHASE_RANK[candidate.phase]
      ) {
        const value = this.supersede(candidate.id, receipt, now)
        if (value) superseded.push(value)
      } else {
        pending.push(candidate)
      }
    }
    return Object.freeze({
      accepted: this.remember(receipt),
      superseded: freeze(superseded),
      pending: freeze(pending),
    })
  }

  snapshot(): RecoveryControlLedgerSnapshot {
    if (this.#snapshot) return this.#snapshot
    const receipts = freeze([...this.#byId.values()].sort(receiptSort))
    const inFlight = freeze(
      receipts.filter((receipt) => activeControlPhase(receipt.phase)),
    )
    const terminal = freeze(
      receipts.filter((receipt) => terminalControlPhase(receipt.phase)),
    )
    this.#snapshot = Object.freeze({
      revision: this.#revision,
      receipts,
      inFlight,
      terminal,
      deniedCount: receipts.filter(
        (receipt) => receipt.phase === RecoveryControlPhase.DENIED,
      ).length,
      failedCount: receipts.filter(
        (receipt) => receipt.phase === RecoveryControlPhase.FAILED,
      ).length,
      timedOutCount: receipts.filter((receipt) => receipt.timedOut).length,
      pendingCount: inFlight.length,
      latest: receipts[0],
    })
    return this.#snapshot
  }

  clearTerminal(before: number): number {
    let removed = 0
    for (const receipt of this.#byId.values()) {
      if (
        terminalControlPhase(receipt.phase) &&
        receipt.updatedAt < before
      ) {
        this.#remove(receipt)
        removed += 1
      }
    }
    if (removed) this.#changed()
    return removed
  }

  #write(receipt: RecoveryControlReceipt): void {
    const existing = this.#byId.get(receipt.id)
    if (existing === receipt) return
    if (existing && existing.commandId !== receipt.commandId) {
      this.#byCommand.delete(existing.commandId)
    }
    if (existing && existing.requestId !== receipt.requestId) {
      this.#byRequest.delete(existing.requestId)
    }
    if (existing && existing.idempotencyKey !== receipt.idempotencyKey) {
      this.#byIdempotency.delete(existing.idempotencyKey)
    }
    this.#byId.set(receipt.id, receipt)
    this.#byCommand.set(receipt.commandId, receipt.id)
    this.#byRequest.set(receipt.requestId, receipt.id)
    this.#byIdempotency.set(receipt.idempotencyKey, receipt.id)
    this.#changed()
  }

  #remove(receipt: RecoveryControlReceipt): void {
    this.#byId.delete(receipt.id)
    if (this.#byCommand.get(receipt.commandId) === receipt.id) {
      this.#byCommand.delete(receipt.commandId)
    }
    if (this.#byRequest.get(receipt.requestId) === receipt.id) {
      this.#byRequest.delete(receipt.requestId)
    }
    if (this.#byIdempotency.get(receipt.idempotencyKey) === receipt.id) {
      this.#byIdempotency.delete(receipt.idempotencyKey)
    }
  }

  #prune(): void {
    const excess = this.#byId.size - this.maximumReceipts
    if (excess <= 0) return
    const removable = [...this.#byId.values()]
      .filter((receipt) => terminalControlPhase(receipt.phase))
      .sort(
        (left, right) =>
          left.updatedAt - right.updatedAt ||
          left.commandId.localeCompare(right.commandId),
      )
    for (const receipt of removable.slice(0, excess)) {
      this.#remove(receipt)
    }
    this.#changed()
  }

  #changed(): void {
    this.#revision += 1
    this.#snapshot = undefined
  }
}
