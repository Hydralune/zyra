import type { CommandReceipt } from "../../../../../packages/commands/src/contracts.ts"
import type { CanonicalProjectionState } from "../../state/contracts.ts"
import {
  type SealedSubagentIntervention,
  type SubagentControlAction,
  type SubagentControlBaseline,
  type SubagentControlBinding,
  type SubagentControlOperation,
  type SubagentControlPhase,
  type SubagentControlRuntimeOptions,
  type SubagentControlSnapshot,
  type SubagentEffectSettlement,
  type SubagentProjection,
  type SubagentRow,
} from "./contracts.ts"
import {
  steeringEvidence,
  subagentEvents,
  terminalControlEvidence,
} from "./projection.ts"
import {
  assertBoundedText,
  compareText,
  fingerprint,
  identity,
  sortStable,
  text,
  unique,
} from "./value.ts"

export class SubagentControlRuntime {
  readonly #commands: SubagentControlRuntimeOptions["commands"]
  readonly #state: SubagentControlRuntimeOptions["state"]
  readonly #projection: SubagentControlRuntimeOptions["projection"]
  readonly #interventions: SubagentControlRuntimeOptions["interventions"]
  readonly #online: () => boolean
  readonly #now: () => Date
  readonly #nonce: () => string
  readonly #actorId: () => string
  readonly #maximum: number
  readonly #listeners = new Set<() => void>()
  readonly #operations = new Map<string, SubagentControlOperation>()
  readonly #operationByIdempotency = new Map<string, string>()
  readonly #inflight = new Map<string, Promise<SubagentControlOperation>>()
  readonly #interventionRecords = new Map<string, SealedSubagentIntervention>()
  #snapshot: SubagentControlSnapshot
  #closed = false
  #disabledReason = "Subagent controls are disabled."
  #unsubscribeCommands?: () => void

