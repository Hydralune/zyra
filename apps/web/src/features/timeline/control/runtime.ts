import {
  createIdentity,
} from "../../../../../../packages/core/typed-api-client/src/index.ts"
import {
  RecoveryControlConnection,
  RecoveryControlError,
  RecoveryControlPhase,
  activeControlPhase,
  terminalControlPhase,
  type NormalizedRecoveryControlRequest,
  type RecoveryControlAudit,
  type RecoveryControlConnectionValue,
  type RecoveryControlObservation,
  type RecoveryControlReceipt,
  type RecoveryControlRequest,
  type RecoveryControlRuntimeOptions,
  type RecoveryControlRuntimeSnapshot,
  type RecoveryControlSubmission,
} from "./contracts.ts"
import type { WorkerCausalTimelineProjection } from "../projection/index.ts"
import { RecoveryControlLedger } from "./ledger.ts"
import {
  failedControlReceipt,
  initialControlReceipt,
  receiptFromControlResponse,
  timedOutControlReceipt,
} from "./receipts.ts"
import {
  normalizeRecoveryControlRequest,
  validateRecoveryControlScope,
} from "./validation.ts"
import {
  observeAllRecoveryControlReceipts,
} from "./observation.ts"
import { assessSealedControlReceipt } from "./sealed.ts"

type Listener = () => void

interface InFlightControl {
  request: NormalizedRecoveryControlRequest
  receiptId: string
  completion: Promise<RecoveryControlReceipt>
  resolve: (receipt: RecoveryControlReceipt) => void
  timeoutHandle?: ReturnType<typeof setTimeout>
  transportSettled: boolean
  timeoutSettled: boolean
  completionSettled: boolean
}

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function promiseWithResolvers<T>(): {
  promise: Promise<T>
  resolve: (value: T | PromiseLike<T>) => void
  reject: (reason?: unknown) => void
} {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((next, fail) => {
    resolve = next
    reject = fail
  })
  return { promise, resolve, reject }
}

function connectionReason(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === "string") return error
  try {
    return JSON.stringify(error)
  } catch {
    return String(error)
  }
}

function disconnectedError(error: unknown): boolean {
  const text = connectionReason(error).toLocaleLowerCase()
  return (
    text.includes("network") ||
    text.includes("fetch") ||
    text.includes("connection") ||
    text.includes("offline") ||
    text.includes("socket") ||
    text.includes("timeout") ||
    text.includes("abort")
  )
}

function immutableSnapshot(
  taskId: string,
  revision: number,
  closed: boolean,
  detached: boolean,
  disabled: boolean,
  connection: RecoveryControlConnectionValue,
  connectionReasonValue: string | undefined,
  disconnectedAt: number | undefined,
  reconnectedAt: number | undefined,
  ledger: RecoveryControlLedger,
  announcements: readonly string[],
): RecoveryControlRuntimeSnapshot {
  return Object.freeze({
    taskId,
    revision,
    closed,
    detached,
    disabled,
    connection,
    connectionReason: connectionReasonValue,
    disconnectedAt,
    reconnectedAt,
    ledger: ledger.snapshot(),
    announcements: freeze(announcements),
  })
}

export class RecoveryControlRuntime {
  readonly taskId: string
  readonly runId: string
  readonly transport: RecoveryControlRuntimeOptions["transport"]
  readonly now: () => number
  readonly defaultTimeoutMs: number
  readonly maximumTimeoutMs: number
  readonly requestId: () => string
  readonly commandId: () => string
  readonly ledger: RecoveryControlLedger
  readonly detachOnClose: boolean
  #disabled: boolean
  #closed = false
  #detached = false
  #connection: RecoveryControlConnectionValue =
    RecoveryControlConnection.ONLINE
  #connectionReason?: string
  #disconnectedAt?: number
  #reconnectedAt?: number
  #listeners = new Set<Listener>()
  #inFlight = new Map<string, InFlightControl>()
  #requests = new Map<string, NormalizedRecoveryControlRequest>()
  #announcements: string[] = []
  #countedTerminal = new Set<string>()
  #revision = 0
  #snapshot?: RecoveryControlRuntimeSnapshot
  #audit: RecoveryControlAudit

