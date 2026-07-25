import type {
  CuratorDecision,
  CuratorPhase,
  CuratorRun,
  MemoryConsoleProjection,
} from "./projection.ts"
import {
  compareNumber,
  compareText,
  fingerprint,
  sortStable,
  text,
  unique,
} from "../session/value.ts"

export interface CuratorReceipt {
  id: string
  runId: string
  candidateId?: string
  memoryId?: string
  decision: "accept" | "reject" | "recover" | "automatic"
  reason: string
  actorId: string
  expectedPhase?: CuratorPhase
  expectedSequence?: number
  commandId?: string
  requestId?: string
  eventIds: readonly string[]
  artifactIds: readonly string[]
  durable: boolean
  executed: boolean
  createdAt: string
}

export interface CuratorSettlement {
  id: string
  receipt: CuratorReceipt
  run?: CuratorRun
  decision?: CuratorDecision
  phase: "waiting" | "committed" | "rejected" | "conflict"
  phaseMatched: boolean
  sequenceAdvanced: boolean
  eventIdsObserved: boolean
  artifactIdsObserved: boolean
  missingEventIds: readonly string[]
  missingArtifactIds: readonly string[]
  warnings: readonly string[]
  error?: string
}

export class CuratorReceiptLedger {
  readonly #receipts = new Map<string, CuratorReceipt>()
  readonly #settlements = new Map<string, CuratorSettlement>()
  #enabled = true
  #disabledReason = "Curator receipt ledger is disabled."

  admit(input: Omit<CuratorReceipt, "id"> & { id?: string }): CuratorReceipt {
    this.#assertEnabled()
    const receipt: CuratorReceipt = Object.freeze({
      ...input,
      id: input.id ?? fingerprint([
        input.runId,
        input.candidateId,
        input.memoryId,
        input.decision,
        input.commandId,
        input.requestId,
      ]),
      reason: text(input.reason, "No curator decision reason."),
      actorId: text(input.actorId, "console-operator"),
      eventIds: Object.freeze(unique(input.eventIds)),
      artifactIds: Object.freeze(unique(input.artifactIds)),
    })
    const existing = this.#receipts.get(receipt.id)
    if (existing) {
      if (fingerprint([existing]) !== fingerprint([receipt])) {
        throw new Error("Curator receipt identity conflicts with an existing payload.")
      }
      return existing
    }
    if (!receipt.durable || !receipt.executed) {
      throw new Error("Curator receipt must be durable and executed.")
    }
    if (!receipt.candidateId && !receipt.memoryId && receipt.decision !== "recover") {
      throw new Error("Curator decision must bind a candidate or memory identity.")
    }
    this.#receipts.set(receipt.id, receipt)
    return receipt
  }

  reconcile(
    receipt: CuratorReceipt,
    projection: MemoryConsoleProjection,
    eventExists: (eventId: string) => boolean,
    artifactExists: (artifactId: string) => boolean,
  ): CuratorSettlement {
    this.#assertEnabled()
    const admitted = this.#receipts.get(receipt.id) ?? this.admit(receipt)
    const run = projection.curatorRuns.find((entry) => entry.id === receipt.runId)
    const decision = run?.phases.find((entry) =>
      (!receipt.candidateId || entry.candidateId === receipt.candidateId) &&
      (!receipt.memoryId || entry.memoryId === receipt.memoryId) &&
      decisionMatches(receipt.decision, entry.phase))
    const missingEventIds = admitted.eventIds.filter((eventId) => !eventExists(eventId))
    const missingArtifactIds = admitted.artifactIds.filter((artifactId) => !artifactExists(artifactId))
    const phaseMatched =
      !admitted.expectedPhase ||
      decision?.previousPhase === admitted.expectedPhase ||
      decision?.phase === admitted.expectedPhase
    const sequenceAdvanced =
      admitted.expectedSequence === undefined ||
      (decision?.sequence ?? -1) > admitted.expectedSequence
    const eventIdsObserved = missingEventIds.length === 0
    const artifactIdsObserved = missingArtifactIds.length === 0
    const warnings: string[] = []
    if (!run) warnings.push("curator run is not projected")
    if (!decision) warnings.push("curator decision is not projected")
    if (!phaseMatched) warnings.push("curator phase differs from receipt expectation")
    if (!sequenceAdvanced) warnings.push("curator sequence did not advance")
    if (!eventIdsObserved) warnings.push("curator receipt events are missing")
    if (!artifactIdsObserved) warnings.push("curator receipt artifacts are missing")
    if (decision && !decision.transitionValid) warnings.push("projected curator transition is invalid")
    let phase: CuratorSettlement["phase"] = "waiting"
    let error: string | undefined
    if (decision && !decision.transitionValid) {
      phase = "conflict"
      error = "Curator state machine rejected the projected transition."
    } else if (
      decision &&
      phaseMatched &&
      sequenceAdvanced &&
      eventIdsObserved &&
      artifactIdsObserved
    ) {
      phase = "committed"
    } else if (run?.failed) {
      phase = "rejected"
      error = "Curator run failed before decision settlement."
    }
    const settlement: CuratorSettlement = Object.freeze({
      id: fingerprint([admitted.id, projection.revision, decision?.sourceEventId]),
      receipt: admitted,
      run,
      decision,
      phase,
      phaseMatched,
      sequenceAdvanced,
      eventIdsObserved,
      artifactIdsObserved,
      missingEventIds: Object.freeze(missingEventIds),
      missingArtifactIds: Object.freeze(missingArtifactIds),
      warnings: Object.freeze(warnings),
      error,
    })
    this.#settlements.set(admitted.id, settlement)
    return settlement
  }

  get(receiptId: string): CuratorSettlement | undefined {
    this.#assertEnabled()
    return this.#settlements.get(receiptId)
  }

  list(): readonly CuratorSettlement[] {
    this.#assertEnabled()
    return Object.freeze(sortStable([...this.#settlements.values()], (left, right) =>
      compareNumber(
        right.decision?.sequence,
        left.decision?.sequence,
      ) || compareText(left.id, right.id)))
  }

  pending(): readonly CuratorSettlement[] {
    this.#assertEnabled()
    return Object.freeze(this.list().filter((entry) => entry.phase === "waiting"))
  }

  conflicts(): readonly CuratorSettlement[] {
    this.#assertEnabled()
    return Object.freeze(this.list().filter((entry) => entry.phase === "conflict"))
  }

  committed(): readonly CuratorSettlement[] {
    this.#assertEnabled()
    return Object.freeze(this.list().filter((entry) => entry.phase === "committed"))
  }

  disable(reason = "Curator receipt ledger is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Curator receipt ledger is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#assertEnabled()
    this.#receipts.clear()
    this.#settlements.clear()
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function decisionMatches(
  decision: CuratorReceipt["decision"],
  phase: CuratorPhase,
): boolean {
  if (decision === "accept") return ["accepted", "committed", "published"].includes(phase)
  if (decision === "reject") return phase === "rejected"
  if (decision === "recover") return phase === "recovered"
  return ["accepted", "committed", "published", "rejected"].includes(phase)
}
