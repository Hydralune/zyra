import type { TaskApi } from "../../../api/task-api.ts"
import {
  BrowserControlPhase,
  stableDigest,
  type BrowserControlReceipt,
  type BrowserControlRequest,
} from "../contracts.ts"
import {
  assessSealedBrowserReceipt,
  browserReceiptTerminal,
  parseBrowserControlReceipt,
  type BrowserSealedReceiptAssessment,
} from "./receipts.ts"

export interface BrowserControlTransport {
  submit(
    request: BrowserControlRequest,
    signal: AbortSignal,
  ): Promise<{
    body: Readonly<Record<string, unknown>>
    statusCode?: number
  }>
}

export interface BrowserControlRuntimeOptions {
  transport: BrowserControlTransport
  maximumReceipts?: number
  maximumConcurrent?: number
  now?: () => number
}

export interface BrowserControlRuntimeSnapshot {
  pending: readonly BrowserControlReceipt[]
  settled: readonly BrowserControlReceipt[]
  latest?: BrowserControlReceipt
  sealedAssessment?: BrowserSealedReceiptAssessment
  submitted: number
  applied: number
  observed: number
  denied: number
  failed: number
  timedOut: number
  stale: number
  replayed: number
  cancelled: number
  revision: number
  closed: boolean
  disabled: boolean
}

interface PendingControl {
  request: BrowserControlRequest
  controller: AbortController
  timeout?: ReturnType<typeof setTimeout>
  promise: Promise<BrowserControlReceipt>
  resolve: (receipt: BrowserControlReceipt) => void
  reject: (error: unknown) => void
  submittedReceipt: BrowserControlReceipt
  generation: number
}

type BrowserControlListener = (
  snapshot: BrowserControlRuntimeSnapshot,
) => void

export function taskApiBrowserControlTransport(
  taskApi: Pick<TaskApi, "browserControl">,
): BrowserControlTransport {
  return Object.freeze({
    async submit(
      request: BrowserControlRequest,
      signal: AbortSignal,
    ) {
      const body = await taskApi.browserControl({
        taskId: request.identity.taskId,
        runId: request.identity.runId,
        browserSessionId: request.identity.browserSessionId,
        workerRequestId: request.identity.workerRequestId,
        actionId: request.identity.actionId,
        action: request.action,
        commandId: request.commandId,
        requestId: request.requestId,
        actorId: request.identity.actorId,
        reason: request.reason,
        url: request.url,
        retryAction: request.retryAction,
        expectedGeneration: request.identity.expectedGeneration,
        expectedTaskRevision: request.identity.expectedTaskRevision,
        sealed: request.identity.sealed,
        idempotencyKey: request.idempotencyKey,
        signal,
        timeoutMs: request.timeoutMs,
      })
      const statusCode = Number(body.status_code)
      return {
        body,
        statusCode: Number.isSafeInteger(statusCode) ? statusCode : undefined,
      }
    },
  })
}

export function disabledBrowserControlTransport(
  reason = "Browser control transport is disabled.",
): BrowserControlTransport {
  return Object.freeze({
    async submit() {
      throw Object.assign(new Error(reason), {
        code: "browser_control_transport_disabled",
      })
    },
  })
}

function pendingReceipt(
  request: BrowserControlRequest,
  phase: BrowserControlReceipt["phase"],
): BrowserControlReceipt {
  return Object.freeze({
    schema: request.schema,
    commandId: request.commandId,
    requestId: request.requestId,
    idempotencyKey: request.idempotencyKey,
    action: request.action,
    phase,
    taskId: request.identity.taskId,
    runId: request.identity.runId,
    browserSessionId: request.identity.browserSessionId,
    workerRequestId: request.identity.workerRequestId,
    actionId: request.identity.actionId,
    eventIds: Object.freeze([]),
    mutationIds: Object.freeze([]),
    artifactIds: Object.freeze([]),
    retryable: false,
    replayed: false,
    sealed: request.identity.sealed,
    interventionCounted: false,
    humanInterventionCount: 0,
    manualMutationApplied: false,
    approvalWaitEntered: false,
    submittedAt: request.createdAt,
    completedAt: 0,
    raw: Object.freeze({
      browser_control_request: Object.freeze({
        command_id: request.commandId,
        request_id: request.requestId,
        action: request.action,
        status: phase,
      }),
    }),
  })
}

function failureReceipt(
  request: BrowserControlRequest,
  error: unknown,
  completedAt: number,
): BrowserControlReceipt {
  const source =
    error && typeof error === "object"
      ? error as Record<string, unknown>
      : {}
  const name =
    error instanceof DOMException && error.name === "AbortError"
      ? "browser_control_cancelled"
      : String(source.code ?? source.name ?? "browser_control_failed")
  const timedOut =
    name.toLowerCase().includes("timeout")
    || String(source.message ?? "").toLowerCase().includes("timed out")
  const cancelled =
    name.toLowerCase().includes("abort")
    || name.toLowerCase().includes("cancel")
  return Object.freeze({
    ...pendingReceipt(
      request,
      timedOut
        ? BrowserControlPhase.TIMED_OUT
        : cancelled
          ? BrowserControlPhase.CANCELLED
          : BrowserControlPhase.FAILED,
    ),
    errorCode: name,
    errorMessage:
      error instanceof Error ? error.message : String(error ?? name),
    retryable: timedOut,
    completedAt,
    raw: Object.freeze({
      error_code: name,
      error_message:
        error instanceof Error ? error.message : String(error ?? name),
      retryable: timedOut,
      browser_control_request: {
        command_id: request.commandId,
        request_id: request.requestId,
        action: request.action,
      },
    }),
  })
}