  constructor(options: SubagentControlRuntimeOptions) {
    this.#commands = options.commands
    this.#state = options.state
    this.#projection = options.projection
    this.#interventions = options.interventions
    this.#online = options.online ?? (() =>
      typeof navigator === "undefined" ? true : navigator.onLine)
    this.#now = options.now ?? (() => new Date())
    this.#nonce = options.nonce ?? createNonceFactory()
    this.#actorId = options.actorId ?? (() => "zyra-web-operator")
    this.#maximum = Math.max(16, Math.min(20_000, options.maximumOperations ?? 1_000))
    this.#snapshot = Object.freeze({
      enabled: true,
      connected: this.#online(),
      sealed: false,
      operations: Object.freeze([]),
      interventions: Object.freeze([]),
      revision: 0,
    })
    if (options.commands.subscribe) {
      this.#unsubscribeCommands = options.commands.subscribe(() => {
        this.#commandChanged()
      })
    }
  }

  getSnapshot = (): SubagentControlSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  setSealed(sealed: boolean): void {
    this.#assertOpen()
    this.#replace({ sealed })
  }

  setConnected(connected: boolean): void {
    if (this.#closed) return
    if (!connected) {
      for (const operation of this.#operations.values()) {
        if (terminalControlPhase(operation.phase)) continue
        this.#settle(operation.id, {
          phase: "disconnected",
          error: "Browser transport disconnected before canonical reconciliation.",
        })
      }
    }
    this.#replace({ connected })
  }

  async kill(input: {
    childId: string
    reason: string
    expectedRevision?: number
    ownerId?: string
    attempt?: number
    parentId?: string
    nonce?: string
    idempotencyKey?: string
    sealed?: boolean
  }): Promise<SubagentControlOperation> {
    return this.#submit({
      action: "kill",
      childId: input.childId,
      reason: input.reason,
      expectedRevision: input.expectedRevision,
      ownerId: input.ownerId,
      attempt: input.attempt,
      parentId: input.parentId,
      nonce: input.nonce,
      idempotencyKey: input.idempotencyKey,
      sealed: input.sealed,
    })
  }

  async steer(input: {
    childId: string
    instruction: string
    reason?: string
    expectedRevision?: number
    ownerId?: string
    attempt?: number
    parentId?: string
    nonce?: string
    idempotencyKey?: string
    sealed?: boolean
  }): Promise<SubagentControlOperation> {
    return this.#submit({
      action: "steer",
      childId: input.childId,
      instruction: input.instruction,
      reason: input.reason ?? "Operator submitted a scoped steering instruction.",
      expectedRevision: input.expectedRevision,
      ownerId: input.ownerId,
      attempt: input.attempt,
      parentId: input.parentId,
      nonce: input.nonce,
      idempotencyKey: input.idempotencyKey,
      sealed: input.sealed,
    })
  }

  reconcile(): readonly SubagentControlOperation[] {
    this.#assertOpen()
    const projection = this.#projection()
    if (!projection) return this.#snapshot.operations
    for (const operation of this.#operations.values()) {
      if (
        operation.phase !== "reconciling" &&
        operation.phase !== "queued" &&
        operation.phase !== "running" &&
        operation.phase !== "disconnected"
      ) {
        continue
      }
      this.#reconcileOperation(operation, projection, this.#state())
    }
    return this.#snapshot.operations
  }

  operation(id: string): SubagentControlOperation | undefined {
    return this.#operations.get(id)
  }

  idempotentOperation(idempotencyKey: string): SubagentControlOperation | undefined {
    const id = this.#operationByIdempotency.get(idempotencyKey)
    return id ? this.#operations.get(id) : undefined
  }

  disable(reason = "Subagent controls are disabled."): void {
    if (this.#closed) return
    this.#disabledReason = text(reason, "Subagent controls are disabled.")
    for (const operation of this.#operations.values()) {
      if (terminalControlPhase(operation.phase)) continue
      this.#settle(operation.id, {
        phase: "disabled",
        error: this.#disabledReason,
      })
    }
    this.#replace({ enabled: false })
  }

  enable(): void {
    if (this.#closed) return
    this.#replace({
      enabled: true,
      connected: this.#online(),
    })
  }

  close(reason = "Subagent control runtime closed."): void {
    if (this.#closed) return
    this.disable(reason)
    this.#closed = true
    this.#unsubscribeCommands?.()
    this.#unsubscribeCommands = undefined
    this.#listeners.clear()
  }

  async #submit(input: {
    action: SubagentControlAction
    childId: string
    instruction?: string
    reason: string
    expectedRevision?: number
    ownerId?: string
    attempt?: number
    parentId?: string
    nonce?: string
    idempotencyKey?: string
    sealed?: boolean
  }): Promise<SubagentControlOperation> {
    this.#assertAvailable()
    const row = this.#requireRow(input.childId)
    const binding = bindControl(row, input)
    const reason = assertBoundedText("Subagent control reason", input.reason, 1, 8_192)
    const instruction = input.action === "steer"
      ? assertBoundedText("Subagent steering instruction", input.instruction, 1, 65_536)
      : undefined
    assertActionAllowed(row, input.action)
    const nonce = normalizeNonce(input.nonce ?? this.#nonce())
    const payloadFingerprint = fingerprint([
      input.action,
      binding,
      reason,
      instruction,
      nonce,
    ])
    const idempotencyKey = normalizeIdempotencyKey(
      input.idempotencyKey ??
      `zyra-subagent:${payloadFingerprint}:${nonce.slice(0, 24)}`,
    )
    const replay = this.#replay(idempotencyKey, payloadFingerprint)
    if (replay) return replay
    const operation = this.#begin({
      action: input.action,
      binding,
      nonce,
      idempotencyKey,
      payloadFingerprint,
      reason,
      instruction,
      sealed: input.sealed ?? this.#snapshot.sealed,
    })
    if (operation.sealed) {
      const pending = this.#denySealed(operation)
      this.#inflight.set(idempotencyKey, pending)
      try {
        return await pending
      } finally {
        this.#inflight.delete(idempotencyKey)
      }
    }
    const pending = this.#dispatch(operation)
    this.#inflight.set(idempotencyKey, pending)
    try {
      return await pending
    } finally {
      this.#inflight.delete(idempotencyKey)
    }
  }

  #replay(
    idempotencyKey: string,
    payloadFingerprint: string,
  ): Promise<SubagentControlOperation> | SubagentControlOperation | undefined {
    const existingId = this.#operationByIdempotency.get(idempotencyKey)
    if (!existingId) return undefined
    const existing = this.#operations.get(existingId)
    if (!existing) {
      this.#operationByIdempotency.delete(idempotencyKey)
      return undefined
    }
    if (existing.payloadFingerprint !== payloadFingerprint) {
      throw new Error("Subagent control idempotency key was reused with different input.")
    }
    const replay = this.#settle(existing.id, {
      replayCount: existing.replayCount + 1,
    })
    return this.#inflight.get(idempotencyKey) ?? replay
  }

  #begin(input: {
    action: SubagentControlAction
    binding: SubagentControlBinding
    nonce: string
    idempotencyKey: string
    payloadFingerprint: string
    reason: string
    instruction?: string
    sealed: boolean
  }): SubagentControlOperation {
    const projection = this.#projection()
    if (!projection) throw new Error("Canonical subagent projection is unavailable.")
    const row = projection.rowById[input.binding.childId]
    if (!row) throw new Error("Canonical child disappeared before control admission.")
    const now = this.#now().toISOString()
    const baseline = captureControlBaseline(this.#state(), projection, row)
    const operation: SubagentControlOperation = Object.freeze({
      id: fingerprint([
        input.action,
        input.binding,
        input.nonce,
        input.idempotencyKey,
        now,
      ]),
      action: input.action,
      phase: "admitted",
      binding: input.binding,
      nonce: input.nonce,
      idempotencyKey: input.idempotencyKey,
      payloadFingerprint: input.payloadFingerprint,
      reason: input.reason,
      instruction: input.instruction,
      sealed: input.sealed,
      startedAt: now,
      updatedAt: now,
      baseline,
      canonicalEventIds: Object.freeze([]),
      replayCount: 0,
    })
    this.#operations.set(operation.id, operation)
    this.#operationByIdempotency.set(operation.idempotencyKey, operation.id)
    this.#prune()
    this.#replace({
      active: operation,
      operations: this.#operationList(),
    })
    return operation
  }

  async #dispatch(
    operation: SubagentControlOperation,
  ): Promise<SubagentControlOperation> {
    this.#settle(operation.id, { phase: "permission_pending" })
    const command = buildControlCommand(operation)
    try {
      const receipt = await this.#commands.submit(command, {
        mode: operation.action === "steer" ? "steer" : "enqueue",
        sealed: false,
      })
      assertReceiptBinding(receipt, operation)
      const phase = receiptPhase(receipt)
      const updated = this.#settle(operation.id, {
        phase,
        receipt,
        requestId: receipt.requestId,
        commandId: receipt.commandId,
        error: receipt.error?.message,
        canonicalEventIds: receipt.eventIds,
      })
      if (phase === "denied" || phase === "failed") return updated
      if (phase === "reconciling") {
        this.#reconcileOperation(updated, this.#projection(), this.#state())
      }
      return this.#operations.get(operation.id) ?? updated
    } catch (error) {
      return this.#settle(operation.id, {
        phase: "failed",
        error: error instanceof Error ? error.message : String(error),
      })
    }
  }

  async #denySealed(
    operation: SubagentControlOperation,
  ): Promise<SubagentControlOperation> {
    if (!this.#interventions) {
      return this.#settle(operation.id, {
        phase: "denied",
        error:
          "Sealed subagent control was denied; canonical intervention recorder is unavailable.",
      })
    }
    try {
      const canonical = await this.#interventions.record({
        action: operation.action,
        binding: operation.binding,
        actorId: this.#actorId(),
        nonce: operation.nonce,
        reason: operation.reason,
      })
      if (
        !canonical.interventionCounted ||
        canonical.humanInterventionCount !== 0 ||
        canonical.operatorInterventionAttemptCount < 1
      ) {
        throw new Error("Canonical sealed intervention invariant was not satisfied.")
      }
      const createdAt = this.#now().toISOString()
      const intervention: SealedSubagentIntervention = Object.freeze({
        id: canonical.id,
        action: operation.action,
        binding: operation.binding,
        actorId: this.#actorId(),
        nonce: operation.nonce,
        reason: operation.reason,
        denied: true,
        noHumanWait: true,
        interventionCounted: true,
        humanInterventionCount: 0,
        automaticRecoveryAction: canonical.automaticRecoveryAction,
        canonicalEventIds: Object.freeze([...(canonical.eventIds ?? [])]),
        createdAt,
      })
      this.#interventionRecords.set(intervention.id, intervention)
      this.#replace({
        interventions: this.#interventionList(),
      })
      return this.#settle(operation.id, {
        phase: "denied",
        error: "Sealed run denied manual subagent mutation without a human wait.",
        canonicalEventIds: intervention.canonicalEventIds,
      })
    } catch (error) {
      return this.#settle(operation.id, {
        phase: "denied",
        error: error instanceof Error ? error.message : String(error),
      })
    }
  }

  #reconcileOperation(
    operation: SubagentControlOperation,
    projection: SubagentProjection | undefined,
    state: CanonicalProjectionState,
  ): void {
    if (!projection) return
    const row = projection.rowById[operation.binding.childId]
    if (!row) {
      if (projection.canonicalRevision > operation.baseline.projectionRevision) {
        this.#settle(operation.id, {
          phase: "quarantined",
          error: "Canonical child disappeared before the requested effect could be proven.",
        })
      }
      return
    }
    if (
      row.taskId !== operation.binding.taskId ||
      row.runId !== operation.binding.runId ||
      row.sessionId !== operation.binding.sessionId ||
      row.parentId !== operation.binding.parentId ||
      row.attempt !== operation.binding.attempt ||
      row.ownerId !== operation.binding.ownerId
    ) {
      this.#settle(operation.id, {
        phase: "quarantined",
        error: "Canonical child scope changed while the control was in flight.",
      })
      return
    }
    const effect = evaluateControlEffect(state, projection, row, operation, this.#now)
    if (effect.satisfied) {
      this.#settle(operation.id, {
        phase: "committed",
        effect,
        canonicalEventIds: effect.evidenceEventIds,
        error: undefined,
      })
      return
    }
    if (row.lateResults.length > 0 && operation.action === "kill") {
      this.#settle(operation.id, {
        phase: "quarantined",
        effect,
        canonicalEventIds: effect.evidenceEventIds,
        error: "Kill settled, but a late result was quarantined behind the terminal fence.",
      })
      return
    }
    this.#settle(operation.id, {
      phase: this.#snapshot.connected ? "reconciling" : "disconnected",
      effect,
    })
  }

  #commandChanged(): void {
    const snapshot = this.#commands.getSnapshot?.()
    if (!snapshot || typeof snapshot !== "object") return
    const receipt = (snapshot as { lastOverlayReceipt?: CommandReceipt }).lastOverlayReceipt
    if (!receipt || receipt.name !== "/agents") return
    const operation = [...this.#operations.values()].find((entry) =>
      entry.commandId === receipt.commandId ||
      entry.requestId === receipt.requestId ||
      entry.receipt?.idempotencyKey === receipt.idempotencyKey)
    if (!operation) return
    try {
      assertReceiptBinding(receipt, operation)
    } catch (error) {
      this.#settle(operation.id, {
        phase: "quarantined",
        receipt,
        error: error instanceof Error ? error.message : String(error),
      })
      return
    }
    const phase = receiptPhase(receipt)
    this.#settle(operation.id, {
      phase,
      receipt,
      requestId: receipt.requestId,
      commandId: receipt.commandId,
      error: receipt.error?.message,
      canonicalEventIds: receipt.eventIds,
    })
    if (phase === "reconciling") this.reconcile()
  }

  #requireRow(childId: string): SubagentRow {
    const id = identity(childId, "subagent")
    const projection = this.#projection()
    if (!projection) throw new Error("Canonical subagent projection is unavailable.")
    const row = projection.rowById[id]
    if (!row) throw new Error("Subagent is absent from the canonical projection.")
    if (!row.admitted) throw new Error("Subagent failed strict canonical projection admission.")
    if (!row.controlEligible) throw new Error("Subagent is not eligible for operator control.")
    return row
  }

  #settle(
    id: string,
    patch: Partial<SubagentControlOperation>,
  ): SubagentControlOperation {
    const current = this.#operations.get(id)
    if (!current) throw new Error(`Unknown subagent control operation: ${id}`)
    const updated: SubagentControlOperation = Object.freeze({
      ...current,
      ...patch,
      updatedAt: this.#now().toISOString(),
      canonicalEventIds: unique([
        ...current.canonicalEventIds,
        ...(patch.canonicalEventIds ?? []),
      ]),
    })
    this.#operations.set(id, updated)
    this.#replace({
      active: updated,
      operations: this.#operationList(),
    })
    return updated
  }

  #operationList(): readonly SubagentControlOperation[] {
    return sortStable(
      this.#operations.values(),
      (left, right) =>
        compareText(right.startedAt, left.startedAt) ||
        compareText(left.id, right.id),
    ).slice(0, this.#maximum)
  }

  #interventionList(): readonly SealedSubagentIntervention[] {
    return sortStable(
      this.#interventionRecords.values(),
      (left, right) =>
        compareText(right.createdAt, left.createdAt) ||
        compareText(left.id, right.id),
    ).slice(0, this.#maximum)
  }

  #prune(): void {
    if (this.#operations.size <= this.#maximum) return
    const keep = new Set(this.#operationList().map((operation) => operation.id))
    for (const [id, operation] of this.#operations) {
      if (keep.has(id) || this.#inflight.has(operation.idempotencyKey)) continue
      this.#operations.delete(id)
      this.#operationByIdempotency.delete(operation.idempotencyKey)
    }
  }

  #replace(patch: Partial<SubagentControlSnapshot>): void {
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      operations: patch.operations ?? this.#operationList(),
      interventions: patch.interventions ?? this.#interventionList(),
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        continue
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new Error("Subagent control runtime is closed.")
  }

  #assertAvailable(): void {
    this.#assertOpen()
    if (!this.#snapshot.enabled) throw new Error(this.#disabledReason)
    if (!this.#snapshot.connected || !this.#online()) {
      throw new Error("Subagent controls are unavailable while disconnected.")
    }
  }
}