  constructor(options: RecoveryControlRuntimeOptions) {
    this.taskId = options.taskId
    this.runId = options.runId
    this.transport = options.transport
    this.now = options.now ?? Date.now
    this.defaultTimeoutMs = Math.max(
      250,
      Math.floor(options.defaultTimeoutMs ?? 30_000),
    )
    this.maximumTimeoutMs = Math.max(
      this.defaultTimeoutMs,
      Math.floor(options.maximumTimeoutMs ?? 120_000),
    )
    this.requestId =
      options.requestId ?? (() => createIdentity("request"))
    this.commandId =
      options.commandId ?? (() => createIdentity("control_command"))
    this.ledger = new RecoveryControlLedger({
      maximumReceipts: options.maximumReceipts,
      maximumObservations: options.maximumObservations,
    })
    this.detachOnClose = options.detachOnClose ?? true
    this.#disabled = options.disabled ?? false
    this.#audit = Object.freeze({
      disabled: this.#disabled,
      closed: false,
      detached: false,
      submitted: 0,
      applied: 0,
      denied: 0,
      failed: 0,
      timedOut: 0,
      replayed: 0,
      staleObservations: 0,
      conflictingObservations: 0,
      detachedCompletions: 0,
    })
  }

  subscribe = (listener: Listener): (() => void) => {
    if (this.#closed && !this.detachOnClose) return () => undefined
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  getSnapshot = (): RecoveryControlRuntimeSnapshot => {
    if (!this.#snapshot) {
      this.#snapshot = immutableSnapshot(
        this.taskId,
        this.#revision,
        this.#closed,
        this.#detached,
        this.#disabled,
        this.#connection,
        this.#connectionReason,
        this.#disconnectedAt,
        this.#reconnectedAt,
        this.ledger,
        this.#announcements,
      )
    }
    return this.#snapshot
  }

  audit(): RecoveryControlAudit {
    return this.#audit
  }

  request(commandId: string): NormalizedRecoveryControlRequest | undefined {
    return this.#requests.get(commandId)
  }

  inFlight(): readonly RecoveryControlReceipt[] {
    return this.ledger.snapshot().inFlight
  }

  submit(request: RecoveryControlRequest): RecoveryControlSubmission {
    this.#requireActive()
    if (this.#disabled) {
      throw new RecoveryControlError(
        "timeline_control_binding_disabled",
        "Timeline recovery control binding is disabled.",
      )
    }
    const normalized = normalizeRecoveryControlRequest(request, {
      now: this.now,
      requestId: this.requestId,
      commandId: this.commandId,
      defaultTimeoutMs: this.defaultTimeoutMs,
      maximumTimeoutMs: this.maximumTimeoutMs,
    })
    validateRecoveryControlScope(normalized, this.taskId, this.runId)
    const existing = this.ledger.receiptForIdempotency(
      normalized.idempotencyKey,
    )
    if (existing) {
      const completion =
        this.#inFlight.get(existing.id)?.completion ??
        Promise.resolve(existing)
      return Object.freeze({
        request: normalized,
        receipt: existing,
        completion,
      })
    }
    const now = this.now()
    const initial = this.ledger.remember(
      initialControlReceipt(normalized, now),
    )
    this.#requests.set(normalized.commandId, normalized)
    this.#auditUpdate({
      submitted: this.#audit.submitted + 1,
      lastRequestId: normalized.requestId,
      lastCommandId: normalized.commandId,
      lastError: undefined,
    })
    this.#announce(
      `${normalized.commandName} submitted for ${normalized.targetKind} ` +
        `${normalized.targetId ?? normalized.taskId}.`,
    )
    this.#changed()

    const resolvers = promiseWithResolvers<RecoveryControlReceipt>()
    const state: InFlightControl = {
      request: normalized,
      receiptId: initial.id,
      completion: resolvers.promise,
      resolve: resolvers.resolve,
      transportSettled: false,
      timeoutSettled: false,
      completionSettled: false,
    }
    this.#inFlight.set(initial.id, state)
    state.timeoutHandle = setTimeout(() => {
      state.timeoutSettled = true
      const current = this.ledger.receipt(initial.id) ?? initial
      if (!terminalControlPhase(current.phase)) {
        const timedOut = this.ledger.remember(
          timedOutControlReceipt(current, this.now()),
        )
        this.#auditUpdate({
          timedOut: this.#audit.timedOut + 1,
          lastError: timedOut.errorMessage,
        })
        this.#setConnection(
          RecoveryControlConnection.DEGRADED,
          "Control response deadline expired; waiting for canonical evidence.",
        )
        this.#announce(timedOut.summary)
        this.#changed()
      }
    }, normalized.timeoutMs)

    void this.#submitTransport(state, resolvers)
    return Object.freeze({
      request: normalized,
      receipt: initial,
      completion: resolvers.promise,
    })
  }

  async submitAndWait(
    request: RecoveryControlRequest,
  ): Promise<RecoveryControlReceipt> {
    return this.submit(request).completion
  }

  observe(projection: WorkerCausalTimelineProjection): readonly RecoveryControlReceipt[] {
    if (projection.taskId !== this.taskId) {
      throw new RecoveryControlError(
        "control_projection_scope_mismatch",
        "Timeline projection does not belong to this control runtime.",
      )
    }
    const observations = observeAllRecoveryControlReceipts(
      this.ledger.snapshot().receipts,
      {
        projection,
        rows: projection.rows,
        now: this.now(),
      },
    )
    const changed: RecoveryControlReceipt[] = []
    for (const [receiptId, observation] of observations) {
      const before = this.ledger.receipt(receiptId)
      if (!before) continue
      const currentRevision = before.observedRevision ?? -1
      const incomingRevision = observation.observedRevision ?? -1
      if (
        incomingRevision >= 0 &&
        incomingRevision < currentRevision &&
        terminalControlPhase(before.phase)
      ) {
        this.#auditUpdate({
          staleObservations: this.#audit.staleObservations + 1,
        })
        continue
      }
      const next = this.ledger.observe(receiptId, observation)
      if (!next || next === before) continue
      if (!observation.consistent) {
        this.#auditUpdate({
          conflictingObservations:
            this.#audit.conflictingObservations + 1,
        })
      }
      changed.push(next)
      this.#settleFromObservation(next)
    }
    if (changed.length) {
      this.#race(changed)
      this.#changed()
    }
    if (
      this.#connection !== RecoveryControlConnection.ONLINE &&
      projection.diagnostics.ready &&
      projection.diagnostics.lag === 0
    ) {
      this.#setConnection(
        RecoveryControlConnection.ONLINE,
        "Canonical timeline caught up.",
      )
    }
    return freeze(changed)
  }

  observeReceipt(
    receiptId: string,
    observation: RecoveryControlObservation,
  ): RecoveryControlReceipt | undefined {
    const before = this.ledger.receipt(receiptId)
    if (!before) return undefined
    const next = this.ledger.observe(receiptId, observation)
    if (next && next !== before) {
      this.#settleFromObservation(next)
      this.#changed()
    }
    return next
  }

  setConnection(
    connection: RecoveryControlConnectionValue,
    reason = "",
  ): void {
    this.#setConnection(connection, reason)
  }

  reconnect(projection?: WorkerCausalTimelineProjection): void {
    this.#setConnection(
      RecoveryControlConnection.RECONNECTING,
      "Refreshing canonical control receipts.",
    )
    if (projection) this.observe(projection)
  }

  detach(reason = "Timeline viewer detached."): void {
    if (this.#detached) return
    this.#detached = true
    for (const receipt of this.ledger.snapshot().inFlight) {
      this.ledger.markDetached(receipt.id, this.now())
    }
    this.#announce(
      `${reason} Durable control requests continue in the canonical runtime.`,
    )
    this.#auditUpdate({ detached: true })
    this.#listeners.clear()
    this.#changed(false)
  }

  attach(): void {
    if (!this.#detached || this.#closed) return
    this.#detached = false
    this.#auditUpdate({ detached: false })
    this.#changed()
  }

  close(reason = "Timeline control view closed."): void {
    if (this.#closed) return
    if (this.detachOnClose) {
      this.detach(reason)
      return
    }
    this.#closed = true
    this.#detached = true
    this.#listeners.clear()
    this.#announce(
      `${reason} In-flight requests were not cancelled or replayed.`,
    )
    this.#auditUpdate({ closed: true, detached: true })
    this.#changed(false)
  }

  dispose(reason = "Timeline control runtime disposed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#detached = true
    this.#listeners.clear()
    for (const state of this.#inFlight.values()) {
      if (state.timeoutHandle) clearTimeout(state.timeoutHandle)
      const current = this.ledger.receipt(state.receiptId)
      if (current && !terminalControlPhase(current.phase)) {
        this.ledger.markDetached(current.id, this.now())
      }
    }
    this.#announce(
      `${reason} Backend effects remain durable; no transport abort was sent.`,
    )
    this.#auditUpdate({ closed: true, detached: true })
    this.#changed(false)
  }

  setDisabled(disabled: boolean): void {
    if (this.#disabled === disabled) return
    this.#disabled = disabled
    this.#auditUpdate({ disabled })
    this.#announce(
      disabled
        ? "Timeline recovery control binding disabled."
        : "Timeline recovery control binding enabled.",
    )
    this.#changed()
  }

  takeAnnouncements(): readonly string[] {
    if (!this.#announcements.length) return Object.freeze([])
    const values = freeze(this.#announcements)
    this.#announcements = []
    this.#changed()
    return values
  }

  async #submitTransport(
    state: InFlightControl,
    resolvers: ReturnType<
      typeof promiseWithResolvers<RecoveryControlReceipt>
    >,
  ): Promise<void> {
    const request = state.request
    try {
      const response = await this.transport.submit({
        taskId: request.taskId,
        runId: request.runId,
        text: request.commandText,
        arguments: request.commandArguments,
        requestId: request.requestId,
        commandId: request.commandId,
        idempotencyKey: request.idempotencyKey,
        actorId: request.actorId,
        sessionId: request.owner.sessionId,
        sealed: request.sealed,
        expectedRevision: request.owner.sessionRevision,
        timeoutMs: request.timeoutMs,
      })
      state.transportSettled = true
      if (state.timeoutHandle) clearTimeout(state.timeoutHandle)
      const current =
        this.ledger.receipt(state.receiptId) ??
        initialControlReceipt(request, this.now())
      let next = this.ledger.remember(
        receiptFromControlResponse(
          request,
          current,
          response,
          this.now(),
        ),
      )
      if (request.sealed) {
        const assessment = assessSealedControlReceipt(request, next)
        if (!assessment.valid) {
          next = this.ledger.remember(
            Object.freeze({
              ...next,
              phase: RecoveryControlPhase.FAILED,
              summary:
                "Sealed control response violated the no-human/manual-mutation invariant.",
              errorCode: "sealed_control_invariant_failed",
              errorMessage: assessment.violations.join("; "),
              noHumanWait: assessment.noHumanWait,
              metadata: Object.freeze({
                ...next.metadata,
                sealedInvariantValid: false,
              }),
            }),
          )
        }
      }
      if (next.replayed) {
        this.#auditUpdate({ replayed: this.#audit.replayed + 1 })
      }
      this.#recordTerminal(next)
      this.#race([next])
      if (state.timeoutSettled || this.#detached) {
        this.#auditUpdate({
          detachedCompletions: this.#audit.detachedCompletions + 1,
        })
      }
      this.#announce(next.summary)
      this.#setConnection(
        RecoveryControlConnection.ONLINE,
        "Control transport returned a canonical receipt.",
      )
      this.#changed()
      if (terminalControlPhase(next.phase)) {
        this.#finish(state, next, resolvers)
      } else {
        // A queued/pending response is a complete transport response but not
        // a complete canonical effect. Keep the public completion pending
        // until projection evidence settles it.
        this.#changed()
      }
    } catch (error) {
      state.transportSettled = true
      if (state.timeoutHandle) clearTimeout(state.timeoutHandle)
      const current =
        this.ledger.receipt(state.receiptId) ??
        initialControlReceipt(request, this.now())
      const next = this.ledger.remember(
        failedControlReceipt(current, error, this.now()),
      )
      this.#recordTerminal(next)
      this.#auditUpdate({
        lastError: connectionReason(error),
      })
      if (disconnectedError(error)) {
        this.#setConnection(
          RecoveryControlConnection.OFFLINE,
          connectionReason(error),
        )
      }
      this.#announce(next.summary)
      this.#changed()
      this.#finish(state, next, resolvers)
    }
  }

  #settleFromObservation(receipt: RecoveryControlReceipt): void {
    if (!terminalControlPhase(receipt.phase)) return
    const state = this.#inFlight.get(receipt.id)
    this.#recordTerminal(receipt)
    this.#announce(receipt.summary)
    if (!state) return
    if (state.timeoutHandle) clearTimeout(state.timeoutHandle)
    if (!state.completionSettled) {
      state.completionSettled = true
      state.resolve(receipt)
    }
    this.#inFlight.delete(receipt.id)
  }

  #finish(
    state: InFlightControl,
    receipt: RecoveryControlReceipt,
    resolvers: ReturnType<
      typeof promiseWithResolvers<RecoveryControlReceipt>
    >,
  ): void {
    if (state.timeoutHandle) clearTimeout(state.timeoutHandle)
    this.#inFlight.delete(state.receiptId)
    if (!state.completionSettled) {
      state.completionSettled = true
      resolvers.resolve(receipt)
    }
  }

  #race(receipts: readonly RecoveryControlReceipt[]): void {
    for (const receipt of receipts) {
      if (!terminalControlPhase(receipt.phase)) continue
      const result = this.ledger.race(receipt, this.now())
      for (const superseded of result.superseded) {
        this.#announce(superseded.summary)
      }
    }
  }

  #recordTerminal(receipt: RecoveryControlReceipt): void {
    if (!terminalControlPhase(receipt.phase)) return
    const key = `${receipt.id}:${receipt.phase}`
    if (this.#countedTerminal.has(key)) return
    this.#countedTerminal.add(key)
    if (receipt.phase === RecoveryControlPhase.APPLIED) {
      this.#auditUpdate({ applied: this.#audit.applied + 1 })
    } else if (receipt.phase === RecoveryControlPhase.DENIED) {
      this.#auditUpdate({ denied: this.#audit.denied + 1 })
    } else if (receipt.phase === RecoveryControlPhase.FAILED) {
      this.#auditUpdate({ failed: this.#audit.failed + 1 })
    }
  }

  #setConnection(
    connection: RecoveryControlConnectionValue,
    reason: string,
  ): void {
    if (
      this.#connection === connection &&
      this.#connectionReason === reason
    ) {
      return
    }
    const previous = this.#connection
    this.#connection = connection
    this.#connectionReason = reason || undefined
    const now = this.now()
    if (
      connection === RecoveryControlConnection.OFFLINE ||
      connection === RecoveryControlConnection.DEGRADED
    ) {
      this.#disconnectedAt ??= now
    }
    if (
      connection === RecoveryControlConnection.ONLINE &&
      previous !== RecoveryControlConnection.ONLINE
    ) {
      this.#reconnectedAt = now
      this.#disconnectedAt = undefined
    }
    this.#changed()
  }

  #announce(message: string): void {
    const value = message.trim()
    if (!value || this.#announcements.at(-1) === value) return
    this.#announcements.push(value)
    if (this.#announcements.length > 100) {
      this.#announcements.splice(0, this.#announcements.length - 100)
    }
  }

  #auditUpdate(patch: Partial<RecoveryControlAudit>): void {
    this.#audit = Object.freeze({ ...this.#audit, ...patch })
  }

  #changed(publish = true): void {
    this.#revision += 1
    this.#snapshot = undefined
    if (!publish || this.#detached) return
    for (const listener of this.#listeners) listener()
  }

  #requireActive(): void {
    if (this.#closed) {
      throw new RecoveryControlError(
        "timeline_control_runtime_closed",
        "Timeline recovery control runtime is closed.",
      )
    }
    if (this.#detached) {
      throw new RecoveryControlError(
        "timeline_control_view_detached",
        "Timeline recovery control viewer is detached.",
      )
    }
  }
}