function normalizeMaximum(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const candidate = Number(value)
  if (!Number.isSafeInteger(candidate)) return fallback
  return Math.max(minimum, Math.min(maximum, candidate))
}

export class BrowserControlRuntime {
  readonly #transport: BrowserControlTransport
  readonly #maximumReceipts: number
  readonly #maximumConcurrent: number
  readonly #now: () => number
  readonly #pending = new Map<string, PendingControl>()
  readonly #receipts = new Map<string, BrowserControlReceipt>()
  readonly #fingerprints = new Map<string, string>()
  readonly #order: string[] = []
  readonly #listeners = new Set<BrowserControlListener>()
  #submitted = 0
  #applied = 0
  #observed = 0
  #denied = 0
  #failed = 0
  #timedOut = 0
  #stale = 0
  #replayed = 0
  #cancelled = 0
  #revision = 0
  #closed = false
  #disabled = false

  constructor(options: BrowserControlRuntimeOptions) {
    if (!options.transport) {
      throw new TypeError("Browser control runtime requires a transport.")
    }
    this.#transport = options.transport
    this.#maximumReceipts = normalizeMaximum(
      options.maximumReceipts,
      512,
      16,
      10_000,
    )
    this.#maximumConcurrent = normalizeMaximum(
      options.maximumConcurrent,
      4,
      1,
      32,
    )
    this.#now = options.now ?? Date.now
  }

  getSnapshot = (): BrowserControlRuntimeSnapshot => {
    const all = [...this.#receipts.values()]
    const pending = all.filter((receipt) => !browserReceiptTerminal(receipt))
    const settled = all.filter(browserReceiptTerminal)
    const latest = [...all].sort(
      (left, right) =>
        Math.max(right.completedAt, right.submittedAt)
        - Math.max(left.completedAt, left.submittedAt),
    )[0]
    return Object.freeze({
      pending: Object.freeze(pending),
      settled: Object.freeze(settled),
      latest,
      sealedAssessment:
        latest?.sealed ? assessSealedBrowserReceipt(latest) : undefined,
      submitted: this.#submitted,
      applied: this.#applied,
      observed: this.#observed,
      denied: this.#denied,
      failed: this.#failed,
      timedOut: this.#timedOut,
      stale: this.#stale,
      replayed: this.#replayed,
      cancelled: this.#cancelled,
      revision: this.#revision,
      closed: this.#closed,
      disabled: this.#disabled,
    })
  }

  subscribe = (listener: BrowserControlListener): (() => void) => {
    this.#assertOpen()
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  submit(request: BrowserControlRequest): Promise<BrowserControlReceipt> {
    this.#assertOpen()
    if (this.#disabled) {
      return Promise.reject(
        Object.assign(new Error("Browser control runtime is disabled."), {
          code: "browser_control_runtime_disabled",
        }),
      )
    }
    const fingerprint = stableDigest(request)
    const existingFingerprint = this.#fingerprints.get(request.commandId)
    const existing = this.#receipts.get(request.commandId)
    if (existingFingerprint) {
      if (existingFingerprint !== fingerprint) {
        return Promise.reject(
          Object.assign(
            new Error("Browser control command id was reused with another body."),
            { code: "browser_control_idempotency_conflict" },
          ),
        )
      }
      const pending = this.#pending.get(request.commandId)
      if (pending) return pending.promise
      if (existing) {
        this.#replayed += 1
        const replay = Object.freeze({ ...existing, replayed: true })
        this.#remember(replay)
        this.#changed()
        return Promise.resolve(replay)
      }
    }
    if (this.#pending.size >= this.#maximumConcurrent) {
      return Promise.reject(
        Object.assign(
          new Error("Browser control concurrency limit reached."),
          { code: "browser_control_concurrency_limit" },
        ),
      )
    }
    const controller = new AbortController()
    let resolve!: (receipt: BrowserControlReceipt) => void
    let reject!: (error: unknown) => void
    const promise = new Promise<BrowserControlReceipt>((yes, no) => {
      resolve = yes
      reject = no
    })
    const submitted = pendingReceipt(request, BrowserControlPhase.SUBMITTING)
    const pending: PendingControl = {
      request,
      controller,
      promise,
      resolve,
      reject,
      submittedReceipt: submitted,
      generation: this.#revision + 1,
    }
    this.#pending.set(request.commandId, pending)
    this.#fingerprints.set(request.commandId, fingerprint)
    this.#submitted += 1
    this.#remember(submitted)
    this.#changed()
    pending.timeout = setTimeout(() => {
      if (this.#pending.get(request.commandId) !== pending) return
      controller.abort(
        Object.assign(new Error("Browser control request timed out."), {
          code: "browser_control_timed_out",
        }),
      )
      this.#settleFailure(
        pending,
        Object.assign(new Error("Browser control request timed out."), {
          code: "browser_control_timed_out",
        }),
      )
    }, request.timeoutMs)
    void this.#dispatch(pending)
    return promise
  }

  cancel(commandId: string, reason = "Browser control cancelled."): boolean {
    const pending = this.#pending.get(commandId)
    if (!pending) return false
    pending.controller.abort(
      Object.assign(new Error(reason), {
        code: "browser_control_cancelled",
      }),
    )
    this.#settleFailure(
      pending,
      Object.assign(new Error(reason), {
        code: "browser_control_cancelled",
      }),
    )
    return true
  }

  disable(reason = "Browser control runtime disabled."): void {
    if (this.#disabled) return
    this.#disabled = true
    for (const pending of [...this.#pending.values()]) {
      pending.controller.abort(
        Object.assign(new Error(reason), {
          code: "browser_control_runtime_disabled",
        }),
      )
      this.#settleFailure(
        pending,
        Object.assign(new Error(reason), {
          code: "browser_control_runtime_disabled",
        }),
      )
    }
    this.#changed()
  }

  enable(): void {
    this.#assertOpen()
    if (!this.#disabled) return
    this.#disabled = false
    this.#changed()
  }

  close(reason = "Browser control runtime closed."): void {
    if (this.#closed) return
    for (const pending of [...this.#pending.values()]) {
      pending.controller.abort(
        Object.assign(new Error(reason), {
          code: "browser_control_runtime_closed",
        }),
      )
      this.#settleFailure(
        pending,
        Object.assign(new Error(reason), {
          code: "browser_control_runtime_closed",
        }),
      )
    }
    this.#closed = true
    this.#changed()
    this.#listeners.clear()
  }

  async #dispatch(pending: PendingControl): Promise<void> {
    try {
      const response = await this.#transport.submit(
        pending.request,
        pending.controller.signal,
      )
      if (this.#pending.get(pending.request.commandId) !== pending) return
      const receipt = parseBrowserControlReceipt(
        response.body,
        pending.request,
        {
          statusCode: response.statusCode,
          completedAt: this.#now(),
        },
      )
      if (receipt.sealed) {
        const assessment = assessSealedBrowserReceipt(receipt)
        if (!assessment.valid) {
          const failure = Object.freeze({
            ...receipt,
            phase: BrowserControlPhase.FAILED,
            errorCode: "sealed_browser_control_invariant_failed",
            errorMessage: assessment.violations.join("; "),
            retryable: false,
          })
          this.#settle(pending, failure)
          return
        }
      }
      this.#settle(pending, receipt)
    } catch (error) {
      if (this.#pending.get(pending.request.commandId) !== pending) return
      this.#settleFailure(pending, error)
    }
  }

  #settleFailure(pending: PendingControl, error: unknown): void {
    const receipt = failureReceipt(pending.request, error, this.#now())
    this.#settle(pending, receipt)
  }

  #settle(
    pending: PendingControl,
    receipt: BrowserControlReceipt,
  ): void {
    if (this.#pending.get(pending.request.commandId) !== pending) return
    this.#pending.delete(pending.request.commandId)
    if (pending.timeout) clearTimeout(pending.timeout)
    this.#remember(receipt)
    switch (receipt.phase) {
      case BrowserControlPhase.APPLIED:
        this.#applied += 1
        break
      case BrowserControlPhase.OBSERVED:
        this.#observed += 1
        break
      case BrowserControlPhase.DENIED:
        this.#denied += 1
        break
      case BrowserControlPhase.TIMED_OUT:
        this.#timedOut += 1
        break
      case BrowserControlPhase.STALE:
        this.#stale += 1
        break
      case BrowserControlPhase.CANCELLED:
        this.#cancelled += 1
        break
      case BrowserControlPhase.FAILED:
        this.#failed += 1
        break
    }
    if (receipt.replayed) this.#replayed += 1
    this.#changed()
    pending.resolve(receipt)
  }

  #remember(receipt: BrowserControlReceipt): void {
    const commandId = receipt.commandId
    this.#receipts.set(commandId, receipt)
    const existing = this.#order.indexOf(commandId)
    if (existing >= 0) this.#order.splice(existing, 1)
    this.#order.push(commandId)
    while (this.#order.length > this.#maximumReceipts) {
      const removed = this.#order.shift()!
      if (this.#pending.has(removed)) {
        this.#order.push(removed)
        break
      }
      this.#receipts.delete(removed)
      this.#fingerprints.delete(removed)
    }
  }

  #changed(): void {
    this.#revision += 1
    const snapshot = this.getSnapshot()
    for (const listener of this.#listeners) listener(snapshot)
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw Object.assign(new Error("Browser control runtime is closed."), {
        code: "browser_control_runtime_closed",
      })
    }
  }
}