function bindControl(
  row: SubagentRow,
  input: {
    expectedRevision?: number
    ownerId?: string
    attempt?: number
    parentId?: string
  },
): SubagentControlBinding {
  if (
    input.expectedRevision !== undefined &&
    input.expectedRevision !== row.revision
  ) {
    throw new Error("Subagent expected revision is stale.")
  }
  if (input.ownerId && input.ownerId !== row.ownerId) {
    throw new Error("Subagent owner identity changed.")
  }
  if (input.attempt !== undefined && input.attempt !== row.attempt) {
    throw new Error("Subagent attempt identity changed.")
  }
  if (input.parentId && input.parentId !== row.parentId) {
    throw new Error("Subagent parent identity changed.")
  }
  return Object.freeze({
    taskId: row.taskId,
    runId: row.runId,
    sessionId: row.sessionId,
    parentId: row.parentId,
    childId: row.id,
    attempt: row.attempt,
    ownerId: row.ownerId,
    expectedRevision: row.revision,
  })
}

function assertActionAllowed(
  row: SubagentRow,
  action: SubagentControlAction,
): void {
  if (row.terminal) throw new Error("Terminal subagents cannot be mutated.")
  if (row.quarantined) throw new Error("Quarantined subagents cannot be mutated.")
  if (action === "kill" && !row.scope.allowKill) {
    throw new Error("Subagent scope denies kill.")
  }
  if (action === "steer" && !row.scope.allowSteer) {
    throw new Error("Subagent scope denies steer.")
  }
  if (!row.scope.bounded) {
    throw new Error("Subagent scope is not canonically bounded.")
  }
}

function captureControlBaseline(
  state: CanonicalProjectionState,
  projection: SubagentProjection,
  row: SubagentRow,
): SubagentControlBaseline {
  const events = subagentEvents(state, row.id)
  const baseline = {
    projectionRevision: projection.canonicalRevision,
    childRevision: row.revision,
    lifecycle: row.lifecycle,
    checkpointId: row.checkpointId,
    resultDigest: row.result?.digest,
    errorCode: row.error?.code,
    heartbeatSequence: row.heartbeat.sequence,
    eventSequence: Math.max(0, ...events.map((event) => event.sequence)),
  }
  return Object.freeze({
    ...baseline,
    fingerprint: fingerprint([row.id, row.attempt, baseline]),
  })
}

export function evaluateControlEffect(
  state: CanonicalProjectionState,
  projection: SubagentProjection,
  row: SubagentRow,
  operation: SubagentControlOperation,
  now: () => Date = () => new Date(),
): SubagentEffectSettlement {
  const changedFields: string[] = []
  const baseline = operation.baseline
  if (row.revision !== baseline.childRevision) changedFields.push("child.revision")
  if (row.lifecycle !== baseline.lifecycle) changedFields.push("child.lifecycle")
  if (row.checkpointId !== baseline.checkpointId) changedFields.push("child.checkpoint")
  if (row.result?.digest !== baseline.resultDigest) changedFields.push("child.result")
  if (row.error?.code !== baseline.errorCode) changedFields.push("child.error")
  if (row.heartbeat.sequence !== baseline.heartbeatSequence) {
    changedFields.push("child.heartbeat")
  }
  if (row.attempt !== operation.binding.attempt) changedFields.push("child.attempt")
  const evidence =
    operation.action === "kill"
      ? terminalControlEvidence(
          state,
          row.id,
          baseline.eventSequence,
          operation.commandId,
        )
      : steeringEvidence(
          state,
          row.id,
          baseline.eventSequence,
          operation.commandId,
        )
  const receiptEventIds = operation.canonicalEventIds.filter((eventId) =>
    Boolean(state.causality.byEvent[eventId]))
  const evidenceEventIds = unique([
    ...receiptEventIds,
    ...evidence.map((event) => event.eventId),
  ])
  const evidenceArtifactIds = unique(
    evidenceEventIds.flatMap((eventId) =>
      state.causality.byEvent[eventId]?.artifactIds ?? []),
  )
  const revisionAdvanced =
    projection.canonicalRevision > baseline.projectionRevision &&
    row.revision > baseline.childRevision
  const commandObserved =
    !operation.commandId ||
    Boolean(state.commands[operation.commandId]) ||
    Boolean(state.causality.byControlCommand[operation.commandId]?.length) ||
    receiptEventIds.length > 0
  const terminalObserved =
    operation.action === "kill" &&
    ["cancelled", "killed"].includes(row.lifecycle) &&
    row.terminal &&
    evidence.length > 0
  const steeringObserved =
    operation.action === "steer" &&
    evidence.length > 0 &&
    (
      changedFields.includes("child.revision") ||
      changedFields.includes("child.checkpoint") ||
      changedFields.includes("child.heartbeat")
    )
  const satisfied =
    revisionAdvanced &&
    commandObserved &&
    (terminalObserved || steeringObserved)
  const reasons: string[] = []
  if (!revisionAdvanced) reasons.push("canonical child revision has not advanced")
  if (!commandObserved) reasons.push("canonical command correlation is absent")
  if (operation.action === "kill" && !terminalObserved) {
    reasons.push("canonical cancelled/killed terminal effect is absent")
  }
  if (operation.action === "steer" && !steeringObserved) {
    reasons.push("canonical steering application effect is absent")
  }
  if (row.lateResults.length > 0) {
    reasons.push("late result is quarantined behind the terminal fence")
  }
  if (satisfied) reasons.push(`${operation.action} effect committed by the canonical owner`)
  return Object.freeze({
    satisfied,
    pending: !satisfied,
    semanticEffect:
      row.lateResults.length > 0
        ? "late_result_quarantined"
        : operation.action === "kill"
          ? "terminal_cancel"
          : "steering_applied",
    baselineRevision: baseline.childRevision,
    observedRevision: row.revision,
    changedFields: Object.freeze(changedFields),
    evidenceEventIds,
    evidenceArtifactIds,
    reasons: Object.freeze(reasons),
    checkedAt: now().toISOString(),
  })
}

function buildControlCommand(
  operation: SubagentControlOperation,
): string {
  const bindingReceipt = [
    `nonce=${operation.nonce}`,
    `idempotency=${operation.idempotencyKey}`,
    `revision=${operation.binding.expectedRevision}`,
    `attempt=${operation.binding.attempt}`,
    `owner=${operation.binding.ownerId ?? ""}`,
    `parent=${operation.binding.parentId}`,
  ].join(";")
  const reason = `${operation.reason} [zyra-control:${bindingReceipt}]`
  if (operation.action === "kill") {
    return `/agents kill ${quote(operation.binding.childId)} --reason ${quote(reason)}`
  }
  return `/agents steer ${quote(operation.binding.childId)} --instruction ${quote(
    operation.instruction ?? "",
  )} --reason ${quote(reason)}`
}

function assertReceiptBinding(
  receipt: CommandReceipt,
  operation: SubagentControlOperation,
): void {
  if (receipt.name !== "/agents") throw new Error("Control receipt belongs to another command.")
  if (receipt.taskId !== operation.binding.taskId) {
    throw new Error("Control receipt belongs to another task.")
  }
  if (receipt.runId !== operation.binding.runId) {
    throw new Error("Control receipt belongs to another run.")
  }
  if (
    receipt.sessionId &&
    operation.binding.sessionId &&
    receipt.sessionId !== operation.binding.sessionId
  ) {
    throw new Error("Control receipt belongs to another session.")
  }
  if (!receipt.idempotencyKey) throw new Error("Control receipt has no idempotency identity.")
  if (receipt.humanInterventionCount < 0) {
    throw new Error("Control receipt contains an invalid intervention count.")
  }
}

function receiptPhase(
  receipt: CommandReceipt,
): SubagentControlPhase {
  if (receipt.phase === "rejected") return "denied"
  if (receipt.phase === "cancelled" || receipt.phase === "expired") return "failed"
  if (receipt.phase === "queued") return "queued"
  if (receipt.phase === "running" || receipt.phase === "validated") return "running"
  if (receipt.phase === "applied") return "reconciling"
  return "permission_pending"
}

function terminalControlPhase(
  phase: SubagentControlPhase,
): boolean {
  return ["committed", "denied", "failed", "quarantined", "disabled"].includes(phase)
}

function normalizeNonce(value: string): string {
  const selected = identity(value, "subagent control nonce")
  if (selected.length < 8 || selected.length > 256) {
    throw new TypeError("Subagent control nonce must contain 8 through 256 characters.")
  }
  return selected
}

function normalizeIdempotencyKey(value: string): string {
  const selected = text(value, "", 513)
  if (
    selected.length < 8 ||
    selected.length > 512 ||
    /[\u0000\r\n]/.test(selected)
  ) {
    throw new TypeError("Subagent control idempotency key is invalid.")
  }
  return selected
}

function quote(value: string): string {
  return JSON.stringify(value)
}

function createNonceFactory(): () => string {
  let counter = 0
  return () => {
    counter += 1
    const random = globalThis.crypto?.getRandomValues
      ? [...globalThis.crypto.getRandomValues(new Uint32Array(2))]
          .map((value) => value.toString(16).padStart(8, "0"))
          .join("")
      : fingerprint([Date.now(), counter, Math.random()]).slice(-16)
    return `subagent-${Date.now().toString(36)}-${counter.toString(36)}-${random}`
  }
}
